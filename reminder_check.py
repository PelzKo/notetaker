"""
Per-minute cron job: fire any reminders whose remind_at has passed.

Cron entry:
  * * * * * /usr/bin/python3.11 /home/YOU/todobot/reminder_check.py >> /var/log/todobot_reminders.log 2>&1
"""
import httpx

import config
import db
from formatting import CATEGORY_EMOJI, fmt_date


def _send(text: str, reply_markup: dict | None = None) -> None:
    payload: dict = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def _reminder_keyboard(task_id: int) -> dict:
    """Keyboard rendered as raw JSON for the Telegram HTTP API
    (we don't have a python-telegram-bot Application context here)."""
    return {
        "inline_keyboard": [
            [
                {"text": "✅ Done", "callback_data": f"act:{task_id}:done"},
                {"text": "💤 Snooze 1h", "callback_data": f"act:{task_id}:snz1h"},
                {"text": "🌅 Tomorrow AM", "callback_data": f"act:{task_id}:snzAM"},
            ]
        ]
    }


def _format_reminder(task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    lines = [
        f"⏰ Reminder (#{task['id']}):",
        f"📌 {task['title']}",
        f"{emoji} {task['category']}",
    ]
    if task.get("is_priority"):
        lines.append("⭐ priority")
    if task.get("due_date"):
        lines.append(f"📅 {fmt_date(task['due_date'])}")
    return "\n".join(lines)


def main() -> None:
    db.init_db()
    pending = db.get_due_reminders()
    if not pending:
        return
    for task in pending:
        try:
            _send(_format_reminder(task), reply_markup=_reminder_keyboard(task["id"]))
            db.mark_reminder_sent(task["id"])
        except Exception as exc:  # noqa: BLE001
            # Don't mark sent if delivery failed — try again next minute.
            print(f"reminder_check: failed to send #{task['id']}: {exc}")


if __name__ == "__main__":
    main()
