import asyncio
import logging
import re
from datetime import date, datetime, timedelta

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import config
import db
import claude_client
import notion
import url_capture
import google_calendar
from formatting import (
    CATEGORY_EMOJI,
    build_task_list,
    build_task_list_sectioned,
    build_task_list_simple,
    fmt_date,
    fmt_task_detail,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

EDIT_FIELDS = {"title", "category", "date", "due", "priority"}
EDIT_TTL = timedelta(minutes=10)


def _split_ids(s: str) -> list[str]:
    return [p for p in re.split(r"[\s,]+", s.strip()) if p]

# ---------------------------------------------------------------------------
# Auth guard — only respond to your own chat
# ---------------------------------------------------------------------------

def _authorized(update: Update) -> bool:
    return update.effective_chat.id == config.TELEGRAM_CHAT_ID


# ---------------------------------------------------------------------------
# Notion sync helpers (fire-and-forget, never crash the bot)
# ---------------------------------------------------------------------------

def _sync_task_to_notion(task_id: int) -> None:
    """Push the current DB state of task_id to Notion."""
    task = db.get_task(task_id)
    if not task:
        return
    page_id = task.get("notion_page_id")
    if page_id:
        synced_at = notion.update_page(page_id, task)
        if synced_at:
            db.set_notion_page_id(task_id, page_id, synced_at)
    else:
        page_id, synced_at = notion.create_page(task)
        if page_id:
            db.set_notion_page_id(task_id, page_id, synced_at)


def _archive_task_in_notion(page_id: str | None) -> None:
    if page_id:
        notion.archive_page(page_id)


# ---------------------------------------------------------------------------
# Inline keyboard helpers
# ---------------------------------------------------------------------------

def _edit_button(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"✏️ /edit {task_id}", callback_data=f"edit:{task_id}")]]
    )


def _category_picker_rows(task_id: int) -> list[list[InlineKeyboardButton]]:
    cats = [c for c in config.CATEGORIES if c != "Unknown"]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(cats), 4):
        rows.append([
            InlineKeyboardButton(
                f"{CATEGORY_EMOJI.get(c, '📌')} {c}",
                callback_data=f"setcat:{task_id}:{c}",
            )
            for c in cats[i:i + 4]
        ])
    return rows


def _capture_action_rows(task_id: int, *, is_priority: bool = False) -> list[list[InlineKeyboardButton]]:
    star = "⭐" if is_priority else "☆"
    return [
        [
            InlineKeyboardButton(f"{star} Priority", callback_data=f"act:{task_id}:pri"),
            InlineKeyboardButton("📅 Today", callback_data=f"act:{task_id}:today"),
            InlineKeyboardButton("📅 Tomorrow", callback_data=f"act:{task_id}:tomorrow"),
        ],
        [
            InlineKeyboardButton("➕1d", callback_data=f"act:{task_id}:defer1"),
            InlineKeyboardButton(f"✏️ /edit {task_id}", callback_data=f"edit:{task_id}"),
            InlineKeyboardButton("❌ Drop", callback_data=f"act:{task_id}:drop"),
        ],
    ]


def _category_picker_markup(task_id: int) -> InlineKeyboardMarkup:
    rows = _category_picker_rows(task_id)
    rows.append([InlineKeyboardButton(f"✏️ /edit {task_id}", callback_data=f"edit:{task_id}")])
    return InlineKeyboardMarkup(rows)


def _undo_markup(task_ids: list[int]) -> InlineKeyboardMarkup:
    csv = ",".join(str(i) for i in task_ids)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{csv}")
    ]])


# ---------------------------------------------------------------------------
# Date keyword parsing for field-targeted /edit
# ---------------------------------------------------------------------------

def _parse_date_keyword(s: str) -> tuple[bool, date | None]:
    """Return (ok, value). value=None means clear the date."""
    s = s.strip().lower()
    if s in ("none", "clear", "null", "off", "-"):
        return True, None
    if s == "today":
        return True, date.today()
    if s == "tomorrow":
        return True, date.today() + timedelta(days=1)
    if s.startswith("+") and s.endswith("d"):
        try:
            return True, date.today() + timedelta(days=int(s[1:-1]))
        except ValueError:
            return False, None
    try:
        return True, date.fromisoformat(s)
    except ValueError:
        return False, None


# ---------------------------------------------------------------------------
# Shared "added" reply renderer
# ---------------------------------------------------------------------------

def _render_added_card(task: dict, *, parse_error: str | None = None,
                       attachment_count: int = 0,
                       prefix: str = "✅ Added") -> tuple[str, InlineKeyboardMarkup | None]:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    lines = [
        f"{prefix} (#{task['id']}):",
        f"📌 {task['title']}",
        f"{emoji} {task['category']}",
    ]
    if task.get("is_priority"):
        lines.append("⭐ priority")
    lines.append(f"📅 {fmt_date(task.get('due_date'))}")
    if task.get("recurrence"):
        lines.append(f"🔁 {task['recurrence']}")
    if task.get("remind_at"):
        ra = task["remind_at"]
        if isinstance(ra, datetime):
            lines.append(f"⏰ {ra.strftime('%Y-%m-%d %H:%M')}")
        else:
            lines.append(f"⏰ {ra}")
    if attachment_count == 1:
        lines.append("📎 1 attachment")
    elif attachment_count > 1:
        lines.append(f"📎 {attachment_count} attachments")
    if parse_error:
        lines.append(f"\n⚠️ Parse warning: {parse_error}")
    rows: list[list[InlineKeyboardButton]] = []
    if task["category"] == "Unknown":
        lines.append(f"\n❓ Couldn't detect category — tap below or use /edit {task['id']}.")
        rows.extend(_category_picker_rows(task["id"]))
    rows.extend(_capture_action_rows(task["id"], is_priority=bool(task.get("is_priority"))))
    return "\n".join(lines), InlineKeyboardMarkup(rows) if rows else None


async def _send_added_reply(send_fn, task: dict, *, parse_error: str | None = None,
                            attachment_count: int = 0, prefix: str = "✅ Added"):
    text, markup = _render_added_card(
        task, parse_error=parse_error, attachment_count=attachment_count, prefix=prefix,
    )
    await send_fn(text, reply_markup=markup)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "👋 Todo bot ready. Send any text to add a task, or photos/documents with captions.\n"
        "Type /help for the full command list, /menu for the keyboard."
    )


# ---------------------------------------------------------------------------
# Free-text → add task
# ---------------------------------------------------------------------------

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return

    raw = update.message.text.strip()

    # Numeric reply (space- or comma-separated) marks listed tasks done
    parts = _split_ids(raw)
    if parts and all(p.isdigit() for p in parts):
        await _handle_done_reply(update, ctx, raw)
        return

    # URL-only message → fetch the page title for a cleaner parse input
    parse_input = raw
    url_only = url_capture.detect_url_only(raw)
    if url_only:
        await update.message.reply_text("⏳ Fetching link…")
        title = url_capture.fetch_title(url_only)
        if title:
            parse_input = f"Read: {title}\n{url_only}"
    elif update.message.forward_origin is not None:
        # Forwarded message → tell Claude this is a captured/saved item, not a personal todo
        parse_input = f"(forwarded message) {raw}"
        await update.message.reply_text("⏳ Parsing…")
    else:
        await update.message.reply_text("⏳ Parsing…")

    parsed = claude_client.parse_task(parse_input)
    title = parsed["title"]
    category = parsed["category"]
    due_date_str = parsed.get("due_date")
    due_date = date.fromisoformat(due_date_str) if due_date_str else None
    is_priority = bool(parsed.get("is_priority", False))
    parse_error = parsed.get("error")
    recurrence = parsed.get("recurrence")
    remind_at_str = parsed.get("remind_at")
    remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M") if remind_at_str else None

    task_id = db.add_task(
        raw, title, category, due_date,
        is_priority=is_priority,
        recurrence=recurrence,
        remind_at=remind_at,
    )
    _sync_task_to_notion(task_id)

    task = db.get_task(task_id)
    await _send_added_reply(
        update.message.reply_text,
        task,
        parse_error=parse_error,
        attachment_count=int(task.get("attachment_count") or 0),
    )


async def _handle_done_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle a reply like '42 17' that marks tasks done by their IDs."""
    session = ctx.user_data.get("task_list", [])
    if not session:
        await update.message.reply_text(
            "No active task list in memory. Use /done to get a fresh list."
        )
        return

    session_ids = {t["id"]: t for t in session}
    ids = [int(x) for x in _split_ids(text) if x.isdigit()]
    marked: list[tuple[int, str]] = []  # (task_id, title)
    failed: list[str] = []
    for task_id in ids:
        if task_id in session_ids:
            task = session_ids[task_id]
            transitioned, new_id = db.mark_done_ex(task["id"])
            if transitioned:
                marked.append((task["id"], task["title"]))
                _sync_task_to_notion(task["id"])
                if new_id is not None:
                    _sync_task_to_notion(new_id)
            else:
                failed.append(task["title"])
        else:
            failed.append(f"#{task_id} (not in list)")

    ctx.user_data["task_list"] = []  # clear session

    lines = []
    if marked:
        lines.append("✅ Done:\n" + "\n".join(f"  • {t}" for _, t in marked))
    if failed:
        lines.append("⚠️ Couldn't mark:\n" + "\n".join(f"  • {t}" for t in failed))
    text_out = "\n\n".join(lines) or "Nothing changed."

    reply_markup = _undo_markup([tid for tid, _ in marked]) if marked else None
    await update.message.reply_text(text_out, reply_markup=reply_markup)


# ---------------------------------------------------------------------------
# /list [onlytext]
# ---------------------------------------------------------------------------

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    only_text = bool(args and args[0].lower() == "onlytext")
    tasks = db.get_open_tasks()
    if only_text:
        ctx.user_data["task_list"] = []  # no numbered done flow
        await update.message.reply_text(build_task_list_simple(tasks))
        return
    ctx.user_data["task_list"] = tasks
    msg = build_task_list_sectioned(tasks)
    if tasks:
        msg += "\n\nReply with task IDs to mark done."
    await update.message.reply_text(msg)


async def cmd_listtext(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_open_tasks()
    ctx.user_data["task_list"] = []
    await update.message.reply_text(build_task_list_simple(tasks))


# ---------------------------------------------------------------------------
# /done
# ---------------------------------------------------------------------------

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_open_tasks()
    if not tasks:
        await update.message.reply_text("✅ No open tasks.")
        return
    ctx.user_data["task_list"] = tasks
    msg = build_task_list_sectioned(tasks, "Which tasks are done? Reply with task IDs:")
    await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# /drop <id>
# ---------------------------------------------------------------------------

async def cmd_drop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /drop <task_id>")
        return
    task_id = int(args[0])
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    page_id = task.get("notion_page_id")
    db.delete_task(task_id)
    _archive_task_in_notion(page_id)
    await update.message.reply_text(f"🗑 Deleted: {task['title']}")


# ---------------------------------------------------------------------------
# /edit <id> [field value]
# ---------------------------------------------------------------------------

EDIT_WAITING: dict[int, tuple[int, datetime]] = {}  # chat_id → (task_id, started_at)


async def _start_edit(chat_id: int, task_id: int, reply_fn) -> None:
    task = db.get_task(task_id)
    if not task:
        await reply_fn(f"Task #{task_id} not found.")
        return
    EDIT_WAITING[chat_id] = (task_id, datetime.now())
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    await reply_fn(
        f"Editing #{task_id}: {task['title']}\n"
        f"Current: {emoji} {task['category']} — {fmt_date(task['due_date'])}\n\n"
        "Send new text describing the task again (I'll re-parse it), "
        "or send just a category name to change only the category, "
        "or /cancel to abort."
    )


async def _edit_field(update: Update, task_id: int, field: str, value: str):
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return

    if field == "title":
        if not value.strip():
            await update.message.reply_text("Need a title value.")
            return
        db.update_task(task_id, title=value.strip()[:500])
    elif field == "category":
        if value not in config.CATEGORIES:
            await update.message.reply_text(
                "Unknown category. Pick one of: " + ", ".join(config.CATEGORIES)
            )
            return
        db.update_task(task_id, category=value)
    elif field in ("date", "due"):
        ok, d = _parse_date_keyword(value)
        if not ok:
            await update.message.reply_text(
                "Couldn't parse date. Use YYYY-MM-DD, today, tomorrow, +Nd, or none."
            )
            return
        db.update_task(task_id, due_date=d)
    elif field == "priority":
        v = value.strip().lower()
        if v in ("on", "true", "1", "yes", "y"):
            db.update_task(task_id, is_priority=True)
        elif v in ("off", "false", "0", "no", "n"):
            db.update_task(task_id, is_priority=False)
        else:
            await update.message.reply_text("Use: /edit <id> priority on|off")
            return

    _sync_task_to_notion(task_id)
    new_task = db.get_task(task_id)
    emoji = CATEGORY_EMOJI.get(new_task["category"], "📌")
    lines = [
        f"✅ Updated #{task_id}:",
        f"📌 {new_task['title']}",
        f"{emoji} {new_task['category']}",
    ]
    if new_task.get("is_priority"):
        lines.append("⭐ priority")
    lines.append(f"📅 {fmt_date(new_task['due_date'])}")
    await update.message.reply_text("\n".join(lines))


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /edit <task_id> [field value]\n"
            "Fields: title, category, date, priority"
        )
        return
    task_id = int(args[0])
    if len(args) >= 2 and args[1].lower() in EDIT_FIELDS:
        await _edit_field(update, task_id, args[1].lower(), " ".join(args[2:]))
        return
    await _start_edit(
        update.effective_chat.id,
        task_id,
        update.message.reply_text,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    EDIT_WAITING.pop(update.effective_chat.id, None)
    await update.message.reply_text("Cancelled.")


async def handle_edit_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entry = EDIT_WAITING.get(chat_id)
    if entry is None:
        return False
    task_id, started = entry
    if datetime.now() - started > EDIT_TTL:
        EDIT_WAITING.pop(chat_id, None)
        return False
    EDIT_WAITING.pop(chat_id, None)

    text = update.message.text.strip()

    if text in config.CATEGORIES:
        db.update_task(task_id, category=text)
        _sync_task_to_notion(task_id)
        await update.message.reply_text(f"✅ Category updated to {text}.")
        return True

    await update.message.reply_text("⏳ Re-parsing…")
    parsed = claude_client.parse_task(text)
    due_date = date.fromisoformat(parsed["due_date"]) if parsed.get("due_date") else None
    remind_at_str = parsed.get("remind_at")
    remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M") if remind_at_str else None
    db.update_task(
        task_id,
        title=parsed["title"],
        category=parsed["category"],
        due_date=due_date,
        is_priority=bool(parsed.get("is_priority", False)),
        recurrence=parsed.get("recurrence"),
        remind_at=remind_at,
    )
    _sync_task_to_notion(task_id)

    new_task = db.get_task(task_id)
    emoji = CATEGORY_EMOJI.get(new_task["category"], "📌")
    lines = [
        f"✅ Updated #{task_id}:",
        f"📌 {new_task['title']}",
        f"{emoji} {new_task['category']}",
    ]
    if new_task.get("is_priority"):
        lines.append("⭐ priority")
    lines.append(f"📅 {fmt_date(new_task['due_date'])}")
    await update.message.reply_text("\n".join(lines))
    return True


# ---------------------------------------------------------------------------
# /defer <id> [days]
# ---------------------------------------------------------------------------

async def cmd_defer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /defer <task_id> [days]")
        return
    task_id = int(args[0])
    days = 1
    if len(args) >= 2:
        try:
            days = int(args[1])
        except ValueError:
            await update.message.reply_text("Days must be an integer.")
            return
    new_due = db.defer_task(task_id, days)
    if new_due is None:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    _sync_task_to_notion(task_id)
    await update.message.reply_text(f"📅 Deferred #{task_id} to {fmt_date(new_due)}.")


# ---------------------------------------------------------------------------
# /priority <id> [on|off]
# ---------------------------------------------------------------------------

_SNOOZE_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def _parse_snooze_duration(spec: str) -> datetime | None:
    s = spec.strip().lower()
    if not s:
        return None
    now = datetime.now()
    # 1h, 2h, 30m, 90m
    if s.endswith("h"):
        try:
            return now + timedelta(hours=int(s[:-1]))
        except ValueError:
            return None
    if s.endswith("m"):
        try:
            return now + timedelta(minutes=int(s[:-1]))
        except ValueError:
            return None
    if s == "tomorrow":
        return datetime.combine(date.today() + timedelta(days=1),
                                datetime.min.time().replace(hour=8))
    if s in _SNOOZE_WEEKDAYS:
        target = _SNOOZE_WEEKDAYS.index(s)
        d = date.today() + timedelta(days=1)
        while d.weekday() != target:
            d += timedelta(days=1)
        return datetime.combine(d, datetime.min.time().replace(hour=8))
    return None


async def cmd_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /snooze <task_id> <1h|30m|tomorrow|mon|tue|...>"
        )
        return
    task_id = int(args[0])
    new_remind = _parse_snooze_duration(args[1])
    if new_remind is None:
        await update.message.reply_text(
            "Couldn't parse duration. Try: 1h, 30m, tomorrow, mon, fri."
        )
        return
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    db.update_task(task_id, remind_at=new_remind, remind_sent=False)
    await update.message.reply_text(
        f"💤 Snoozed #{task_id} until {new_remind.strftime('%a %d %b %H:%M')}."
    )


def _currently_in_meeting(events: dict) -> dict | None:
    """If the user is in a timed event right now, return that event."""
    now = datetime.now()
    today_str = date.today().strftime("%H:%M")
    for e in events.get("today", []):
        if e["all_day"] or not e.get("end"):
            continue
        try:
            start = datetime.combine(date.today(), datetime.strptime(e["start"], "%H:%M").time())
            end = datetime.combine(date.today(), datetime.strptime(e["end"], "%H:%M").time())
        except ValueError:
            continue
        if start <= now <= end:
            return e
    return None


def _score_task(task: dict) -> float:
    today = date.today()
    due = task.get("due_date")
    if isinstance(due, datetime):
        due = due.date()
    score = 0.0
    if task.get("is_priority"):
        score += 100
    if due is not None:
        if due < today:
            score += (today - due).days * 10
        elif due == today:
            score += 50
    created = task.get("created_at")
    if isinstance(created, datetime):
        created_d = created.date()
    elif isinstance(created, date):
        created_d = created
    else:
        created_d = today
    score -= (today - created_d).days * 0.5
    return score


def _pick_next_keyboard(task_id: int) -> InlineKeyboardMarkup:
    rows = _capture_action_rows(task_id, is_priority=False)
    rows.append([InlineKeyboardButton("🔁 Pick another", callback_data=f"nextagain:{task_id}")])
    return InlineKeyboardMarkup(rows)


def _format_next_card(task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    lines = [f"🎯 Up next (#{task['id']}):", f"📌 {task['title']}", f"{emoji} {task['category']}"]
    if task.get("is_priority"):
        lines.append("⭐ priority")
    lines.append(f"📅 {fmt_date(task.get('due_date'))}")
    return "\n".join(lines)


async def cmd_next(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    cal = google_calendar.get_events()
    in_meeting = _currently_in_meeting(cal)
    if in_meeting:
        await update.message.reply_text(
            f"📅 You're in '{in_meeting['title']}' right now "
            f"({in_meeting['start']}–{in_meeting['end']}). I'll skip a recommendation."
        )
        return

    tasks = db.get_open_tasks()
    excluded: set = ctx.user_data.get("next_excluded", set())
    candidates = [t for t in tasks if t["id"] not in excluded]
    if not candidates:
        ctx.user_data["next_excluded"] = set()  # reset
        await update.message.reply_text("Nothing left to suggest. /list to see everything open.")
        return

    pick = max(candidates, key=_score_task)
    excluded = set(excluded)
    excluded.add(pick["id"])
    ctx.user_data["next_excluded"] = excluded
    await update.message.reply_text(
        _format_next_card(pick),
        reply_markup=_pick_next_keyboard(pick["id"]),
    )


async def cmd_priority(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /priority <task_id> [on|off]")
        return
    task_id = int(args[0])
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    if len(args) >= 2:
        v = args[1].lower()
        new_val = v in ("on", "true", "1", "yes", "y")
    else:
        new_val = not bool(task.get("is_priority"))
    db.set_priority(task_id, new_val)
    _sync_task_to_notion(task_id)
    icon = "⭐" if new_val else "☆"
    await update.message.reply_text(
        f"{icon} Priority {'on' if new_val else 'off'} for #{task_id}: {task['title']}"
    )


# ---------------------------------------------------------------------------
# /search <query>
# ---------------------------------------------------------------------------

async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    q = " ".join(ctx.args or []).strip()
    if len(q) < 2:
        await update.message.reply_text("Usage: /search <query> (≥2 chars)")
        return
    results = db.search_tasks(q)
    ctx.user_data["task_list"] = results
    if not results:
        await update.message.reply_text(f"🔍 No matches for '{q}'.")
        return
    msg = build_task_list(results, header=f"🔍 Search results for '{q}':")
    msg += "\n\nReply with task IDs to mark done."
    await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# /filter [category]
# ---------------------------------------------------------------------------

async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if args and args[0] in config.CATEGORIES:
        cat = args[0]
        results = db.get_tasks_by_category(cat)
        ctx.user_data["task_list"] = results
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        msg = build_task_list(results, header=f"📂 {emoji} {cat}:")
        if results:
            msg += "\n\nReply with task IDs to mark done."
        await update.message.reply_text(msg)
        return

    cats = [c for c in config.CATEGORIES if c != "Unknown"]
    rows: list[list[InlineKeyboardButton]] = []
    for i in range(0, len(cats), 4):
        rows.append([
            InlineKeyboardButton(
                f"{CATEGORY_EMOJI.get(c, '📌')} {c}",
                callback_data=f"filter:{c}",
            )
            for c in cats[i:i + 4]
        ])
    rows.append([
        InlineKeyboardButton(
            f"{CATEGORY_EMOJI['Unknown']} Unknown",
            callback_data="filter:Unknown",
        )
    ])
    await update.message.reply_text(
        "📂 Filter by category:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


# ---------------------------------------------------------------------------
# /history [days]
# ---------------------------------------------------------------------------

async def cmd_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    days = 7
    if args and args[0].isdigit():
        days = int(args[0])
    rows = db.get_done_tasks(days)
    if not rows:
        await update.message.reply_text(f"No completed tasks in the last {days} day(s).")
        return

    today = date.today()
    groups: dict[str, list[dict]] = {}
    order: list[str] = []
    for t in rows:
        d = t["done_at"]
        if isinstance(d, datetime):
            d = d.date()
        if d == today:
            label = "Today"
        elif d == today - timedelta(days=1):
            label = "Yesterday"
        elif (today - d).days < 7:
            label = d.strftime("%A")
        else:
            label = d.strftime("%a %d %b")
        if label not in groups:
            groups[label] = []
            order.append(label)
        groups[label].append(t)

    lines = [f"📅 Completed in the last {days} day(s):", ""]
    for label in order:
        lines.append(f"— {label}")
        for t in groups[label]:
            emoji = CATEGORY_EMOJI.get(t["category"], "📌")
            star = "⭐ " if t.get("is_priority") else ""
            lines.append(f"  ✅ {star}{emoji} {t['title']} — {t['category']}")
        lines.append("")
    await update.message.reply_text("\n".join(lines).rstrip())


# ---------------------------------------------------------------------------
# /show <id>
# ---------------------------------------------------------------------------

async def cmd_show(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /show <task_id>")
        return
    task_id = int(args[0])
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    await update.message.reply_text(fmt_task_detail(task), reply_markup=_edit_button(task_id))

    chat_id = update.effective_chat.id
    for att in db.get_attachments(task_id):
        kind = att["kind"]
        cap = att.get("caption") or None
        try:
            if kind == "photo":
                await ctx.bot.send_photo(chat_id, att["file_id"], caption=cap)
            elif kind == "document":
                await ctx.bot.send_document(chat_id, att["file_id"], caption=cap)
            elif kind == "voice":
                await ctx.bot.send_voice(chat_id, att["file_id"], caption=cap)
            elif kind == "audio":
                await ctx.bot.send_audio(chat_id, att["file_id"], caption=cap)
            elif kind == "video":
                await ctx.bot.send_video(chat_id, att["file_id"], caption=cap)
        except Exception as e:  # noqa: BLE001
            log.warning("Failed to resend attachment %s: %s", att["id"], e)


# ---------------------------------------------------------------------------
# /menu
# ---------------------------------------------------------------------------

async def cmd_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    keyboard = ReplyKeyboardMarkup(
        [
            [KeyboardButton("/list"), KeyboardButton("/done"), KeyboardButton("/search")],
            [KeyboardButton("/history"), KeyboardButton("/stats"), KeyboardButton("/help")],
        ],
        resize_keyboard=True,
    )
    await update.message.reply_text("📲 Menu set.", reply_markup=keyboard)


# ---------------------------------------------------------------------------
# /help
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "🤖 Notetaker Bot\n\n"
    "Adding\n"
    "• Send any text to add a task\n"
    "• Send a photo or document with caption to attach a file\n\n"
    "Viewing\n"
    "/list — open tasks with IDs\n"
    "/list onlytext — compact bullet list\n"
    "/show <id> — task detail + attachments\n"
    "/search <query> — find tasks by title/text\n"
    "/filter [category] — filter by category\n"
    "/history [days] — recently completed (default 7d)\n"
    "/stats — counts per category\n\n"
    "Modifying\n"
    "/done — mark tasks done by ID\n"
    "/edit <id> — re-parse from new text\n"
    "/edit <id> <field> <value> — set title|category|date|priority\n"
    "/defer <id> [days] — push due date back (default 1)\n"
    "/priority <id> [on|off] — toggle ⭐\n"
    "/snooze <id> <1h|30m|tomorrow|mon> — push reminder forward\n"
    "/next — suggest what to do right now\n"
    "/drop <id> — delete\n\n"
    "Notion\n"
    "/sync — pull changes from Notion\n"
    "/pushnotion — push DB tasks to Notion\n\n"
    "Misc\n"
    "/menu — show keyboard\n"
    "/cancel — abort an /edit"
)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(HELP_TEXT)


# ---------------------------------------------------------------------------
# /stats
# ---------------------------------------------------------------------------

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    rows = db.get_stats()
    if not rows:
        await update.message.reply_text("No tasks yet.")
        return
    lines = ["📊 Stats:"]
    for row in rows:
        emoji = CATEGORY_EMOJI.get(row["category"], "📌")
        lines.append(f"{emoji} {row['category']}: {row['open']} open, {row['done']} done")
    await update.message.reply_text("\n".join(lines))


# ---------------------------------------------------------------------------
# /sync — manual Notion → DB pull
# ---------------------------------------------------------------------------

async def cmd_sync(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if not notion.enabled():
        await update.message.reply_text(
            "⚠️ Notion is not configured (missing NOTION_API_KEY / NOTION_DATABASE_ID)."
        )
        return
    await update.message.reply_text("🔄 Pulling changes from Notion…")
    changes = notion.sync_from_notion()
    if changes:
        await update.message.reply_text(
            "✅ Synced from Notion:\n" + "\n".join(f"• {c}" for c in changes)
        )
    else:
        await update.message.reply_text("✅ Nothing to sync — Notion is up to date.")


# ---------------------------------------------------------------------------
# /pushnotion — bulk push tasks lacking notion_page_id
# ---------------------------------------------------------------------------

async def cmd_pushnotion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if not notion.enabled():
        await update.message.reply_text("⚠️ Notion is not configured.")
        return
    args = ctx.args or []
    include_done = bool(args and args[0].lower() == "all")
    rows = db.get_tasks_without_notion_page()
    if not include_done:
        rows = [r for r in rows if not r.get("is_done")]
    if not rows:
        await update.message.reply_text("✅ Nothing to push — all tasks already in Notion.")
        return

    await update.message.reply_text(f"📤 Pushing {len(rows)} task(s) to Notion…")
    pushed = 0
    failed = 0
    for i, t in enumerate(rows, 1):
        page_id, synced_at = notion.create_page(t)
        if page_id:
            db.set_notion_page_id(t["id"], page_id, synced_at)
            pushed += 1
        else:
            failed += 1
        if len(rows) > 20 and i % 10 == 0 and i < len(rows):
            await update.message.reply_text(f"… {i}/{len(rows)}")
        await asyncio.sleep(0.35)
    msg = f"📤 Pushed {pushed} task(s) to Notion."
    if failed:
        msg += f" ({failed} failed)"
    await update.message.reply_text(msg)


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------

# (chat_id, media_group_id) → {"lock": Lock, "task_id": int|None, "first_done": bool}
MEDIA_BUFFER: dict[tuple[int, str], dict] = {}


def _create_task_for_caption(caption: str | None) -> tuple[int, str | None, str]:
    """Create a task from a caption, or a placeholder if none. Returns (task_id, parse_error, raw_text)."""
    if caption and caption.strip():
        raw = caption.strip()
        parsed = claude_client.parse_task(raw)
        title = parsed["title"]
        category = parsed["category"]
        due_str = parsed.get("due_date")
        due_date = date.fromisoformat(due_str) if due_str else None
        is_priority = bool(parsed.get("is_priority", False))
        remind_at_str = parsed.get("remind_at")
        remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M") if remind_at_str else None
        task_id = db.add_task(
            raw, title, category, due_date,
            is_priority=is_priority,
            recurrence=parsed.get("recurrence"),
            remind_at=remind_at,
        )
        return task_id, parsed.get("error"), raw
    task_id = db.add_task("📎 (attachment)", "📎 Untitled attachment", "Unknown", None, is_priority=False)
    return task_id, None, "📎 (attachment)"


def _update_task_from_caption(task_id: int, caption: str) -> str | None:
    """Re-parse caption and overwrite title/category/date/priority on the task."""
    parsed = claude_client.parse_task(caption.strip())
    due_str = parsed.get("due_date")
    due_date = date.fromisoformat(due_str) if due_str else None
    remind_at_str = parsed.get("remind_at")
    remind_at = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M") if remind_at_str else None
    db.update_task(
        task_id,
        title=parsed["title"],
        category=parsed["category"],
        due_date=due_date,
        is_priority=bool(parsed.get("is_priority", False)),
        recurrence=parsed.get("recurrence"),
        remind_at=remind_at,
    )
    return parsed.get("error")


async def _delayed_album_reply(msg, key: tuple[int, str], task_id: int, parse_error_box: dict):
    """Sleep briefly so sibling album items can attach, then send one combined reply."""
    await asyncio.sleep(2.0)
    MEDIA_BUFFER.pop(key, None)
    task = db.get_task(task_id)
    if not task:
        return
    await _send_added_reply(
        msg.reply_text,
        task,
        parse_error=parse_error_box.get("parse_error"),
        attachment_count=int(task.get("attachment_count") or 0),
    )


async def _process_media(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                         kind: str, file_id: str, file_unique_id: str,
                         file_name: str | None, mime_type: str | None,
                         caption: str | None):
    chat_id = update.effective_chat.id
    msg = update.message
    media_group_id = msg.media_group_id

    if media_group_id:
        key = (chat_id, media_group_id)
        buf = MEDIA_BUFFER.get(key)
        if buf is None:
            buf = {"lock": asyncio.Lock(), "task_id": None, "parse_error": None}
            MEDIA_BUFFER[key] = buf

        first = False
        async with buf["lock"]:
            if buf["task_id"] is None:
                tid, err, _ = _create_task_for_caption(caption)
                buf["task_id"] = tid
                buf["parse_error"] = err
                first = True
            else:
                # If this album item carries the caption, retrofit the task title
                if caption and caption.strip():
                    err = _update_task_from_caption(buf["task_id"], caption)
                    if err and not buf["parse_error"]:
                        buf["parse_error"] = err
            task_id = buf["task_id"]

        db.add_attachment(task_id, file_id, file_unique_id, kind, file_name, mime_type, caption)
        _sync_task_to_notion(task_id)

        if first:
            # Schedule the reply asynchronously so the next album item can be
            # processed immediately even when PTB processes updates sequentially.
            asyncio.create_task(_delayed_album_reply(msg, key, task_id, buf))
        return

    # Single message (no album)
    task_id, parse_error, _ = _create_task_for_caption(caption)
    db.add_attachment(task_id, file_id, file_unique_id, kind, file_name, mime_type, caption)
    _sync_task_to_notion(task_id)
    task = db.get_task(task_id)
    await _send_added_reply(
        msg.reply_text,
        task,
        parse_error=parse_error,
        attachment_count=int(task.get("attachment_count") or 0),
    )


async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if not update.message or not update.message.photo:
        return
    largest = update.message.photo[-1]
    await _process_media(
        update, ctx,
        kind="photo",
        file_id=largest.file_id,
        file_unique_id=largest.file_unique_id,
        file_name=None,
        mime_type=None,
        caption=update.message.caption,
    )


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    doc = update.message.document if update.message else None
    if not doc:
        return
    await _process_media(
        update, ctx,
        kind="document",
        file_id=doc.file_id,
        file_unique_id=doc.file_unique_id,
        file_name=doc.file_name,
        mime_type=doc.mime_type,
        caption=update.message.caption,
    )


# ---------------------------------------------------------------------------
# Callback query handler
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != config.TELEGRAM_CHAT_ID:
        await query.answer("Unauthorized.")
        return
    await query.answer()

    data = query.data or ""

    # Any non-edit button press cancels a pending /edit waiting on this chat.
    if not data.startswith("edit:"):
        EDIT_WAITING.pop(query.message.chat.id, None)

    if data.startswith("edit:"):
        task_id = int(data.split(":", 1)[1])
        await _start_edit(
            query.message.chat.id,
            task_id,
            query.message.reply_text,
        )
        return

    if data.startswith("setcat:"):
        _, raw_id, cat = data.split(":", 2)
        task_id = int(raw_id)
        if cat not in config.CATEGORIES:
            await query.message.reply_text(f"Unknown category: {cat}")
            return
        db.update_task(task_id, category=cat)
        _sync_task_to_notion(task_id)
        task = db.get_task(task_id)
        if not task:
            await query.message.reply_text(f"Task #{task_id} not found.")
            return
        text, markup = _render_added_card(
            task,
            attachment_count=int(task.get("attachment_count") or 0),
            prefix="✅ Updated",
        )
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.warning("edit_message_text (setcat) failed: %s", e)
            await query.message.reply_text(text, reply_markup=markup)
        return

    if data.startswith("act:"):
        _, raw_id, action = data.split(":", 2)
        task_id = int(raw_id)
        task = db.get_task(task_id)
        if not task:
            await query.message.reply_text(f"Task #{task_id} not found.")
            return

        if action == "drop":
            page_id = task.get("notion_page_id")
            db.delete_task(task_id)
            _archive_task_in_notion(page_id)
            text = f"🗑 Deleted #{task_id}: {task['title']}"
            try:
                await query.edit_message_text(text)
            except Exception as e:  # noqa: BLE001
                log.warning("edit_message_text (act:drop) failed: %s", e)
                await query.message.reply_text(text)
            return

        if action == "done":
            transitioned, new_id = db.mark_done_ex(task_id)
            if transitioned:
                _sync_task_to_notion(task_id)
                if new_id is not None:
                    _sync_task_to_notion(new_id)
                    text = (f"✅ Done #{task_id}: {task['title']}\n"
                            f"🔁 Next instance queued as #{new_id}.")
                else:
                    text = f"✅ Done #{task_id}: {task['title']}"
            else:
                text = f"Task #{task_id} was already done."
            try:
                await query.edit_message_text(text)
            except Exception as e:  # noqa: BLE001
                log.warning("edit_message_text (act:done) failed: %s", e)
                await query.message.reply_text(text)
            return

        if action == "pri":
            db.set_priority(task_id, not bool(task.get("is_priority")))
        elif action == "today":
            db.update_task(task_id, due_date=date.today())
        elif action == "tomorrow":
            db.update_task(task_id, due_date=date.today() + timedelta(days=1))
        elif action.startswith("defer"):
            try:
                days = int(action[len("defer"):]) or 1
            except ValueError:
                days = 1
            db.defer_task(task_id, days)
        elif action == "snz1h":
            db.update_task(
                task_id,
                remind_at=datetime.now() + timedelta(hours=1),
                remind_sent=False,
            )
        elif action == "snzAM":
            tomorrow_8 = datetime.combine(date.today() + timedelta(days=1),
                                          datetime.min.time().replace(hour=8))
            db.update_task(task_id, remind_at=tomorrow_8, remind_sent=False)
        else:
            await query.message.reply_text(f"Unknown action: {action}")
            return

        _sync_task_to_notion(task_id)
        new_task = db.get_task(task_id)
        if not new_task:
            return
        text, markup = _render_added_card(
            new_task,
            attachment_count=int(new_task.get("attachment_count") or 0),
            prefix="✅ Updated",
        )
        try:
            await query.edit_message_text(text, reply_markup=markup)
        except Exception as e:  # noqa: BLE001
            log.warning("edit_message_text (act) failed: %s", e)
            await query.message.reply_text(text, reply_markup=markup)
        return

    if data.startswith("undo:"):
        ids_str = data.split(":", 1)[1]
        ids = [int(x) for x in ids_str.split(",") if x.strip().isdigit()]
        reopened: list[str] = []
        for tid in ids:
            if db.mark_open(tid):
                _sync_task_to_notion(tid)
                t = db.get_task(tid)
                if t:
                    reopened.append(t["title"])
        if reopened:
            text = "↩️ Reopened:\n" + "\n".join(f"  • {t}" for t in reopened)
        else:
            text = "Nothing to undo."
        try:
            await query.edit_message_text(text)  # also drops the button
        except Exception as e:  # noqa: BLE001
            log.warning("edit_message_text (undo) failed: %s", e)
            await query.message.reply_text(text)
        return

    if data.startswith("nextagain:"):
        prev = int(data.split(":", 1)[1])
        excluded = set(ctx.user_data.get("next_excluded", set()))
        excluded.add(prev)
        tasks = db.get_open_tasks()
        candidates = [t for t in tasks if t["id"] not in excluded]
        if not candidates:
            ctx.user_data["next_excluded"] = set()
            try:
                await query.edit_message_text("Nothing left to suggest. /list to see everything.")
            except Exception:  # noqa: BLE001
                await query.message.reply_text("Nothing left to suggest.")
            return
        pick = max(candidates, key=_score_task)
        excluded.add(pick["id"])
        ctx.user_data["next_excluded"] = excluded
        try:
            await query.edit_message_text(
                _format_next_card(pick),
                reply_markup=_pick_next_keyboard(pick["id"]),
            )
        except Exception as e:  # noqa: BLE001
            log.warning("edit_message_text (nextagain) failed: %s", e)
            await query.message.reply_text(
                _format_next_card(pick),
                reply_markup=_pick_next_keyboard(pick["id"]),
            )
        return

    if data.startswith("filter:"):
        cat = data.split(":", 1)[1]
        if cat not in config.CATEGORIES:
            await query.message.reply_text(f"Unknown category: {cat}")
            return
        results = db.get_tasks_by_category(cat)
        ctx.user_data["task_list"] = results
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        msg = build_task_list(results, header=f"📂 {emoji} {cat}:")
        if results:
            msg += "\n\nReply with task IDs to mark done."
        try:
            await query.edit_message_text(msg)
        except Exception as e:  # noqa: BLE001
            log.warning("edit_message_text (filter) failed: %s", e)
            await query.message.reply_text(msg)
        return


# ---------------------------------------------------------------------------
# Unified text handler
# ---------------------------------------------------------------------------

async def text_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if update.effective_chat.id in EDIT_WAITING:
        handled = await handle_edit_reply(update, ctx)
        if handled:
            return
    await handle_text(update, ctx)


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

async def _post_init(app):
    await app.bot.set_my_commands([
        BotCommand("list", "Show open tasks"),
        BotCommand("listtext", "Compact text-only task list"),
        BotCommand("done", "Mark tasks as done"),
        BotCommand("search", "Search tasks"),
        BotCommand("filter", "Filter by category"),
        BotCommand("history", "Recently completed"),
        BotCommand("show", "Show task detail"),
        BotCommand("edit", "Edit a task"),
        BotCommand("defer", "Push due date later"),
        BotCommand("priority", "Toggle ⭐ priority"),
        BotCommand("snooze", "Snooze a reminder"),
        BotCommand("next", "Suggest what to do right now"),
        BotCommand("drop", "Delete a task"),
        BotCommand("stats", "Counts per category"),
        BotCommand("sync", "Pull from Notion"),
        BotCommand("pushnotion", "Push tasks to Notion"),
        BotCommand("menu", "Show keyboard menu"),
        BotCommand("help", "Show all commands"),
        BotCommand("cancel", "Cancel /edit"),
    ])


def main():
    db.init_db()
    app = (
        ApplicationBuilder()
        .token(config.TELEGRAM_TOKEN)
        .post_init(_post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("menu", cmd_menu))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("listtext", cmd_listtext))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("drop", cmd_drop))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CommandHandler("pushnotion", cmd_pushnotion))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("filter", cmd_filter))
    app.add_handler(CommandHandler("history", cmd_history))
    app.add_handler(CommandHandler("show", cmd_show))
    app.add_handler(CommandHandler("defer", cmd_defer))
    app.add_handler(CommandHandler("priority", cmd_priority))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
