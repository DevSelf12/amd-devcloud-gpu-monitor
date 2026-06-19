#!/usr/bin/env python3
"""
AMD Dev Cloud GPU Monitor + Instant Auto-Deploy (v7)
Imports cookies at startup. Handles session expiry with retry.

Usage:
  python telegram_alert.py cookies.json              # Monitor only
  python telegram_alert.py cookies.json --auto-deploy # Monitor + auto-deploy
"""

import json, time, sys, os, logging, asyncio, argparse
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("amd-gpu")

GPU_URL = "https://devcloud.amd.com/gpus?i=0dd79f"
BROWSER_DATA_DIR = str(Path(__file__).parent / "browser_data")
SSH_KEY_ID = 57192576
IMAGE_FALLBACK = "195932981"

SAME_SITE_MAP = {"strict": "Strict", "lax": "Lax", "no_restriction": "None", "none": "None", None: "None"}

def load_env():
    for line in (Path(__file__).parent / ".env").read_text().splitlines() if (Path(__file__).parent / ".env").exists() else []:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def load_cookies(path):
    with open(path) as f:
        data = json.load(f)
    cookies = []
    for c in data:
        cookie = {
            "name": c["name"], "value": c["value"],
            "domain": c.get("domain", ".devcloud.amd.com"),
            "path": c.get("path", "/"),
            "secure": c.get("secure", True),
            "httpOnly": c.get("httpOnly", False),
        }
        raw_ss = c.get("sameSite")
        cookie["sameSite"] = SAME_SITE_MAP.get(raw_ss.lower(), "None") if isinstance(raw_ss, str) else "None"
        if "expirationDate" in c and c.get("expirationDate") and not c.get("session", False):
            cookie["expires"] = float(c["expirationDate"])
        cookies.append(cookie)
    return cookies

def send_tg(token, chat_id, text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        return r.status_code == 200
    except:
        return False

def extract_images(gql_data):
    images = {}
    for d in gql_data.get("data", {}).get("dropletOptions", {}).get("distributions", []):
        for img in d.get("images", []):
            if "rocm" in img.get("name", "").lower() or "rocm" in d.get("name", "").lower():
                images["rocm"] = img["id"]
            elif "24.04" in img.get("name", "") and "ubuntu" in d.get("name", "").lower():
                images["ubuntu-24"] = img["id"]
    return images

async def do_deploy(ctx, available, cached_images, region, tg_token, tg_chat):
    target = available[0]
    ts = datetime.now().strftime("%H:%M:%S")
    size_id = target["id"]
    image_id = cached_images.get("rocm") or cached_images.get("ubuntu-24") or IMAGE_FALLBACK
    log.info(f"Deploy: size={size_id} image={image_id} region={region}")

    page = await ctx.new_page()
    await page.add_init_script("""
        Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
        window.chrome={runtime:{},loadTimes:function(){},csi:function(){}};
    """)
    try:
        await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)
        raw = await page.evaluate("""
            async (p) => {
                try {
                    const r = await fetch('/graphql?i=0dd79f', {
                        method:'POST', credentials:'include',
                        headers:{'Content-Type':'application/json','apollographql-client-name':'ui-droplets','apollographql-client-version':'2026.6.17'},
                        body: JSON.stringify({operationName:'DropletCreate',
                            query:`mutation DropletCreate($req:DropletCreateRequest){createDroplet(DropletCreateRequest:$req){droplet{id name}}}`,
                            variables:{req:{name:'gpu-mi300x-auto',size:p.sizeId,image:p.imageId,region:p.regionId,ssh_keys:[p.sshKeyId],monitoring:true,with_droplet_agent:true}}})
                    });
                    return await r.text();
                } catch(e) { return JSON.stringify({error:e.message}); }
            }
        """, {"sizeId": size_id, "imageId": image_id, "regionId": str(region), "sshKeyId": SSH_KEY_ID})

        result = json.loads(raw)
        if "data" in result and result["data"].get("createDroplet", {}).get("droplet"):
            d = result["data"]["createDroplet"]["droplet"]
            msg = f"🚀🚀🚀 *GPU DEPLOYED!!!* 🚀🚀🚀\n\n✅ Name: `{d['name']}`\n✅ ID: `{d['id']}`\n🕐 {ts}\n\n🔗 https://devcloud.amd.com/droplets/{d['id']}"
            send_tg(tg_token, tg_chat, msg)
            log.info(f"✅ DEPLOYED! {d['name']} ({d['id']})")
            return True
        elif "errors" in result:
            err = result["errors"][0].get("message", "?")
            log.error(f"Deploy error: {err}")
            send_tg(tg_token, tg_chat, f"🚨 Deploy gagal: `{err}`\n⚡ https://devcloud.amd.com/gpus")
        else:
            log.error(f"Unexpected: {raw[:300]}")
            send_tg(tg_token, tg_chat, f"🚨 Deploy error!\n⚡ https://devcloud.amd.com/gpus")
    except Exception as e:
        log.error(f"Deploy exception: {e}")
        send_tg(tg_token, tg_chat, f"🚨 Deploy error: `{e}`\n⚡ https://devcloud.amd.com/gpus")
    finally:
        await page.close()
    return False

async def check_stock(page):
    """Single stock check on existing page. Returns (gpus, gql_data) or (None, None) if session expired."""
    gql_result = None

    async def on_gql(response):
        nonlocal gql_result
        if "graphql" not in response.url or response.request.method != "POST":
            return
        try:
            req = json.loads(response.request.post_data or "{}")
            body = await response.text()
            resp = json.loads(body)
            if "errors" not in resp and req.get("operationName") == "dropletOptions" and "gpu_info" in body:
                gql_result = resp
        except:
            pass

    page.on("response", on_gql)
    try:
        await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(10):
            await asyncio.sleep(2)
            if gql_result:
                break

        # Check if redirected to login (real login page, not Cloudflare)
        url = page.url
        if "/sign_in" in url or "/login" in url:
            return None, None, "session_expired"

        if not gql_result:
            return [], None, "no_data"

        gpus = []
        for s in gql_result.get("data", {}).get("dropletOptions", {}).get("sizes", []):
            if not s.get("gpu_info"):
                continue
            g = s["gpu_info"]
            gpus.append({
                "name": s["name"], "id": s["id"],
                "model": g["model"], "count": g["count"],
                "vram": int(g["vram"]["amount"]),
                "price": s["price_per_hour"],
                "regions": s.get("region_ids", []),
                "restriction": s.get("restriction"),
                "in_stock": len(s.get("region_ids", [])) > 0,
            })
        return gpus, gql_result, "ok"
    except Exception as e:
        log.warning(f"Check error: {e}")
        return None, None, "error"
    finally:
        page.remove_listener("response", on_gql)

async def run_monitor(interval, tg_token, tg_chat, auto_deploy, cookies_file):
    from playwright.async_api import async_playwright

    if not cookies_file and not Path(BROWSER_DATA_DIR).exists():
        log.error("No cookies file and no browser_data/. Need cookies.json")
        return

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            BROWSER_DATA_DIR, headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-gpu",
                  "--no-first-run", "--no-default-browser-check"],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )
        anti = "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});window.chrome={runtime:{},loadTimes:function(){},csi:function(){}};"

        # Import cookies at startup (same as --setup but inline)
        if cookies_file:
            cookies = load_cookies(cookies_file)
            await ctx.add_cookies(cookies)
            log.info(f"Imported {len(cookies)} cookies")

        # Initial visit to establish session (like --setup does)
        init_page = await ctx.new_page()
        await init_page.add_init_script(anti)
        gql_init = None
        async def on_init(response):
            nonlocal gql_init
            if "graphql" in response.url and response.request.method == "POST":
                try:
                    req = json.loads(response.request.post_data or "{}")
                    body = await response.text()
                    resp = json.loads(body)
                    if "errors" not in resp and req.get("operationName") == "dropletOptions" and "gpu_info" in body:
                        gql_init = resp
                except:
                    pass
        init_page.on("response", on_init)
        await init_page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(10):
            await asyncio.sleep(2)
            if gql_init:
                break

        if gql_init:
            n = sum(1 for s in gql_init.get("data",{}).get("dropletOptions",{}).get("sizes",[]) if s.get("gpu_info"))
            log.info(f"Session OK ({n} GPU types)")
        else:
            url = init_page.url
            if "/sign_in" in url or "/login" in url:
                log.error("Cookies already expired! Export FRESH cookies from browser.")
                if tg_token and tg_chat:
                    send_tg(tg_token, tg_chat, "❌ Cookies expired at startup! Export FRESH cookies.")
                await ctx.close()
                return
            log.warning("No GraphQL data at startup, continuing anyway...")

        await init_page.close()

        # Main monitoring loop — reuse single page
        check_page = await ctx.new_page()
        await check_page.add_init_script(anti)
        fail_count = 0
        cached_images = {}
        session_recovered_count = 0

        log.info(f"Monitor running (interval: {interval}s, auto-deploy: {auto_deploy})")
        if tg_token and tg_chat:
            send_tg(tg_token, tg_chat, f"✅ AMD GPU Monitor started!\nMode: {'⚡ Fast Deploy' if auto_deploy else '👁 Monitor'}\nInterval: {interval}s")

        while True:
            gpus, gql_data, status = await check_stock(check_page)

            # Handle session expiry
            if status == "session_expired":
                session_recovered_count += 1
                if session_recovered_count > 3:
                    log.error("Session expired 3x — need fresh cookies")
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat,
                            "⚠️ Session expired 3x!\n"
                            "Re-setup:\n1. Export FRESH cookies\n2. scp ke VPS\n"
                            "3. python3 monitor.py --setup cookies.json")
                    break

                log.warning(f"Session expired (attempt {session_recovered_count}), re-importing cookies...")
                await check_page.close()

                # Re-import cookies and create fresh page
                if cookies_file:
                    cookies = load_cookies(cookies_file)
                    await ctx.add_cookies(cookies)

                # Try visit login page to trigger remember_me
                recover_page = await ctx.new_page()
                await recover_page.add_init_script(anti)
                try:
                    await recover_page.goto("https://devcloud.amd.com/users/sign_in", wait_until="domcontentloaded", timeout=20000)
                    await asyncio.sleep(5)
                    url = recover_page.url
                    if "/sign_in" not in url and "/login" not in url:
                        log.info("Session recovered!")
                        session_recovered_count = 0
                except:
                    pass
                finally:
                    await recover_page.close()

                check_page = await ctx.new_page()
                await check_page.add_init_script(anti)
                time.sleep(interval)
                continue

            if status == "error" or gpus is None:
                fail_count += 1
                log.warning(f"Check error (fail #{fail_count})")
                if fail_count >= 5:
                    log.error("5x fail — stopping")
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat, "⚠️ 5x gagal! Cek logs.")
                    break
                time.sleep(interval)
                continue

            if gql_data:
                new_images = extract_images(gql_data)
                if new_images:
                    cached_images.update(new_images)

            if status == "no_data":
                fail_count += 1
                log.warning(f"No data (fail #{fail_count})")
                if fail_count >= 5:
                    break
                time.sleep(interval)
                continue

            fail_count = 0
            session_recovered_count = 0
            available = [g for g in gpus if g["in_stock"]]

            if not available:
                log.info("All GPU out of stock")
                time.sleep(interval)
                continue

            # STOCK FOUND
            target = available[0]
            region = target["regions"][0]
            ts = datetime.now().strftime("%H:%M:%S")
            log.info(f"🟢 STOCK at {ts}: {target['count']}x {target['model']} ({target['vram']}GB)")

            alert = (
                f"🚨🚨🚨 *STOK ADA!!!* 🚨🚨🚨\n\n"
                f"✅ *{target['count']}x {target['model'].upper()}* ({target['vram']}GB VRAM)\n"
                f"   ${target['price']}/hr | Region: {region}\n"
            )
            if target["restriction"]:
                alert += f"   ⚠️ {target['restriction']}\n"

            if not auto_deploy:
                alert += "\n⚡ Deploy manual: https://devcloud.amd.com/gpus"
                if tg_token and tg_chat:
                    send_tg(tg_token, tg_chat, alert)
                time.sleep(interval)
                continue

            alert += "\n⚡ Auto-deploying now..."
            if tg_token and tg_chat:
                send_tg(tg_token, tg_chat, alert)

            deployed = await do_deploy(ctx, available, cached_images, region, tg_token, tg_chat)
            if deployed:
                log.info("Deployed! Exiting.")
                break

            time.sleep(interval)

        await ctx.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("cookies", nargs="?", default=None, help="cookies.json path")
    parser.add_argument("--auto-deploy", "-a", action="store_true")
    args = parser.parse_args()

    load_env()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))

    if not tg_token or not tg_chat:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    # Auto-find cookies.json if not specified
    cookies_file = args.cookies
    if not cookies_file:
        default = Path(__file__).parent / "cookies.json"
        if default.exists():
            cookies_file = str(default)

    if not cookies_file:
        log.error("Usage: python telegram_alert.py cookies.json [--auto-deploy]")
        sys.exit(1)

    asyncio.run(run_monitor(interval, tg_token, tg_chat, args.auto_deploy, cookies_file))

if __name__ == "__main__":
    main()
