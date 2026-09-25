"""Pure input-parsing and text helpers shared by the bot and cron scripts."""
import calendar
import re
from datetime import date, datetime, timedelta

import config

# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

DATE_FORMATS_HELP = (
    "Accepted date formats:\n"
    "• 3d or +3d — in 3 days\n"
    "• DD.MM.YYYY — e.g. 15.10.2026\n"
    "• DD.MM. — this year, e.g. 15.10.\n"
    "• YYYY-MM-DD — e.g. 2026-10-15\n"
    "• today · tomorrow\n"
    "• next month — same day next month"
)

CLEAR_WORDS = ("none", "clear", "null", "off", "-")

_DAYS_RE = re.compile(r"^\+?(\d{1,4})d$")
_DMY_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_DM_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.$")


def add_months(d: date, n: int) -> date:
    """Same day n months later, clamped to the last day of the target month."""
    total = d.month - 1 + n
    year, month = d.year + total // 12, total % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def parse_days(s: str) -> int | None:
    """'3d' / '+3d' → 3. Bare numbers are rejected."""
    m = _DAYS_RE.match(s.strip().lower())
    return int(m.group(1)) if m else None


def parse_date(s: str, *, today: date | None = None) -> date | None:
    """Parse any supported date spec (see DATE_FORMATS_HELP). None if invalid."""
    s = " ".join(s.strip().lower().split())
    today = today or date.today()
    if not s:
        return None
    days = parse_days(s)
    if days is not None:
        return today + timedelta(days=days)
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s == "next month":
        return add_months(today, 1)
    try:
        m = _DMY_RE.match(s)
        if m:
            return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        m = _DM_RE.match(s)
        if m:
            return date(today.year, int(m.group(2)), int(m.group(1)))
        if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
            return date.fromisoformat(s)
    except ValueError:
        return None
    return None


# ---------------------------------------------------------------------------
# Date + time (reminders)
# ---------------------------------------------------------------------------

DATETIME_FORMATS_HELP = (
    "Accepted reminder formats:\n"
    "• 2h · 30m — from now\n"
    "• HH:MM — today (tomorrow if already past)\n"
    "• <date> HH:MM — e.g. tomorrow 9:00, 15.10. 14:30, 2026-10-15 09:00\n"
    "• <date> alone — 08:00 on that day\n\n"
    + DATE_FORMATS_HELP
)

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_REL_RE = re.compile(r"^(\d{1,4})(h|m)$")


def _parse_time(s: str) -> tuple[int, int] | None:
    m = _TIME_RE.match(s)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse_datetime(s: str, *, now: datetime | None = None) -> datetime | None:
    s = " ".join(s.strip().lower().split())
    now = now or datetime.now()
    if not s:
        return None
    m = _REL_RE.match(s)
    if m:
        n = int(m.group(1))
        return now + (timedelta(hours=n) if m.group(2) == "h" else timedelta(minutes=n))
    t = _parse_time(s)
    if t:
        dt = now.replace(hour=t[0], minute=t[1], second=0, microsecond=0)
        return dt if dt > now else dt + timedelta(days=1)
    date_part, _, time_part = s.rpartition(" ")
    t = _parse_time(time_part) if date_part else None
    if t:
        d = parse_date(date_part, today=now.date())
        return datetime.combine(d, datetime.min.time().replace(hour=t[0], minute=t[1])) if d else None
    d = parse_date(s, today=now.date())
    if d:
        return datetime.combine(d, datetime.min.time().replace(hour=8))
    return None


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------

def match_category(s: str) -> str | None:
    """Case-insensitive exact match, else unique prefix match. None if no/ambiguous match."""
    s = s.strip().lower()
    if not s:
        return None
    for c in config.CATEGORIES:
        if c.lower() == s:
            return c
    hits = [c for c in config.CATEGORIES if c.lower().startswith(s)]
    return hits[0] if len(hits) == 1 else None


# ---------------------------------------------------------------------------
# Recurrence
# ---------------------------------------------------------------------------

RECURRENCE_RE = re.compile(
    r"^(daily|weekday|weekly:(mon|tue|wed|thu|fri|sat|sun)"
    r"|monthly:(?:[1-9]|[12][0-9]|3[01])|every:[1-9][0-9]?[dwm])$"
)

RECURRENCE_HELP = (
    "Patterns:\n"
    "• daily · weekday\n"
    "• weekly:mon — every Monday\n"
    "• monthly:15 — 15th of every month\n"
    "• every:2w — every 2 weeks (also: 2w, every 2 weeks)\n"
    "• every:3m — every 3 months (also: 3m, every 3 months)\n"
    "• every:10d — every 10 days (also: 10d, every 10 days)\n"
    "every:… counts from the task's last due date."
)

_EVERY_RE = re.compile(r"^(?:every[:\s]*)?(\d{1,2})\s*(d|days?|w|weeks?|m|months?)$")


def normalize_recurrence(s: str) -> str | None:
    """Map user input to a canonical recurrence pattern, or None if invalid."""
    s = " ".join(s.strip().lower().split())
    m = _EVERY_RE.match(s)
    if m:
        s = f"every:{int(m.group(1))}{m.group(2)[0]}"
    return s if RECURRENCE_RE.match(s) else None


# ---------------------------------------------------------------------------
# Long message splitting (Telegram limit is 4096 chars)
# ---------------------------------------------------------------------------

CONTINUED = "\n\n… (continued ⬇️)"
_SENTENCE_END_RE = re.compile(r"[.!?](?=\s)")


def _cut_point(text: str, limit: int) -> int:
    """Best index ≤ limit to cut at: blank line > line break > sentence end > space.
    A blank line only wins if it keeps the chunk at least half full."""
    window = text[:limit + 1]
    i = window.rfind("\n\n")
    if i > limit // 2:
        return i
    i = window.rfind("\n")
    if i > 0:
        return i
    ends = [m.end() for m in _SENTENCE_END_RE.finditer(window)]
    if ends:
        return ends[-1]
    i = window.rfind(" ")
    return i if i > 0 else limit


def split_message(text: str, limit: int = 3500) -> list[str]:
    """Split text into chunks ≤ limit chars at natural boundaries.
    Every chunk except the last ends with a 'continued' marker."""
    if len(text) <= limit:
        return [text]
    budget = limit - len(CONTINUED)
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        i = _cut_point(rest, budget)
        chunks.append(rest[:i].rstrip() + CONTINUED)
        rest = rest[i:].lstrip("\n ")
    if rest:
        chunks.append(rest)
    return chunks
