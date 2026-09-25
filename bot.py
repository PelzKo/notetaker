import asyncio
import logging
import os
import re
import traceback
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
from parsing import (
    CLEAR_WORDS,
    DATE_FORMATS_HELP,
    DATETIME_FORMATS_HELP,
    RECURRENCE_HELP,
    match_category,
    normalize_recurrence,
    parse_date,
    parse_datetime,
    parse_days,
    split_message,
)
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


def _parse_id_args(args: list[str]) -> list[int] | None:
    """'/drop 3 4,5' → [3, 4, 5]. None if empty or any part isn't a number."""
    parts = _split_ids(" ".join(args))
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return list(dict.fromkeys(int(p) for p in parts))


async def _reply_long(message, text: str, reply_markup=None) -> None:
    """reply_text that splits over Telegram's length limit; markup goes on the last chunk."""
    chunks = split_message(text)
    for i, chunk in enumerate(chunks):
        await message.reply_text(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)


async def _edit_long(query, text: str, reply_markup=None) -> None:
    """Edit a callback's message with the first chunk, send the rest as new messages."""
    chunks = split_message(text)
    first_markup = reply_markup if len(chunks) == 1 else None
    try:
        await query.edit_message_text(chunks[0], reply_markup=first_markup)
    except Exception as e:  # noqa: BLE001
        log.warning("edit_message_text failed: %s", e)
        await query.message.reply_text(chunks[0], reply_markup=first_markup)
    for i, chunk in enumerate(chunks[1:], 1):
        await query.message.reply_text(chunk, reply_markup=reply_markup if i == len(chunks) - 1 else None)

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

def _edit_buttons(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✏️ Title", callback_data=f"editf:{task_id}:title"),
        InlineKeyboardButton("📅 Date", callback_data=f"editf:{task_id}:date"),
        InlineKeyboardButton("📂 Category", callback_data=f"editf:{task_id}:category"),
    ]])


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
            InlineKeyboardButton("✏️ Title", callback_data=f"editf:{task_id}:title"),
            InlineKeyboardButton("❌ Drop", callback_data=f"act:{task_id}:drop"),
        ],
    ]


def _category_picker_markup(task_id: int) -> InlineKeyboardMarkup:
    rows = _category_picker_rows(task_id)
    rows.append([InlineKeyboardButton("✏️ Title", callback_data=f"editf:{task_id}:title")])
    return InlineKeyboardMarkup(rows)


def _undo_markup(task_ids: list[int]) -> InlineKeyboardMarkup:
    csv = ",".join(str(i) for i in task_ids)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Undo", callback_data=f"undo:{csv}")
    ]])


def _undrop_markup(task_ids: list[int]) -> InlineKeyboardMarkup:
    csv = ",".join(str(i) for i in task_ids)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("↩️ Undo", callback_data=f"undrop:{csv}")
    ]])


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

    task_id = db.add_task(
        raw, title, category, due_date,
        is_priority=is_priority,
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

    session_ids = {t["id"] for t in session}
    ids = [int(x) for x in _split_ids(text) if x.isdigit()]
    ctx.user_data["task_list"] = []  # clear session
    await _mark_done_ids(update.message, ids, allowed=session_ids)


async def _mark_done_ids(message, ids: list[int], *, allowed: set[int] | None = None):
    """Mark tasks done and reply with a summary + undo button.
    If `allowed` is given, IDs outside it are rejected as 'not in list'."""
    marked: list[tuple[int, str]] = []  # (task_id, title)
    failed: list[str] = []
    for task_id in ids:
        if allowed is not None and task_id not in allowed:
            failed.append(f"#{task_id} (not in list)")
            continue
        task = db.get_task(task_id)
        if not task:
            failed.append(f"#{task_id} (not found)")
            continue
        transitioned, new_id = db.mark_done_ex(task_id)
        if transitioned:
            marked.append((task_id, task["title"]))
            _sync_task_to_notion(task_id)
            if new_id is not None:
                _sync_task_to_notion(new_id)
        else:
            failed.append(f"{task['title']} (already done)")

    lines = []
    if marked:
        lines.append("✅ Done:\n" + "\n".join(f"  • {t}" for _, t in marked))
    if failed:
        lines.append("⚠️ Couldn't mark:\n" + "\n".join(f"  • {t}" for t in failed))
    text_out = "\n\n".join(lines) or "Nothing changed."

    reply_markup = _undo_markup([tid for tid, _ in marked]) if marked else None
    await _reply_long(message, text_out, reply_markup=reply_markup)


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
        await _reply_long(update.message, build_task_list_simple(tasks))
        return
    ctx.user_data["task_list"] = tasks
    msg = build_task_list_sectioned(tasks)
    if tasks:
        msg += "\n\nReply with task IDs to mark done."
    await _reply_long(update.message, msg)


async def cmd_listtext(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_open_tasks()
    ctx.user_data["task_list"] = []
    await _reply_long(update.message, build_task_list_simple(tasks))


# ---------------------------------------------------------------------------
# /done
# ---------------------------------------------------------------------------

async def cmd_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if args:
        ids = _parse_id_args(args)
        if ids is None:
            await update.message.reply_text("Usage: /done [task_id ...]  e.g. /done 42 43")
            return
        await _mark_done_ids(update.message, ids)
        return
    tasks = db.get_open_tasks()
    if not tasks:
        await update.message.reply_text("✅ No open tasks.")
        return
    ctx.user_data["task_list"] = tasks
    msg = build_task_list_sectioned(tasks, "Which tasks are done? Reply with task IDs:")
    await _reply_long(update.message, msg)


# ---------------------------------------------------------------------------
# /drop <id ...>
# ---------------------------------------------------------------------------

_DROPPED_MAX = 50  # how many deleted-task snapshots to keep for undo


def _drop_task(ctx: ContextTypes.DEFAULT_TYPE, task: dict) -> None:
    """Delete a task, keeping a snapshot in user_data so it can be undone."""
    dropped: dict = ctx.user_data.setdefault("dropped", {})
    dropped[task["id"]] = (task, db.get_attachments(task["id"]))
    while len(dropped) > _DROPPED_MAX:
        dropped.pop(next(iter(dropped)))
    db.delete_task(task["id"])
    _archive_task_in_notion(task.get("notion_page_id"))


async def cmd_drop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    ids = _parse_id_args(ctx.args or [])
    if ids is None:
        await update.message.reply_text("Usage: /drop <task_id ...>  e.g. /drop 42 43")
        return
    deleted: list[tuple[int, str]] = []
    missing: list[int] = []
    for task_id in ids:
        task = db.get_task(task_id)
        if not task:
            missing.append(task_id)
            continue
        _drop_task(ctx, task)
        deleted.append((task_id, task["title"]))
    lines = []
    if deleted:
        lines.append("🗑 Deleted:\n" + "\n".join(f"  • #{tid} {t}" for tid, t in deleted))
    if missing:
        lines.append("⚠️ Not found: " + ", ".join(f"#{i}" for i in missing))
    markup = _undrop_markup([tid for tid, _ in deleted]) if deleted else None
    await _reply_long(update.message, "\n\n".join(lines), reply_markup=markup)


# ---------------------------------------------------------------------------
# /edit <id> [field value]
# ---------------------------------------------------------------------------

# chat_id → (task_id, field, started_at); field=None means full re-parse mode
EDIT_WAITING: dict[int, tuple[int, str | None, datetime]] = {}

_PRIORITY_ON = ("on", "true", "1", "yes", "y")
_PRIORITY_OFF = ("off", "false", "0", "no", "n")


def _normalize_field(field: str) -> str:
    return "date" if field == "due" else field


def _updated_text(task: dict) -> str:
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    lines = [
        f"✅ Updated #{task['id']}:",
        f"📌 {task['title']}",
        f"{emoji} {task['category']}",
    ]
    if task.get("is_priority"):
        lines.append("⭐ priority")
    lines.append(f"📅 {fmt_date(task['due_date'])}")
    return "\n".join(lines)


async def _start_edit(chat_id: int, task_id: int, reply_fn) -> None:
    task = db.get_task(task_id)
    if not task:
        await reply_fn(f"Task #{task_id} not found.")
        return
    EDIT_WAITING[chat_id] = (task_id, None, datetime.now())
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    await reply_fn(
        f"Editing #{task_id}: {task['title']}\n"
        f"Current: {emoji} {task['category']} — {fmt_date(task['due_date'])}\n\n"
        "Send new text describing the task again (I'll re-parse it), "
        "or send just a category name to change only the category, "
        "or /cancel to abort."
    )


async def _start_field_edit(chat_id: int, task_id: int, field: str, reply_fn) -> None:
    """Ask for a new value of one field; the next text message is taken as that value."""
    field = _normalize_field(field)
    task = db.get_task(task_id)
    if not task:
        await reply_fn(f"Task #{task_id} not found.")
        return
    EDIT_WAITING[chat_id] = (task_id, field, datetime.now())
    markup = None
    header = f"Editing #{task_id}: {task['title']}\n\n"
    if field == "title":
        body = "Send the new title."
    elif field == "date":
        body = (f"Current due date: {fmt_date(task['due_date'])}\n"
                "Send the new due date, or 'none' to clear it.\n\n" + DATE_FORMATS_HELP)
    elif field == "category":
        emoji = CATEGORY_EMOJI.get(task["category"], "📌")
        body = (f"Current category: {emoji} {task['category']}\n"
                "Tap a category or send its name (case doesn't matter, a prefix like 'pers' works): "
                + ", ".join(config.CATEGORIES))
        markup = InlineKeyboardMarkup(_category_picker_rows(task_id))
    else:  # priority
        body = f"Priority is {'on ⭐' if task.get('is_priority') else 'off'}. Send 'on' or 'off'."
    await reply_fn(header + body + "\n\n/cancel to abort.", reply_markup=markup)


async def _edit_field(update: Update, task_id: int, field: str, value: str,
                      *, retry_hint: bool = False) -> bool:
    """Apply a single-field edit. Returns False if the value was invalid."""
    field = _normalize_field(field)
    retry = "\n\nSend another value, or /cancel." if retry_hint else ""
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return True  # nothing left to retry

    value = value.strip()
    if field == "title":
        if not value:
            await update.message.reply_text("Need a title value." + retry)
            return False
        db.update_task(task_id, title=value[:500])
    elif field == "category":
        cat = match_category(value)
        if cat is None:
            await update.message.reply_text(
                "Unknown category. Pick one of: " + ", ".join(config.CATEGORIES) + retry
            )
            return False
        db.update_task(task_id, category=cat)
    elif field == "date":
        if value.lower() in CLEAR_WORDS:
            d = None
        else:
            d = parse_date(value)
            if d is None:
                await update.message.reply_text(
                    f"Couldn't parse date '{value}'.\n\n{DATE_FORMATS_HELP}\n• none — clear the date" + retry
                )
                return False
        db.update_task(task_id, due_date=d)
    elif field == "priority":
        v = value.lower()
        if v in _PRIORITY_ON:
            db.update_task(task_id, is_priority=True)
        elif v in _PRIORITY_OFF:
            db.update_task(task_id, is_priority=False)
        else:
            await update.message.reply_text("Use: on or off" + retry)
            return False

    _sync_task_to_notion(task_id)
    await update.message.reply_text(_updated_text(db.get_task(task_id)))
    return True


EDIT_USAGE = (
    "Usage:\n"
    "/edit <id> — re-parse the task from new text\n"
    "/edit <id> <field> — I'll ask for the new value\n"
    "/edit <id> <field> <value> — set it directly\n"
    "Fields: title, date, category, priority"
)


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(EDIT_USAGE)
        return
    task_id = int(args[0])
    if len(args) >= 2:
        field = args[1].lower()
        if field not in EDIT_FIELDS:
            await update.message.reply_text(f"Unknown field '{args[1]}'.\n\n{EDIT_USAGE}")
            return
        value = " ".join(args[2:])
        if value.strip():
            await _edit_field(update, task_id, field, value)
        else:
            await _start_field_edit(update.effective_chat.id, task_id, field,
                                    update.message.reply_text)
        return
    await _start_edit(
        update.effective_chat.id,
        task_id,
        update.message.reply_text,
    )


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    if EDIT_WAITING.pop(update.effective_chat.id, None) is None:
        await update.message.reply_text("Nothing to cancel.")
        return
    await update.message.reply_text("Cancelled.")


async def handle_edit_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    entry = EDIT_WAITING.get(chat_id)
    if entry is None:
        return False
    task_id, field, started = entry
    if datetime.now() - started > EDIT_TTL:
        EDIT_WAITING.pop(chat_id, None)
        await update.message.reply_text("⏱️ Edit session expired. Please run /edit again.")
        return True
    EDIT_WAITING.pop(chat_id, None)

    text = update.message.text.strip()

    if field is not None:
        ok = await _edit_field(update, task_id, field, text, retry_hint=True)
        if not ok:
            EDIT_WAITING[chat_id] = (task_id, field, datetime.now())  # keep waiting
        return True

    cat = match_category(text)
    if cat is not None:
        db.update_task(task_id, category=cat)
        _sync_task_to_notion(task_id)
        await update.message.reply_text(f"✅ Category updated to {cat}.")
        return True

    await update.message.reply_text("⏳ Re-parsing…")
    parsed = claude_client.parse_task(text)
    due_date = date.fromisoformat(parsed["due_date"]) if parsed.get("due_date") else None
    db.update_task(
        task_id,
        title=parsed["title"],
        category=parsed["category"],
        due_date=due_date,
        is_priority=bool(parsed.get("is_priority", False)),
    )
    _sync_task_to_notion(task_id)
    await update.message.reply_text(_updated_text(db.get_task(task_id)))
    return True


# ---------------------------------------------------------------------------
# /defer <id> [days|date]
# ---------------------------------------------------------------------------

async def cmd_defer(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /defer <task_id> [3d|date] (default 1d)\n"
            "3d pushes the current due date back 3 days; a date sets it directly.\n\n"
            + DATE_FORMATS_HELP
        )
        return
    task_id = int(args[0])
    spec = " ".join(args[1:]) or "1d"
    days = parse_days(spec)
    if days is not None:
        new_due = db.defer_task(task_id, days)
    else:
        new_due = parse_date(spec)
        if new_due is None:
            await update.message.reply_text(f"Couldn't parse '{spec}'.\n\n{DATE_FORMATS_HELP}")
            return
        if db.get_task(task_id) is not None:
            db.update_task(task_id, due_date=new_due)
        else:
            new_due = None
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
    if s in _SNOOZE_WEEKDAYS:
        target = _SNOOZE_WEEKDAYS.index(s)
        d = date.today() + timedelta(days=1)
        while d.weekday() != target:
            d += timedelta(days=1)
        return datetime.combine(d, datetime.min.time().replace(hour=8))
    return parse_datetime(s)


async def cmd_snooze(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /snooze <task_id> <1h|30m|tomorrow|mon|tue|...|date [HH:MM]>"
        )
        return
    task_id = int(args[0])
    new_remind = _parse_snooze_duration(" ".join(args[1:]))
    if new_remind is None:
        await update.message.reply_text(
            "Couldn't parse duration. Also accepted: mon, tue, … (next weekday, 08:00)\n\n"
            + DATETIME_FORMATS_HELP
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


async def cmd_remind(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /remind <task_id> <when|off>\n"
            "Examples: /remind 42 tomorrow 9:00 · /remind 42 15.10. 14:30 · "
            "/remind 42 2h · /remind 42 off\n\n" + DATETIME_FORMATS_HELP
        )
        return
    task_id = int(args[0])
    spec = " ".join(args[1:]).strip().lower()
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    if spec == "off":
        db.update_task(task_id, remind_at=None, remind_sent=False)
        await update.message.reply_text(f"⏰ Reminder cleared for #{task_id}.")
        return
    remind_at = parse_datetime(spec)
    if remind_at is None:
        await update.message.reply_text(f"Couldn't parse '{spec}'.\n\n{DATETIME_FORMATS_HELP}")
        return
    if remind_at <= datetime.now():
        await update.message.reply_text(
            f"⚠️ {remind_at.strftime('%a %d %b %H:%M')} is in the past — pick a future time."
        )
        return
    db.update_task(task_id, remind_at=remind_at, remind_sent=False)
    await update.message.reply_text(
        f"⏰ Reminder set for #{task_id}: {remind_at.strftime('%a %d %b %H:%M')}."
    )


async def cmd_repeat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "Usage: /repeat <task_id> <pattern|off>\n"
            "Example: /repeat 42 weekly:mon · /repeat 42 every 2 weeks · /repeat 42 off\n\n"
            + RECURRENCE_HELP
        )
        return
    task_id = int(args[0])
    raw_pattern = " ".join(args[1:]).strip()
    task = db.get_task(task_id)
    if not task:
        await update.message.reply_text(f"Task #{task_id} not found.")
        return
    if raw_pattern.lower() == "off":
        db.update_task(task_id, recurrence=None)
        _sync_task_to_notion(task_id)
        await update.message.reply_text(f"🔁 Recurrence cleared for #{task_id}.")
        return
    pattern = normalize_recurrence(raw_pattern)
    if pattern is None:
        await update.message.reply_text(f"Invalid pattern '{raw_pattern}'.\n\n{RECURRENCE_HELP}")
        return
    db.update_task(task_id, recurrence=pattern)
    _sync_task_to_notion(task_id)
    await update.message.reply_text(f"🔁 #{task_id} repeats: {pattern}.")


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
    await _reply_long(update.message, msg)


# ---------------------------------------------------------------------------
# /filter [category]
# ---------------------------------------------------------------------------

async def cmd_filter(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args or []
    cat = match_category(" ".join(args)) if args else None
    if args and cat is None:
        await update.message.reply_text(
            f"Unknown category '{' '.join(args)}'. Pick one of: " + ", ".join(config.CATEGORIES)
        )
        return
    if cat:
        results = db.get_tasks_by_category(cat)
        ctx.user_data["task_list"] = results
        emoji = CATEGORY_EMOJI.get(cat, "📌")
        msg = build_task_list(results, header=f"📂 {emoji} {cat}:")
        if results:
            msg += "\n\nReply with task IDs to mark done."
        await _reply_long(update.message, msg)
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
    spec = " ".join(ctx.args or []).strip()
    days = 7
    if spec:
        days = parse_days(spec)
        if days is None:
            since = parse_date(spec)
            if since is None or since > date.today():
                await update.message.reply_text(
                    "Usage: /history [7d|date] — last N days, or everything since a past date.\n\n"
                    + DATE_FORMATS_HELP
                )
                return
            days = (date.today() - since).days + 1
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
    await _reply_long(update.message, "\n".join(lines).rstrip())


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
    await _reply_long(update.message, fmt_task_detail(task), reply_markup=_edit_buttons(task_id))

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
    "/done [id ...] — mark done (no IDs: pick from list)\n"
    "/edit <id> — re-parse from new text\n"
    "/edit <id> <field> — I'll ask for the new value\n"
    "/edit <id> <field> <value> — set title|category|date|priority\n"
    "/defer <id> [3d|date] — push due date back (default 1d) or set it\n"
    "/priority <id> [on|off] — toggle ⭐\n"
    "/remind <id> <when|off> — e.g. tomorrow 9:00, 15.10. 14:30, 2h\n"
    "/repeat <id> <pattern|off> — daily, weekday, weekly:mon, monthly:15, every 2 weeks, every 3 months\n"
    "/snooze <id> <1h|30m|tomorrow|mon|date> — push reminder forward\n"
    "/next — suggest what to do right now\n"
    "/drop <id ...> — delete (with undo)\n\n"
    "Dates: 3d, 15.10.2026, 15.10., 2026-10-15, today, tomorrow, next month\n"
    "Categories are case-insensitive; a prefix like 'pers' works.\n\n"
    "Notion\n"
    "/sync — pull changes from Notion\n"
    "/pushnotion — push DB tasks to Notion\n\n"
    "Misc\n"
    "/menu — show keyboard\n"
    "/cancel — abort a pending /edit"
)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await _reply_long(update.message, HELP_TEXT)


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
        await _reply_long(
            update.message, "✅ Synced from Notion:\n" + "\n".join(f"• {c}" for c in changes)
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
        task_id = db.add_task(
            raw, title, category, due_date,
            is_priority=is_priority,
        )
        return task_id, parsed.get("error"), raw
    task_id = db.add_task("📎 (attachment)", "📎 Untitled attachment", "Unknown", None, is_priority=False)
    return task_id, None, "📎 (attachment)"


def _update_task_from_caption(task_id: int, caption: str) -> str | None:
    """Re-parse caption and overwrite title/category/date/priority on the task."""
    parsed = claude_client.parse_task(caption.strip())
    due_str = parsed.get("due_date")
    due_date = date.fromisoformat(due_str) if due_str else None
    db.update_task(
        task_id,
        title=parsed["title"],
        category=parsed["category"],
        due_date=due_date,
        is_priority=bool(parsed.get("is_priority", False)),
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
    if not data.startswith(("edit:", "editf:")):
        EDIT_WAITING.pop(query.message.chat.id, None)

    if data.startswith("editf:"):
        _, raw_id, field = data.split(":", 2)
        if field not in EDIT_FIELDS:
            await query.message.reply_text(f"Unknown field: {field}")
            return
        await _start_field_edit(
            query.message.chat.id,
            int(raw_id),
            field,
            query.message.reply_text,
        )
        return

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
            _drop_task(ctx, task)
            text = f"🗑 Deleted #{task_id}: {task['title']}"
            markup = _undrop_markup([task_id])
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except Exception as e:  # noqa: BLE001
                log.warning("edit_message_text (act:drop) failed: %s", e)
                await query.message.reply_text(text, reply_markup=markup)
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

    if data.startswith("undrop:"):
        ids = [int(x) for x in data.split(":", 1)[1].split(",") if x.strip().isdigit()]
        dropped: dict = ctx.user_data.get("dropped", {})
        restored: list[str] = []
        failed: list[str] = []
        for tid in ids:
            snap = dropped.pop(tid, None)
            if snap is None:
                failed.append(f"#{tid} (no longer in memory)")
                continue
            task, attachments = snap
            if db.restore_task(task, attachments):
                _sync_task_to_notion(tid)
                restored.append(f"#{tid} {task['title']}")
            else:
                failed.append(f"#{tid} (ID already in use)")
        lines = []
        if restored:
            lines.append("↩️ Restored:\n" + "\n".join(f"  • {t}" for t in restored))
        if failed:
            lines.append("⚠️ Couldn't restore:\n" + "\n".join(f"  • {t}" for t in failed))
        await _edit_long(query, "\n\n".join(lines) or "Nothing to undo.")
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
        await _edit_long(query, msg)
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
# Fallbacks: unknown commands and unhandled errors
# ---------------------------------------------------------------------------

async def cmd_unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    cmd = (update.message.text or "").split()[0]
    await update.message.reply_text(f"Unknown command {cmd}. Type /help for the full list.")


def _error_location(err: BaseException) -> str | None:
    """'bot.py:123 in cmd_edit' for the innermost traceback frame inside this project."""
    project_dir = os.path.dirname(os.path.abspath(__file__))
    frames = [f for f in traceback.extract_tb(err.__traceback__)
              if os.path.abspath(f.filename).startswith(project_dir + os.sep)
              and f"{os.sep}venv{os.sep}" not in os.path.abspath(f.filename)]
    if not frames:
        return None
    f = frames[-1]
    return f"{os.path.basename(f.filename)}:{f.lineno} in {f.name}"


async def _on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    err = ctx.error
    log.error("Unhandled error while processing update", exc_info=err)
    lines = ["⚠️ Something went wrong.", "", f"Error: {type(err).__name__}: {str(err)[:1500] or '(no message)'}"]
    where = _error_location(err)
    if where:
        lines.append(f"Where: {where}")
    if isinstance(update, Update):
        if update.message and update.message.text:
            lines.append(f"While handling: {update.message.text[:200]}")
        elif update.callback_query:
            lines.append(f"While handling button: {update.callback_query.data}")
    lines += ["", "Please try again."]
    try:
        await ctx.bot.send_message(config.TELEGRAM_CHAT_ID, "\n".join(lines))
    except Exception as e:  # noqa: BLE001
        log.warning("Failed to send error notice: %s", e)


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
        BotCommand("remind", "Set or clear a reminder"),
        BotCommand("repeat", "Set or clear recurrence"),
        BotCommand("snooze", "Snooze a reminder"),
        BotCommand("next", "Suggest what to do right now"),
        BotCommand("drop", "Delete task(s)"),
        BotCommand("stats", "Counts per category"),
        BotCommand("sync", "Pull from Notion"),
        BotCommand("pushnotion", "Push tasks to Notion"),
        BotCommand("menu", "Show keyboard menu"),
        BotCommand("help", "Show all commands"),
        BotCommand("cancel", "Cancel a pending /edit"),
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
    app.add_handler(CommandHandler("remind", cmd_remind))
    app.add_handler(CommandHandler("repeat", cmd_repeat))
    app.add_handler(CommandHandler("snooze", cmd_snooze))
    app.add_handler(CommandHandler("next", cmd_next))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))
    app.add_handler(MessageHandler(filters.COMMAND, cmd_unknown))
    app.add_error_handler(_on_error)

    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
