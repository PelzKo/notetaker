"""
20:00 evening check-in. Sends:
  - Tasks closed today
  - Open tasks still due today (with action buttons)
  - Tomorrow's calendar preview
  - A capture prompt

Cron entry:
  0 20 * * * /usr/bin/python3.11 /home/YOU/todobot/evening.py >> /var/log/todobot_evening.log 2>&1
"""
from datetime import date, datetime

import httpx

import config
import db
import google_calendar
from formatting import CATEGORY_EMOJI, fmt_date


def _send(text: str, reply_markup: dict | None = None) -> None:
    payload: dict = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def _today_done() -> list[dict]:
    today = date.today()
    rows = db.get_done_tasks(days=2)  # generous window, then filter
    out = []
    for t in rows:
        d = t.get("done_at")
        if isinstance(d, datetime):
            d = d.date()
        if d == today:
            out.append(t)
    return out


def _format_done(rows: list[dict]) -> str:
    lines = [f"✅ Today you closed {len(rows)}:"]
    for t in rows:
        emoji = CATEGORY_EMOJI.get(t["category"], "📌")
        star = "⭐ " if t.get("is_priority") else ""
        lines.append(f"  • {star}{emoji} {t['title']}")
    return "\n".join(lines)


def _open_today_keyboard(task_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Done", "callback_data": f"act:{task_id}:done"},
                {"text": "📅 Tomorrow", "callback_data": f"act:{task_id}:tomorrow"},
                {"text": "+1d", "callback_data": f"act:{task_id}:defer1"},
            ]
        ]
    }


def _format_open_today(task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    star = "⭐ " if task.get("is_priority") else ""
    return f"⏳ Still open today (#{task['id']}): {star}{emoji} {task['title']}"


def _format_calendar_preview(events: dict) -> str:
    tomorrow = events.get("tomorrow", [])
    if not tomorrow:
        return "📅 Tomorrow: nothing scheduled."
    lines = ["📅 Tomorrow:"]
    for e in tomorrow:
        if e["all_day"]:
            lines.append(f"  • {e['title']} (all day)")
        else:
            lines.append(f"  • {e['start']}–{e['end']}  {e['title']}")
    return "\n".join(lines)


def main() -> None:
    db.init_db()

    today_done = _today_done()
    open_today = db.get_open_tasks_due_today()
    cal = google_calendar.get_events()

    header = f"🌙 Evening check-in — {date.today().strftime('%a %d %b %Y')}"
    blocks: list[str] = [header, ""]

    if today_done:
        blocks.append(_format_done(today_done))
        blocks.append("")
    else:
        blocks.append("✅ Today you closed: nothing yet.")
        blocks.append("")

    blocks.append(_format_calendar_preview(cal))
    blocks.append("")

    _send("\n".join(blocks).rstrip())

    # Each open task gets its own message so the inline keyboard targets that task.
    for t in open_today:
        _send(_format_open_today(t), reply_markup=_open_today_keyboard(t["id"]))

    _send("📝 Anything to capture? Send a message — I'll add it to your list.")


if __name__ == "__main__":
    main()
