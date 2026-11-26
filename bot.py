
import os
import logging

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Hide per-request httpx polling logs
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# -----------------------------
# CONFIGURATION
# -----------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_USER_ID = os.getenv("ADMIN_USER_ID")
RENDER_URL = os.getenv("RENDER_URL", "").rstrip("/")

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

AUTO_MONITOR = os.getenv(
    "AUTO_MONITOR", "false"
).lower() == "true"

if not BOT_TOKEN or not ADMIN_USER_ID or not RENDER_URL:
    raise ValueError(
        "Missing BOT_TOKEN, ADMIN_USER_ID or RENDER_URL in environment/.env"
    )

try:
    ADMIN_USER_ID = int(ADMIN_USER_ID)
except ValueError as exc:
    raise ValueError(
        "ADMIN_USER_ID must be a numeric Telegram user ID"
    ) from exc

HEALTH_PATH = os.getenv("HEALTH_PATH", "/health")
if not HEALTH_PATH.startswith("/"):
    HEALTH_PATH = "/" + HEALTH_PATH

logger.info("Health check target: %s%s", RENDER_URL, HEALTH_PATH)
# -----------------------------
# SECURITY
# -----------------------------

def is_admin(update: Update) -> bool:
    return (
        update.effective_user is not None
        and update.effective_user.id == ADMIN_USER_ID
    )


async def restricted(update: Update) -> bool:
    if not is_admin(update):
        msg = update.effective_message
        if msg:
            try:
                await msg.reply_text("Unauthorized access.")
            except TelegramError as e:
                logger.warning("Failed to reply unauthorized: %s", e)
        return True
    return False


async def safe_send(bot, chat_id: int, text: str) -> None:
    try:
        await bot.send_message(chat_id=chat_id, text=text)
    except TelegramError as e:
        logger.warning("Failed to send Telegram message: %s", e)


# -----------------------------
# SERVER CHECK
# -----------------------------

async def check_server():
    url = f"{RENDER_URL}{HEALTH_PATH}"
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(100.0)
        ) as client:
            response = await client.get(url)
            return (
                200 <= response.status_code < 300,
                response.status_code,
            )
    except httpx.RequestError as error:
        logger.warning("Server check failed: %s (%s)", error, url)
        return False, str(error)


# -----------------------------
# TELEGRAM COMMANDS
# -----------------------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return

    await update.message.reply_text(
        "Welcome to Render Wake-Up Bot!\n\n"
        "/wake - Wake/check your server\n"
        "/status - Check server status\n"
        "/monitor_on - Enable monitoring\n"
        "/monitor_off - Disable monitoring\n"
        "/info - Show config/status\n"
        "/help - Show commands"
    )


async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return

    await update.message.reply_text(
        "Available commands:\n\n"
        "/wake - Wake/check your Render server\n"
        "/status - Check server response\n"
        "/monitor_on - Enable automatic monitoring\n"
        "/monitor_off - Disable monitoring\n"
        "/info - Show bot/config status\n"
        "/help - Show help"
    )


async def info_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return
    jobs = context.application.job_queue.get_jobs_by_name("server_monitor")
    monitor_running = len(jobs) > 0
    text = (
        "Bot info (admin view)\n\n"
        f"Checked URL: {RENDER_URL}{HEALTH_PATH}\n"
        f"Monitor interval: {MONITOR_INTERVAL}s\n"
        f"AUTO_MONITOR env: {AUTO_MONITOR}\n"
        f"Monitoring job active: {monitor_running}"
    )
    await update.message.reply_text(text)


async def wake(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return

    try:
        message = await update.message.reply_text(
            "Starting server check... Please wait."
        )
    except TelegramError as e:
        logger.warning("Failed to send wake prompt: %s", e)
        return

    online, result = await check_server()

    checked_url = f"{RENDER_URL}{HEALTH_PATH}"
    try:
        if online:
            await message.edit_text(
                f"SERVER ONLINE\n\n"
                f"HTTP Status: {result}\n"
                f"Checked URL: {checked_url}"
            )
        else:
            await message.edit_text(
                f"Server did not respond successfully.\n\n"
                f"Result: {result}\n"
                f"Checked URL: {checked_url}\n"
                "Check your Render dashboard and logs."
            )
    except TelegramError as e:
        logger.warning("Failed to edit wake message: %s", e)
        try:
            await update.effective_message.reply_text("Check completed but failed to update the previous message.")
        except TelegramError:
            pass


async def status(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return

    try:
        await update.message.reply_text("Checking server status...")
    except TelegramError as e:
        logger.warning("Failed to send status prompt: %s", e)
        return

    online, result = await check_server()

    checked_url = f"{RENDER_URL}{HEALTH_PATH}"
    if online:
        text = (
            "SERVER ONLINE\n"
            f"HTTP: {result}\n"
            f"Checked URL: {checked_url}"
        )
    else:
        text = (
            "SERVER NOT RESPONDING\n"
            f"Result: {result}\n"
            f"Checked URL: {checked_url}"
        )

    try:
        await update.message.reply_text(text)
    except TelegramError as e:
        logger.warning("Failed to send status result: %s", e)


# -----------------------------
# AUTOMATIC MONITORING
# -----------------------------

async def monitor_server(
    context: ContextTypes.DEFAULT_TYPE
):
    online, result = await check_server()

    previous = context.application.bot_data.get(
        "last_status"
    )

    context.application.bot_data["last_status"] = online

    # Notify on first run (baseline) and on changes.
    if previous is None:
        if online:
            text = (
                "MONITORING STARTED (baseline)\n\n"
                f"SERVER ONLINE\nHTTP Status: {result}\nURL: {RENDER_URL}{HEALTH_PATH}"
            )
        else:
            text = (
                "MONITORING STARTED (baseline)\n\n"
                f"SERVER NOT RESPONDING\nResult: {result}\nURL: {RENDER_URL}{HEALTH_PATH}"
            )
        await safe_send(context.bot, ADMIN_USER_ID, text)
        return

    if previous == online:
        return

    if online:
        message = (
            "SERVER RECOVERED\n\n"
            f"HTTP Status: {result}\nURL: {RENDER_URL}{HEALTH_PATH}"
        )
    else:
        message = (
            "SERVER NOT RESPONDING\n\n"
            f"Result: {result}\nPlease check Render logs."
        )

    await safe_send(context.bot, ADMIN_USER_ID, message)


async def monitor_on(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return

    job = context.application.job_queue.get_jobs_by_name(
        "server_monitor"
    )

    if job:
        await update.message.reply_text(
            "Automatic monitoring is already enabled."
        )
        return

    context.application.job_queue.run_repeating(
        monitor_server,
        interval=MONITOR_INTERVAL,
        first=1,
        name="server_monitor"
    )

    await update.message.reply_text(
        f"Automatic monitoring enabled!\n"
        f"Interval: {MONITOR_INTERVAL} seconds."
    )


async def monitor_off(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    if await restricted(update):
        return

    jobs = context.application.job_queue.get_jobs_by_name(
        "server_monitor"
    )

    for job in jobs:
        job.schedule_removal()

    await update.message.reply_text(
        "Automatic monitoring disabled."
    )


# -----------------------------
# APPLICATION
# -----------------------------

async def post_init(application):
    if AUTO_MONITOR:
        application.job_queue.run_repeating(
            monitor_server,
            interval=MONITOR_INTERVAL,
            first=1,
            name="server_monitor"
        )

        logger.info("Automatic monitoring enabled.")


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("wake", wake))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("monitor_on", monitor_on))
    app.add_handler(CommandHandler("monitor_off", monitor_off))
    app.add_handler(CommandHandler("info", info_command))

    logger.info("Render Wake-Up Bot started.")

    app.run_polling()


if __name__ == "__main__":
    main()
