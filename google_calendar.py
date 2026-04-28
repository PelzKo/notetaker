"""
google_calendar.py — fetch today's and tomorrow's events.

ONE-TIME SETUP (on your local machine, not the server):
  1. Go to https://console.cloud.google.com
  2. Create a project (or reuse one)
  3. Enable "Google Calendar API"
  4. APIs & Services → Credentials → Create Credentials → OAuth client ID
     → Desktop app → Download JSON → save as credentials.json
  5. On your LOCAL machine (needs a browser):
       pip install google-auth-oauthlib google-api-python-client
       python google_calendar.py   # opens browser, ask you to log in
     This creates token.json in the same directory.
  6. Copy both credentials.json and token.json to ~/todobot/ on your server.
  7. Add to requirements.txt:
       google-auth-oauthlib==1.2.1
       google-api-python-client==2.140.0
"""

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
BASE_DIR = Path(__file__).parent
CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"


def _get_service():
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            # This only runs on first-time setup (needs a browser)
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


# Calendars to include by name. "primary" is always included regardless.
# Add or remove names to match exactly what you see in Google Calendar.
INCLUDED_CALENDARS = {"Arbeit", "DaiSyBio Birthdays", "https://rest.konzertmeister.app/api/v1/ical/ee473592-0fcc-4288-aa2a-21d2cf07b749?hideNegative=true"}


def _get_calendar_ids(service) -> list[str]:
    """Return calendarIds for primary + any calendar whose name is in INCLUDED_CALENDARS."""
    cal_ids = ["primary"]
    page_token = None
    while True:
        response = service.calendarList().list(pageToken=page_token).execute()
        for cal in response.get("items", []):
            if cal.get("summary") in INCLUDED_CALENDARS:
                cal_ids.append(cal["id"])
        page_token = response.get("nextPageToken")
        if not page_token:
            break
    return cal_ids


def get_events(days_ahead: int = 1) -> dict[str, list[dict]]:
    """
    Returns {"today": [...], "tomorrow": [...]}
    Each event: {"title": str, "start": str, "end": str, "all_day": bool}
    Events are deduplicated by (title, start) in case of calendar overlap.
    """
    try:
        service = _get_service()
    except Exception as e:
        return {"today": [], "tomorrow": [], "error": str(e)}

    local_tz = datetime.now().astimezone().tzinfo
    today_start = datetime.combine(date.today(), datetime.min.time(), tzinfo=local_tz)
    window_end = today_start + timedelta(days=2)
    today_str = date.today().isoformat()
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    try:
        cal_ids = _get_calendar_ids(service)
    except Exception as e:
        return {"today": [], "tomorrow": [], "error": f"Could not list calendars: {e}"}

    buckets: dict[str, list[dict]] = {"today": [], "tomorrow": []}
    seen: set[tuple] = set()  # deduplicate across calendars

    for cal_id in cal_ids:
        try:
            result = (
                service.events()
                .list(
                    calendarId=cal_id,
                    timeMin=today_start.isoformat(),
                    timeMax=window_end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )
        except Exception:
            continue  # skip unavailable calendars silently

        for item in result.get("items", []):
            start = item["start"]
            end = item["end"]

            if "date" in start:
                all_day = True
                event_date = start["date"]
                start_str = "all day"
                end_str = ""
            else:
                all_day = False
                start_dt = datetime.fromisoformat(start["dateTime"]).astimezone(local_tz)
                end_dt = datetime.fromisoformat(end["dateTime"]).astimezone(local_tz)
                event_date = start_dt.date().isoformat()
                start_str = start_dt.strftime("%H:%M")
                end_str = end_dt.strftime("%H:%M")

            title = item.get("summary", "(no title)")
            dedup_key = (title, start_str, event_date)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)

            event = {"title": title, "start": start_str, "end": end_str, "all_day": all_day}

            if event_date == today_str:
                buckets["today"].append(event)
            elif event_date == tomorrow_str:
                buckets["tomorrow"].append(event)

    # Sort each day by start time (all-day events first)
    for day in buckets.values():
        day.sort(key=lambda e: ("" if e["all_day"] else e["start"]))

    return buckets


def fmt_events_for_summary(events: dict) -> str:
    """Format calendar events as a text block for the daily summary."""
    if events.get("error"):
        return f"⚠️ Calendar unavailable: {events['error']}"

    lines = []

    for day_key, label in [("today", "📅 Today"), ("tomorrow", "📅 Tomorrow")]:
        day_events = events[day_key]
        if not day_events:
            lines.append(f"{label}: nothing scheduled")
            continue
        lines.append(f"{label}:")
        for e in day_events:
            if e["all_day"]:
                lines.append(f"  • {e['title']} (all day)")
            else:
                lines.append(f"  • {e['start']}–{e['end']}  {e['title']}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Run this directly on your LOCAL machine to do the one-time OAuth flow
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    events = get_events()
    print(fmt_events_for_summary(events))
