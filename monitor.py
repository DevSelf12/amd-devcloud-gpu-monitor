#!/usr/bin/env python3
"""
AMD Dev Cloud GPU Stock Monitor
Uses Playwright to intercept the GraphQL API response.

Usage:
  python monitor.py                    # One-shot check
  python monitor.py --loop 60          # Check every 60s
  python monitor.py --telegram         # Send Telegram alerts
"""

import json, time, sys, os, argparse, logging, asyncio
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-gpu")

def load_cookies(path):
    """Load cookies and normalize to Playwright format."""
    with open(path) as f:
        data = json.load(f)
    if isinstance(data, list):
        # Cookie-Editor format — ensure domain/path present
        for c in data:
            if "domain" not in c:
                c["domain"] = ".devcloud.amd.com"
            if "path" not in c:
                c["path"] = "/"
        return data
    # Simple dict format
    return [{"name": k, "value": v, "domain": ".devcloud.amd.com", "path": "/"} for k, v in data.items()]

async def check_gpu_stock(cookie_file):
    from playwright.async_api import async_playwright
    
    cookies = load_cookies(cookie_file)
    result = {}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, args=[
            "--disable-blink-features=AutomationControlled",
            "--window-position=-2400,-2400",  # Off-screen
        ])
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        async def on_resp(response):
            if 'graphql' in response.url and response.request.method == 'POST':
                try:
                    req = json.loads(response.request.post_data or '{}')
                    if req.get('operationName') == 'dropletOptions':
                        body = await response.text()
                        result['data'] = json.loads(body)
                except:
                    pass

        page.on("response", on_resp)
        
        try:
            await page.goto("https://devcloud.amd.com/gpus?i=0dd79f", wait_until="networkidle", timeout=45000)
            await asyncio.sleep(8)
        except:
            pass
        
        await browser.close()
    
    if 'data' not in result:
        return None
    
    sizes = result['data'].get('data', {}).get('dropletOptions', {}).get('sizes', [])
    gpu_sizes = []
    for s in sizes:
        if not s.get('gpu_info'):
            continue
        gpu = s['gpu_info']
        gpu_sizes.append({
            "name": s['name'],
            "gpu_model": gpu['model'],
            "gpu_count": gpu['count'],
            "vram_gb": int(gpu['vram']['amount']),
            "vcpus": s['cpu_count'],
            "ram_gb": round(int(s['memory_in_bytes']) / (1024**3)),
            "price_hourly": s['price_per_hour'],
            "restriction": s.get('restriction'),
            "region_ids": s.get('region_ids', []),
            "in_stock": len(s.get('region_ids', [])) > 0,
        })
    return gpu_sizes

def format_report(gpus):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"AMD Dev Cloud GPU — {now}\n"]
    any_stock = False
    for g in gpus:
        if g['in_stock']:
            any_stock = True
            lines.append(f"🟢 IN STOCK: {g['name']}")
            lines.append(f"   {g['gpu_count']}x {g['gpu_model'].upper()} ({g['vram_gb']}GB) | ${g['price_hourly']}/hr")
            if g['restriction']:
                lines.append(f"   ⚠️ {g['restriction']}")
        else:
            lines.append(f"🔴 {g['name']} — {g['gpu_count']}x {g['gpu_model'].upper()} ({g['vram_gb']}GB) — kosong")
        lines.append("")
    if any_stock:
        lines.insert(1, "🟢 STOK ADA! Deploy: https://devcloud.amd.com/gpus\n")
    else:
        lines.insert(1, "🔴 Semua GPU kosong\n")
    return "\n".join(lines)

def send_telegram(token, chat_id, message):
    import requests
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": message}, timeout=10)
    return r.status_code == 200

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookies", "-c", default="cookies.json")
    parser.add_argument("--loop", "-l", type=int, default=0)
    parser.add_argument("--telegram", "-t", action="store_true")
    parser.add_argument("--guide", action="store_true")
    args = parser.parse_args()

    if args.guide:
        print("1. Login https://devcloud.amd.com")
        print("2. Cookie-Editor > Export > JSON > save as cookies.json")
        print("3. python monitor.py")
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    last_status = None

    while True:
        try:
            gpus = asyncio.run(check_gpu_stock(args.cookies))
        except Exception as e:
            log.error(f"Check failed: {e}")
            gpus = None

        if gpus:
            report = format_report(gpus)
            print(report)
            current = tuple(g['in_stock'] for g in gpus)
            if current != last_status:
                if args.telegram and tg_token and tg_chat:
                    send_telegram(tg_token, tg_chat, report)
                    log.info("Telegram alert sent")
                last_status = current
        else:
            log.warning("No data returned — cookies expired? Re-export from browser.")

        if args.loop <= 0:
            break
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
