# Todobot Setup Guide (CentOS 7)

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

CREATE DATABASE todobot CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'todobot'@'localhost' IDENTIFIED BY 'choose_a_strong_password';
GRANT ALL PRIVILEGES ON todobot.* TO 'todobot'@'localhost';
FLUSH PRIVILEGES;
EXIT;
```

## 4. Python setup

CentOS 7 ships Python 3.6. Check if 3.11 is available:

```bash
sudo yum install epel-release -y
sudo yum install python311 -y
python3.11 --version
```

If that fails (not in repos), compile from source:
```bash
sudo yum groupinstall "Development Tools" -y
sudo yum install openssl-devel bzip2-devel libffi-devel zlib-devel -y
wget https://www.python.org/ftp/python/3.11.9/Python-3.11.9.tgz
tar xf Python-3.11.9.tgz
cd Python-3.11.9
./configure --enable-optimizations
make -j$(nproc)
sudo make altinstall
python3.11 --version
```

## 5. Google Calendar setup

This requires a one-time OAuth flow on your **local machine** (needs a browser),
then copying the resulting token to the server.

### 5a. Google Cloud project (one-time)

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

### 5b. One-time OAuth flow (on your LOCAL machine)

```bash
pip install google-auth-oauthlib google-api-python-client
# Put credentials.json in the same directory as google_calendar.py, then:
python google_calendar.py
# A browser window opens — log in and allow access
# This creates token.json in the same directory
```

### 5c. Verify your calendar names

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
INCLUDED_CALENDARS = {"Arbeit", "Doctoral Representatives", "MCM"}
```

### 5d. Copy credentials to server

```bash
scp credentials.json token.json user@yourserver:~/todobot/
```

`token.json` refreshes itself automatically — you will not need to redo the OAuth flow
unless you revoke access in your Google account.

## 6. Deploy the bot

```bash
# Copy all files to server (from your local machine)
scp -r todobot/ user@yourserver:~/

# On the server
cd ~/todobot
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
cd ~/todobot
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
double-check that `credentials.json` and `token.json` are in `~/todobot/`.

## 8. Set up cron jobs

The bot runs via two cron entries: one that keeps it alive (checks every 5 minutes),
and one that fires the daily summary at 08:00.

```bash
crontab -e
```

Add these two lines, replacing `YOUR_LINUX_USER` with your actual username:

```
*/5 * * * * set -a; source /home/YOUR_LINUX_USER/todobot/.env; set +a; pgrep -f "venv/bin/python bot.py" > /dev/null || nohup /home/YOUR_LINUX_USER/todobot/venv/bin/python /home/YOUR_LINUX_USER/todobot/bot.py >> /tmp/todobot.log 2>&1 &

0 8 * * * set -a; source /home/YOUR_LINUX_USER/todobot/.env; set +a; /home/YOUR_LINUX_USER/todobot/venv/bin/python /home/YOUR_LINUX_USER/todobot/scheduler.py >> /tmp/todobot_cron.log 2>&1
```

Then start the bot immediately without waiting for cron:

```bash
set -a; source ~/todobot/.env; set +a
nohup ~/todobot/venv/bin/python ~/todobot/bot.py >> /tmp/todobot.log 2>&1 &
```

## 9. Verify everything works

```bash
# Check bot is running
pgrep -f "bot.py"           # should print a PID
tail -f /tmp/todobot.log    # should show "Bot starting…"
```

Then in Telegram:

1. Send a task: `fix HIPPIE migration bug by Friday`
   → Bot replies with parsed title, category (Work), due date
2. Send `/list` → task appears numbered
3. Reply with `1` → task marked done
4. Send `/stats` → category counts
5. Run `python ~/todobot/scheduler.py` manually → summary arrives with calendar block

## 10. Keeping it running after reboots

The `*/5 * * * *` cron entry handles restarts automatically — if the server reboots,
the bot will be back within 5 minutes with no action needed on your part.

Check at any time:
```bash
pgrep -f "bot.py" && echo "running" || echo "not running"
```

## Logs

| File | Contains |
|---|---|
| `/tmp/todobot.log` | Bot stdout — startup, errors, incoming messages |
| `/tmp/todobot_cron.log` | Daily summary runs |

Note: `/tmp/` is cleared on reboot. For persistent logs change the crontab paths
to something like `~/todobot/bot.log`.
