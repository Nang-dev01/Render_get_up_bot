# Render Guardian

A Telegram server-monitoring dashboard bot for Render services. It checks a configured health endpoint, shows a live dashboard, tracks history/uptime, and sends smart state-change alerts.

## Install

```bash
python -m venv .venv
# Windows PowerShell
.venv\Scripts\Activate.ps1
# macOS/Linux
# source .venv/bin/activate

pip install -r requirements.txt
```

## Configure

Copy `.env.example` to `.env` and fill in your values:

```env
BOT_TOKEN=your_telegram_bot_token
ADMIN_USER_ID=123456789
RENDER_URL=https://your-app.onrender.com
HEALTH_PATH=/up

# Optional
MONITOR_INTERVAL=600
AUTO_MONITOR=false
SITE_NAME=Render Guardian
TZ_OFFSET_HOURS=7
```

Notes:
- `ADMIN_USER_ID` must be your numeric Telegram user ID.
- `RENDER_URL` should be the base URL of the service to monitor.
- `HEALTH_PATH` defaults to `/health` if omitted; `/up` is a common choice.
- `TZ_OFFSET_HOURS` controls the dashboard/check timestamp display timezone (default UTC+7).

## Run

```bash
python bot.py
```

On startup, the bot validates required env vars, loads `state.json` if present, registers Telegram commands, starts monitoring if enabled, and sends an admin startup message.

## Commands

| Command | Description |
|---|---|
| `/start` | Show the Render Guardian dashboard |
| `/dashboard` | Show the dashboard |
| `/status` | Run a live check and show HTTP status + response time |
| `/wake` | Request the server and retry until healthy or retries are exhausted |
| `/ping` | Measure response time with rating (Excellent <500ms, Good <1.5s, Fair <3s, Slow) |
| `/monitor_on` | Enable automatic monitoring |
| `/monitor_off` | Disable automatic monitoring |
| `/history` | Show last 10 checks and uptime % |
| `/info` | Show current configuration (no secrets) |
| `/help` | Show command list |

## Dashboard Buttons

- **Check Status** - Runs a live check and refreshes the dashboard.
- **Wake Server** - Requests the server and retries until healthy or retries run out.
- **Ping** - Measures response time and shows a rating.
- **History** - Shows recent checks and uptime.
- **Alerts ON/OFF** - Toggles state-change alerts.
- **Monitor ON/OFF** - Toggles automatic monitoring.
- **Open Website / Health Endpoint** - Opens configured URLs.

## Deploy

- Run as a long-lived worker/process (Render Worker, VPS, Docker, or system service).
- Set environment variables in your deployment platform; do not commit `.env`.
- Ensure the process can write `state.json` in the project directory for persisted history/toggles.
- For Render, deploy this bot as a **Background Worker** or private service; the monitored app can remain a separate Render Web Service.
