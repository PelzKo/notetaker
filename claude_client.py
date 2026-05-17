import json
import re
import httpx
from datetime import date, datetime
import config

_RECURRENCE_RE = re.compile(
    r"^(daily|weekday|weekly:(mon|tue|wed|thu|fri|sat|sun)|monthly:(?:[1-9]|[12][0-9]|3[01]))$"
)

API_URL = "https://api.anthropic.com/v1/messages"
HEADERS = {
    "x-api-key": config.ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "anthropic-beta": "prompt-caching-2024-07-31",
    "content-type": "application/json",
}

PARSE_SYSTEM_STATIC = """You extract structured data from todo text.

Categories:
- Work: HIPPIE, CoBiNet, TUM, ExBio, sysadmin, pipeline, bioinformatics
- MCM: Musicalcompany München, musical, theater, Dreamland, rehearsal
- YFU: YFU, youth for understanding, exchange student, volunteer
- Home: apartment, shopping, errands, household, repairs
- Personal: health, finance, sport, doctor, investing
- Other: anything else that is clearly one category but not above
- Unknown: truly ambiguous

Return ONLY valid JSON, no markdown, no explanation. Only return the string and do not surround it with quotes or the description "json":
{"title": "...", "category": "...", "due_date": "YYYY-MM-DD or null", "is_priority": true|false, "recurrence": "<pattern or null>", "remind_at": "YYYY-MM-DD HH:MM or null"}

Title should be imperative and concise (max 80 chars).
For due dates: interpret relative dates using the "Today is" line from the system context as the reference date.
If no date is mentioned, return null.
Set is_priority to true when the text contains explicit urgency signals such as "urgent",
"important", "asap", "high priority", "wichtig", "dringend", "eilig", "sofort", "!". Default false.

Recurrence (default null) — set when the text describes a repeating task. Allowed patterns:
- "daily" — every day
- "weekday" — every Mon-Fri
- "weekly:<mon|tue|wed|thu|fri|sat|sun>" — every given weekday (English 3-letter)
- "monthly:<1-31>" — every month on the given day-of-month
Examples: "every Monday" → weekly:mon; "jeden Dienstag" → weekly:tue; "monthly on the 15th" → monthly:15;
"every weekday" / "Mo-Fr" → weekday; "täglich" / "every day" → daily.
If the text says "every day at 7am", set recurrence="daily" AND remind_at to today's 07:00 (or tomorrow's
07:00 if it's already past 07:00 today).

remind_at (default null) — set when the text contains a time-of-day reminder ("at 3pm", "in 2 hours",
"um 14 Uhr", "tomorrow at 9", "heute Abend 20:00"). Combine with the resolved due_date when one was given,
otherwise use today's date if the time is later than now, else tomorrow's. Use 24-hour HH:MM format.
If only a date is mentioned (no time), leave remind_at null."""

SUMMARY_SYSTEM = """You are a helpful personal assistant giving a brief, direct priority recommendation.
You will receive a list of tasks grouped as overdue, due soon, and long-pending.
Write 2-4 sentences max. Be specific about task names. No fluff.
Use plain text only — no markdown, no asterisks, no bold, no bullet points"""


def parse_task(raw_text: str) -> dict:
    """Call Claude to extract title, category, due_date from free text."""
    today = date.today().isoformat()
    try:
        resp = httpx.post(
            API_URL,
            headers=HEADERS,
            json={
                "model": "claude-haiku-4-5",
                "max_tokens": 200,
                "system": [
                    {
                        "type": "text",
                        "text": PARSE_SYSTEM_STATIC,
                        "cache_control": {"type": "ephemeral"},
                    },
                    {"type": "text", "text": f"Today is {today}."},
                ],
                "messages": [{"role": "user", "content": raw_text}],
            },
            timeout=15,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        if "\n" in text:
            text = text.split("\n")[1]
        parsed = json.loads(text)
        # Validate category
        if parsed.get("category") not in config.CATEGORIES:
            parsed["category"] = "Unknown"
        # Validate date
        due = parsed.get("due_date")
        if due:
            try:
                date.fromisoformat(due)
            except ValueError:
                parsed["due_date"] = None
        # Coerce priority to a strict bool
        parsed["is_priority"] = bool(parsed.get("is_priority", False))

        # Validate recurrence
        rec = parsed.get("recurrence")
        if rec and isinstance(rec, str) and _RECURRENCE_RE.match(rec.strip().lower()):
            parsed["recurrence"] = rec.strip().lower()
        else:
            parsed["recurrence"] = None

        # Validate remind_at
        ra = parsed.get("remind_at")
        if ra and isinstance(ra, str):
            try:
                datetime.strptime(ra.strip(), "%Y-%m-%d %H:%M")
                parsed["remind_at"] = ra.strip()
            except ValueError:
                parsed["remind_at"] = None
        else:
            parsed["remind_at"] = None

        return parsed
    except Exception as e:
        # Graceful fallback — save with raw text as title
        return {
            "title": raw_text[:80],
            "category": "Unknown",
            "due_date": None,
            "is_priority": False,
            "recurrence": None,
            "remind_at": None,
            "error": str(e),
        }


def generate_summary_comment(tasks: dict, cal_events: dict = None) -> str:
    """Call Claude to generate a short priority recommendation for the daily summary."""
    all_tasks = tasks["overdue"] + tasks["upcoming"] + tasks["old_noduedate"]
    if not all_tasks:
        return ""

    lines = []
    if tasks["overdue"]:
        lines.append("OVERDUE:")
        for t in tasks["overdue"]:
            lines.append(f"  - {t['title']} ({t['category']}, was due {t['due_date']})")
    if tasks["upcoming"]:
        lines.append("DUE SOON:")
        for t in tasks["upcoming"]:
            lines.append(f"  - {t['title']} ({t['category']}, due {t['due_date']})")
    if tasks["old_noduedate"]:
        lines.append("LONG PENDING (no due date, 30+ days old):")
        for t in tasks["old_noduedate"]:
            lines.append(f"  - {t['title']} ({t['category']}, added {t['created_at'].date()})")

    if cal_events and not cal_events.get("error"):
        lines.append("\nCALENDAR TODAY:")
        for e in cal_events.get("today", []):
            time_str = "all day" if e["all_day"] else f"{e['start']}–{e['end']}"
            lines.append(f"  - {e['title']} ({time_str})")
        lines.append("CALENDAR TOMORROW:")
        for e in cal_events.get("tomorrow", []):
            time_str = "all day" if e["all_day"] else f"{e['start']}–{e['end']}"
            lines.append(f"  - {e['title']} ({time_str})")

    try:
        resp = httpx.post(
            API_URL,
            headers=HEADERS,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 300,
                "system": [
                    {
                        "type": "text",
                        "text": SUMMARY_SYSTEM,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": "\n".join(lines)}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json()["content"][0]["text"].strip()
    except Exception as e:
        return f"(AI summary unavailable: {e})"
