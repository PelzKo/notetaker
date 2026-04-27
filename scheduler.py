"""
Called by cron every morning. Sends the daily summary to Telegram.
Cron entry example:
  0 8 * * * /usr/bin/python3.11 /home/YOU/todobot/scheduler.py >> /var/log/todobot_cron.log 2>&1
"""
import httpx
from datetime import date

import config
import db
import claude_client
import google_calendar
from formatting import CATEGORY_EMOJI, fmt_date, fmt_task_line, build_task_list


def send_message(text: str):
    url = f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN}/sendMessage"
    resp = httpx.post(url, json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text}, timeout=15)
    resp.raise_for_status()

def build_summary() -> tuple[str, list[dict]]:
    """Returns (summary_text, flat_numbered_task_list)."""
    tasks = db.get_summary_tasks()
    today = date.today().strftime("%a %d %b %Y")
 
    sections = [f"📋 Daily Summary — {today}\n"]
    all_tasks = []
 
    def add_section(header: str, task_list: list[dict]):
        if not task_list:
            return
        sections.append(header)
        for t in task_list:
            idx = len(all_tasks) + 1
            all_tasks.append(t)
            sections.append(fmt_task_line(idx, t))
        sections.append("")
 
    add_section("⚠️ OVERDUE:", tasks["overdue"])
    add_section("📅 DUE SOON (next 7 days):", tasks["upcoming"])
    add_section("🕰 LONG PENDING (30+ days, no due date):", tasks["old_noduedate"])
 
    # ── NEW: fetch and append calendar block ──────────────────
    cal_events = google_calendar.get_events()
    cal_block = google_calendar.fmt_events_for_summary(cal_events)
    sections.append("─────────────────")
    sections.append(cal_block)
    sections.append("")
    # ─────────────────────────────────────────────────────────
 
    if not all_tasks and not any(cal_events.get(k) for k in ("today", "tomorrow")):
        sections.append("✅ Nothing urgent and calendar is clear!")
        return "\n".join(sections), []
 
    # ── NEW: pass calendar events into the AI summary prompt ──
    ai_comment = claude_client.generate_summary_comment(tasks, cal_events)
    # ─────────────────────────────────────────────────────────
    if ai_comment:
        sections.append(f"🤖 Priority tip:\n{ai_comment}")
 
    return "\n".join(sections), all_tasks


def build_done_prompt(tasks: list[dict]) -> str:
    if not tasks:
        return ""
    lines = ["Mark done? Reply with numbers (e.g. '1 3'):\n"]
    for i, t in enumerate(tasks, 1):
        emoji = CATEGORY_EMOJI.get(t["category"], "📌")
        lines.append(f"{i}. {emoji} {t['title']}")
    return "\n".join(lines)


def main():
    db.init_db()
    summary, tasks = build_summary()
    send_message(summary)

    if tasks:
        done_prompt = build_done_prompt(tasks)
        send_message(done_prompt)


if __name__ == "__main__":
    main()
