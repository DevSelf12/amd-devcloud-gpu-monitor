#!/usr/bin/env python3
"""
AMD Dev Cloud GPU Stock Monitor
Uses Playwright to intercept GraphQL responses.

Usage:
  python monitor.py                    # One-shot check
  python monitor.py --loop 60          # Check every 60s  
  python monitor.py --telegram         # Telegram alerts on status change
  python monitor.py --guide            # Cookie export help
"""

import json, time, sys, os, argparse, logging, asyncio
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-gpu")

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

async def check_gpu_stock(cookie_file):
    from playwright.async_api import async_playwright
    
    cookies = load_cookies(cookie_file)
    result = {"gql": None, "errors": [], "page_text": ""}
    
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
            ]
        )
        ctx = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        await ctx.add_cookies(cookies)
        page = await ctx.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
        """)

        async def on_resp(response):
            url = response.url
            if "graphql" not in url or response.request.method != "POST":
                return
            try:
                req = json.loads(response.request.post_data or "{}")
                op = req.get("operationName", "")
                body = await response.text()
                if op == "dropletOptions" and "gpu_info" in body:
                    result["gql"] = json.loads(body)
                elif response.status >= 400:
                    result["errors"].append(f"{op}: {body[:100]}")
            except Exception as e:
                result["errors"].append(f"resp parse err: {e}")

        page.on("response", on_resp)

        try:
            await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
            # Wait for GraphQL to load
            for _ in range(15):
                await asyncio.sleep(2)
                if result["gql"]:
                    break
            result["page_text"] = await page.inner_text("body")
        except Exception as e:
            result["errors"].append(f"nav: {e}")
        finally:
            await browser.close()
    
    return result

def parse_gpu_sizes(gql_data):
    sizes = gql_data.get("data", {}).get("dropletOptions", {}).get("sizes", [])
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
            "vcpus": s["cpu_count"],
            "ram": round(int(s["memory_in_bytes"]) / 2**30),
            "price": s["price_per_hour"],
            "regions": s.get("region_ids", []),
            "restriction": s.get("restriction"),
            "in_stock": len(s.get("region_ids", [])) > 0,
        })
    return out

def format_report(gpus):
    now = datetime.now().strftime("%H:%M:%S")
    lines = [f"AMD MI300X GPU Status — {now}", ""]
    any_stock = False
    for g in gpus:
        if g["in_stock"]:
            any_stock = True
            lines.append(f"🟢 IN STOCK: {g['count']}x {g['model'].upper()} ({g['vram']}GB)")
            lines.append(f"   {g['vcpus']}vCPU / {g['ram']}GB RAM / ${g['price']}/hr")
            if g["restriction"]:
                lines.append(f"   ⚠️ {g['restriction']}")
        else:
            lines.append(f"🔴 {g['count']}x {g['model'].upper()} ({g['vram']}GB) — kosong")
        lines.append("")
    if any_stock:
        lines.insert(1, "🟢 STOK ADA! https://devcloud.amd.com/gpus")
    else:
        lines.insert(1, "🔴 Semua GPU kosong")
    return "\n".join(lines)

def send_telegram(token, chat_id, text):
    import requests
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text}, timeout=10)
        return r.status_code == 200
    except:
        return False

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cookies", "-c", default="cookies.json")
    parser.add_argument("--loop", "-l", type=int, default=0)
    parser.add_argument("--telegram", "-t", action="store_true")
    parser.add_argument("--guide", action="store_true")
    args = parser.parse_args()

    if args.guide:
        print("1. Login https://devcloud.amd.com")
        print("2. Cookie-Editor extension > Export > JSON")
        print("3. Save as cookies.json")
        print("4. python monitor.py")
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    last = None

    while True:
        try:
            result = asyncio.run(check_gpu_stock(args.cookies))
        except Exception as e:
            log.error(f"Error: {e}")
            result = None

        if result and result.get("gql"):
            gpus = parse_gpu_sizes(result["gql"])
            report = format_report(gpus)
            print(report)
            cur = tuple(g["in_stock"] for g in gpus)
            any_stock = any(cur)
            if cur != last:
                if args.telegram and tg_token and tg_chat and any_stock:
                    send_telegram(tg_token, tg_chat, report)
                    log.info("TG alert sent (stock available)")
                elif args.telegram and not any_stock:
                    log.info("TG alert skipped (all out of stock)")
                last = cur
        else:
            log.warning("Gagal ambil data GPU")
            if result:
                for e in result.get("errors", []):
                    log.warning(f"  {e}")
                if "security check" in result.get("page_text", "").lower():
                    log.warning("  → Cloudflare block! Coba cookies baru.")
                elif "couldn't find" in result.get("page_text", "").lower():
                    log.warning("  → Page 404. Cookies expired?")
            log.warning("  → Re-export cookies dari browser!")

        if args.loop <= 0:
            break
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
