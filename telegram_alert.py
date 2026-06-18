#!/usr/bin/env python3
"""
AMD Dev Cloud — Telegram Alert Bot
Standalone version with inline cookie loading.
"""

import json, time, sys, os, logging
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-tg")

GRAPHQL_URL = "https://devcloud.amd.com/graphql"
TEAM_ID = "0dd79f"

GRAPHQL_QUERY = {
    "operationName": "dropletOptions",
    "variables": {"dropletOptionsParams": {"type": "gpus"}},
    "query": """query dropletOptions($dropletOptionsParams: ListDropletOptionsRequest) {
  dropletOptions(dropletOptionsParams: $dropletOptionsParams) {
    sizes {
      name restriction id
      gpu_info { vram { unit amount } model count }
      price_per_month price_per_hour cpu_count memory_in_bytes region_ids
    }
    __typename
  }
}"""
}

def load_cookies(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        return {c["name"]: c["value"] for c in data if "name" in c}
    return data

def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def check_stock(cookie_file):
    cookies = load_cookies(cookie_file)
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json", "Content-Type": "application/json",
        "Referer": "https://devcloud.amd.com/gpus",
        "Origin": "https://devcloud.amd.com",
    })
    for name, value in cookies.items():
        domain = ".devcloud.amd.com" if name.startswith("_") else "devcloud.amd.com"
        s.cookies.set(name, value, domain=domain)

    r = s.post(f"{GRAPHQL_URL}?i={TEAM_ID}", json=GRAPHQL_QUERY, timeout=15)
    if r.status_code != 200:
        return None
    data = r.json()
    sizes = data.get("data", {}).get("dropletOptions", {}).get("sizes", [])
    return [s for s in sizes if s.get("gpu_info")]

def format_alert(gpu_sizes):
    now = datetime.now().strftime("%H:%M:%S")
    available = [s for s in gpu_sizes if len(s.get("region_ids", [])) > 0]
    if available:
        lines = [f"🟢 *GPU AVAILABLE — AMD Dev Cloud*", f"🕐 {now}\n"]
        for s in available:
            gpu = s["gpu_info"]
            lines.append(f"✅ *{s['name']}*")
            lines.append(f"   {gpu['count']}x {gpu['model'].upper()} ({gpu['vram']['amount']}GB VRAM)")
            lines.append(f"   ${s['price_per_hour']}/hr")
        lines.append(f"\n🔗 https://devcloud.amd.com/gpus")
    else:
        lines = [f"🔴 All GPUs out of stock — {now}"]
    return "\n".join(lines)

def main():
    load_env()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    cookie_file = os.environ.get("COOKIE_FILE", str(Path(__file__).parent / "cookies.json"))
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))

    if not tg_token or not tg_chat:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    log.info(f"Monitoring AMD Dev Cloud GPU stock (interval: {interval}s)")
    last_status = None

    while True:
        sizes = check_stock(cookie_file)
        if sizes:
            current = tuple(len(s.get("region_ids", [])) > 0 for s in sizes)
            if current != last_status:
                alert = format_alert(sizes)
                r = requests.post(
                    f"https://api.telegram.org/bot{tg_token}/sendMessage",
                    json={"chat_id": tg_chat, "text": alert, "parse_mode": "Markdown"},
                    timeout=10
                )
                log.info(f"Alert sent ({r.status_code})")
                last_status = current
        time.sleep(interval)

if __name__ == "__main__":
    main()
