#!/usr/bin/env python3
"""
AMD Dev Cloud GPU Stock Monitor
================================
Monitors GPU instance availability on devcloud.amd.com
Uses browser cookies for authentication.

Setup:
  1. Login to https://devcloud.amd.com in your browser
  2. Export cookies (or copy from DevTools → Application → Cookies)
  3. Save cookies to cookies.json (see format below)
  4. Run: python monitor.py

Cookie format (cookies.json):
[
  {"name": "cookie_name", "value": "cookie_value", "domain": ".amd.com"},
  ...
]

Or use browser extension "EditThisCookie" / "Cookie-Editor" to export.
"""

import json
import time
import sys
import os
import logging
import argparse
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Installing requests...")
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests

# ─── Config ───────────────────────────────────────────────────────────
DEFAULT_CHECK_INTERVAL = 60  # seconds
DEFAULT_COOKIE_FILE = "cookies.json"

# AMD Dev Cloud API endpoints (DigitalOcean/Paperspace backend)
# These are the known endpoints - update after capturing from DevTools
API_ENDPOINTS = {
    # Primary: Paperspace-style API for machine/instance availability
    "machines": "https://devcloud.amd.com/api/machines",
    "instances": "https://devcloud.amd.com/api/instances",
    "gpu_types": "https://devcloud.amd.com/api/gpu-types",
    "availability": "https://devcloud.amd.com/api/availability",
    "quotas": "https://devcloud.amd.com/api/quotas",

    # DigitalOcean federation endpoints (discovered from frontend JS)
    "federation_api": "https://devcloud.amd.com/v1/machines",
    "regions": "https://devcloud.amd.com/v1/regions",
}

# GPU types to monitor (common AMD Dev Cloud GPUs)
GPU_TARGETS = [
    "MI300X",
    "MI250X",
    "MI250",
    "MI210",
    "MI100",
    "V620",
    "W7900",
]

# ─── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("amd-monitor")


class AMDCookieAuth(requests.auth.AuthBase):
    """Load cookies from JSON file and attach to requests."""

    def __init__(self, cookie_file: str):
        self.cookies = {}
        self.cookie_file = cookie_file
        self._load_cookies()

    def _load_cookies(self):
        path = Path(self.cookie_file)
        if not path.exists():
            log.error(f"Cookie file not found: {self.cookie_file}")
            log.error("Export cookies from browser and save to cookies.json")
            sys.exit(1)

        with open(path) as f:
            data = json.load(f)

        # Support both formats:
        # 1. Array of {name, value, domain} objects (Cookie-Editor export)
        # 2. Simple {name: value} dict
        if isinstance(data, list):
            for c in data:
                if "name" in c and "value" in c:
                    self.cookies[c["name"]] = c["value"]
        elif isinstance(data, dict):
            self.cookies = data

        log.info(f"Loaded {len(self.cookies)} cookies from {self.cookie_file}")

    def __call__(self, r):
        cookie_str = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        r.headers["Cookie"] = cookie_str
        return r


class AMDDCloudMonitor:
    def __init__(self, cookie_file: str, interval: int = DEFAULT_CHECK_INTERVAL):
        self.auth = AMDCookieAuth(cookie_file)
        self.interval = interval
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://devcloud.amd.com/",
            "Origin": "https://devcloud.amd.com",
        })
        self.last_status = {}
        self.found_endpoints = False
        self.working_endpoint = None

    def _request(self, url: str) -> dict | None:
        """Make authenticated request. Returns JSON or None on error."""
        try:
            resp = self.session.get(url, auth=self.auth, timeout=15)
            if resp.status_code == 401 or resp.status_code == 403:
                log.warning(f"Auth failed ({resp.status_code}) for {url}")
                log.warning("Cookies may be expired. Re-export from browser.")
                return None
            if resp.status_code == 200:
                try:
                    return resp.json()
                except json.JSONDecodeError:
                    return {"_raw": resp.text[:500]}
            log.debug(f"HTTP {resp.status_code} for {url}")
            return None
        except requests.RequestException as e:
            log.error(f"Request failed: {e}")
            return None

    def discover_endpoints(self) -> str | None:
        """
        Try all known endpoints to find which one works.
        Returns the working endpoint URL or None.
        """
        log.info("Discovering working API endpoint...")

        # Try known endpoints
        for name, url in API_ENDPOINTS.items():
            log.info(f"  Trying {name}: {url}")
            data = self._request(url)
            if data and not data.get("_raw"):
                log.info(f"  ✅ {name} works!")
                self.working_endpoint = url
                return url

        # Try the main page and extract API calls from JS
        log.info("  Trying to find API from main page...")
        try:
            resp = self.session.get(
                "https://devcloud.amd.com/",
                auth=self.auth,
                timeout=15,
            )
            if resp.status_code == 200:
                # Look for API base URLs in the HTML/JS
                import re
                api_matches = re.findall(
                    r'(https?://[^"\']+/(?:api|v1|v2)/[^"\']+)',
                    resp.text,
                )
                for url in set(api_matches):
                    log.info(f"  Found in page: {url}")
                    data = self._request(url)
                    if data and not data.get("_raw"):
                        log.info(f"  ✅ Works: {url}")
                        self.working_endpoint = url
                        return url
        except Exception as e:
            log.debug(f"  Page scan failed: {e}")

        log.warning("❌ No working endpoint found automatically.")
        log.warning("Please capture the correct API endpoint from DevTools.")
        log.warning("Run with --capture to get instructions.")
        return None

    def check_availability(self) -> dict:
        """
        Check GPU availability. Returns dict of {gpu_type: available_count}.
        """
        if not self.working_endpoint:
            self.working_endpoint = self.discover_endpoints()
            if not self.working_endpoint:
                return {}

        data = self._request(self.working_endpoint)
        if not data:
            return {}

        # Parse response - adapt based on actual API response format
        result = {}

        # Try common response structures
        if isinstance(data, list):
            # Array of instances/machines
            for item in data:
                gpu = item.get("gpuType") or item.get("gpu_type") or item.get("type") or "unknown"
                status = item.get("status") or item.get("state") or ""
                if status.lower() in ("available", "ready", "idle", "running"):
                    result[gpu] = result.get(gpu, 0) + 1

        elif isinstance(data, dict):
            # Could be {gpus: [...]} or {availability: {...}} or {machines: [...]}
            for key in ["gpus", "machines", "instances", "availability", "data", "results"]:
                if key in data:
                    items = data[key]
                    if isinstance(items, list):
                        for item in items:
                            gpu = item.get("gpuType") or item.get("gpu_type") or item.get("type") or "unknown"
                            avail = item.get("available") or item.get("count") or item.get("stock")
                            if avail is not None:
                                result[gpu] = int(avail) if avail else 0
                            elif item.get("status", "").lower() in ("available", "ready", "idle"):
                                result[gpu] = result.get(gpu, 0) + 1
                    elif isinstance(items, dict):
                        for gpu, count in items.items():
                            result[gpu] = int(count) if count else 0
                    break

            # Fallback: check if top-level keys are GPU names
            if not result:
                for gpu in GPU_TARGETS:
                    if gpu.lower() in str(data).lower():
                        # Found reference to this GPU
                        pass

        return result

    def check_custom_endpoint(self, url: str) -> dict | None:
        """Check a custom endpoint and return raw response."""
        return self._request(url)

    def format_status(self, availability: dict) -> str:
        """Format availability status for display."""
        if not availability:
            return "❌ No availability data"

        lines = [f"🕐 {datetime.now().strftime('%H:%M:%S')} — AMD Dev Cloud GPU Status\n"]
        found_available = False

        for gpu, count in sorted(availability.items()):
            if count > 0:
                lines.append(f"✅ {gpu}: **{count} available**")
                found_available = True
            else:
                lines.append(f"❌ {gpu}: out of stock")

        if found_available:
            lines.insert(0, "🟢 **GPU AVAILABLE!**\n")
        else:
            lines.insert(0, "🔴 All GPUs out of stock\n")

        return "\n".join(lines)

    def run_once(self) -> tuple[dict, bool]:
        """
        Single check. Returns (availability, changed).
        changed=True if status differs from last check.
        """
        avail = self.check_availability()
        changed = avail != self.last_status

        if changed:
            log.info("Status changed!")
            for gpu in set(list(avail.keys()) + list(self.last_status.keys())):
                old = self.last_status.get(gpu, 0)
                new = avail.get(gpu, 0)
                if old != new:
                    log.info(f"  {gpu}: {old} → {new}")

        self.last_status = avail
        return avail, changed

    def run_loop(self, callback=None):
        """
        Continuous monitoring loop.
        callback(availability, status_text) is called on each check.
        """
        log.info(f"Starting monitor (interval: {self.interval}s)")
        log.info(f"Cookie file: {self.auth.cookie_file}")
        log.info("Press Ctrl+C to stop\n")

        # Initial endpoint discovery
        if not self.working_endpoint:
            self.working_endpoint = self.discover_endpoints()
            if not self.working_endpoint:
                log.error("Cannot find working endpoint. Exiting.")
                return

        consecutive_errors = 0

        while True:
            try:
                avail, changed = self.run_once()

                if avail:
                    consecutive_errors = 0
                    status_text = self.format_status(avail)

                    if callback:
                        callback(avail, status_text)
                    else:
                        print(status_text)
                        print("-" * 50)
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= 5:
                        log.warning("5 consecutive empty responses. Re-discovering endpoint...")
                        self.working_endpoint = None
                        consecutive_errors = 0

            except KeyboardInterrupt:
                log.info("Stopped by user.")
                break
            except Exception as e:
                log.error(f"Check error: {e}")
                consecutive_errors += 1

            time.sleep(self.interval)


def print_cookie_guide():
    """Print instructions for getting cookies."""
    print("""
╔══════════════════════════════════════════════════════════════╗
║        AMD Dev Cloud Cookie Export Guide                    ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  1. Open https://devcloud.amd.com in Chrome/Firefox         ║
║  2. Login with your AMD account                              ║
║  3. Open DevTools (F12)                                      ║
║  4. Go to Application → Cookies → devcloud.amd.com          ║
║  5. Or use extension "Cookie-Editor" to export all as JSON   ║
║                                                              ║
║  Save to cookies.json in this format:                        ║
║                                                              ║
║  [                                                           ║
║    {"name": "...", "value": "...", "domain": ".amd.com"},   ║
║    ...                                                       ║
║  ]                                                           ║
║                                                              ║
║  OR simple format:                                           ║
║  {"cookie_name": "cookie_value", ...}                        ║
║                                                              ║
╠══════════════════════════════════════════════════════════════╣
║  CAPTURING THE API ENDPOINT:                                ║
║                                                              ║
║  1. Open DevTools → Network tab                              ║
║  2. Filter by XHR/Fetch                                      ║
║  3. Navigate to the page showing GPU availability            ║
║  4. Find the request that returns stock/availability data    ║
║  5. Copy the request URL                                     ║
║  6. Edit API_ENDPOINTS in this script or use --endpoint flag ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
""")


def main():
    parser = argparse.ArgumentParser(description="AMD Dev Cloud GPU Stock Monitor")
    parser.add_argument("--cookies", "-c", default=DEFAULT_COOKIE_FILE,
                        help="Path to cookies JSON file")
    parser.add_argument("--interval", "-i", type=int, default=DEFAULT_CHECK_INTERVAL,
                        help="Check interval in seconds (default: 60)")
    parser.add_argument("--once", action="store_true",
                        help="Check once and exit")
    parser.add_argument("--endpoint", "-e",
                        help="Override API endpoint URL")
    parser.add_argument("--guide", action="store_true",
                        help="Show cookie export guide")
    parser.add_argument("--discover", action="store_true",
                        help="Try to discover API endpoints and exit")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.guide:
        print_cookie_guide()
        return

    monitor = AMDDCloudMonitor(args.cookies, args.interval)

    if args.endpoint:
        monitor.working_endpoint = args.endpoint
        log.info(f"Using custom endpoint: {args.endpoint}")

    if args.discover:
        endpoint = monitor.discover_endpoints()
        if endpoint:
            print(f"\n✅ Working endpoint: {endpoint}")
            # Try to fetch and display data
            data = monitor.check_custom_endpoint(endpoint)
            if data:
                print(f"\nResponse preview:")
                print(json.dumps(data, indent=2)[:2000])
        return

    if args.once:
        avail, _ = monitor.run_once()
        print(monitor.format_status(avail))
        return

    monitor.run_loop()


if __name__ == "__main__":
    main()
