from datetime import date, datetime

CATEGORY_EMOJI = {
    "Work": "💻",
    "Home": "🏠",
    "MCM": "🎭",
    "YFU": "🌍",
    "Personal": "👤",
    "Other": "📌",
    "Unknown": "❓",
}


def fmt_date(d) -> str:
    if d is None:
        return "no due date"
    if isinstance(d, str):
        d = date.fromisoformat(d)
    if isinstance(d, datetime):
        d = d.date()
    today = date.today()
    delta = (d - today).days
    if delta < 0:
        return f"⚠️ overdue ({d.strftime('%d %b')})"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return d.strftime("%a %d %b")


def _badges(task: dict) -> str:
    """Build a small badge string of ⭐ / 📎 to suffix to a list line."""
    parts = []
    if task.get("is_priority"):
        parts.append("⭐")
    n = int(task.get("attachment_count") or 0)
    if n == 1:
        parts.append("📎")
    elif n > 1:
        parts.append(f"📎×{n}")
    return " ".join(parts)


def fmt_task_line(task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    due = fmt_date(task.get("due_date"))
    badges = _badges(task)
    line = f"• {emoji} {task['title']} #{task['id']} — {task['category']} — {due}"
    if badges:
        line = f"{line} {badges}"
    return line


def build_task_list(tasks: list[dict], header: str = "📋 Open tasks:") -> str:
    if not tasks:
        return "✅ No open tasks."
    lines = [header, ""]
    for t in tasks:
        lines.append(fmt_task_line(t))
    return "\n".join(lines)


def build_task_list_simple(tasks: list[dict], header: str = "📋 Open tasks:") -> str:
    """Compact bullet rendering — no emojis, no dates."""
    if not tasks:
        return "✅ No open tasks."
    lines = [header]
    for t in tasks:
        prio = "⭐ " if t.get("is_priority") else ""
        lines.append(f"• {prio}{t['title']} #{t['id']}")
    return "\n".join(lines)


def fmt_task_detail(task: dict) -> str:
    """Render a /show <id> detail block."""
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    parts = [f"{emoji} {task['category']}"]
    if task.get("is_priority"):
        parts.append("⭐ priority")
    parts.append(f"📅 {fmt_date(task.get('due_date'))}")
    parts.append("done" if task.get("is_done") else "open")
    meta = " · ".join(parts)

    created = task.get("created_at")
    if isinstance(created, datetime):
        created_str = created.strftime("%Y-%m-%d")
    elif isinstance(created, date):
        created_str = created.isoformat()
    else:
        created_str = str(created or "?")

    n = int(task.get("attachment_count") or 0)
    attach_str = ""
    if n == 1:
        attach_str = " · 📎 1 attachment"
    elif n > 1:
        attach_str = f" · 📎 {n} attachments"

    lines = [
        f"📌 {task['title']} (#{task['id']})",
        meta,
        f"Created {created_str}{attach_str}",
    ]
    raw = (task.get("raw_text") or "").strip()
    if raw and raw != task.get("title"):
        excerpt = raw[:240]
        if len(raw) > 240:
            excerpt += "…"
        lines.append("")
        for ln in excerpt.splitlines():
            lines.append(f"> {ln}")
    return "\n".join(lines)
