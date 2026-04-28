"""Notion API integration — bidirectional sync with the tasks table."""
import logging
from datetime import datetime, date

import httpx

import config

log = logging.getLogger(__name__)

_BASE = "https://api.notion.com/v1"
_NOTION_VERSION = "2022-06-28"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NOTION_API_KEY}",
        "Notion-Version": _NOTION_VERSION,
        "Content-Type": "application/json",
    }


def enabled() -> bool:
    return bool(getattr(config, "NOTION_API_KEY", "") and getattr(config, "NOTION_DATABASE_ID", ""))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_notion_dt(s: str | None) -> datetime | None:
    """Parse Notion's ISO-8601 UTC timestamp to a naive UTC datetime."""
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _date_str(d) -> str | None:
    if d is None:
        return None
    if isinstance(d, datetime):
        return d.date().isoformat()
    if isinstance(d, date):
        return d.isoformat()
    return str(d)[:10]  # assume ISO string


def _task_to_properties(task: dict) -> dict:
    """Convert a DB task row to Notion page properties."""
    props: dict = {
        "Name": {"title": [{"text": {"content": (task.get("title") or "")[:2000]}}]},
        "Category": {"select": {"name": task.get("category") or "Unknown"}},
        "Done": {"checkbox": bool(task.get("is_done", False))},
    }
    if task.get("id"):
        props["Task ID"] = {"number": int(task["id"])}

    due = _date_str(task.get("due_date"))
    props["Due Date"] = {"date": {"start": due} if due else None}

    done_at = _date_str(task.get("done_at"))
    props["Done At"] = {"date": {"start": done_at} if done_at else None}

    return props


def _extract_page(page: dict) -> dict:
    """Extract relevant fields from a raw Notion page response."""
    props = page.get("properties", {})

    def text(key: str) -> str:
        p = props.get(key, {})
        items = p.get("title") or p.get("rich_text") or []
        return "".join(i.get("plain_text", "") for i in items)

    def select(key: str) -> str | None:
        p = props.get(key, {})
        s = p.get("select")
        return s["name"] if s else None

    def date_prop(key: str) -> str | None:
        p = props.get(key, {})
        d = p.get("date")
        return d["start"] if d else None

    def number(key: str):
        p = props.get(key, {})
        return p.get("number")

    def checkbox(key: str) -> bool:
        p = props.get(key, {})
        return bool(p.get("checkbox", False))

    return {
        "notion_page_id": page["id"],
        "last_edited_time": page.get("last_edited_time", ""),
        "task_id": number("Task ID"),
        "title": text("Name"),
        "category": select("Category"),
        "due_date": date_prop("Due Date"),
        "is_done": checkbox("Done"),
    }


# ---------------------------------------------------------------------------
# DB → Notion (write operations)
# ---------------------------------------------------------------------------

def create_page(task: dict) -> tuple[str | None, datetime | None]:
    """
    Create a Notion page for a task.
    Returns (page_id, last_edited_time) on success, (None, None) on failure.
    """
    if not enabled():
        return None, None
    body = {
        "parent": {"database_id": config.NOTION_DATABASE_ID},
        "properties": _task_to_properties(task),
    }
    try:
        r = httpx.post(f"{_BASE}/pages", headers=_headers(), json=body, timeout=10)
        r.raise_for_status()
        data = r.json()
        return data["id"], _parse_notion_dt(data.get("last_edited_time"))
    except Exception as exc:
        log.warning("Notion create_page failed: %s", exc)
        return None, None


def update_page(page_id: str, task: dict) -> datetime | None:
    """
    Update a Notion page from a task dict.
    Returns the new last_edited_time on success, None on failure.
    """
    if not enabled() or not page_id:
        return None
    body = {"properties": _task_to_properties(task)}
    try:
        r = httpx.patch(f"{_BASE}/pages/{page_id}", headers=_headers(), json=body, timeout=10)
        r.raise_for_status()
        return _parse_notion_dt(r.json().get("last_edited_time"))
    except Exception as exc:
        log.warning("Notion update_page failed: %s", exc)
        return None


def archive_page(page_id: str) -> bool:
    """Archive (soft-delete) a Notion page."""
    if not enabled() or not page_id:
        return False
    try:
        r = httpx.patch(
            f"{_BASE}/pages/{page_id}",
            headers=_headers(),
            json={"archived": True},
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as exc:
        log.warning("Notion archive_page failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Notion → DB (sync read operations)
# ---------------------------------------------------------------------------

def _query_all_pages() -> list[dict]:
    """Fetch every non-archived page from the linked Notion database."""
    if not enabled():
        return []
    pages: list[dict] = []
    cursor: str | None = None
    while True:
        body: dict = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        try:
            r = httpx.post(
                f"{_BASE}/databases/{config.NOTION_DATABASE_ID}/query",
                headers=_headers(),
                json=body,
                timeout=15,
            )
            r.raise_for_status()
            data = r.json()
            pages.extend(data.get("results", []))
            if data.get("has_more") and data.get("next_cursor"):
                cursor = data["next_cursor"]
            else:
                break
        except Exception as exc:
            log.warning("Notion query failed: %s", exc)
            break
    return pages


def sync_from_notion() -> list[str]:
    """
    Pull changes from Notion into MariaDB.

    Strategy:
    - For pages with a Task ID: if Notion's last_edited_time > notion_synced_at,
      apply the Notion data to the DB row.
    - For pages without a Task ID (created directly in Notion): create a new DB
      task and write the Task ID back to Notion.

    Returns a list of human-readable change descriptions (for logging/alerting).
    """
    if not enabled():
        return []

    # Import here to avoid circular imports at module level
    import db  # noqa: PLC0415

    pages = _query_all_pages()
    changes: list[str] = []

    for raw_page in pages:
        info = _extract_page(raw_page)
        notion_page_id = info["notion_page_id"]
        last_edited = _parse_notion_dt(info["last_edited_time"])

        task_id = info.get("task_id")

        # ── New page created in Notion (no Task ID) ────────────────────────
        if not task_id:
            title = info["title"].strip()
            if not title:
                continue  # empty page, skip
            category = info["category"] if info["category"] in config.CATEGORIES else "Unknown"
            due_str = info["due_date"]
            due_date: date | None = None
            if due_str:
                try:
                    due_date = date.fromisoformat(due_str[:10])
                except ValueError:
                    pass
            new_id = db.add_task(title, title, category, due_date)
            db.set_notion_page_id(new_id, notion_page_id, last_edited)
            # Write the Task ID back to Notion so we link them permanently
            task = db.get_task(new_id)
            if task:
                new_edited = update_page(notion_page_id, task)
                if new_edited:
                    db.set_notion_page_id(new_id, notion_page_id, new_edited)
            changes.append(f"Task #{new_id} created from Notion page {notion_page_id[:8]}…")
            log.info("Created task #%s from Notion", new_id)
            continue

        # ── Existing task — check if Notion changed since last sync ────────
        task = db.get_task(task_id)
        if not task:
            log.warning("Notion page references missing task #%s", task_id)
            continue

        synced_at: datetime | None = task.get("notion_synced_at")
        if synced_at and last_edited and last_edited <= synced_at:
            continue  # Notion unchanged since our last write

        # Apply Notion changes to DB
        updates: dict = {}

        new_title = (info["title"] or "").strip()[:500]
        if new_title and new_title != task.get("title"):
            updates["title"] = new_title

        new_cat = info["category"]
        if new_cat and new_cat in config.CATEGORIES and new_cat != task.get("category"):
            updates["category"] = new_cat

        due_str = info["due_date"]
        new_due: date | None = None
        if due_str:
            try:
                new_due = date.fromisoformat(due_str[:10])
            except ValueError:
                pass
        if new_due != task.get("due_date"):
            updates["due_date"] = new_due

        if updates:
            db.update_task(task_id, **updates)
            changes.append(f"Task #{task_id} updated from Notion: {sorted(updates)}")
            log.info("Synced task #%s from Notion: %s", task_id, sorted(updates))

        # Handle done status
        if info["is_done"] and not task.get("is_done"):
            db.mark_done(task_id)
            changes.append(f"Task #{task_id} marked done via Notion")
            log.info("Task #%s marked done from Notion", task_id)

        # Update the sync watermark so we won't re-process this edit
        db.set_notion_page_id(task_id, notion_page_id, last_edited)

    return changes
