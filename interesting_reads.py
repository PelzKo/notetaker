import re
import httpx
from datetime import date, datetime, timedelta


def _is_recent(d: date) -> bool:
    today = date.today()
    return d in (today, today - timedelta(days=1))


def check_devto_ai_weekly() -> str | None:
    """Return URL if alexmercedcoder posted an 'AI weekly' article today or yesterday."""
    try:
        resp = httpx.get(
            "https://dev.to/api/articles",
            params={"username": "alexmercedcoder", "per_page": 10},
            timeout=10,
            headers={"User-Agent": "notetaker-bot/1.0"},
        )
        resp.raise_for_status()
        for article in resp.json():
            if "ai weekly" not in article.get("title", "").lower():
                continue
            published = article.get("published_at", "")[:10]
            try:
                if _is_recent(date.fromisoformat(published)):
                    url = article.get("url") or f"https://dev.to{article.get('path', '')}"
                    return url
            except ValueError:
                pass
    except Exception:
        pass
    return None


def check_aiweekly() -> str | None:
    """Return issue URL if aiweekly.co posted a new issue today or yesterday."""
    try:
        resp = httpx.get(
            "https://aiweekly.co/issues",
            timeout=10,
            headers={"User-Agent": "notetaker-bot/1.0"},
        )
        resp.raise_for_status()
        html = resp.text

        issue_links = re.findall(r'href="(/issues/[^"#?]+)', html)
        if not issue_links:
            return None

        first_link = issue_links[0]
        pos = html.find(first_link)
        window = html[max(0, pos - 300): pos + 600]

        date_patterns = [
            (r'\d{4}-\d{2}-\d{2}', "%Y-%m-%d"),
            (r'[A-Za-z]+ \d{1,2},? \d{4}', "%B %d %Y"),
            (r'\d{1,2} [A-Za-z]+ \d{4}', "%d %B %Y"),
        ]

        for pattern, fmt in date_patterns:
            for match in re.findall(pattern, window):
                try:
                    d = datetime.strptime(match.replace(",", ""), fmt).date()
                    if _is_recent(d):
                        return f"https://aiweekly.co{first_link}"
                except ValueError:
                    pass
    except Exception:
        pass
    return None


def get_interesting_reads() -> list[str]:
    """Return URLs for AI reading material published today or yesterday."""
    links = []
    devto = check_devto_ai_weekly()
    if devto:
        links.append(devto)
    aiweekly = check_aiweekly()
    if aiweekly:
        links.append(aiweekly)
    return links
