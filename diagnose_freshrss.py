"""Standalone FreshRSS API diagnostic. Does not start MCP or the LLM."""

import os
from pathlib import Path

import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
ENV_PATH = Path(os.getenv("QUERYCRAFT_ENV", ROOT / ".env")).expanduser().resolve()
load_dotenv(ENV_PATH, override=False)

base_url = os.getenv("FRESHRSS_URL", "").strip().rstrip("/")
api_url = os.getenv("FRESHRSS_API_URL", "").strip().rstrip("/")
user = os.getenv("FRESHRSS_USER", "").strip()
password = (
    os.getenv("FRESHRSS_API_PASSWORD", "").strip()
    or os.getenv("FRESHRSS_API_KEY", "").strip()
)

if not api_url:
    api_url = base_url if base_url.endswith("/api/greader.php") else f"{base_url}/api/greader.php"

print(f".env: {ENV_PATH} ({'found' if ENV_PATH.exists() else 'missing'})")
print(f"FreshRSS API: {api_url or '(missing)'}")
print(f"FreshRSS user: {user or '(missing)'}")

if not api_url or not user or not password:
    raise SystemExit("Missing FreshRSS URL, user, or API password.")
if password.startswith(("http://", "https://")):
    raise SystemExit("FRESHRSS_API_PASSWORD is an API URL. Use the special FreshRSS API password instead.")

login = requests.post(
    f"{api_url}/accounts/ClientLogin",
    data={"Email": user, "Passwd": password},
    timeout=10,
)
print(f"ClientLogin HTTP: {login.status_code}")
login.raise_for_status()

auth = None
for line in login.text.splitlines():
    if line.startswith("Auth="):
        auth = line[5:].strip()
        break
if not auth:
    raise SystemExit("FreshRSS returned no Auth token. Check API enablement and the special API password.")

unread = requests.get(
    f"{api_url}/reader/api/0/stream/contents/reading-list",
    params={"n": 3, "xt": "user/-/state/com.google/read"},
    headers={"Authorization": f"GoogleLogin auth={auth}"},
    timeout=10,
)
print(f"Unread request HTTP: {unread.status_code}")
unread.raise_for_status()
items = unread.json().get("items", [])
print(f"Unread sample count: {len(items)}")
for item in items:
    print(f"- {item.get('title', '(no title)')}")
