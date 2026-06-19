#!/usr/bin/env python3
"""
AMD Dev Cloud GPU Stock Monitor — v3 Headless Session Import
Uses Playwright persistent context. No GUI needed.

Usage:
  python monitor.py --setup cookies.json   # Import cookies (headless, no browser needed)
  python monitor.py --setup                # Re-establish session from existing browser_data/
  python monitor.py --loop 60 --telegram   # Monitor loop
  python monitor.py                        # One-shot check
  python monitor.py --login                # Manual login (needs display/Xvfb)
"""

import json, time, sys, os, argparse, logging, asyncio
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-gpu")

GPU_URL = "https://devcloud.amd.com/gpus?i=0dd79f"
BROWSER_DATA_DIR = str(Path(__file__).parent / "browser_data")

# Playwright sameSite mapping
SAME_SITE_MAP = {
    "strict": "Strict",
    "lax": "Lax",
    "no_restriction": "None",
    "none": "None",
    None: "None",
}

def load_cookies(path):
    """Load cookies from Cookie-Editor JSON and normalize for Playwright."""
    with open(path) as f:
        data = json.load(f)

    cookies = []
    for c in data:
        cookie = {
            "name": c["name"],
            "value": c["value"],
            "domain": c.get("domain", ".devcloud.amd.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
        }
        # Playwright sameSite: "Strict" | "Lax" | "None"
        raw_ss = c.get("sameSite")
        if isinstance(raw_ss, str):
            cookie["sameSite"] = SAME_SITE_MAP.get(raw_ss.lower(), "None")
        else:
            cookie["sameSite"] = "None"

        # Only set expirationDate if it's a persistent cookie (not session)
        if "expirationDate" in c and c.get("expirationDate") and not c.get("session", False):
            cookie["expires"] = float(c["expirationDate"])

        cookies.append(cookie)

    return cookies

async def do_setup(cookies_file=None):
    """Import cookies into persistent context in headless mode. No display needed."""
    from playwright.async_api import async_playwright

    if not cookies_file and not Path(BROWSER_DATA_DIR).exists():
        log.error("No cookies file and no existing browser_data/. Nothing to setup.")
        log.error("Usage: python monitor.py --setup cookies.json")
        return False

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

        # Step 1: Import cookies if provided
        if cookies_file:
            cookies = load_cookies(cookies_file)
            await browser.add_cookies(cookies)
            log.info(f"Imported {len(cookies)} cookies from {cookies_file}")

        # Step 2: Visit the site to establish session + refresh Cloudflare tokens
        log.info("Visiting AMD DevCloud to establish session...")
        result = {"gql": None, "errors": [], "status": None, "page_text": ""}

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
                    result["errors"].append(f"{op} ({response.status}): {body[:200]}")
            except Exception as e:
                result["errors"].append(f"resp err: {e}")

        page.on("response", on_resp)

        try:
            resp = await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
            result["status"] = resp.status if resp else None

            # Wait for GraphQL
            for i in range(15):
                await asyncio.sleep(2)
                if result["gql"]:
                    break

            result["page_text"] = await page.inner_text("body")
        except Exception as e:
            result["errors"].append(f"nav: {e}")
        finally:
            await browser.close()

    # Step 3: Report
    if result["gql"]:
        sizes = result["gql"].get("data", {}).get("dropletOptions", {}).get("sizes", [])
        gpu_count = sum(1 for s in sizes if s.get("gpu_info"))
        log.info(f"✅ Session OK! GraphQL working ({gpu_count} GPU types found)")
        return True
    else:
        page_lower = result["page_text"].lower()
        log.error("❌ Session setup FAILED!")
        for e in result["errors"]:
            log.error(f"  {e}")
        if "sign in" in page_lower or "log in" in page_lower:
            log.error("  → Redirected to login. Cookies sudah expired di server.")
            log.error("  → Export FRESH cookies dari browser lo SEKARANG, jangan yang lama.")
            log.error("  → Pastikan lo baru login ke devcloud.amd.com, langsung export.")
        elif "security check" in page_lower or "captcha" in page_lower:
            log.error("  → Cloudflare block. Coba lagi dalam beberapa menit.")
            log.error("  → Atau buka devcloud.amd.com di browser, lewati challenge, export lagi.")
        return False

async def do_login(cookies_file=None):
    """Open browser for manual login. Needs display/Xvfb."""
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

        if cookies_file:
            cookies = load_cookies(cookies_file)
            await browser.add_cookies(cookies)
            log.info(f"Imported {len(cookies)} cookies")

        page = browser.pages[0] if browser.pages else await browser.new_page()
        await page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
        """)

        await page.goto("https://devcloud.amd.com", wait_until="domcontentloaded")
        log.info("Browser opened. Login manually, then press Ctrl+C here.")

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await browser.close()
            log.info("Session saved to browser_data/")

async def check_gpu_stock():
    """Check GPU stock using persistent browser context."""
    from playwright.async_api import async_playwright

    result = {"gql": None, "errors": [], "page_text": ""}

    if not Path(BROWSER_DATA_DIR).exists():
        log.error(f"No session! Run: python monitor.py --setup cookies.json")
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
    parser.add_argument("--setup", nargs="?", const="__NO_FILE__", default=None,
                        help="Import cookies & establish session (headless). Pass cookies.json path.")
    parser.add_argument("--cookies", "-c", default=None, help="Cookie file (for --login)")
    parser.add_argument("--loop", "-l", type=int, default=0)
    parser.add_argument("--telegram", "-t", action="store_true")
    parser.add_argument("--guide", action="store_true")
    parser.add_argument("--login", action="store_true", help="Manual login (needs display)")
    args = parser.parse_args()

    if args.guide:
        print("=== AMD GPU Monitor — VPS Setup (No Display Needed) ===")
        print()
        print("1. Login https://devcloud.amd.com di browser PC/HP lo")
        print("2. Cookie-Editor > Export > JSON")
        print("3. Copy ke VPS: ~/bot/amd-devcloud-gpu-monitor/cookies.json")
        print("4. Import & test:")
        print("   python3 monitor.py --setup cookies.json")
        print("   (Harus muncul: ✅ Session OK!)")
        print("5. Jalankan monitor:")
        print("   python3 monitor.py --loop 60 --telegram")
        print()
        print("Session tersimpan di browser_data/. Kalau expired:")
        print("   → Export FRESH cookies dari browser, ulangi step 3-4.")
        print("   → `_digitalocean_remember_me` bertahan ~30 hari.")
        print("   → `__cf_bm` di-refresh otomatis tiap visit.")
        return

    if args.login:
        asyncio.run(do_login(args.cookies))
        return

    if args.setup is not None:
        cookie_file = args.setup if args.setup != "__NO_FILE__" else None
        if cookie_file and not Path(cookie_file).exists():
            log.error(f"File not found: {cookie_file}")
            sys.exit(1)
        success = asyncio.run(do_setup(cookie_file))
        sys.exit(0 if success else 1)

    # Monitor mode
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
            if cur != last:
                if args.telegram and tg_token and tg_chat:
                    send_telegram(tg_token, tg_chat, report)
                    log.info("TG alert sent")
                last = cur
        else:
            fail_count += 1
            log.warning("Gagal ambil data GPU")
            if result:
                for e in result.get("errors", []):
                    log.warning(f"  {e}")
                page_text = result.get("page_text", "").lower()
                if "sign in" in page_text or "log in" in page_text:
                    log.warning("  → Session expired! Re-setup needed.")
                elif "security check" in page_text:
                    log.warning("  → Cloudflare challenge. Tunggu beberapa menit.")

            if fail_count >= 3:
                log.error("3x gagal berturut-turut!")
                log.error("Re-setup: python3 monitor.py --setup cookies.json")
                if args.telegram and tg_token and tg_chat:
                    send_telegram(tg_token, tg_chat,
                        "⚠️ AMD Monitor: Session expired!\n"
                        "Re-setup:\n"
                        "1. Export FRESH cookies dari browser\n"
                        "2. scp cookies.json ubuntu@199.244.48.151:~/bot/amd-devcloud-gpu-monitor/\n"
                        "3. python3 monitor.py --setup cookies.json")
                break

        if args.loop <= 0:
            break
        time.sleep(args.loop)

if __name__ == "__main__":
    main()
