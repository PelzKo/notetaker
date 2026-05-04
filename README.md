# Notetaker Setup Guide (CentOS 7)

| Command | Action |
|---------|--------|
| /list | Show all open tasks, numbered; reply with IDs to mark done |
| /list onlytext | Compact bullet list (no emojis or dates) — IDs only |
| /listtext | Same compact output as `/list onlytext` (no mark-done session) |
| /show &lt;id&gt; | Show full task detail and re-send any attachments |
| /search &lt;query&gt; | Find open tasks whose title or original text contains the query |
| /filter [category] | Filter open tasks by category (no arg → category picker buttons) |
| /history [days] | List tasks completed in the last N days (default 7), grouped by date |
| /done | Show numbered open task list; reply with IDs to mark done |
| /edit &lt;id&gt; | Bot asks what to change (free-text re-parse mode) |
| /edit &lt;id&gt; &lt;field&gt; &lt;value&gt; | Set a single field: `title`, `category`, `date`/`due`, `priority` |
| /defer &lt;id&gt; [days] | Push the due date later by N days (default 1) |
| /priority &lt;id&gt; [on\|off] | Toggle ⭐ priority (no arg → toggle) |
| /snooze &lt;id&gt; &lt;1h\|30m\|tomorrow\|mon&gt; | Push a reminder forward |
| /next | Suggest one task to do right now (skips if you're in a meeting) |
| /drop &lt;id&gt; | Delete a task permanently |
| /stats | Quick counts per category (open and done) |
| /sync | Pull any Notion changes into MariaDB on demand |
| /pushnotion [all] | Push every DB task missing a Notion page to Notion (open only by default) |
| /menu | Show a persistent reply keyboard with the most-used commands |
| /help | Print every command grouped by purpose |
| /cancel | Abort an in-progress /edit |

### Adding tasks

- Send any text to add a task — Claude parses it into title, category, due date, ⭐ priority, recurrence, and an optional reminder time.
- "every Monday", "jeden Dienstag", "monthly on the 15th", "every weekday", "daily" all create recurring tasks. When marked done, the next instance is auto-scheduled.
- "remind me at 3pm to call mum" or "tomorrow at 9 dentist" sets a one-shot reminder fired by the per-minute reminder cron.
- Pasting a URL by itself fetches the page title and uses it as the task name; the original URL is kept in `raw_text`.
- Forwarded messages are treated as captured items.
- Urgency keywords ("urgent", "asap", "wichtig", "dringend") set priority to true.
- Send a photo or document **with a caption** — the caption is parsed as the task text and the file is attached. The Telegram `file_id` is stored; nothing is uploaded to your server or to Notion. Tasks with attachments show a 📎 badge in `/list` and a checkbox in Notion (if the property exists).
- Album uploads (multiple photos at once) are coalesced into a single task with multiple attachments.
- Photos/documents sent without a caption create a placeholder task you can rename via `/edit`.

### Inline buttons

- Every freshly captured task comes with **⭐ Priority / 📅 Today / 📅 Tomorrow / ➕1d / ✏️ /edit / ❌ Drop** buttons.
- After `/done`, an **↩️ Undo** button lets you re-open the most recently completed tasks.
- New tasks parsed as `Unknown` come with quick-pick category buttons.
- `/filter` with no argument shows a one-tap category picker.
- Reminders sent by `reminder_check.py` come with **✅ Done / 💤 Snooze 1h / 🌅 Tomorrow AM** buttons.
- The Sunday weekly review surfaces stale tasks with **❌ Drop / 📅 Defer 30d / ✅ Done** buttons.
- The evening check-in surfaces open tasks due today with **✅ Done / 📅 Tomorrow / +1d** buttons.
- `/next` suggestion cards include a **🔁 Pick another** button to cycle through candidates without repeating.

Assuming you clone the repository into ~/notetaker with
```bash
git clone https://github.com/PelzKo/notetaker.git
```

## 1. Create Telegram bot

1. Open Telegram, search for @BotFather
2. Send `/newbot`, follow prompts, get your **token**
3. Start a chat with your new bot, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Send any message to the bot, refresh the URL, find `"chat":{"id":XXXXXXX}` — that's your **TELEGRAM_CHAT_ID**

## 2. Get Anthropic API key

1. Go to https://console.anthropic.com
2. Settings → API Keys → Create key
3. Copy it, you won't see it again

## 3. MariaDB setup

```sql
mysql -u root -p

CREATE DATABASE konstip_notetaker CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'notetaker'@'localhost' IDENTIFIED BY 'choose_a_strong_password';
GRANT ALL PRIVILEGES ON notetaker.* TO 'notetaker'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

## 4. Google Calendar setup

This requires a one-time OAuth flow on your **local machine** (needs a browser),
then copying the resulting token to the server.

### 4a. Google Cloud project (one-time)

1. Go to https://console.cloud.google.com
2. Create a new project (or reuse an existing one)
3. Enable the **Google Calendar API**:
   APIs & Services → Library → search "Google Calendar API" → Enable
4. Create credentials:
   APIs & Services → Credentials → Create Credentials → OAuth client ID → **Desktop app**
   → Download JSON → rename file to `credentials.json`
5. Set up the OAuth consent screen:
   APIs & Services → OAuth consent screen → External → fill in any app name →
   Save and continue through the screens
6. Add your Google account as a test user:
   OAuth consent screen → scroll to "Test users" → Add users → add your Gmail address → Save

### 4b. One-time OAuth flow (on your LOCAL machine)

```bash
pip install google-auth-oauthlib google-api-python-client
# Put credentials.json in the same directory as google_calendar.py, then:
python google_calendar.py
# A browser window opens — log in and allow access
# This creates token.json in the same directory
```

### 4c. Verify your calendar names

The bot filters calendars by name. Check what names the API sees:

```bash
python - <<'EOF'
from google_calendar import _get_service
service = _get_service()
for cal in service.calendarList().list().execute().get("items", []):
    print(repr(cal["summary"]))
EOF
```

Open `google_calendar.py` and update `INCLUDED_CALENDARS` to match exactly:

```python
INCLUDED_CALENDARS = {"CALENDAR1", "CALENDAR2", "CALENDAR3"}
```

### 4d. Copy credentials to server

```bash
scp credentials.json token.json user@yourserver:~/notetaker/
```

`token.json` refreshes itself automatically — you will not need to redo the OAuth flow
unless you revoke access in your Google account.

## 5. Notion sync (optional)

Notion sync is completely optional. If you skip this section, the bot works exactly as before — just leave `NOTION_API_KEY` and `NOTION_DATABASE_ID` blank in `.env`.

When configured, every task you add/edit/complete/delete is mirrored to a Notion database in real time. You can also edit tasks directly in Notion and the changes are pulled back into MariaDB each morning (or on demand via `/sync`).

### 5a. Create a Notion integration (API key)

1. Go to https://www.notion.so/my-integrations
2. Click **"+ New integration"**
3. Give it a name (e.g. `Notetaker Bot`), select your workspace, leave type as **Internal**
4. Click **Submit**
5. On the next screen, copy the **"Installation access token"** — this is your `NOTION_API_KEY`.
   It starts with `ntn_` and looks like `ntn_abc123...`

### 5b. Create the Notion database

Create a new **full-page database** in Notion (not an inline/embedded one — it must be its own page so it has its own URL). Add exactly these properties with these exact names and types:

| Property name | Type      | Notes |
|---------------|-----------|-------|
| `Name`        | Title     | Built-in, already exists |
| `Category`    | Select    | Add options: `Work`, `Home`, `MCM`, `YFU`, `Personal`, `Other`, `Unknown` |
| `Due Date`    | Date      | |
| `Done`        | Checkbox  | |
| `Done At`     | Date      | |
| `Task ID`     | Number    | Used to link Notion pages back to MariaDB rows |
| `Priority`    | Checkbox  | Optional. ⭐ flag synced both ways; bot skips it gracefully if absent |
| `Attachment`  | Checkbox  | Optional. Set automatically when a task has Telegram attachments (read-only, files stay in Telegram) |
| `Recurrence`  | Rich Text | Optional. Shows the recurrence rule (e.g. `weekly:mon`, `daily`); read-only in Notion |

### 5c. Get the database ID

1. Open the database as a full page in your browser (click the title, then "Open as full page" if needed)
2. Look at the URL — it will look like one of:
   - `https://www.notion.so/yourworkspace/abc1def2abc1def2abc1def2abc1def2?v=...`
   - `https://www.notion.so/abc1def2-abc1-def2-abc1-def2abc1def2?v=...`
3. Copy the 32-character hex string before the `?v=` (with or without dashes, both work)
   — that is your `NOTION_DATABASE_ID`

### 5d. Add credentials to .env

```
NOTION_API_KEY=ntn_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
NOTION_DATABASE_ID=abc1def2abc1def2abc1def2abc1def2
```

### How the sync works

- **MariaDB → Notion (real-time):** every time you add, edit, complete, or delete a task via Telegram, the change is pushed to Notion immediately.
- **Notion → MariaDB (daily + on-demand):** each morning the scheduler pulls any changes made directly in Notion and applies them to MariaDB. Use `/sync` in Telegram to trigger this manually at any time.
- **New pages in Notion:** if you create a row directly in the Notion database (without a Task ID), it will be imported as a new task in MariaDB on the next sync.
- **Conflict resolution:** if a Notion page was edited more recently than the last sync watermark, Notion wins. Otherwise the MariaDB value is kept.

## 6. Deploy the bot

```bash
cd ~/notetaker
python3.11 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
chmod 600 .env
nano .env   # fill in all values
```

## 7. Test manually

```bash
cd ~/notetaker
set -a; source .env; set +a

# Test the bot
python bot.py
```

Send a message to your bot in Telegram. If it responds, it works. Ctrl+C to stop.

```bash
# Test the daily summary (including calendar)
python scheduler.py
```

You should receive the summary in Telegram. If the calendar block shows an error,
double-check that `credentials.json` and `token.json` are in `~/notetaker/`.

## 8. Set up cron jobs

```bash
crontab -e
```

Add these lines, replacing `YOUR_LINUX_USER` with your actual username:

```
# Keep bot alive (restart if down)
*/5 * * * * /home/YOUR_LINUX_USER/notetaker/keepalive.sh >> /home/YOUR_LINUX_USER/notetaker/logs/keepalive.log 2>&1

# Morning summary at 08:00 (Notion sync + calendar + task digest + interesting reads)
0 8 * * * set -a; source /home/YOUR_LINUX_USER/notetaker/.env; set +a; /home/YOUR_LINUX_USER/notetaker/venv/bin/python /home/YOUR_LINUX_USER/notetaker/scheduler.py >> /home/YOUR_LINUX_USER/notetaker/logs/notetaker_cron.log 2>&1

# Per-minute reminder check
* * * * * set -a; source /home/YOUR_LINUX_USER/notetaker/.env; set +a; /home/YOUR_LINUX_USER/notetaker/venv/bin/python /home/YOUR_LINUX_USER/notetaker/reminder_check.py >> /home/YOUR_LINUX_USER/notetaker/logs/reminders.log 2>&1

# Evening check-in at 20:00 (today's done, open tasks, tomorrow's calendar, auto-prep tasks)
0 20 * * * set -a; source /home/YOUR_LINUX_USER/notetaker/.env; set +a; /home/YOUR_LINUX_USER/notetaker/venv/bin/python /home/YOUR_LINUX_USER/notetaker/evening.py >> /home/YOUR_LINUX_USER/notetaker/logs/evening.log 2>&1

# Sunday 18:00 weekly review
0 18 * * 0 set -a; source /home/YOUR_LINUX_USER/notetaker/.env; set +a; /home/YOUR_LINUX_USER/notetaker/venv/bin/python /home/YOUR_LINUX_USER/notetaker/weekly_review.py >> /home/YOUR_LINUX_USER/notetaker/logs/weekly.log 2>&1
```

## 9. Verify everything works

```bash
# Check bot is running
pgrep -f "bot.py"           # should print a PID
tail -f /home/YOUR_LINUX_USER/notetaker/logs/notetaker.log    # should show "Bot starting…"
```

Then in Telegram:

1. Send a task: `fix HIPPIE migration bug by Friday`
   → Bot replies with parsed title, category (Work), due date
2. Send `/list` → task appears numbered
3. Reply with `1` → task marked done
4. Send `/stats` → category counts
5. Run `python ~/notetaker/scheduler.py` manually → summary arrives with calendar block

## 10. Keeping it running after reboots

The `*/5 * * * *` cron entry handles restarts automatically — if the server reboots,
the bot will be back within 5 minutes with no action needed on your part.

Check at any time:
```bash
pgrep -f "bot.py" && echo "running" || echo "not running"
```
