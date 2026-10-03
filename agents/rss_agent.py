"""FreshRSS MCP server for QueryCraft."""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_env_override = os.getenv("QUERYCRAFT_ENV")
ENV_PATH = Path(_env_override).expanduser().resolve() if _env_override else PROJECT_ROOT / ".env"
load_dotenv(dotenv_path=ENV_PATH, override=False)

FRESHRSS_URL = os.getenv("FRESHRSS_URL", "").strip().rstrip("/")
FRESHRSS_API_URL = os.getenv("FRESHRSS_API_URL", "").strip().rstrip("/")
FRESHRSS_USER = os.getenv("FRESHRSS_USER", "").strip()
FRESHRSS_API_PASSWORD = (
    os.getenv("FRESHRSS_API_PASSWORD", "").strip()
    or os.getenv("FRESHRSS_API_KEY", "").strip()
)
FETCH_TIMEOUT = int(os.getenv("RSS_FETCH_TIMEOUT", "10"))
MAX_ITEMS = int(os.getenv("RSS_MAX_ITEMS", "20"))
SNIPPET_MAX_CHARS = int(os.getenv("RSS_SNIPPET_MAX_CHARS", "400"))

def _resolve_api_url() -> str:
    if FRESHRSS_API_URL:
        return FRESHRSS_API_URL
    if not FRESHRSS_URL:
        return ""
    if FRESHRSS_URL.endswith("/api/greader.php"):
        return FRESHRSS_URL
    return f"{FRESHRSS_URL}/api/greader.php"


API_URL = _resolve_api_url()


def _validate_config() -> None:
    missing = []
    if not API_URL:
        missing.append("FRESHRSS_URL or FRESHRSS_API_URL")
    if not FRESHRSS_USER:
        missing.append("FRESHRSS_USER")
    if not FRESHRSS_API_PASSWORD:
        missing.append("FRESHRSS_API_PASSWORD")
    if missing:
        raise RuntimeError("Missing FreshRSS setting(s): " + ", ".join(missing))
    if FRESHRSS_API_PASSWORD.startswith(("http://", "https://")):
        raise RuntimeError(
            "FRESHRSS_API_PASSWORD must be the special FreshRSS API password, not an API URL"
        )


class FreshRSSClient:
    def __init__(self):
        self._token: str | None = None
        self._token_expiry = 0.0
        self._session = requests.Session()

    def _get_token(self) -> str:
        _validate_config()
        response = self._session.post(
            f"{API_URL}/accounts/ClientLogin",
            data={"Email": FRESHRSS_USER, "Passwd": FRESHRSS_API_PASSWORD},
            timeout=FETCH_TIMEOUT,
            verify=True,
        )
        response.raise_for_status()
        for line in response.text.splitlines():
            if line.startswith("Auth="):
                return line[5:].strip()
        raise ValueError("No Auth token in FreshRSS response")

    def _auth_header(self) -> dict:
        now = time.time()
        if self._token is None or now > self._token_expiry:
            self._token = self._get_token()
            self._token_expiry = now + 3500
        return {"Authorization": f"GoogleLogin auth={self._token}"}

    def get_unread(self, n: int = MAX_ITEMS) -> list[dict]:
        response = self._session.get(
            f"{API_URL}/reader/api/0/stream/contents/reading-list",
            params={"n": n, "xt": "user/-/state/com.google/read"},
            headers=self._auth_header(),
            timeout=FETCH_TIMEOUT,
            verify=True,
        )
        response.raise_for_status()
        return [self._clean_item(item) for item in response.json().get("items", [])]

    def get_items_by_feed(self, feed_title: str, n: int = MAX_ITEMS) -> list[dict]:
        pattern = re.compile(re.escape(feed_title.strip()), re.IGNORECASE)
        items = self.get_unread(n=min(n * 5, 100))
        return [item for item in items if pattern.search(item.get("feed", ""))][:n]

    def _edit_token(self) -> str:
        response = self._session.get(
            f"{API_URL}/reader/api/0/token",
            headers=self._auth_header(),
            timeout=FETCH_TIMEOUT,
            verify=True,
        )
        response.raise_for_status()
        token = response.text.strip()
        if not token:
            raise ValueError("No edit token in FreshRSS response")
        return token

    def mark_read(self, item_id: str) -> bool:
        response = self._session.post(
            f"{API_URL}/reader/api/0/edit-tag",
            data={
                "i": item_id,
                "a": "user/-/state/com.google/read",
                "T": self._edit_token(),
            },
            headers=self._auth_header(),
            timeout=FETCH_TIMEOUT,
            verify=True,
        )
        return response.status_code == 200

    @staticmethod
    def _clean_item(raw: dict) -> dict:
        published = raw.get("published", 0)
        date_text = (
            datetime.fromtimestamp(published, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if published else "unknown"
        )
        summary_html = (raw.get("summary") or {}).get("content", "") or ""
        summary_text = re.sub(r"<[^>]+>", "", summary_html).strip()
        return {
            "id": raw.get("id", ""),
            "title": raw.get("title", "(no title)"),
            "feed": (raw.get("origin") or {}).get("title", ""),
            "url": (raw.get("alternate") or [{}])[0].get("href", ""),
            "date": date_text,
            "snippet": summary_text[:SNIPPET_MAX_CHARS],
        }


_client = FreshRSSClient()
mcp = FastMCP(
    "rss",
    instructions=(
        "Access the user's FreshRSS feed reader. Use get_unread_articles for new items, "
        "get_articles_from_feed to filter by feed, mark_article_read to mark an item read, "
        "and check_freshrss_connection to diagnose API configuration."
    ),
)




@mcp.tool()
def check_freshrss_connection() -> str:
    """Check FreshRSS API configuration and authentication without exposing credentials."""
    try:
        _validate_config()
        _client._get_token()
        return json.dumps({
            "ok": True,
            "api_url": API_URL,
            "user": FRESHRSS_USER,
            "env_file": str(ENV_PATH),
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({
            "ok": False,
            "api_url": API_URL or None,
            "user": FRESHRSS_USER or None,
            "env_file": str(ENV_PATH),
            "error": f"{type(exc).__name__}: {exc}",
        }, ensure_ascii=False)


@mcp.tool()
def get_unread_articles(max_items: int = 10) -> str:
    """Fetch unread FreshRSS articles."""
    max_items = max(1, min(int(max_items), MAX_ITEMS))
    try:
        items = _client.get_unread(n=max_items)
        return json.dumps({"count": len(items), "articles": items}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"FreshRSS error: {type(exc).__name__}: {exc}"})


@mcp.tool()
def get_articles_from_feed(feed_name: str, max_items: int = 10) -> str:
    """Fetch unread articles whose feed title contains feed_name."""
    if not feed_name or not feed_name.strip():
        return json.dumps({"error": "feed_name cannot be empty"})
    feed_name = feed_name.strip()[:100]
    max_items = max(1, min(int(max_items), MAX_ITEMS))
    try:
        items = _client.get_items_by_feed(feed_name, n=max_items)
        return json.dumps({
            "feed": feed_name,
            "count": len(items),
            "articles": items,
        }, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({"error": f"FreshRSS error: {type(exc).__name__}: {exc}"})


@mcp.tool()
def mark_article_read(article_id: str) -> str:
    """Mark one FreshRSS article as read."""
    if not article_id or not article_id.strip():
        return json.dumps({"error": "article_id cannot be empty"})
    article_id = article_id.strip()[:500]
    try:
        return json.dumps({
            "success": _client.mark_read(article_id),
            "article_id": article_id,
        })
    except Exception as exc:
        return json.dumps({"error": f"FreshRSS error: {type(exc).__name__}"})


if __name__ == "__main__":
    import logging
    import sys

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    mcp.run(transport="stdio")
