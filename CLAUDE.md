# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A personal Telegram bot that manages a todo list using natural language. Free-text messages are parsed by Claude (Haiku) into structured tasks stored in MariaDB. A daily cron job sends a morning summary with Google Calendar events and an AI priority tip. Tasks optionally sync bidirectionally with a Notion database.

## Running and testing

```bash
# Activate virtualenv and load env vars
source venv/bin/activate
set -a; source .env; set +a

# Run the bot (interactive, Ctrl+C to stop)
python bot.py

# Trigger the daily summary manually
python scheduler.py

# Test calendar auth (one-time OAuth, needs a browser)
python google_calendar.py
```

There are no automated tests. Verify changes by running the bot manually and sending Telegram messages.

## Environment setup

Copy `.env.example` to `.env` and fill in values. Required vars: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `ANTHROPIC_API_KEY`, `DB_PASSWORD`. Optional (Notion sync): `NOTION_API_KEY`, `NOTION_DATABASE_ID`.

Google Calendar requires `credentials.json` and `token.json` in the project root (generated via one-time OAuth flow described in README).

## Architecture

The bot is single-process, long-polling. All modules import `config` at startup which reads env vars — no config objects are passed around.

| File | Role |
|------|------|
| `bot.py` | Telegram handlers, entry point (`python bot.py`) |
| `config.py` | Reads env vars, defines `CATEGORIES` list |
| `db.py` | All MariaDB access via PyMySQL; opens a new connection per call (no connection pool) |
| `claude_client.py` | Calls Anthropic API directly via `httpx`; two functions: `parse_task()` (Haiku) and `generate_summary_comment()` (Sonnet) |
| `notion.py` | Notion REST API calls; `enabled()` guards all operations when credentials are absent |
| `google_calendar.py` | OAuth2 token-based Google Calendar reads |
| `formatting.py` | Pure formatting helpers and `CATEGORY_EMOJI` dict |
| `scheduler.py` | Run by cron; calls `notion.sync_from_notion()` then sends daily summary via raw Telegram HTTP |
| `keepalive.sh` | Cron script that restarts `bot.py` if it's not running |

### Key data flow

1. User sends text → `bot.py:text_router` → `claude_client.parse_task()` → `db.add_task()` → `notion.create_page()` (if configured)
2. `/edit` uses in-memory `EDIT_WAITING` dict (chat_id → task_id) to track pending edits across messages
3. `/list` and `/done` store the current task list in `ctx.user_data["task_list"]` so numbered replies can be resolved to task IDs
4. `scheduler.py` runs standalone (no bot process), sends messages via raw HTTP POST to the Telegram sendMessage endpoint

### Notion sync strategy

- **DB → Notion (real-time):** every mutating bot command calls `_sync_task_to_notion()` / `_archive_task_in_notion()`
- **Notion → DB (daily + `/sync` command):** `notion.sync_from_notion()` uses `notion_synced_at` as a watermark; Notion wins if `last_edited_time > notion_synced_at`
- Pages without a `Task ID` property are treated as new tasks created directly in Notion

### Categories

Defined in `config.CATEGORIES`: `Work`, `Home`, `MCM`, `YFU`, `Personal`, `Other`, `Unknown`. The Claude prompt in `claude_client.py` maps domain keywords (e.g. HIPPIE, TUM, Dreamland) to these categories. Changing categories requires updating both `config.py` and the `PARSE_SYSTEM` prompt in `claude_client.py`, and the MariaDB `ENUM` column.

## Deployment

Runs on a CentOS 7 server via two cron entries:
- `*/5 * * * *` → `keepalive.sh` (restarts bot if down)
- `0 8 * * *` → `scheduler.py` (daily summary)

Logs: `logs/notetaker.log`, `logs/keepalive.log`, `logs/notetaker_cron.log`
