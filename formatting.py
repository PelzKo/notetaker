from datetime import date

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
    today = date.today()
    delta = (d - today).days
    if delta < 0:
        return f"⚠️ overdue ({d.strftime('%d %b')})"
    if delta == 0:
        return "today"
    if delta == 1:
        return "tomorrow"
    return d.strftime("%a %d %b")


def fmt_task_line(index: int, task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    due = fmt_date(task.get("due_date"))
    return f"{index}. {emoji} {task['title']} — {task['category']} — {due}"


def build_task_list(tasks: list[dict], header: str = "📋 Open tasks:") -> str:
    if not tasks:
        return "✅ No open tasks."
    lines = [header, ""]
    for i, t in enumerate(tasks, 1):
        lines.append(fmt_task_line(i, t))
    return "\n".join(lines)
