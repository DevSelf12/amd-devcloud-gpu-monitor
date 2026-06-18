#!/usr/bin/env python3
"""
AMD Dev Cloud — Telegram Alert Bot
Uses Playwright to intercept GraphQL. Sends Telegram every check.
"""

import json, time, sys, os, logging, asyncio
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-tg")

GPU_URL = "https://devcloud.amd.com/gpus?i=0dd79f"

def load_cookies(path):
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        for c in data:
            c.setdefault("domain", ".devcloud.amd.com")
            c.setdefault("path", "/")
            c.setdefault("sameSite", "None")
            c.setdefault("secure", True)
            c.setdefault("httpOnly", False)
        return data
    return [{"name": k, "value": v, "domain": ".devcloud.amd.com", "path": "/"} for k, v in data.items()]

def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

async def check_gpu_stock(cookie_file):
    from playwright.async_api import async_playwright

    cookies = load_cookies(cookie_file)
    result = {}

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", "--window-position=-3000,-3000",
                  "--no-first-run", "--no-default-browser-check"]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        async def on_resp(response):
            if "graphql" in response.url and response.request.method == "POST":
                try:
                    req = json.loads(response.request.post_data or "{}")
                    body = await response.text()
                    if req.get("operationName") == "dropletOptions" and "gpu_info" in body:
                        result["data"] = json.loads(body)
                except:
                    pass

        page.on("response", on_resp)
        try:
            await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
            for _ in range(15):
                await asyncio.sleep(2)
                if "data" in result:
                    break
        except:
            pass
        finally:
            await browser.close()

    if "data" not in result:
        return None

    sizes = result["data"].get("data", {}).get("dropletOptions", {}).get("sizes", [])
    out = []
    for s in sizes:
        if not s.get("gpu_info"):
            continue
        g = s["gpu_info"]
        out.append({
            "name": s["name"],
            "model": g["model"],
            "count": g["count"],
            "vram": int(g["vram"]["amount"]),
            "price": s["price_per_hour"],
            "regions": s.get("region_ids", []),
            "restriction": s.get("restriction"),
            "in_stock": len(s.get("region_ids", [])) > 0,
        })
    return out

def format_alert(gpus):
    now = datetime.now().strftime("%H:%M:%S")
    available = [g for g in gpus if g["in_stock"]]
    if available:
        lines = [f"🚨🚨🚨 *STOK ADA!!! AMD MI300X AVAILABLE!* 🚨🚨🚨", f"🕐 {now}", ""]
        for g in available:
            lines.append(f"✅ *{g['count']}x {g['model'].upper()}* ({g['vram']}GB VRAM)")
            lines.append(f"   ${g['price']}/hr")
            if g["restriction"]:
                lines.append(f"   ⚠️ {g['restriction']}")
        lines += ["", "⚡ *LANGSUNG DEPLOY SEKARANG:*", "🔗 https://devcloud.amd.com/gpus"]
    else:
        lines = [f"🔴 Monitor — {now}", ""]
        for g in gpus:
            lines.append(f"🔴 {g['count']}x {g['model'].upper()} ({g['vram']}GB) — kosong")
        lines += ["", "_Next check dalam 60 detik..._"]
    return "\n".join(lines)

def send_tg(token, chat_id, text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        return r.status_code == 200
    except:
        return False

def main():
    load_env()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    cookie_file = os.environ.get("COOKIE_FILE", str(Path(__file__).parent / "cookies.json"))
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))

    if not tg_token or not tg_chat:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env or env vars")
        sys.exit(1)

    log.info(f"Monitoring (interval: {interval}s)")
    send_tg(tg_token, tg_chat, f"✅ AMD GPU Monitor started! Checking every {interval}s")

    while True:
        try:
            gpus = asyncio.run(check_gpu_stock(cookie_file))
        except Exception as e:
            log.error(f"Error: {e}")
            gpus = None

        if gpus:
            alert = format_alert(gpus)
            send_tg(tg_token, tg_chat, alert)
            log.info("Alert sent")
        else:
            log.warning("Check failed — cookies expired? Re-export from browser.")

        time.sleep(interval)

if __name__ == "__main__":
    main()
