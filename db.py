import pymysql
import config
from datetime import date, datetime, timedelta


def _conn():
    return pymysql.connect(
        host=config.DB_HOST,
        port=config.DB_PORT,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )


def init_db():
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    raw_text TEXT NOT NULL,
                    title VARCHAR(500) NOT NULL,
                    category ENUM('Work','Home','MCM','YFU','Personal','Other','Unknown')
                        DEFAULT 'Unknown',
                    due_date DATE NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    done_at DATETIME NULL,
                    is_done BOOLEAN DEFAULT FALSE,
                    is_priority BOOLEAN NOT NULL DEFAULT FALSE,
                    notion_page_id VARCHAR(36) NULL DEFAULT NULL,
                    notion_synced_at DATETIME NULL DEFAULT NULL,
                    recurrence VARCHAR(40) NULL DEFAULT NULL,
                    remind_at DATETIME NULL DEFAULT NULL,
                    remind_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    source_event_id VARCHAR(255) NULL DEFAULT NULL
                ) CHARACTER SET utf8mb4
            """)
            # Migrate existing installations
            cur.execute("""
                ALTER TABLE tasks
                    ADD COLUMN IF NOT EXISTS notion_page_id VARCHAR(36) NULL DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS notion_synced_at DATETIME NULL DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS is_priority BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS recurrence VARCHAR(40) NULL DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS remind_at DATETIME NULL DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS remind_sent BOOLEAN NOT NULL DEFAULT FALSE,
                    ADD COLUMN IF NOT EXISTS source_event_id VARCHAR(255) NULL DEFAULT NULL
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS attachments (
                    id INT AUTO_INCREMENT PRIMARY KEY,
                    task_id INT NOT NULL,
                    file_id VARCHAR(255) NOT NULL,
                    file_unique_id VARCHAR(64) NOT NULL,
                    kind ENUM('photo','document','audio','video','voice') NOT NULL,
                    file_name VARCHAR(255) NULL,
                    mime_type VARCHAR(120) NULL,
                    caption TEXT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    INDEX idx_task (task_id),
                    FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
                ) CHARACTER SET utf8mb4
            """)


def add_task(raw_text: str, title: str, category: str, due_date: date | None,
             is_priority: bool = False,
             recurrence: str | None = None,
             remind_at: datetime | None = None,
             source_event_id: str | None = None) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (raw_text, title, category, due_date, is_priority, "
                "recurrence, remind_at, source_event_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (raw_text, title, category, due_date, bool(is_priority),
                 recurrence, remind_at, source_event_id),
            )
            return conn.insert_id()


def _select_open_tasks_sql(extra_where: str = "", order: str | None = None) -> str:
    """Build a SELECT for open tasks with attachment_count joined in."""
    base = """
        SELECT t.id, t.title, t.category, t.due_date, t.created_at,
               t.is_priority, t.is_done, t.done_at, t.raw_text,
               t.notion_page_id, t.notion_synced_at,
               t.recurrence, t.remind_at, t.remind_sent, t.source_event_id,
               COALESCE(a.cnt, 0) AS attachment_count
        FROM tasks t
        LEFT JOIN (
            SELECT task_id, COUNT(*) AS cnt FROM attachments GROUP BY task_id
        ) a ON a.task_id = t.id
        WHERE t.is_done = FALSE
    """
    if extra_where:
        base += " AND " + extra_where
    if order:
        base += " ORDER BY " + order
    else:
        base += (" ORDER BY t.is_priority DESC, t.due_date IS NULL, "
                 "t.due_date ASC, t.created_at ASC")
    return base


def get_open_tasks() -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_select_open_tasks_sql())
            return cur.fetchall()


def get_summary_tasks() -> dict:
    """Return tasks bucketed for the daily summary."""
    today = date.today()
    with _conn() as conn:
        with conn.cursor() as cur:
            # Due within 7 days (including overdue)
            cur.execute("""
                SELECT t.id, t.title, t.category, t.due_date, t.created_at,
                       t.is_priority,
                       COALESCE(a.cnt, 0) AS attachment_count
                FROM tasks t
                LEFT JOIN (
                    SELECT task_id, COUNT(*) AS cnt FROM attachments GROUP BY task_id
                ) a ON a.task_id = t.id
                WHERE t.is_done = FALSE
                  AND t.due_date IS NOT NULL
                  AND t.due_date <= DATE_ADD(%s, INTERVAL 7 DAY)
                ORDER BY t.due_date ASC
            """, (today,))
            due_soon = cur.fetchall()

            # No due date, added more than 30 days ago
            cur.execute("""
                SELECT t.id, t.title, t.category, t.due_date, t.created_at,
                       t.is_priority,
                       COALESCE(a.cnt, 0) AS attachment_count
                FROM tasks t
                LEFT JOIN (
                    SELECT task_id, COUNT(*) AS cnt FROM attachments GROUP BY task_id
                ) a ON a.task_id = t.id
                WHERE t.is_done = FALSE
                  AND t.due_date IS NULL
                  AND t.created_at <= DATE_SUB(%s, INTERVAL 30 DAY)
                ORDER BY t.created_at ASC
            """, (today,))
            old_noduedate = cur.fetchall()

    overdue = [t for t in due_soon if t["due_date"] and t["due_date"] < today]
    upcoming = [t for t in due_soon if t["due_date"] and t["due_date"] >= today]
    old_noduedate = [t for t in old_noduedate]

    return {"overdue": overdue, "upcoming": upcoming, "old_noduedate": old_noduedate}


_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def compute_next_due(recurrence: str, base: date) -> date | None:
    """Given a recurrence string and a base date, return the next due date (after base).
    Recognized formats: 'daily', 'weekday', 'weekly:<mon|tue|...>', 'monthly:<1-31>'.
    Returns None if the string can't be parsed."""
    rec = (recurrence or "").strip().lower()
    if not rec:
        return None
    if rec == "daily":
        return base + timedelta(days=1)
    if rec == "weekday":
        d = base + timedelta(days=1)
        while d.weekday() >= 5:  # 5=Sat, 6=Sun
            d += timedelta(days=1)
        return d
    if rec.startswith("weekly:"):
        wd = rec.split(":", 1)[1].strip()[:3]
        if wd not in _WEEKDAYS:
            return None
        target = _WEEKDAYS.index(wd)
        d = base + timedelta(days=1)
        while d.weekday() != target:
            d += timedelta(days=1)
        return d
    if rec.startswith("monthly:"):
        try:
            day = int(rec.split(":", 1)[1])
        except ValueError:
            return None
        if not 1 <= day <= 31:
            return None
        # Walk forward day-by-day to land on the first matching day-of-month after base.
        d = base + timedelta(days=1)
        for _ in range(366):
            if d.day == day:
                return d
            d += timedelta(days=1)
        return None
    return None


def mark_done(task_id: int) -> bool:
    """Mark a task done. If it's recurring, also spawn the next instance.
    Returns True if the original task transitioned from open to done."""
    transitioned, _ = mark_done_ex(task_id)
    return transitioned


def mark_done_ex(task_id: int) -> tuple[bool, int | None]:
    """Like mark_done but also returns the spawned next-instance task ID
    when the completed task was recurring."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET is_done = TRUE, done_at = %s WHERE id = %s AND is_done = FALSE",
                (datetime.now(), task_id),
            )
            if cur.rowcount == 0:
                return False, None
            cur.execute(
                "SELECT raw_text, title, category, due_date, is_priority, recurrence "
                "FROM tasks WHERE id = %s",
                (task_id,),
            )
            row = cur.fetchone()
    if not row or not row.get("recurrence"):
        return True, None

    base = row.get("due_date")
    if isinstance(base, datetime):
        base = base.date()
    if base is None:
        base = date.today()
    next_due = compute_next_due(row["recurrence"], base)
    if next_due is None:
        return True, None
    new_id = add_task(
        raw_text=row["raw_text"],
        title=row["title"],
        category=row["category"],
        due_date=next_due,
        is_priority=bool(row.get("is_priority")),
        recurrence=row["recurrence"],
    )
    return True, new_id


def get_due_reminders() -> list[dict]:
    """Return open tasks with remind_at <= NOW and remind_sent = FALSE."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, category, due_date, is_priority, remind_at, recurrence
                FROM tasks
                WHERE is_done = FALSE
                  AND remind_at IS NOT NULL
                  AND remind_sent = FALSE
                  AND remind_at <= NOW()
                ORDER BY remind_at ASC
            """)
            return cur.fetchall()


def mark_reminder_sent(task_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET remind_sent = TRUE WHERE id = %s",
                (task_id,),
            )
            return cur.rowcount > 0


def get_task_by_event_id(event_id: str) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, title, due_date, is_done FROM tasks WHERE source_event_id = %s LIMIT 1",
                (event_id,),
            )
            return cur.fetchone()


def get_open_tasks_due_today() -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_select_open_tasks_sql("t.due_date = CURDATE()"))
            return cur.fetchall()


def get_oldest_open_tasks(limit: int = 5) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                _select_open_tasks_sql(
                    order="t.created_at ASC",
                ) + " LIMIT %s",
                (int(limit),),
            )
            return cur.fetchall()


def get_done_stats_by_category(days: int) -> list[dict]:
    """Done counts grouped by category over the last N days."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT category, COUNT(*) AS done
                FROM tasks
                WHERE is_done = TRUE
                  AND done_at >= NOW() - INTERVAL %s DAY
                GROUP BY category
                ORDER BY done DESC
            """, (int(days),))
            return cur.fetchall()


def mark_open(task_id: int) -> bool:
    """Re-open a previously completed task."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET is_done = FALSE, done_at = NULL WHERE id = %s AND is_done = TRUE",
                (task_id,),
            )
            return cur.rowcount > 0


def delete_task(task_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks WHERE id = %s", (task_id,))
            return cur.rowcount > 0


def get_task(task_id: int) -> dict | None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT t.*, COALESCE(a.cnt, 0) AS attachment_count
                FROM tasks t
                LEFT JOIN (
                    SELECT task_id, COUNT(*) AS cnt FROM attachments GROUP BY task_id
                ) a ON a.task_id = t.id
                WHERE t.id = %s
            """, (task_id,))
            return cur.fetchone()


def update_task(task_id: int, **fields) -> bool:
    allowed = {"title", "category", "due_date", "is_priority",
               "recurrence", "remind_at", "remind_sent"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    set_clause = ", ".join(f"{k} = %s" for k in updates)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = %s",
                (*updates.values(), task_id),
            )
            return cur.rowcount > 0


def set_priority(task_id: int, is_priority: bool) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET is_priority = %s WHERE id = %s",
                (bool(is_priority), task_id),
            )
            return cur.rowcount > 0


def defer_task(task_id: int, days: int = 1) -> date | None:
    """Push the due_date back by `days`. If no due date, set to today + days.
    Returns the resulting due date, or None if the task is missing."""
    task = get_task(task_id)
    if not task:
        return None
    current = task.get("due_date")
    if current is None:
        new_due = date.today() + timedelta(days=days)
    else:
        if isinstance(current, datetime):
            current = current.date()
        new_due = current + timedelta(days=days)
    update_task(task_id, due_date=new_due)
    return new_due


def search_tasks(query: str, include_done: bool = False) -> list[dict]:
    """LIKE-search title and raw_text. Escapes %/_ with backslash."""
    if not query:
        return []
    safe = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{safe}%"
    extra = "(t.title LIKE %s ESCAPE '\\\\' OR t.raw_text LIKE %s ESCAPE '\\\\')"
    if include_done:
        sql = """
            SELECT t.id, t.title, t.category, t.due_date, t.created_at,
                   t.is_priority, t.is_done, t.done_at,
                   COALESCE(a.cnt, 0) AS attachment_count
            FROM tasks t
            LEFT JOIN (
                SELECT task_id, COUNT(*) AS cnt FROM attachments GROUP BY task_id
            ) a ON a.task_id = t.id
            WHERE """ + extra + """
            ORDER BY t.is_done ASC, t.is_priority DESC,
                     t.due_date IS NULL, t.due_date ASC, t.created_at ASC
        """
        params = (pattern, pattern)
    else:
        sql = _select_open_tasks_sql(extra_where=extra)
        params = (pattern, pattern)
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()


def get_tasks_by_category(category: str) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(_select_open_tasks_sql("t.category = %s"), (category,))
            return cur.fetchall()


def get_done_tasks(days: int = 7) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, category, due_date, created_at, done_at, is_priority
                FROM tasks
                WHERE is_done = TRUE
                  AND done_at >= NOW() - INTERVAL %s DAY
                ORDER BY done_at DESC
            """, (int(days),))
            return cur.fetchall()


def add_attachment(task_id: int, file_id: str, file_unique_id: str,
                   kind: str, file_name: str | None = None,
                   mime_type: str | None = None,
                   caption: str | None = None) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO attachments (task_id, file_id, file_unique_id, kind, "
                "file_name, mime_type, caption) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (task_id, file_id, file_unique_id, kind, file_name, mime_type, caption),
            )
            return conn.insert_id()


def get_attachments(task_id: int) -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, task_id, file_id, file_unique_id, kind, file_name, "
                "mime_type, caption, created_at FROM attachments "
                "WHERE task_id = %s ORDER BY id ASC",
                (task_id,),
            )
            return cur.fetchall()


def count_attachments(task_id: int) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM attachments WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
            return int(row["c"]) if row else 0


def set_notion_page_id(task_id: int, page_id: str, synced_at: datetime | None = None) -> None:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET notion_page_id = %s, notion_synced_at = %s WHERE id = %s",
                (page_id, synced_at, task_id),
            )


def get_tasks_without_notion_page() -> list[dict]:
    """Return open tasks that have no linked Notion page yet."""
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT * FROM tasks
                WHERE notion_page_id IS NULL
                ORDER BY created_at ASC
            """)
            return cur.fetchall()


def get_stats() -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT category,
                       SUM(is_done = FALSE) AS open,
                       SUM(is_done = TRUE)  AS done
                FROM tasks
                GROUP BY category
                ORDER BY open DESC
            """)
            return cur.fetchall()
