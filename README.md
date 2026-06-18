# AMD Dev Cloud GPU Stock Monitor 🖥️

Real-time monitor for GPU instance availability on [AMD Dev Cloud](https://devcloud.amd.com). Get instant Telegram alerts when GPUs become available!

## Why?

AMD Dev Cloud GPUs (MI300X, MI250X, etc.) are in high demand and often out of stock. This tool monitors availability and notifies you the moment stock opens up so you can deploy before it's gone.

## Features

- **Continuous monitoring** — polls AMD Dev Cloud API at configurable intervals
- **Telegram alerts** — instant notifications when GPU stock changes
- **Auto endpoint discovery** — tries known API endpoints automatically
- **Cookie-based auth** — uses your browser session cookies (no API key needed)
- **Multiple GPU targets** — monitors MI300X, MI250X, MI250, MI210, MI100, V620, W7900

## Scripts

| Script | Purpose |
|--------|---------|
| `monitor.py` | Main monitor — continuous stock checking with CLI options |
| `telegram_alert.py` | Standalone Telegram alert bot with .env config |
| `test_endpoint.py` | Helper to test/discover API endpoints from DevTools |

## Quick Start

### 1. Clone the repo

```bash
git clone https://github.com/DevSelf12/amd-devcloud-gpu-monitor.git
cd amd-devcloud-gpu-monitor
```

### 2. Install dependencies

```bash
pip install requests
```

### 3. Export your browser cookies

1. Open https://devcloud.amd.com in Chrome/Firefox
2. Login with your AMD account
3. Install [Cookie-Editor](https://chrome.google.com/webstore/detail/cookie-editor/hlkenndednhfkekhgcdicdfddnkalmdm) extension (or use DevTools)
4. Export cookies as JSON → save as `cookies.json`

**Cookie format** (Cookie-Editor export):
```json
[
  {"name": "cookie_name", "value": "cookie_value", "domain": ".amd.com"},
  ...
]
```

**Or simple format:**
```json
{"cookie_name": "cookie_value", ...}
```

### 4. Capture the API endpoint

1. Open DevTools (F12) → Network tab → filter by XHR/Fetch
2. Navigate to the GPU availability page on devcloud.amd.com
3. Find the request returning stock/availability data
4. Copy the URL

Test it:
```bash
python test_endpoint.py "https://devcloud.amd.com/api/machines"
```

### 5. Run the monitor

**Simple one-shot check:**
```bash
python monitor.py --once
```

**Continuous monitoring:**
```bash
python monitor.py --interval 30
```

**With custom endpoint:**
```bash
python monitor.py --endpoint "https://devcloud.amd.com/api/your-endpoint"
```

**Discover endpoints automatically:**
```bash
python monitor.py --discover
```

## Telegram Alerts Setup

### 1. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram
2. Send `/newbot` and follow instructions
3. Copy the bot token

### 2. Get your chat ID

1. Message your new bot
2. Visit `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
3. Find your `chat.id` in the response

### 3. Configure .env

```bash
cp .env.example .env
```

Edit `.env`:
```
TELEGRAM_BOT_TOKEN=123456789:ABCdefGhIjKlMnOpQrStUvWxYz
TELEGRAM_CHAT_ID=your_chat_id
AMD_ENDPOINT=https://devcloud.amd.com/api/machines
COOKIE_FILE=cookies.json
CHECK_INTERVAL=60
```

### 4. Run the alert bot

```bash
python telegram_alert.py
```

## CLI Options (monitor.py)

```
--cookies, -c    Path to cookies JSON file (default: cookies.json)
--interval, -i   Check interval in seconds (default: 60)
--once           Check once and exit
--endpoint, -e   Override API endpoint URL
--guide          Show cookie export instructions
--discover       Try to discover API endpoints automatically
--verbose, -v    Enable debug logging
```

## Running as a Background Service

**Linux/macOS (with nohup):**
```bash
nohup python telegram_alert.py > monitor.log 2>&1 &
```

**Windows (with pythonw):**
```bash
start /B pythonw telegram_alert.py
```

**With systemd** (create `/etc/systemd/system/amd-monitor.service`):
```ini
[Unit]
Description=AMD Dev Cloud GPU Monitor
After=network.target

[Service]
ExecStart=/usr/bin/python3 /path/to/telegram_alert.py
WorkingDirectory=/path/to/amd-devcloud-gpu-monitor
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
```

## ⚠️ Important Notes

- **Cookies expire** — you'll need to re-export periodically
- **Rate limiting** — don't set intervals too low (< 30s) to avoid being blocked
- **API changes** — AMD may change their API; use `test_endpoint.py` to find new endpoints
- **For personal use** — this is a scraping tool, use responsibly

## License

MIT
