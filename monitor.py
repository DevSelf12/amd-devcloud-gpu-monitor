#!/usr/bin/env python3
"""
AMD Dev Cloud GPU Stock Monitor — v2 Persistent Browser
Uses Playwright persistent context to keep session alive.

Usage:
  python monitor.py                    # One-shot check
  python monitor.py --loop 60          # Check every 60s
  python monitor.py --telegram         # Telegram alerts on status change
  python monitor.py --guide            # Cookie import help
  python monitor.py --login            # Open browser for manual login (saves session)
"""

import json, time, sys, os, argparse, logging, asyncio
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-gpu")

GPU_URL = "https://devcloud.amd.com/gpus?i=0dd79f"
BROWSER_DATA_DIR = str(Path(__file__).parent / "browser_data")

def load_cookies(path):
    """Load cookies from Cookie-Editor JSON (only used for initial import into browser_data)."""
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

async def do_login(cookies_file=None):
    """Open browser for manual login. Session saved to browser_data/."""
    from playwright.async_api import async_playwright

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            BROWSER_DATA_DIR,
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        # If cookies file provided, import them first
        if cookies_file:
            cookies = load_cookies(cookies_file)
            await browser.add_cookies(cookies)
            log.info(f"Imported {len(cookies)} cookies from {cookies_file}")

        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
        """)

        await page.goto("https://devcloud.amd.com", wait_until="domcontentloaded")
        log.info("Browser opened. Login manually, then press Ctrl+C here to close.")
        log.info("Session will be saved to browser_data/")

        try:
            # Keep browser open until user kills it
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await browser.close()
            log.info("Session saved!")

async def check_gpu_stock():
    """Check GPU stock using persistent browser context (auto-refreshes cookies)."""
    from playwright.async_api import async_playwright

    result = {"gql": None, "errors": [], "page_text": ""}

    if not Path(BROWSER_DATA_DIR).exists():
        log.error(f"No browser session found! Run: python monitor.py --login")
        log.error(f"  or: python monitor.py --cookies cookies.json --login")
        return result

    async with async_playwright() as p:
        browser = await p.chromium.launch_persistent_context(
            BROWSER_DATA_DIR,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        page = browser.pages[0] if browser.pages else await browser.new_page()
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
                    result["errors"].append(f"{op}: {body[:200]}")
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
    parser.add_argument("--cookies", "-c", default=None, help="Cookie-Editor JSON (for --login import)")
    parser.add_argument("--loop", "-l", type=int, default=0)
    parser.add_argument("--telegram", "-t", action="store_true")
    parser.add_argument("--guide", action="store_true")
    parser.add_argument("--login", action="store_true", help="Open browser for login (saves session)")
    args = parser.parse_args()

    if args.guide:
        print("=== AMD GPU Monitor Setup ===")
        print()
        print("Option A: Cookie import + persistent session")
        print("  1. Login https://devcloud.amd.com in your browser")
        print("  2. Cookie-Editor extension > Export > JSON")
        print("  3. Save as cookies.json in this folder")
        print("  4. python monitor.py --cookies cookies.json --login")
        print("  5. Close browser (Ctrl+C)")
        print("  6. python monitor.py --loop 60 --telegram")
        print()
        print("Option B: Manual login (headful)")
        print("  1. python monitor.py --login")
        print("  2. Login in the browser window")
        print("  3. Close browser (Ctrl+C)")
        print("  4. python monitor.py --loop 60 --telegram")
        return

    if args.login:
        asyncio.run(do_login(args.cookies))
        return

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    last = None
    fail_count = 0

    while True:
        try:
            result = asyncio.run(check_gpu_stock())
        except Exception as e:
            log.error(f"Error: {e}")
            result = None

        if result and result.get("gql"):
            gpus = parse_gpu_sizes(result["gql"])
            report = format_report(gpus)
            print(report)
            fail_count = 0
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
            fail_count += 1
            log.warning("Gagal ambil data GPU")
            if result:
                for e in result.get("errors", []):
                    log.warning(f"  {e}")
                page_text = result.get("page_text", "").lower()
                if "security check" in page_text:
                    log.warning("  → Cloudflare block! Session mungkin expired.")
                elif "couldn't find" in page_text or "404" in page_text:
                    log.warning("  → Page redirect/404. Session expired?")
                elif "sign in" in page_text or "log in" in page_text or "login" in page_text:
                    log.warning("  → Redirected to login! Session expired.")
            
            if fail_count >= 3:
                log.error("3x gagal berturut-turut. Session expired!")
                log.error("Re-login: python monitor.py --login")
                if args.telegram and tg_token and tg_chat:
                    send_telegram(tg_token, tg_chat, "⚠️ AMD Monitor: Session expired! Re-login needed.\nssh VPS → python monitor.py --login")
                break

        if args.loop <= 0:
            break
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
