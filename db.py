import pymysql
import config
from datetime import date, datetime


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
                    notion_page_id VARCHAR(36) NULL DEFAULT NULL,
                    notion_synced_at DATETIME NULL DEFAULT NULL
                ) CHARACTER SET utf8mb4
            """)
            # Migrate existing installations that don't have the Notion columns yet
            cur.execute("""
                ALTER TABLE tasks
                    ADD COLUMN IF NOT EXISTS notion_page_id VARCHAR(36) NULL DEFAULT NULL,
                    ADD COLUMN IF NOT EXISTS notion_synced_at DATETIME NULL DEFAULT NULL
            """)


def add_task(raw_text: str, title: str, category: str, due_date: date | None) -> int:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO tasks (raw_text, title, category, due_date) VALUES (%s, %s, %s, %s)",
                (raw_text, title, category, due_date),
            )
            return conn.insert_id()


def get_open_tasks() -> list[dict]:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, title, category, due_date, created_at
                FROM tasks
                WHERE is_done = FALSE
                ORDER BY due_date IS NULL, due_date ASC, created_at ASC
            """)
            return cur.fetchall()


def get_summary_tasks() -> dict:
    """Return tasks bucketed for the daily summary."""
    today = date.today()
    with _conn() as conn:
        with conn.cursor() as cur:
            # Due within 7 days (including overdue)
            cur.execute("""
                SELECT id, title, category, due_date, created_at
                FROM tasks
                WHERE is_done = FALSE AND due_date IS NOT NULL AND due_date <= DATE_ADD(%s, INTERVAL 7 DAY)
                ORDER BY due_date ASC
            """, (today,))
            due_soon = cur.fetchall()

            # No due date, added more than 30 days ago
            cur.execute("""
                SELECT id, title, category, due_date, created_at
                FROM tasks
                WHERE is_done = FALSE AND due_date IS NULL AND created_at <= DATE_SUB(%s, INTERVAL 30 DAY)
                ORDER BY created_at ASC
            """, (today,))
            old_noduedate = cur.fetchall()

    # Split due_soon into overdue vs upcoming
    overdue = [t for t in due_soon if t["due_date"] and t["due_date"] < today]
    upcoming = [t for t in due_soon if t["due_date"] and t["due_date"] >= today]
    old_noduedate = [t for t in old_noduedate]

    return {"overdue": overdue, "upcoming": upcoming, "old_noduedate": old_noduedate}


def mark_done(task_id: int) -> bool:
    with _conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tasks SET is_done = TRUE, done_at = %s WHERE id = %s AND is_done = FALSE",
                (datetime.now(), task_id),
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
            cur.execute("SELECT * FROM tasks WHERE id = %s", (task_id,))
            return cur.fetchone()


def update_task(task_id: int, **fields) -> bool:
    allowed = {"title", "category", "due_date"}
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
