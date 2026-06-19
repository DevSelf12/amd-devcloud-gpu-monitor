#!/usr/bin/env python3
"""
AMD Dev Cloud — GPU Monitor + Instant Auto-Deploy (v5)
Persistent session + auto-recovery via remember_me cookie.

Usage:
  python3 telegram_alert.py                    # Monitor only
  python3 telegram_alert.py --auto-deploy      # Monitor + instant deploy

Setup:
  python3 monitor.py --setup cookies.json      # First time (import fresh cookies)
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
CREATE_URL = "https://devcloud.amd.com/droplets/new?i=0dd79f"
LOGIN_URL = "https://devcloud.amd.com/users/sign_in"
BROWSER_DATA_DIR = str(Path(__file__).parent / "browser_data")

# Deploy config
SSH_KEY_ID = 57192576  # "Termux"

ANTI_DETECT = """
    Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
"""

def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

def send_tg(token, chat_id, text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}, timeout=10)
        return r.status_code == 200
    except:
        return False

async def try_refresh_session(ctx):
    """
    Attempt to refresh session by visiting login page.
    _digitalocean_remember_me cookie should auto-login the user.
    Returns True if session was recovered.
    """
    log.info("Attempting session recovery via remember_me...")
    page = await ctx.new_page()
    await page.add_init_script(ANTI_DETECT)
    try:
        await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(5)

        current = page.url
        # If NOT on login page anymore, remember_me worked
        if "sign_in" not in current and "login" not in current:
            log.info(f"✅ Session recovered! Redirected to: {current}")
            await page.close()
            return True

        # Check if page has a dashboard link or user menu (logged in)
        try:
            content = await page.content()
            if "sign_out" in content or "dashboard" in content or "droplets" in content:
                log.info("✅ Session seems alive (page has logged-in elements)")
                await page.close()
                return True
        except:
            pass

        log.warning("❌ Still on login page. remember_me expired or invalid.")
        await page.close()
        return False
    except Exception as e:
        log.warning(f"Recovery attempt error: {e}")
        try:
            await page.close()
        except:
            pass
        return False

async def fetch_gpu_stock(page):
    """Navigate to GPU page and capture stock data."""
    gql_result = None

    async def on_gql(response):
        nonlocal gql_result
        if "graphql" not in response.url or response.request.method != "POST":
            return
        try:
            req = json.loads(response.request.post_data or "{}")
            if req.get("operationName") == "dropletOptions":
                body = await response.text()
                if "gpu_info" in body:
                    gql_result = json.loads(body)
        except:
            pass

    page.on("response", on_gql)
    try:
        await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
        for _ in range(10):
            await asyncio.sleep(2)
            if gql_result:
                break

        # Check login redirect
        if "sign_in" in page.url or "login" in page.url:
            return None, "login_redirect"

        gpus = []
        if gql_result:
            sizes = gql_result.get("data", {}).get("dropletOptions", {}).get("sizes", [])
            for s in sizes:
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
        return gpus, None
    except Exception as e:
        return None, str(e)
    finally:
        try:
            page.remove_listener("response", on_gql)
        except:
            pass

async def deploy_gpu(page, size_id, image_id, region_id):
    """Execute deploy via fetch on the current page context."""
    result = await page.evaluate("""
        async (params) => {
            try {
                const resp = await fetch('/graphql?i=0dd79f', {
                    method: 'POST',
                    credentials: 'include',
                    headers: {
                        'Content-Type': 'application/json',
                        'apollographql-client-name': 'ui-droplets',
                        'apollographql-client-version': '2026.6.17-b9202df6d113a55b927b777fadd2511bf5435d06',
                    },
                    body: JSON.stringify({
                        operationName: 'DropletCreate',
                        query: `mutation DropletCreate($dropletCreateRequest: DropletCreateRequest) {
                            createDroplet(DropletCreateRequest: $dropletCreateRequest) {
                                droplet { id name }
                            }
                        }`,
                        variables: {
                            dropletCreateRequest: {
                                name: 'gpu-mi300x-autodeploy',
                                size: params.sizeId,
                                image: params.imageId,
                                region: params.regionId,
                                ssh_keys: [params.sshKeyId],
                                monitoring: true,
                                with_droplet_agent: true,
                            }
                        }
                    })
                });
                return await resp.text();
            } catch(e) {
                return JSON.stringify({error: e.message});
            }
        }
    """, {
        "sizeId": size_id,
        "imageId": image_id,
        "regionId": str(region_id),
        "sshKeyId": SSH_KEY_ID,
    })
    return json.loads(result)

async def run_monitor(interval, tg_token, tg_chat, auto_deploy):
    """Main monitoring loop with session auto-recovery."""
    from playwright.async_api import async_playwright

    if not Path(BROWSER_DATA_DIR).exists():
        log.error(f"No session! Run: python3 monitor.py --setup cookies.json")
        if tg_token and tg_chat:
            send_tg(tg_token, tg_chat, "❌ No session!\nRun: python3 monitor.py --setup cookies.json")
        return

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            BROWSER_DATA_DIR,
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-gpu",
                "--no-first-run", "--no-default-browser-check",
            ],
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720},
        )

        # Pre-warm deploy cache
        deploy_cache = {"images": {}, "loaded": False}
        if auto_deploy:
            log.info("Pre-warming deploy cache...")
            warm_page = await ctx.new_page()
            await warm_page.add_init_script(ANTI_DETECT)
            warm_data = {}

            async def on_warm(response):
                if "graphql" not in response.url or response.request.method != "POST":
                    return
                try:
                    warm_data[response.url] = json.loads(await response.text())
                except:
                    pass

            warm_page.on("response", on_warm)
            try:
                await warm_page.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(6)
                for _, resp in warm_data.items():
                    data = resp.get("data", {}).get("dropletOptions", {})
                    for d in data.get("distributions", []):
                        for img in d.get("images", []):
                            if "rocm" in img["name"].lower() or "rocm" in d["name"].lower():
                                deploy_cache["images"]["rocm"] = img["id"]
                            elif "24.04" in img["name"] and "ubuntu" in d["name"].lower():
                                deploy_cache["images"]["ubuntu-24"] = img["id"]
                deploy_cache["loaded"] = True
                log.info(f"Cache ready: images={deploy_cache['images']}")
            except Exception as e:
                log.warning(f"Pre-warm failed: {e}")
            finally:
                await warm_page.close()

        # Monitoring page (reuse)
        check_page = await ctx.new_page()
        await check_page.add_init_script(ANTI_DETECT)
        fail_count = 0
        recovery_count = 0

        mode = "⚡ Fast Deploy" if auto_deploy else "👁 Monitor only"
        log.info(f"Monitor running ({mode}, interval: {interval}s)")
        if tg_token and tg_chat:
            send_tg(tg_token, tg_chat,
                f"✅ AMD GPU Monitor started!\n"
                f"Mode: {mode}\n"
                f"Interval: {interval}s\n"
                f"Auto-recovery: ✅ enabled")

        while True:
            # Fetch stock
            gpus, error = await fetch_gpu_stock(check_page)

            # Handle login redirect with auto-recovery
            if error == "login_redirect":
                recovery_count += 1
                log.warning(f"Session expired (recovery #{recovery_count})")

                recovered = await try_refresh_session(ctx)
                if recovered:
                    # Refresh the check page too
                    try:
                        await check_page.close()
                    except:
                        pass
                    check_page = await ctx.new_page()
                    await check_page.add_init_script(ANTI_DETECT)
                    # Retry immediately
                    gpus, error = await fetch_gpu_stock(check_page)
                    if error == "login_redirect":
                        log.error("Recovery succeeded but still redirected!")
                        error = "recovery_failed"
                    else:
                        recovery_count = 0  # Reset on successful recovery
                else:
                    log.error("Auto-recovery failed! Need fresh cookies.")
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat,
                            "⚠️ Session expired & auto-recovery failed!\n\n"
                            "Re-setup:\n"
                            "1. Export FRESH cookies dari browser\n"
                            "2. scp cookies.json ke VPS\n"
                            "3. python3 monitor.py --setup cookies.json")
                    break

            if error and error != "login_redirect":
                fail_count += 1
                log.warning(f"Check error: {error} (fail #{fail_count})")
                if fail_count >= 3:
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat, "⚠️ 3x gagal! Check logs.")
                    break
                time.sleep(interval)
                continue

            if not gpus:
                fail_count += 1
                log.warning(f"No data (fail #{fail_count})")
                if fail_count >= 3:
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat, "⚠️ 3x no data! Check logs.")
                    break
                time.sleep(interval)
                continue

            fail_count = 0
            available = [g for g in gpus if g["in_stock"]]

            if not available:
                log.info("All GPU out of stock")
                time.sleep(interval)
                continue

            # === STOCK FOUND! ===
            target = available[0]
            region = target["regions"][0]
            ts = datetime.now().strftime("%H:%M:%S")
            log.info(f"🟢 STOCK: {target['count']}x {target['model']} ({target['vram']}GB) region {region}")

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

            # === FAST DEPLOY ===
            alert += "\n⚡ Auto-deploying..."
            if tg_token and tg_chat:
                send_tg(tg_token, tg_chat, alert)

            size_id = target["id"]
            image_id = deploy_cache["images"].get("rocm") or deploy_cache["images"].get("ubuntu-24") or "195932981"

            log.info(f"Deploy: size={size_id} image={image_id} region={region}")

            try:
                result = await deploy_gpu(check_page, size_id, image_id, region)

                if "data" in result and result["data"].get("createDroplet", {}).get("droplet"):
                    droplet = result["data"]["createDroplet"]["droplet"]
                    msg = (
                        f"🚀🚀🚀 *GPU DEPLOYED!!!* 🚀🚀🚀\n\n"
                        f"✅ Name: `{droplet['name']}`\n"
                        f"✅ ID: `{droplet['id']}`\n"
                        f"🕐 {ts}\n\n"
                        f"🔗 https://devcloud.amd.com/droplets/{droplet['id']}"
                    )
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat, msg)
                    log.info(f"✅ DEPLOYED! {droplet['name']} ({droplet['id']})")
                    await ctx.close()
                    return
                elif "errors" in result:
                    err = result["errors"][0].get("message", "unknown")
                    log.error(f"Deploy error: {err}")
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat,
                            f"🚨 Deploy gagal: `{err}`\n⚡ Manual: https://devcloud.amd.com/gpus")
                else:
                    log.error(f"Unexpected: {json.dumps(result)[:300]}")
                    if tg_token and tg_chat:
                        send_tg(tg_token, tg_chat, "🚨 Deploy error!\n⚡ https://devcloud.amd.com/gpus")
            except Exception as e:
                log.error(f"Deploy exception: {e}")
                if tg_token and tg_chat:
                    send_tg(tg_token, tg_chat,
                        f"🚨 Deploy error: `{e}`\n⚡ https://devcloud.amd.com/gpus")

            time.sleep(interval)

        await ctx.close()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-deploy", "-a", action="store_true")
    args = parser.parse_args()

    load_env()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))

    if not tg_token or not tg_chat:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")
        sys.exit(1)

    if not Path(BROWSER_DATA_DIR).exists():
        log.error("No session! Run: python3 monitor.py --setup cookies.json")
        sys.exit(1)

    asyncio.run(run_monitor(interval, tg_token, tg_chat, args.auto_deploy))

if __name__ == "__main__":
    main()
