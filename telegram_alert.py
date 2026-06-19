#!/usr/bin/env python3
"""
AMD Dev Cloud — GPU Monitor + Auto-Deploy (v3 persistent context)
Monitors MI300X stock and auto-deploys when available.
Uses Playwright persistent browser_data/ (setup via: python3 monitor.py --setup cookies.json)

Usage:
  python telegram_alert.py                    # Monitor only (alerts on stock change)
  python telegram_alert.py --auto-deploy      # Monitor + auto-deploy
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
GRAPHQL_URL = "https://devcloud.amd.com/graphql?i=0dd79f"
BROWSER_DATA_DIR = str(Path(__file__).parent / "browser_data")

# Config
SSH_KEY_ID = 57192576       # "Termux"
GPU_SIZE_ID = "325"         # gpu-mi300x1-192gb-devcloud (1x MI300X)
TEAM_ID = "0dd79f"

def load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

async def check_and_deploy(auto_deploy=False):
    """Check GPU stock using persistent browser context. Auto-deploy if enabled."""
    from playwright.async_api import async_playwright

    if not Path(BROWSER_DATA_DIR).exists():
        log.error(f"No browser session! Run first: python3 monitor.py --setup cookies.json")
        return None, None

    result = {"gpus": None, "images": [], "deployed": None}

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

        # Capture dropletOptions for stock check
        async def on_resp(response):
            if "graphql" not in response.url or response.request.method != "POST":
                return
            try:
                req = json.loads(response.request.post_data or "{}")
                op = req.get("operationName", "")
                body = await response.text()
                resp = json.loads(body)
                if "errors" in resp:
                    return
                if op == "dropletOptions" and "gpu_info" in body:
                    result["gpus"] = resp
            except:
                pass

        page.on("response", on_resp)

        try:
            await page.goto(GPU_URL, wait_until="domcontentloaded", timeout=30000)
            for _ in range(15):
                await asyncio.sleep(2)
                if result["gpus"]:
                    break
        except:
            pass

        # Check if redirected to login (session expired)
        try:
            current_url = page.url
            if "sign_in" in current_url or "login" in current_url:
                log.error("Session expired! Redirected to login page.")
                log.error("Re-setup: python3 monitor.py --setup cookies.json")
                await browser.close()
                return None, None
        except:
            pass

        # Parse GPU stock
        gpus = []
        if result["gpus"]:
            sizes = result["gpus"].get("data", {}).get("dropletOptions", {}).get("sizes", [])
            for s in sizes:
                if not s.get("gpu_info"):
                    continue
                g = s["gpu_info"]
                gpus.append({
                    "name": s["name"],
                    "id": s["id"],
                    "model": g["model"],
                    "count": g["count"],
                    "vram": int(g["vram"]["amount"]),
                    "price": s["price_per_hour"],
                    "regions": s.get("region_ids", []),
                    "restriction": s.get("restriction"),
                    "in_stock": len(s.get("region_ids", [])) > 0,
                })

        # Auto-deploy if stock available
        if auto_deploy and any(g["in_stock"] for g in gpus):
            in_stock = [g for g in gpus if g["in_stock"]]
            target = in_stock[0]  # Deploy first available
            region_id = target["regions"][0]
            log.info(f"🟢 STOCK FOUND! Deploying {target['name']} in region {region_id}...")

            # Navigate to create page to get full options
            all_data = {}
            async def on_resp2(response):
                if "graphql" not in response.url or response.request.method != "POST":
                    return
                try:
                    req = json.loads(response.request.post_data or "{}")
                    op = req.get("operationName", "")
                    resp = json.loads(await response.text())
                    if "errors" not in resp:
                        all_data[op] = resp
                except:
                    pass

            page2 = await browser.new_page()
            page2.on("response", on_resp2)
            await page2.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

            try:
                await page2.goto(CREATE_URL, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(10)

                # Find ROCm image
                image_id = None
                if "dropletOptions" in all_data:
                    distros = all_data["dropletOptions"]["data"]["dropletOptions"].get("distributions", [])
                    for d in distros:
                        for img in d.get("images", []):
                            if "rocm" in img["name"].lower() or "rocm" in d["name"].lower():
                                image_id = img["id"]
                                break
                        if image_id:
                            break
                    # Fallback to Ubuntu 24.04
                    if not image_id:
                        for d in distros:
                            for img in d.get("images", []):
                                if "24.04" in img["name"]:
                                    image_id = img["id"]
                                    break
                            if image_id:
                                break

                if not image_id:
                    image_id = "195932981"  # Ubuntu 24.04 LTS fallback

                log.info(f"Image ID: {image_id}, Region: {region_id}, Size: {target['id']}")

                # Fire the create mutation via page.evaluate
                create_result = await page2.evaluate("""
                    async (params) => {
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
                    }
                """, {
                    "sizeId": target["id"],
                    "imageId": image_id,
                    "regionId": str(region_id),
                    "sshKeyId": SSH_KEY_ID,
                })

                result["deployed"] = create_result
                log.info(f"Deploy result: {create_result[:500]}")

            except Exception as e:
                log.error(f"Deploy failed: {e}")
                result["deployed"] = json.dumps({"error": str(e)})
            finally:
                await page2.close()

        await browser.close()

    return gpus, result.get("deployed")

def format_alert(gpus, deployed=None):
    now = datetime.now().strftime("%H:%M:%S")
    available = [g for g in gpus if g["in_stock"]]

    if deployed:
        try:
            d = json.loads(deployed)
            if "data" in d and d["data"].get("createDroplet", {}).get("droplet"):
                droplet = d["data"]["createDroplet"]["droplet"]
                return (
                    f"🚀🚀🚀 *GPU DEPLOYED!!!* 🚀🚀🚀\n\n"
                    f"✅ Name: `{droplet['name']}`\n"
                    f"✅ ID: `{droplet['id']}`\n"
                    f"🕐 {now}\n\n"
                    f"🔗 Cek: https://devcloud.amd.com/droplets/{droplet['id']}"
                )
            elif "errors" in d:
                err = d["errors"][0].get("message", "unknown")
                return f"🚨 *STOK ADA tapi deploy gagal!*\n\nError: {err}\n\n⚡ Deploy manual: https://devcloud.amd.com/gpus"
        except:
            pass

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto-deploy", "-a", action="store_true", help="Auto-deploy GPU when stock available")
    args = parser.parse_args()

    load_env()
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    interval = int(os.environ.get("CHECK_INTERVAL", "60"))

    if not tg_token or not tg_chat:
        log.error("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID")
        sys.exit(1)

    if not Path(BROWSER_DATA_DIR).exists():
        log.error(f"No browser session! Run first: python3 monitor.py --setup cookies.json")
        sys.exit(1)

    mode = "Monitor + Auto-Deploy" if args.auto_deploy else "Monitor only"
    log.info(f"Starting ({mode}, interval: {interval}s)")
    send_tg(tg_token, tg_chat, f"✅ AMD GPU Monitor started!\nMode: {mode}\nInterval: {interval}s")

    fail_count = 0
    while True:
        try:
            gpus, deployed = asyncio.run(check_and_deploy(args.auto_deploy))
        except Exception as e:
            log.error(f"Error: {e}")
            gpus, deployed = None, None

        if gpus:
            fail_count = 0
            has_stock = any(g["in_stock"] for g in gpus)
            if has_stock:
                alert = format_alert(gpus, deployed)
                send_tg(tg_token, tg_chat, alert)
                log.info("Alert sent (stock available)")
            else:
                log.info("All GPU out of stock — skip notif")
            if deployed:
                log.info("Deploy completed — exiting after auto-deploy!")
                break
        else:
            fail_count += 1
            log.warning("Check failed — session expired?")
            if fail_count >= 3:
                msg = ("⚠️ AMD Monitor: 3x gagal! Session expired.\n\n"
                       "Re-setup:\n"
                       "1. Export FRESH cookies dari browser\n"
                       "2. scp cookies.json ke VPS\n"
                       "3. python3 monitor.py --setup cookies.json\n"
                       "4. Restart: python3 telegram_alert.py")
                send_tg(tg_token, tg_chat, msg)
                log.error("3x fail — sent alert, stopping.")
                break

        time.sleep(interval)

if __name__ == "__main__":
    main()
