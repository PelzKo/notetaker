"""Detect URL-only messages and fetch their <title> for nicer task titles."""
import html
import re

import httpx

URL_RE = re.compile(r"https?://\S+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
}


def detect_url_only(text: str) -> str | None:
    """Return the URL if the message is just a single URL (with no surrounding text)."""
    s = text.strip().rstrip(".,;:!?)\"'")
    m = URL_RE.fullmatch(s)
    return m.group(0) if m else None


def fetch_title(url: str) -> str | None:
    try:
        with httpx.Client(headers=HEADERS, follow_redirects=True, timeout=5.0) as client:
            resp = client.get(url)
            resp.raise_for_status()
            m = TITLE_RE.search(resp.text)
            if not m:
                return None
            title = html.unescape(m.group(1)).strip()
            title = re.sub(r"\s+", " ", title)
            return title[:200] or None
    except Exception:
        return None
