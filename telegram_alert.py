#!/usr/bin/env python3
"""
AMD Dev Cloud — Telegram Alert Bot
=====================================
Monitors GPU stock and sends Telegram notifications.
Designed to run as a Hermes cron job or standalone.

Setup:
  1. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or env vars
  2. Export cookies to cookies.json
  3. Run standalone or via Hermes cron
"""

import json
import os
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tg-alert")


def load_env():
    """Load .env file if exists."""
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def send_telegram(token: str, chat_id: str, message: str):
    """Send message via Telegram Bot API."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=data, timeout=10)
        if resp.status_code != 200:
            log.error(f"Telegram send failed: {resp.text[:200]}")
        return resp.status_code == 200
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def check_gpu_stock(cookie_file: str, endpoint: str) -> dict | None:
    """
    Check GPU stock at the given endpoint.
    Returns availability dict or None on error.
    """
    # Load cookies
    with open(cookie_file) as f:
        data = json.load(f)
    if isinstance(data, list):
        cookies = {c["name"]: c["value"] for c in data if "name" in c}
    else:
        cookies = data

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://devcloud.amd.com/",
    })

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        resp = session.get(endpoint, headers={"Cookie": cookie_str}, timeout=15)
        if resp.status_code != 200:
            log.warning(f"HTTP {resp.status_code}")
            return None

        json_data = resp.json()
        result = {}

        # Parse - adapt to actual API response structure
        if isinstance(json_data, list):
            for item in json_data:
                gpu = item.get("gpuType") or item.get("gpu_type") or item.get("type") or "unknown"
                status = item.get("status") or item.get("state") or ""
                if status.lower() in ("available", "ready", "idle", "running"):
                    result[gpu] = result.get(gpu, 0) + 1
        elif isinstance(json_data, dict):
            for key in ["gpus", "machines", "instances", "availability", "data", "results"]:
                if key in json_data:
                    items = json_data[key]
                    if isinstance(items, list):
                        for item in items:
                            gpu = item.get("gpuType") or item.get("gpu_type") or item.get("type")
                            avail = item.get("available") or item.get("count") or item.get("stock")
                            if gpu and avail is not None:
                                result[gpu] = int(avail)
                            elif gpu and item.get("status", "").lower() in ("available", "ready"):
                                result[gpu] = result.get(gpu, 0) + 1
                    elif isinstance(items, dict):
                        for gpu, count in items.items():
                            result[gpu] = int(count) if count else 0
                    break

        return result

    except Exception as e:
        log.error(f"Check failed: {e}")
        return None


def format_alert(availability: dict) -> str:
    """Format Telegram alert message."""
    now = datetime.now().strftime("%H:%M:%S")
    available = {k: v for k, v in availability.items() if v > 0}

    if available:
        lines = [f"🟢 *GPU AVAILABLE — AMD Dev Cloud*", f"🕐 {now}\n"]
        for gpu, count in sorted(available.items()):
            lines.append(f"✅ *{gpu}*: {count} unit")
        lines.append(f"\n🔗 https://devcloud.amd.com")
        lines.append("_Quick! Deploy now before stock runs out!_")
    else:
        lines = [f"🔴 All GPUs out of stock — {now}"]

    return "\n".join(lines)


def main():
    load_env()

    # Config from env or .env
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    cookie_file = os.environ.get("COOKIE_FILE", str(Path(__file__).parent / "cookies.json"))
    endpoint = os.environ.get("AMD_ENDPOINT", "")
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))

    if not tg_token or not tg_chat:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or env vars")
        sys.exit(1)

    if not endpoint:
        log.error("Set AMD_ENDPOINT in .env (capture from DevTools)")
        sys.exit(1)

    log.info(f"Telegram alerts → chat {tg_chat}")
    log.info(f"Endpoint: {endpoint}")
    log.info(f"Interval: {interval}s")

    last_status = {}

    while True:
        avail = check_gpu_stock(cookie_file, endpoint)

        if avail and avail != last_status:
            # Status changed — send alert
            alert = format_alert(avail)
            send_telegram(tg_token, tg_chat, alert)
            log.info(f"Alert sent! {avail}")

            # Also alert if something became available
            new_available = {k: v for k, v in avail.items() if v > 0 and last_status.get(k, 0) == 0}
            if new_available:
                urgent = f"🚨 *STOCK ALERT!* {', '.join(new_available.keys())} just became available!"
                send_telegram(tg_token, tg_chat, urgent)

            last_status = avail
        elif avail:
            log.debug(f"No change: {avail}")
        else:
            log.warning("Empty response")

        time.sleep(interval)


if __name__ == "__main__":
    main()
