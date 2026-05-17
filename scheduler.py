"""
Called by cron every morning. Sends the daily summary to Telegram.
Cron entry example:
  0 8 * * * /usr/bin/python3.11 /home/YOU/todobot/scheduler.py >> /var/log/todobot_cron.log 2>&1
"""
import httpx
from datetime import date, timedelta

import config
import db
import google_calendar
import notion
import interesting_reads
from formatting import fmt_task_line


def send_message(text: str):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    resp = httpx.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()

def build_summary() -> str:
    raw = db.get_summary_tasks()
    today = date.today()
    tomorrow = today + timedelta(days=1)
    today_str = today.strftime("%a %d %b %Y")

    sections = [f"📋 Daily Summary — {today_str}\n"]
    all_tasks = []

    def add_section(header: str, task_list: list[dict], show_date: bool = True):
        if not task_list:
            return
        sections.append(header)
        for t in task_list:
            all_tasks.append(t)
            sections.append(fmt_task_line(t, show_date=show_date))
        sections.append("")

    overdue = raw["overdue"]
    upcoming = raw["upcoming"]
    today_tasks = [t for t in upcoming if t["due_date"] == today]
    tomorrow_tasks = [t for t in upcoming if t["due_date"] == tomorrow]
    due_soon = [t for t in upcoming if t["due_date"] > tomorrow]

    add_section("⚠️ Overdue:", overdue)
    add_section("📅 Today:", today_tasks, show_date=False)
    add_section("📅 Tomorrow:", tomorrow_tasks, show_date=False)
    add_section("🗓 Due Soon (next 7 days):", due_soon)
    add_section("🕰 Long Pending (30+ days, no due date):", raw["old_noduedate"])

    cal_events = google_calendar.get_events()
    cal_block = google_calendar.fmt_events_for_summary(cal_events)
    sections.append("─────────────────")
    sections.append(cal_block)
    sections.append("")

    if not all_tasks and not any(cal_events.get(k) for k in ("today", "tomorrow")):
        sections.append("✅ Nothing urgent and calendar is clear!")
        return "\n".join(sections)

    reads = interesting_reads.get_interesting_reads()
    if reads:
        sections.append("Interesting reads:\n" + "\n".join(f"• {url}" for url in reads))

    return "\n".join(sections)


def _run_notion_sync() -> None:
    """Pull Notion changes into MariaDB and notify if anything changed."""
    if not notion.enabled():
        return
    changes = notion.sync_from_notion()
    if changes:
        lines = ["🔄 Synced from Notion:"] + [f"• {c}" for c in changes]
        send_message("\n".join(lines))


def main():
    db.init_db()
    _run_notion_sync()
    summary = build_summary()
    send_message(summary)


if __name__ == "__main__":
    main()
