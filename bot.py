import logging
from datetime import date

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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
from formatting import CATEGORY_EMOJI, fmt_date, fmt_task_line, build_task_list

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger(__name__)

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


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    await update.message.reply_text(
        "👋 Todo bot ready.\n\n"
        "Just send me any text to add a task.\n"
        "Commands:\n"
        "/list — show all open tasks\n"
        "/done — mark tasks complete\n"
        "/drop <id> — delete a task\n"
        "/edit <id> — edit a task\n"
        "/stats — counts per category\n"
        "/sync — pull changes from Notion"
    )


# ---------------------------------------------------------------------------
# Free-text → add task
# ---------------------------------------------------------------------------

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return

    raw = update.message.text.strip()

    # Check if this looks like a "mark done" reply (numbers only, e.g. "1 3")
    if all(part.isdigit() for part in raw.split()):
        await _handle_done_reply(update, ctx, raw)
        return

    await update.message.reply_text("⏳ Parsing…")

    parsed = claude_client.parse_task(raw)
    title = parsed["title"]
    category = parsed["category"]
    due_date_str = parsed.get("due_date")
    due_date = date.fromisoformat(due_date_str) if due_date_str else None
    parse_error = parsed.get("error")

    task_id = db.add_task(raw, title, category, due_date)
    _sync_task_to_notion(task_id)

    emoji = CATEGORY_EMOJI.get(category, "📌")
    lines = [
        f"✅ Added (#{task_id}):",
        f"📌 {title}",
        f"{emoji} {category}",
        f"📅 {fmt_date(due_date)}",
    ]
    if parse_error:
        lines.append(f"\n⚠️ Parse warning: {parse_error}")

    if category == "Unknown":
        lines.append(f"\n❓ Couldn't detect category — tap the button or use /edit {task_id} to fix.")
        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=_edit_button(task_id),
        )
    else:
        await update.message.reply_text("\n".join(lines))


async def _handle_done_reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE, text: str):
    """Handle a reply like '1 3' that marks numbered tasks done."""
    session = ctx.user_data.get("task_list", [])
    if not session:
        await update.message.reply_text(
            "No active task list in memory. Use /done to get a fresh list."
        )
        return

    indices = [int(x) for x in text.split() if x.isdigit()]
    marked = []
    failed = []
    for idx in indices:
        if 1 <= idx <= len(session):
            task = session[idx - 1]
            if db.mark_done(task["id"]):
                marked.append(task["title"])
                _sync_task_to_notion(task["id"])
            else:
                failed.append(task["title"])
        else:
            failed.append(f"#{idx} (out of range)")

    ctx.user_data["task_list"] = []  # clear session

    lines = []
    if marked:
        lines.append("✅ Done:\n" + "\n".join(f"  • {t}" for t in marked))
    if failed:
        lines.append("⚠️ Couldn't mark:\n" + "\n".join(f"  • {t}" for t in failed))
    await update.message.reply_text("\n\n".join(lines) or "Nothing changed.")


# ---------------------------------------------------------------------------
# /list
# ---------------------------------------------------------------------------

async def cmd_list(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    tasks = db.get_open_tasks()
    ctx.user_data["task_list"] = tasks
    msg = build_task_list(tasks)
    if tasks:
        msg += "\n\nReply with numbers to mark done (e.g. '1 3')."
    await update.message.reply_text(msg)


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
    msg = build_task_list(tasks, "Which tasks are done? Reply with numbers (e.g. '1 3'):")
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
# /edit <id>
# ---------------------------------------------------------------------------

EDIT_WAITING = {}  # simple in-memory state: chat_id → task_id


async def _start_edit(chat_id: int, task_id: int, reply_fn) -> None:
    """Shared logic for entering edit mode (used by command and callback)."""
    task = db.get_task(task_id)
    if not task:
        await reply_fn(f"Task #{task_id} not found.")
        return
    EDIT_WAITING[chat_id] = task_id
    emoji = CATEGORY_EMOJI.get(task["category"], "📌")
    await reply_fn(
        f"Editing #{task_id}: {task['title']}\n"
        f"Current: {emoji} {task['category']} — {fmt_date(task['due_date'])}\n\n"
        "Send new text describing the task again (I'll re-parse it), "
        "or send just a category name to change only the category, "
        "or /cancel to abort."
    )


async def cmd_edit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _authorized(update):
        return
    args = ctx.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("Usage: /edit <task_id>")
        return
    task_id = int(args[0])
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
    """Called when user sends text while an edit is pending."""
    chat_id = update.effective_chat.id
    task_id = EDIT_WAITING.pop(chat_id, None)
    if task_id is None:
        return False  # not in edit mode

    text = update.message.text.strip()

    # If they just sent a category name
    from config import CATEGORIES
    if text in CATEGORIES:
        db.update_task(task_id, category=text)
        _sync_task_to_notion(task_id)
        await update.message.reply_text(f"✅ Category updated to {text}.")
        return True

    # Re-parse full text
    await update.message.reply_text("⏳ Re-parsing…")
    parsed = claude_client.parse_task(text)
    due_date = date.fromisoformat(parsed["due_date"]) if parsed.get("due_date") else None
    db.update_task(task_id, title=parsed["title"], category=parsed["category"], due_date=due_date)
    _sync_task_to_notion(task_id)

    emoji = CATEGORY_EMOJI.get(parsed["category"], "📌")
    await update.message.reply_text(
        f"✅ Updated #{task_id}:\n"
        f"📌 {parsed['title']}\n"
        f"{emoji} {parsed['category']}\n"
        f"📅 {fmt_date(due_date)}"
    )
    return True


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
        await update.message.reply_text("⚠️ Notion is not configured (missing NOTION_API_KEY / NOTION_DATABASE_ID).")
        return
    await update.message.reply_text("🔄 Pulling changes from Notion…")
    changes = notion.sync_from_notion()
    if changes:
        await update.message.reply_text("✅ Synced from Notion:\n" + "\n".join(f"• {c}" for c in changes))
    else:
        await update.message.reply_text("✅ Nothing to sync — Notion is up to date.")


# ---------------------------------------------------------------------------
# Callback query handler (inline buttons)
# ---------------------------------------------------------------------------

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != config.TELEGRAM_CHAT_ID:
        await query.answer("Unauthorized.")
        return
    await query.answer()

    data = query.data or ""
    if data.startswith("edit:"):
        task_id = int(data.split(":", 1)[1])
        await _start_edit(
            query.message.chat.id,
            task_id,
            query.message.reply_text,
        )


# ---------------------------------------------------------------------------
# Unified text handler (routes edit replies first, then free-text add)
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

def main():
    db.init_db()
    app = ApplicationBuilder().token(config.TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("done", cmd_done))
    app.add_handler(CommandHandler("drop", cmd_drop))
    app.add_handler(CommandHandler("edit", cmd_edit))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("sync", cmd_sync))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    log.info("Bot starting…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
