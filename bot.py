"""
Render Guardian - Telegram server monitoring dashboard bot.

Monitors a Render service health endpoint, provides dashboard/status commands,
inline controls, smart recovery, and state-change alerts.
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from http import HTTPStatus
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

import httpx
from dotenv import load_dotenv
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Hide per-request httpx polling logs
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

STATE_FILE = Path(__file__).with_name("state.json")
HISTORY_LIMIT = 100
HISTORY_DISPLAY_LIMIT = 10
HTTP_TIMEOUT_SECONDS = 60.0
RECOVERY_RETRIES = 6
RECOVERY_INTERVAL_SECONDS = 10
SLOW_THRESHOLD_MS = 3000.0
USER_AGENT = "RenderGuardian/1.0 (+https://github.com/Nang-dev01/Render_get_up_bot)"


def _require_env(name: str) -> str:
    """Return a required environment variable or exit with a clear message."""
    value = os.getenv(name)
    if value is None or not value.strip():
        raise SystemExit(f"Missing required environment variable: {name}")
    return value.strip()


def _parse_int_env(name: str, default: int, *, positive: bool = False) -> int:
    """Parse an integer environment variable with validation."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        if name == "MONITOR_INTERVAL":
            logger.warning(
                "Invalid MONITOR_INTERVAL=%r, falling back to %s seconds.",
                raw,
                default,
            )
            return default
        raise SystemExit(f"Environment variable {name} must be an integer, got: {raw!r}")
    if positive and value <= 0:
        if name == "MONITOR_INTERVAL":
            logger.warning(
                "Invalid MONITOR_INTERVAL=%r, falling back to %s seconds.",
                raw,
                default,
            )
            return default
        raise SystemExit(f"Environment variable {name} must be > 0, got: {value}")
    return value


BOT_TOKEN = _require_env("BOT_TOKEN")
ADMIN_USER_ID_RAW = _require_env("ADMIN_USER_ID")
RENDER_URL = _require_env("RENDER_URL").rstrip("/")

try:
    ADMIN_USER_ID = int(ADMIN_USER_ID_RAW)
except ValueError:
    raise SystemExit(
        f"Environment variable ADMIN_USER_ID must be a numeric Telegram user ID, got: {ADMIN_USER_ID_RAW!r}"
    )

_raw_interval = os.getenv("MONITOR_INTERVAL", "600")
try:
    MONITOR_INTERVAL = int(_raw_interval)
    if MONITOR_INTERVAL <= 0:
        raise ValueError("MONITOR_INTERVAL must be positive")
except ValueError:
    logger.warning(
        "Invalid MONITOR_INTERVAL=%r, falling back to 600 seconds.",
        _raw_interval,
    )
    MONITOR_INTERVAL = 600

AUTO_MONITOR = os.getenv("AUTO_MONITOR", "false").lower() == "true"

HEALTH_PATH = os.getenv("HEALTH_PATH", "/health").strip() or "/health"
if not HEALTH_PATH.startswith("/"):
    HEALTH_PATH = "/" + HEALTH_PATH

SITE_NAME = os.getenv("SITE_NAME", "Render Guardian").strip() or "Render Guardian"
TZ_OFFSET_HOURS = _parse_int_env("TZ_OFFSET_HOURS", 7)
if TZ_OFFSET_HOURS < -12 or TZ_OFFSET_HOURS > 14:
    raise SystemExit(
        f"Environment variable TZ_OFFSET_HOURS must be between -12 and 14, got: {TZ_OFFSET_HOURS}"
    )

CHECKED_URL = f"{RENDER_URL}{HEALTH_PATH}"
DISPLAY_TZ = timezone(timedelta(hours=TZ_OFFSET_HOURS))

logger.info("Health check target: %s", CHECKED_URL)

# -----------------------------------------------------------------------------
# State
# -----------------------------------------------------------------------------

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def _default_state() -> dict[str, Any]:
    """Create the default persisted state."""
    return {
        "history": [],
        "monitor_enabled": AUTO_MONITOR,
        "alerts_enabled": True,
        "last_status": None,
        "last_alert_state": None,
        "has_checked": False,
        "has_checked_before_alert": False,
    }


def load_state() -> dict[str, Any]:
    """Load persisted state from disk, falling back to defaults on error."""
    state = _default_state()
    if not STATE_FILE.exists():
        return state
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state root must be an object")
    except Exception as exc:  # noqa: BLE001 - corrupted state must not crash startup
        logger.warning("Failed to load %s, using defaults: %s", STATE_FILE.name, exc)
        return state

    state["history"] = _sanitize_history(data.get("history"))
    for key in ("monitor_enabled", "alerts_enabled", "has_checked", "has_checked_before_alert"):
        value = data.get(key)
        if isinstance(value, bool):
            state[key] = value
    for key in ("last_status", "last_alert_state"):
        value = data.get(key)
        if value is None or isinstance(value, bool):
            state[key] = value
    return state


def _sanitize_history(raw: Any) -> list[dict[str, Any]]:
    """Validate and trim persisted history entries."""
    if not isinstance(raw, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for item in raw[-HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        entry = {
            "timestamp": str(item.get("timestamp", "")),
            "ok": bool(item.get("ok")),
            "status_code": item.get("status_code"),
            "ms": _safe_float(item.get("ms")),
            "error": str(item.get("error")) if item.get("error") else None,
        }
        if entry["status_code"] is not None:
            try:
                entry["status_code"] = int(entry["status_code"])
            except (TypeError, ValueError):
                entry["status_code"] = None
        cleaned.append(entry)
    return cleaned[-HISTORY_LIMIT:]


def _safe_float(value: Any) -> float | None:
    """Convert a value to float, returning None on failure."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def save_state(state: dict[str, Any]) -> None:
    """Persist state atomically to disk."""
    payload = {
        "history": state.get("history", [])[-HISTORY_LIMIT:],
        "monitor_enabled": bool(state.get("monitor_enabled", False)),
        "alerts_enabled": bool(state.get("alerts_enabled", True)),
        "last_status": state.get("last_status"),
        "last_alert_state": state.get("last_alert_state"),
        "has_checked": bool(state.get("has_checked", False)),
        "has_checked_before_alert": bool(state.get("has_checked_before_alert", False)),
    }
    tmp_file = STATE_FILE.with_suffix(".tmp")
    try:
        tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_file.replace(STATE_FILE)
    except Exception as exc:  # noqa: BLE001 - persistence failure should not crash bot
        logger.warning("Failed to save %s: %s", STATE_FILE.name, exc)
        try:
            if tmp_file.exists():
                tmp_file.unlink()
        except OSError:
            pass


def get_state(context: ContextTypes.DEFAULT_TYPE) -> dict[str, Any]:
    """Get runtime state, loading from disk if needed."""
    state = context.application.bot_data.get("guardian_state")
    if not isinstance(state, dict):
        state = load_state()
        context.application.bot_data["guardian_state"] = state
    return state


def record_check(state: dict[str, Any], result: dict[str, Any]) -> None:
    """Append a check result to history and persist state."""
    entry = {
        "timestamp": result["timestamp"],
        "ok": bool(result["ok"]),
        "status_code": result.get("status_code"),
        "ms": result.get("ms"),
        "error": result.get("error"),
    }
    history = state.setdefault("history", [])
    history.append(entry)
    if len(history) > HISTORY_LIMIT:
        del history[:-HISTORY_LIMIT]
    state["has_checked"] = True
    state["last_status"] = bool(result["ok"])
    save_state(state)


# -----------------------------------------------------------------------------
# Checks
# -----------------------------------------------------------------------------


def now_utc() -> datetime:
    """Return current UTC time."""
    return datetime.now(timezone.utc)


def format_local_time(dt: datetime | None = None) -> str:
    """Format a datetime in the configured display timezone."""
    dt = dt or now_utc()
    local_dt = dt.astimezone(DISPLAY_TZ)
    sign = "+" if TZ_OFFSET_HOURS >= 0 else ""
    return f"{local_dt.strftime('%Y-%m-%d %H:%M:%S')} UTC{sign}{TZ_OFFSET_HOURS}"


def http_status_phrase(status_code: int | None) -> str:
    """Return an HTTP status phrase for a status code."""
    if status_code is None:
        return "No Response"
    try:
        phrase = HTTPStatus(status_code).phrase
    except ValueError:
        phrase = "Unknown"
    return f"{status_code} {phrase}"


def classify_status(result: dict[str, Any] | None) -> str:
    """Classify a check result as HEALTHY/SLOW/OFFLINE/UNKNOWN."""
    if not result:
        return "UNKNOWN"
    if not result.get("ok"):
        return "OFFLINE"
    ms = result.get("ms")
    if isinstance(ms, (int, float)) and ms > SLOW_THRESHOLD_MS:
        return "SLOW"
    return "HEALTHY"


def status_badge(status: str) -> str:
    """Return a display badge for a status classification."""
    badges = {
        "HEALTHY": "🟢 HEALTHY",
        "SLOW": "🟡 SLOW",
        "OFFLINE": "🔴 OFFLINE",
        "UNKNOWN": "⚪ UNKNOWN",
    }
    return badges.get(status, "⚪ UNKNOWN")


async def check_server() -> dict[str, Any]:
    """Perform a single health check and measure response time."""
    started = time.perf_counter()
    timestamp = now_utc().isoformat()
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(HTTP_TIMEOUT_SECONDS),
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            response = await client.get(CHECKED_URL)
        elapsed_ms = (time.perf_counter() - started) * 1000
        status_code = response.status_code
        ok = 200 <= status_code < 300
        error = None if ok else f"HTTP {status_code}"
        return {
            "timestamp": timestamp,
            "ok": ok,
            "status_code": status_code,
            "ms": round(elapsed_ms, 2),
            "error": error,
            "url": CHECKED_URL,
        }
    except httpx.RequestError as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.warning("Server check failed: %s (%s)", exc, CHECKED_URL)
        return {
            "timestamp": timestamp,
            "ok": False,
            "status_code": None,
            "ms": round(elapsed_ms, 2),
            "error": str(exc),
            "url": CHECKED_URL,
        }
    except Exception as exc:  # noqa: BLE001 - check should return structured failure
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.exception("Unexpected server check error")
        return {
            "timestamp": timestamp,
            "ok": False,
            "status_code": None,
            "ms": round(elapsed_ms, 2),
            "error": str(exc),
            "url": CHECKED_URL,
        }


async def check_with_recovery(state: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Check server, retrying failures before declaring it down.

    Returns the final result and all attempt results. Attempts are recorded in
    history so recovery behavior is visible; the final attempt determines alert state.
    """
    attempts: list[dict[str, Any]] = []
    result = await check_server()
    attempts.append(result)
    record_check(state, result)
    if result["ok"]:
        return result, attempts

    for attempt in range(1, RECOVERY_RETRIES + 1):
        logger.info(
            "Recovery retry %s/%s for %s in %ss",
            attempt,
            RECOVERY_RETRIES,
            CHECKED_URL,
            RECOVERY_INTERVAL_SECONDS,
        )
        await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
        result = await check_server()
        attempts.append(result)
        record_check(state, result)
        if result["ok"]:
            return result, attempts
    return result, attempts


# -----------------------------------------------------------------------------
# UI builders
# -----------------------------------------------------------------------------


def uptime_percent(state: dict[str, Any]) -> float:
    """Calculate uptime percentage from stored history."""
    history = state.get("history", [])
    if not history:
        return 0.0
    ok_count = sum(1 for item in history if item.get("ok"))
    return (ok_count / len(history)) * 100


def latest_result(state: dict[str, Any]) -> dict[str, Any] | None:
    """Return the most recent check result from history."""
    history = state.get("history", [])
    if not history:
        return None
    last = history[-1]
    return {
        "timestamp": last.get("timestamp"),
        "ok": bool(last.get("ok")),
        "status_code": last.get("status_code"),
        "ms": last.get("ms"),
        "error": last.get("error"),
        "url": CHECKED_URL,
    }


def format_result_line(result: dict[str, Any]) -> str:
    """Format a check result for display."""
    ts = result.get("timestamp")
    dt = None
    if isinstance(ts, str) and ts:
        try:
            dt = datetime.fromisoformat(ts)
        except ValueError:
            dt = None
    when = format_local_time(dt) if dt else "Unknown time"
    status = "OK" if result.get("ok") else "FAIL"
    code = http_status_phrase(result.get("status_code"))
    ms = result.get("ms")
    ms_text = f"{ms:.0f}ms" if isinstance(ms, (int, float)) else "n/a"
    err = result.get("error")
    suffix = f" | {err}" if err and not result.get("ok") else ""
    return f"{when} | {status} | {code} | {ms_text}{suffix}"


def build_dashboard_text(state: dict[str, Any], checking: bool = False) -> str:
    """Build the main dashboard HTML message."""
    result = latest_result(state)
    status = classify_status(result)
    badge = status_badge(status)
    monitor_status = "ACTIVE" if state.get("monitor_enabled") else "PAUSED"
    alerts_status = "ON" if state.get("alerts_enabled", True) else "OFF"
    uptime = uptime_percent(state)

    if checking:
        http_line = "Checking..."
        ms_line = "Checking..."
        last_check = "Checking..."
    elif result:
        http_line = html.escape(http_status_phrase(result.get("status_code")))
        ms = result.get("ms")
        ms_line = f"{ms:.0f} ms" if isinstance(ms, (int, float)) else "n/a"
        ts = result.get("timestamp")
        dt = None
        if isinstance(ts, str) and ts:
            try:
                dt = datetime.fromisoformat(ts)
            except ValueError:
                dt = None
        last_check = html.escape(format_local_time(dt) if dt else "Unknown")
    else:
        http_line = "No checks yet"
        ms_line = "n/a"
        last_check = "Never"

    lines = [
        f"<b>{html.escape(SITE_NAME)}</b>",
        "<i>Server Monitoring System</i>",
        "",
        f"<b>Status:</b> {badge}",
        f"<b>HTTP:</b> {http_line}",
        f"<b>Response Time:</b> {html.escape(ms_line)}",
        f"<b>Monitoring:</b> {monitor_status}",
        f"<b>Alerts:</b> {alerts_status}",
        f"<b>Check Interval:</b> {MONITOR_INTERVAL}s",
        f"<b>Uptime:</b> {uptime:.1f}%",
        f"<b>Last Check:</b> {last_check}",
        f"<b>URL:</b> <code>{html.escape(CHECKED_URL)}</code>",
    ]
    return "\n".join(lines)


def build_dashboard_keyboard(state: dict[str, Any]) -> InlineKeyboardMarkup:
    """Build the dashboard inline keyboard."""
    alerts_label = "Alerts ON" if state.get("alerts_enabled", True) else "Alerts OFF"
    monitor_label = "Monitor ON" if state.get("monitor_enabled") else "Monitor OFF"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Check Status", callback_data="dash:status"),
                InlineKeyboardButton("Wake Server", callback_data="dash:wake"),
            ],
            [
                InlineKeyboardButton("Ping", callback_data="dash:ping"),
                InlineKeyboardButton("History", callback_data="dash:history"),
            ],
            [
                InlineKeyboardButton(alerts_label, callback_data="dash:toggle_alerts"),
                InlineKeyboardButton(monitor_label, callback_data="dash:toggle_monitor"),
            ],
            [
                InlineKeyboardButton("Open Website", url=RENDER_URL),
                InlineKeyboardButton("Health Endpoint", url=CHECKED_URL),
            ],
        ]
    )


def build_history_text(state: dict[str, Any]) -> str:
    """Build history report text."""
    history = state.get("history", [])
    uptime = uptime_percent(state)
    lines = [
        f"<b>{html.escape(SITE_NAME)} - History</b>",
        f"<b>Uptime:</b> {uptime:.1f}% ({len(history)} checks stored)",
        "",
    ]
    if not history:
        lines.append("No checks recorded yet.")
        return "\n".join(lines)

    recent = history[-HISTORY_DISPLAY_LIMIT:]
    for item in reversed(recent):
        result = {
            "timestamp": item.get("timestamp"),
            "ok": item.get("ok"),
            "status_code": item.get("status_code"),
            "ms": item.get("ms"),
            "error": item.get("error"),
        }
        lines.append(f"• <code>{html.escape(format_result_line(result))}</code>")
    return "\n".join(lines)


def build_info_text(state: dict[str, Any]) -> str:
    """Build configuration/info text without secrets."""
    monitor_status = "ACTIVE" if state.get("monitor_enabled") else "PAUSED"
    alerts_status = "ON" if state.get("alerts_enabled", True) else "OFF"
    return "\n".join(
        [
            f"<b>{html.escape(SITE_NAME)} - Info</b>",
            f"<b>Site:</b> <code>{html.escape(SITE_NAME)}</code>",
            f"<b>Checked URL:</b> <code>{html.escape(CHECKED_URL)}</code>",
            f"<b>Monitor Interval:</b> {MONITOR_INTERVAL}s",
            f"<b>Monitoring:</b> {monitor_status}",
            f"<b>Alerts:</b> {alerts_status}",
            f"<b>AUTO_MONITOR env:</b> {AUTO_MONITOR}",
            f"<b>Timezone:</b> UTC{'+' if TZ_OFFSET_HOURS >= 0 else ''}{TZ_OFFSET_HOURS}",
            f"<b>History Size:</b> {len(state.get('history', []))}/{HISTORY_LIMIT}",
            f"<b>Recovery:</b> {RECOVERY_RETRIES} retries @ {RECOVERY_INTERVAL_SECONDS}s",
            f"<b>HTTP Timeout:</b> {int(HTTP_TIMEOUT_SECONDS)}s",
        ]
    )


def build_help_text() -> str:
    """Build help text."""
    return "\n".join(
        [
            f"<b>{html.escape(SITE_NAME)} - Commands</b>",
            "/start - Show dashboard",
            "/dashboard - Show dashboard",
            "/status - HTTP status + response time",
            "/wake - Request server, retry until healthy or retries exhausted",
            "/ping - Measure response time with rating",
            "/monitor_on - Enable automatic monitoring",
            "/monitor_off - Disable automatic monitoring",
            "/history - Last 10 checks + uptime %",
            "/info - Current configuration (no secrets)",
            "/help - Show this command list",
        ]
    )


def ping_rating(ms: float) -> str:
    """Rate ping response time."""
    if ms < 500:
        return "Excellent"
    if ms < 1500:
        return "Good"
    if ms < 3000:
        return "Fair"
    return "Slow"


# -----------------------------------------------------------------------------
# Telegram helpers
# -----------------------------------------------------------------------------


async def safe_send(bot: Any, chat_id: int, text: str, **kwargs: Any) -> None:
    """Send a Telegram message, logging failures instead of raising."""
    try:
        await bot.send_message(chat_id=chat_id, text=text, **kwargs)
    except TelegramError as exc:
        logger.warning("Failed to send Telegram message: %s", exc)


async def safe_edit_message(message: Any, text: str, **kwargs: Any) -> None:
    """Edit a Telegram message, ignoring 'not modified' errors."""
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        logger.warning("Failed to edit message: %s", exc)
    except TelegramError as exc:
        logger.warning("Failed to edit message: %s", exc)


async def reply_html(update: Update, text: str, **kwargs: Any) -> Any:
    """Reply to a command/message with HTML parse mode."""
    msg = update.effective_message
    if msg is None:
        return None
    return await msg.reply_text(text, parse_mode=ParseMode.HTML, **kwargs)


def is_admin(update: Update) -> bool:
    """Check whether the update comes from the configured admin."""
    user = update.effective_user
    return user is not None and user.id == ADMIN_USER_ID


def admin_only(func: F) -> F:
    """Restrict a command or callback handler to the configured admin."""

    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args: Any, **kwargs: Any) -> Any:
        user = update.effective_user
        if user is None or user.id != ADMIN_USER_ID:
            uid = getattr(user, "id", "unknown")
            uname = getattr(user, "username", None)
            logger.warning("Unauthorized access attempt by user_id=%s username=%s", uid, uname)
            if update.callback_query is not None:
                try:
                    await update.callback_query.answer("Not authorized.", show_alert=True)
                except TelegramError as exc:
                    logger.warning("Failed to answer unauthorized callback: %s", exc)
            elif update.effective_message is not None:
                try:
                    await update.effective_message.reply_text(
                        "Sorry, you are not authorized to use this bot."
                    )
                except TelegramError as exc:
                    logger.warning("Failed to send unauthorized reply: %s", exc)
            return None
        return await func(update, context, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


# -----------------------------------------------------------------------------
# Scheduler / monitoring
# -----------------------------------------------------------------------------


def get_monitor_jobs(application: Application) -> tuple[Any, ...]:
    """Return active monitor jobs."""
    if application.job_queue is None:
        return ()
    return tuple(application.job_queue.get_jobs_by_name("server_monitor"))


def ensure_monitor_job(application: Application, enabled: bool) -> None:
    """Ensure the monitor job matches the desired enabled state."""
    jobs = get_monitor_jobs(application)
    if enabled:
        if jobs:
            return
        if application.job_queue is None:
            logger.warning("Job queue unavailable; cannot start monitor job")
            return
        application.job_queue.run_repeating(
            monitor_server,
            interval=MONITOR_INTERVAL,
            first=1,
            name="server_monitor",
        )
        logger.info("Automatic monitoring enabled.")
        return

    for job in jobs:
        job.schedule_removal()
    if jobs:
        logger.info("Automatic monitoring disabled.")


async def maybe_send_alert(context: ContextTypes.DEFAULT_TYPE, state: dict[str, Any], result: dict[str, Any]) -> None:
    """Send a smart state-change alert when appropriate."""
    if not state.get("alerts_enabled", True):
        return

    current_ok = bool(result.get("ok"))
    previous_alert_state = state.get("last_alert_state")
    has_checked = bool(state.get("has_checked_before_alert", False))

    # First observed check: establish baseline, no healthy-first alert.
    if not has_checked:
        state["has_checked_before_alert"] = True
        state["last_alert_state"] = current_ok
        save_state(state)
        if current_ok:
            return
        # First check is offline -> alert because it's a real outage state.
        text = (
            f"🔴 <b>{html.escape(SITE_NAME)}: SERVER OFFLINE</b>\n\n"
            f"<b>Result:</b> {html.escape(str(result.get('error') or http_status_phrase(result.get('status_code'))))}\n"
            f"<b>URL:</b> <code>{html.escape(CHECKED_URL)}</code>\n"
            f"<b>Time:</b> {html.escape(format_local_time())}"
        )
        await safe_send(context.bot, ADMIN_USER_ID, text, parse_mode=ParseMode.HTML)
        return

    if previous_alert_state == current_ok:
        return

    state["last_alert_state"] = current_ok
    save_state(state)

    if current_ok:
        text = (
            f"🟢 <b>{html.escape(SITE_NAME)}: SERVER RECOVERED</b>\n\n"
            f"<b>HTTP:</b> {html.escape(http_status_phrase(result.get('status_code')))}\n"
            f"<b>Response Time:</b> {result.get('ms', 0):.0f} ms\n"
            f"<b>URL:</b> <code>{html.escape(CHECKED_URL)}</code>\n"
            f"<b>Time:</b> {html.escape(format_local_time())}"
        )
    else:
        text = (
            f"🔴 <b>{html.escape(SITE_NAME)}: SERVER OFFLINE</b>\n\n"
            f"<b>Result:</b> {html.escape(str(result.get('error') or http_status_phrase(result.get('status_code'))))}\n"
            f"<b>URL:</b> <code>{html.escape(CHECKED_URL)}</code>\n"
            f"<b>Time:</b> {html.escape(format_local_time())}"
        )
    await safe_send(context.bot, ADMIN_USER_ID, text, parse_mode=ParseMode.HTML)


async def monitor_server(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled monitoring job with recovery and smart alerts."""
    state = get_state(context)
    try:
        result, _attempts = await check_with_recovery(state)
        await maybe_send_alert(context, state, result)
    except Exception:  # noqa: BLE001 - monitor loop must never crash scheduler
        logger.exception("Monitor job failed")


# -----------------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------------


async def send_dashboard_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send the dashboard as a new message."""
    state = get_state(context)
    text = build_dashboard_text(state)
    keyboard = build_dashboard_keyboard(state)
    await reply_html(update, text, reply_markup=keyboard)


async def edit_dashboard_message(message: Any, state: dict[str, Any], checking: bool = False) -> None:
    """Edit an existing message to show the dashboard."""
    text = build_dashboard_text(state, checking=checking)
    keyboard = build_dashboard_keyboard(state)
    await safe_edit_message(message, text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


@admin_only
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start by showing the dashboard."""
    await send_dashboard_message(update, context)


@admin_only
async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /dashboard by showing the dashboard."""
    await send_dashboard_message(update, context)


@admin_only
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /help."""
    await reply_html(update, build_help_text())


@admin_only
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /info with non-secret configuration."""
    state = get_state(context)
    await reply_html(update, build_info_text(state))


@admin_only
async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /status with a live health check."""
    state = get_state(context)
    msg = update.effective_message
    if msg is None:
        return
    try:
        progress = await msg.reply_text("Checking server status...", parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logger.warning("Failed to send status prompt: %s", exc)
        return

    result = await check_server()
    record_check(state, result)
    status = classify_status(result)
    badge = status_badge(status)
    ms = result.get("ms")
    ms_text = f"{ms:.0f} ms" if isinstance(ms, (int, float)) else "n/a"
    text = "\n".join(
        [
            f"<b>{html.escape(SITE_NAME)} - Status</b>",
            f"<b>Status:</b> {badge}",
            f"<b>HTTP:</b> {html.escape(http_status_phrase(result.get('status_code')))}",
            f"<b>Response Time:</b> {html.escape(ms_text)}",
            f"<b>Checked URL:</b> <code>{html.escape(CHECKED_URL)}</code>",
            f"<b>Time:</b> {html.escape(format_local_time())}",
        ]
    )
    if result.get("error") and not result.get("ok"):
        text += f"\n<b>Error:</b> <code>{html.escape(str(result['error']))}</code>"
    await safe_edit_message(progress, text, parse_mode=ParseMode.HTML)


@admin_only
async def wake_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /wake by requesting the server until healthy or retries exhausted."""
    state = get_state(context)
    msg = update.effective_message
    if msg is None:
        return
    try:
        progress = await msg.reply_text(
            f"Waking server... checking {html.escape(CHECKED_URL)}",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError as exc:
        logger.warning("Failed to send wake prompt: %s", exc)
        return

    attempts: list[dict[str, Any]] = []
    result = await check_server()
    attempts.append(result)
    record_check(state, result)
    if not result["ok"]:
        for attempt in range(1, RECOVERY_RETRIES + 1):
            await safe_edit_message(
                progress,
                f"Waking server... attempt {attempt}/{RECOVERY_RETRIES} after failure\n"
                f"<b>Last result:</b> {html.escape(str(result.get('error') or http_status_phrase(result.get('status_code'))))}",
                parse_mode=ParseMode.HTML,
            )
            await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
            result = await check_server()
            attempts.append(result)
            record_check(state, result)
            if result["ok"]:
                break

    status = classify_status(result)
    badge = status_badge(status)
    ms = result.get("ms")
    ms_text = f"{ms:.0f} ms" if isinstance(ms, (int, float)) else "n/a"
    lines = [
        f"<b>{html.escape(SITE_NAME)} - Wake</b>",
        f"<b>Status:</b> {badge}",
        f"<b>HTTP:</b> {html.escape(http_status_phrase(result.get('status_code')))}",
        f"<b>Response Time:</b> {html.escape(ms_text)}",
        f"<b>Attempts:</b> {len(attempts)}",
        f"<b>Checked URL:</b> <code>{html.escape(CHECKED_URL)}</code>",
        f"<b>Time:</b> {html.escape(format_local_time())}",
    ]
    if result.get("ok"):
        lines.append("\nServer is healthy.")
    else:
        lines.append(f"\n<b>Last error:</b> <code>{html.escape(str(result.get('error') or 'Unknown error'))}</code>")
        lines.append("Check your Render dashboard and logs.")
    await safe_edit_message(progress, "\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /ping with response time rating."""
    state = get_state(context)
    msg = update.effective_message
    if msg is None:
        return
    try:
        progress = await msg.reply_text("Pinging server...", parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logger.warning("Failed to send ping prompt: %s", exc)
        return

    result = await check_server()
    record_check(state, result)
    ms = result.get("ms")
    ms_value = float(ms) if isinstance(ms, (int, float)) else None
    rating = ping_rating(ms_value) if ms_value is not None else "Unavailable"
    status = classify_status(result)
    lines = [
        f"<b>{html.escape(SITE_NAME)} - Ping</b>",
        f"<b>Status:</b> {status_badge(status)}",
        f"<b>HTTP:</b> {html.escape(http_status_phrase(result.get('status_code')))}",
        f"<b>Response Time:</b> {f'{ms_value:.0f} ms' if ms_value is not None else 'n/a'}",
        f"<b>Rating:</b> {html.escape(rating)}",
        f"<b>Checked URL:</b> <code>{html.escape(CHECKED_URL)}</code>",
    ]
    if result.get("error") and not result.get("ok"):
        lines.append(f"<b>Error:</b> <code>{html.escape(str(result['error']))}</code>")
    await safe_edit_message(progress, "\n".join(lines), parse_mode=ParseMode.HTML)


@admin_only
async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /history."""
    state = get_state(context)
    await reply_html(update, build_history_text(state))


@admin_only
async def monitor_on_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /monitor_on."""
    state = get_state(context)
    state["monitor_enabled"] = True
    save_state(state)
    ensure_monitor_job(context.application, True)
    await reply_html(
        update,
        f"Automatic monitoring <b>enabled</b>. Interval: {MONITOR_INTERVAL}s.",
    )


@admin_only
async def monitor_off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /monitor_off."""
    state = get_state(context)
    state["monitor_enabled"] = False
    save_state(state)
    ensure_monitor_job(context.application, False)
    await reply_html(update, "Automatic monitoring <b>disabled</b>.")


@admin_only
async def dashboard_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle dashboard inline button presses."""
    query = update.callback_query
    if query is None:
        return
    data = query.data or ""
    state = get_state(context)

    try:
        await query.answer()
    except TelegramError as exc:
        logger.warning("Failed to answer callback query: %s", exc)

    message = query.message
    if message is None:
        return

    if data == "dash:back":
        await edit_dashboard_message(message, state)
        return

    if data == "dash:status":
        await edit_dashboard_message(message, state, checking=True)
        result = await check_server()
        record_check(state, result)
        await edit_dashboard_message(message, state)
        return

    if data == "dash:wake":
        await safe_edit_message(
            message,
            f"<b>{html.escape(SITE_NAME)}</b>\n\nWaking server... checking {html.escape(CHECKED_URL)}",
            parse_mode=ParseMode.HTML,
            reply_markup=build_dashboard_keyboard(state),
        )
        result = await check_server()
        record_check(state, result)
        attempts = 1
        if not result["ok"]:
            for attempt in range(1, RECOVERY_RETRIES + 1):
                await safe_edit_message(
                    message,
                    f"<b>{html.escape(SITE_NAME)}</b>\n\nWaking server... retry {attempt}/{RECOVERY_RETRIES}\n"
                    f"Last result: <code>{html.escape(str(result.get('error') or http_status_phrase(result.get('status_code'))))}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=build_dashboard_keyboard(state),
                )
                await asyncio.sleep(RECOVERY_INTERVAL_SECONDS)
                result = await check_server()
                record_check(state, result)
                attempts += 1
                if result["ok"]:
                    break
        # Show final wake summary briefly in dashboard message style
        status = classify_status(result)
        summary = "Server is healthy." if result["ok"] else "Server did not recover."
        text = build_dashboard_text(state)
        text += (
            f"\n\n<b>Wake:</b> {html.escape(summary)} "
            f"({attempts} attempt{'s' if attempts != 1 else ''}, {status_badge(status)})"
        )
        await safe_edit_message(message, text, parse_mode=ParseMode.HTML, reply_markup=build_dashboard_keyboard(state))
        return

    if data == "dash:ping":
        await safe_edit_message(
            message,
            f"<b>{html.escape(SITE_NAME)}</b>\n\nPinging server...",
            parse_mode=ParseMode.HTML,
            reply_markup=build_dashboard_keyboard(state),
        )
        result = await check_server()
        record_check(state, result)
        ms = result.get("ms")
        ms_value = float(ms) if isinstance(ms, (int, float)) else None
        rating = ping_rating(ms_value) if ms_value is not None else "Unavailable"
        text = build_dashboard_text(state)
        text += (
            f"\n\n<b>Ping:</b> {f'{ms_value:.0f} ms' if ms_value is not None else 'n/a'}"
            f" ({html.escape(rating)}) | {html.escape(http_status_phrase(result.get('status_code')))}"
        )
        await safe_edit_message(message, text, parse_mode=ParseMode.HTML, reply_markup=build_dashboard_keyboard(state))
        return

    if data == "dash:history":
        history_text = build_history_text(state)
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Back to Dashboard", callback_data="dash:back")]]
        )
        await safe_edit_message(message, history_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
        return

    if data == "dash:toggle_alerts":
        state["alerts_enabled"] = not bool(state.get("alerts_enabled", True))
        save_state(state)
        await edit_dashboard_message(message, state)
        return

    if data == "dash:toggle_monitor":
        state["monitor_enabled"] = not bool(state.get("monitor_enabled", False))
        save_state(state)
        ensure_monitor_job(context.application, bool(state["monitor_enabled"]))
        await edit_dashboard_message(message, state)
        return

    logger.warning("Unknown dashboard callback: %s", data)
    await edit_dashboard_message(message, state)


# -----------------------------------------------------------------------------
# Application
# -----------------------------------------------------------------------------


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log unhandled exceptions from handlers."""
    logger.exception("Unhandled exception while handling update", exc_info=context.error)


async def post_init(application: Application) -> None:
    """Initialize commands, state, monitoring, and startup notification."""
    commands = [
        BotCommand("start", "🛡 Open Render Guardian control panel"),
        BotCommand("dashboard", "📊 Live status of lookforgeo.onrender.com"),
        BotCommand("status", "🔄 Check /up health endpoint and HTTP status"),
        BotCommand("wake", "⚡ Wake the Render server and wait until healthy"),
        BotCommand("ping", "🏓 Measure live server response time"),
        BotCommand("monitor_on", "▶️ Enable automatic health checks"),
        BotCommand("monitor_off", "⏸ Pause automatic health checks"),
        BotCommand("history", "📜 Recent checks and uptime percentage"),
        BotCommand("info", "ℹ️ Show monitored URL and bot settings"),
        BotCommand("help", "❓ List all commands"),
    ]
    try:
        await application.bot.set_my_commands(commands)
    except Exception as exc:  # noqa: BLE001 - command menu should not block startup
        logger.warning("Failed to set bot commands: %s", exc)

    state = application.bot_data.get("guardian_state")
    if not isinstance(state, dict):
        state = load_state()
        application.bot_data["guardian_state"] = state

    ensure_monitor_job(application, bool(state.get("monitor_enabled", AUTO_MONITOR)))

    try:
        await application.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=f"🛡️ <b>{html.escape(SITE_NAME)} started</b>\nMonitoring: {'ACTIVE' if state.get('monitor_enabled') else 'PAUSED'}\nAlerts: {'ON' if state.get('alerts_enabled', True) else 'OFF'}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:  # noqa: BLE001 - startup notification must not crash bot
        logger.warning("Failed to send startup message: %s", exc)


def main() -> None:
    """Run the Render Guardian bot."""
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("dashboard", dashboard_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("wake", wake_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("monitor_on", monitor_on_command))
    app.add_handler(CommandHandler("monitor_off", monitor_off_command))
    app.add_handler(CommandHandler("history", history_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CallbackQueryHandler(dashboard_callback, pattern=r"^dash:"))
    app.add_error_handler(error_handler)

    logger.info("%s started.", SITE_NAME)
    app.run_polling()


if __name__ == "__main__":
    main()
