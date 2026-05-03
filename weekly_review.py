"""
Sunday 18:00 weekly review. Sends:
  - Done count this week, broken down by category
  - Top 5 oldest open tasks (with Drop / Defer 30d buttons)
  - Tasks created 14+ days ago that are still open

Cron entry:
  0 18 * * 0 /usr/bin/python3.11 /home/YOU/todobot/weekly_review.py >> /var/log/todobot_weekly.log 2>&1
"""
from datetime import date, datetime

import httpx

import config
import db
from formatting import CATEGORY_EMOJI


def _send(text: str, reply_markup: dict | None = None) -> None:
    payload: dict = {"chat_id": config.TELEGRAM_CHAT_ID, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    resp = httpx.post(url, json=payload, timeout=15)
    resp.raise_for_status()


def _stale_keyboard(task_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "❌ Drop", "callback_data": f"act:{task_id}:drop"},
                {"text": "📅 Defer 30d", "callback_data": f"act:{task_id}:defer30"},
                {"text": "✅ Done", "callback_data": f"act:{task_id}:done"},
            ]
        ]
    }


def _format_done_stats(stats: list[dict], total: int) -> str:
    if not stats:
        return "✅ Done this week: nothing logged."
    lines = [f"✅ Done this week ({total} task{'s' if total != 1 else ''}):"]
    for row in stats:
        emoji = CATEGORY_EMOJI.get(row["category"], "📌")
        lines.append(f"  {emoji} {row['category']}: {row['done']}")
    return "\n".join(lines)


def _age_days(t: dict) -> int:
    created = t.get("created_at")
    if isinstance(created, datetime):
        created = created.date()
    if not isinstance(created, date):
        return 0
    return (date.today() - created).days


def _format_stale(task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    star = "⭐ " if task.get("is_priority") else ""
    return f"🕰 #{task['id']} ({_age_days(task)}d old): {star}{emoji} {task['title']}"


def main() -> None:
    db.init_db()
    today = date.today()
    header = f"📊 Weekly review — {today.strftime('%a %d %b %Y')}"
    _send(header)

    stats = db.get_done_stats_by_category(7)
    total_done = sum(int(r["done"]) for r in stats)
    _send(_format_done_stats(stats, total_done))

    oldest = db.get_oldest_open_tasks(limit=5)
    oldest = [t for t in oldest if _age_days(t) >= 30]
    if oldest:
        _send(f"🧹 Top {len(oldest)} oldest open tasks (30+ days). Triage time:")
        for t in oldest:
            _send(_format_stale(t), reply_markup=_stale_keyboard(t["id"]))

    # 14+ day stale list (excluding the ones we already showed above)
    shown_ids = {t["id"] for t in oldest}
    everything = db.get_open_tasks()
    stale_14 = [t for t in everything if _age_days(t) >= 14 and t["id"] not in shown_ids]
    if stale_14:
        _send(f"📌 Other tasks open 14+ days ({len(stale_14)}):")
        # Cap to a reasonable number to avoid spam
        for t in stale_14[:15]:
            _send(_format_stale(t), reply_markup=_stale_keyboard(t["id"]))
        if len(stale_14) > 15:
            _send(f"…and {len(stale_14) - 15} more.")

    if not stats and not oldest and not stale_14:
        _send("✨ Nothing to triage. Inbox zero vibes.")


if __name__ == "__main__":
    main()
