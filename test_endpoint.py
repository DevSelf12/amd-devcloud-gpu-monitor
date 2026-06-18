#!/usr/bin/env python3
"""
AMD Dev Cloud — Endpoint Capture Helper
=========================================
Run this AFTER manually finding the API endpoint in DevTools.
It will test the endpoint, validate the response, and save the config.
"""

import json
import sys
import os

try:
    import requests
except ImportError:
    os.system(f"{sys.executable} -m pip install requests -q")
    import requests


def test_endpoint(url: str, cookie_file: str = "cookies.json"):
    """Test an endpoint with cookies and display the response."""

    # Load cookies
    with open(cookie_file) as f:
        data = json.load(f)

    if isinstance(data, list):
        cookies = {c["name"]: c["value"] for c in data if "name" in c}
    else:
        cookies = data

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
        "Referer": "https://devcloud.amd.com/",
    })

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    print(f"\n🔍 Testing: {url}")
    print(f"   Cookies: {len(cookies)} loaded\n")

    try:
        resp = session.get(url, headers={"Cookie": cookie_str}, timeout=15)
        print(f"   Status: {resp.status_code}")
        print(f"   Content-Type: {resp.headers.get('content-type', 'N/A')}")

        if resp.status_code == 200:
            try:
                json_data = resp.json()
                print(f"\n✅ Valid JSON response!")
                print(f"   Keys: {list(json_data.keys()) if isinstance(json_data, dict) else f'Array[{len(json_data)}]'}")
                print(f"\n📋 Response preview:\n")
                print(json.dumps(json_data, indent=2)[:3000])

                # Save working config
                config = {
                    "endpoint": url,
                    "cookie_file": cookie_file,
                    "response_keys": list(json_data.keys()) if isinstance(json_data, dict) else ["array"],
                }
                config_file = "config.json"
                with open(config_file, "w") as f:
                    json.dump(config, f, indent=2)
                print(f"\n💾 Config saved to {config_file}")

                return json_data
            except json.JSONDecodeError:
                print(f"\n⚠️ Response is not JSON:")
                print(resp.text[:500])
        elif resp.status_code in (401, 403):
            print(f"\n❌ Auth failed — cookies may be expired")
        else:
            print(f"\n⚠️ Unexpected status: {resp.status_code}")
            print(resp.text[:500])

    except Exception as e:
        print(f"\n❌ Error: {e}")

    return None


def main():
    if len(sys.argv) < 2:
        print("""
AMD Dev Cloud Endpoint Tester
==============================
Usage: python test_endpoint.py <URL> [cookie_file]

Example:
  python test_endpoint.py "https://devcloud.amd.com/api/machines"
  python test_endpoint.py "https://devcloud.amd.com/v1/machines" cookies.json

Steps:
  1. Open devcloud.amd.com → DevTools → Network → XHR
  2. Find the request that returns GPU/instance data
  3. Copy the URL and pass it to this script
  4. It will test the endpoint and save config.json
""")
        return

    url = sys.argv[1]
    cookie_file = sys.argv[2] if len(sys.argv) > 2 else "cookies.json"
    test_endpoint(url, cookie_file)


if __name__ == "__main__":
    main()
