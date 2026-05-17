"""Auto-generate 'Prep: <event>' tasks for tomorrow's meetings.

Called by evening.py after the daily summary. For each tomorrow event >=30min
that isn't already linked to a prep task (via source_event_id), ask Claude
whether prep is warranted and, if so, insert a task due today.
"""
import json
from datetime import date

import httpx

import config
import db

API_URL = "https://api.anthropic.com/v1/messages"
HEADERS = {
    "x-api-key": config.ANTHROPIC_API_KEY,
    "anthropic-version": "2023-06-01",
    "content-type": "application/json",
}

SYSTEM_PROMPT = """You decide which meetings need preparation.

Given a JSON array of meetings, return ONLY a JSON array of decisions:
[{"event_id": "...", "needs_prep": true|false, "prep_title": "...", "category": "Work|MCM|YFU|Personal|Home|Other"}]

Rules:
- needs_prep is true when the meeting is non-routine and benefits from explicit prep
  (presentations, interviews, reviews, important 1:1s, project meetings with new content).
- Skip recurring standups, syncs, casual catch-ups, "block" or "focus" entries, and
  travel/transit. Set needs_prep=false for those.
- prep_title is imperative, concise (max 60 chars), e.g. "Prep slides for Q3 review",
  "Skim CV for interview with Anna".
- category: pick the most likely category from the listed enum based on the meeting title.
- Always include every event_id from the input — do not omit any.
- Return ONLY the JSON array. No markdown, no commentary."""


def _meeting_payload(events: list[dict]) -> str:
    payload = []
    for e in events:
        payload.append({
            "event_id": e.get("id", ""),
            "title": e.get("title", ""),
            "start": e.get("start", ""),
            "end": e.get("end", ""),
            "duration_min": e.get("duration_min", 0),
        })
    return json.dumps(payload, ensure_ascii=False)


def _ask_claude(events: list[dict]) -> list[dict]:
    if not events:
        return []
    try:
        resp = httpx.post(
            API_URL,
            headers=HEADERS,
            json={
                "model": "claude-sonnet-4-5",
                "max_tokens": 800,
                "system": [
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": [{"role": "user", "content": _meeting_payload(events)}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        text = resp.json()["content"][0]["text"].strip()
        # Strip code fences if model added them
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()
        return json.loads(text)
    except Exception as exc:  # noqa: BLE001
        print(f"calendar_prep: Claude call failed: {exc}")
        return []


def generate_prep_tasks(tomorrow_events: list[dict]) -> list[dict]:
    """For each eligible event, create a prep task due today and return the new tasks."""
    candidates = [
        e for e in tomorrow_events
        if not e.get("all_day")
        and e.get("id")
        and e.get("duration_min", 0) >= 30
        and not db.get_task_by_event_id(e["id"])
    ]
    if not candidates:
        return []

    decisions = _ask_claude(candidates)
    by_id = {d.get("event_id"): d for d in decisions if isinstance(d, dict)}
    valid_categories = set(config.CATEGORIES)
    today = date.today()

    created: list[dict] = []
    for ev in candidates:
        decision = by_id.get(ev["id"])
        if not decision or not decision.get("needs_prep"):
            continue
        title = (decision.get("prep_title") or f"Prep: {ev['title']}")[:120]
        category = decision.get("category") or "Work"
        if category not in valid_categories:
            category = "Work"
        new_id = db.add_task(
            raw_text=f"(auto-prep) {ev['title']} @ {ev['start']}",
            title=title,
            category=category,
            due_date=today,
            is_priority=False,
            source_event_id=ev["id"],
        )
        task = db.get_task(new_id)
        if task:
            created.append(task)
    return created
