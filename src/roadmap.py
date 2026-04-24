"""
Roadmap-Feature (v6.7.26)
==========================
Öffentliche Roadmap mit Voting. Einträge werden ausschließlich vom
Global-Admin gepflegt (Title, Beschreibung, Status, Kategorie, Priorität,
Ziel-Version). Jeder eingeloggte User kann pro Item genau eine Stimme
vergeben (toggle).

Tabellen:
  roadmap_items  — Items selbst, Status-Enum, Denorm-Vote-Count
  roadmap_votes  — Voting-Relation, UNIQUE(item_id, user_id)

Status-Werte:
  idea         — Vorschlag, noch nicht bewertet
  planned      — Wird gemacht, aber noch nicht begonnen
  in_progress  — In Arbeit
  done         — Erledigt + released
  rejected     — Abgelehnt / wird nicht umgesetzt
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("printix.roadmap")

STATUS_VALUES = ("idea", "planned", "in_progress", "done", "rejected")
CATEGORY_VALUES = ("feature", "fix", "improvement", "research")
PRIORITY_VALUES = ("low", "medium", "high")


# ─── Schema-Migration ────────────────────────────────────────────────────────

def init_roadmap_schema() -> None:
    """Erzeugt die Tabellen idempotent."""
    from db import _conn
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS roadmap_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                title           TEXT NOT NULL,
                description     TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'idea',
                category        TEXT NOT NULL DEFAULT 'feature',
                priority        TEXT NOT NULL DEFAULT 'medium',
                target_version  TEXT NOT NULL DEFAULT '',
                vote_count      INTEGER NOT NULL DEFAULT 0,
                created_by      TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_roadmap_status
                ON roadmap_items (status);
            CREATE INDEX IF NOT EXISTS idx_roadmap_votes
                ON roadmap_items (vote_count DESC);

            CREATE TABLE IF NOT EXISTS roadmap_votes (
                item_id     INTEGER NOT NULL REFERENCES roadmap_items(id) ON DELETE CASCADE,
                user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (item_id, user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_roadmap_votes_user
                ON roadmap_votes (user_id);
        """)

        # v6.7.29: User-Suggestion-Feature — Feedback-Page wird damit abgelöst.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(roadmap_items)").fetchall()}
        if "submitted_by_user_id" not in cols:
            conn.execute(
                "ALTER TABLE roadmap_items ADD COLUMN submitted_by_user_id TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: roadmap_items.submitted_by_user_id hinzugefügt")
        if "pending_review" not in cols:
            conn.execute(
                "ALTER TABLE roadmap_items ADD COLUMN pending_review INTEGER NOT NULL DEFAULT 0"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_roadmap_pending ON roadmap_items (pending_review)"
            )
            logger.info("Migration: roadmap_items.pending_review hinzugefügt")

    logger.info("Migration: roadmap_items + roadmap_votes geprüft/erstellt")


# ─── CRUD für Items ──────────────────────────────────────────────────────────

def list_items(status: str = "", category: str = "",
               include_pending: bool = False,
               viewer_user_id: str = "") -> list[dict]:
    """Liefert Items optional gefiltert, sortiert nach Status-Ordnung + Votes.

    v6.7.29: Suggestion-Support.
    - `include_pending=False` (Default): Nur approved Items (pending_review=0)
      plus die eigenen pending Items des Viewers (wenn `viewer_user_id` gesetzt).
    - `include_pending=True`: ALLE Items inkl. fremder pending — nur für Admins.

    Pending Items werden immer ZUERST angezeigt (sortiert oben).
    """
    from db import _conn
    order = """CASE
        WHEN pending_review = 1 THEN -1
        WHEN status = 'in_progress' THEN 0
        WHEN status = 'planned' THEN 1
        WHEN status = 'idea' THEN 2
        WHEN status = 'done' THEN 3
        WHEN status = 'rejected' THEN 4
        ELSE 5 END"""
    q = "SELECT * FROM roadmap_items WHERE 1=1"
    params: list = []
    if status and status in STATUS_VALUES:
        q += " AND status = ?"
        params.append(status)
    if category and category in CATEGORY_VALUES:
        q += " AND category = ?"
        params.append(category)
    if not include_pending:
        # Approved ODER eigenes pending-Item
        if viewer_user_id:
            q += " AND (pending_review = 0 OR submitted_by_user_id = ?)"
            params.append(viewer_user_id)
        else:
            q += " AND pending_review = 0"
    q += f" ORDER BY {order}, vote_count DESC, created_at DESC"
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def create_suggestion(
    title: str, description: str, submitted_by_user_id: str,
) -> Optional[int]:
    """v6.7.29: Item-Vorschlag durch einen normalen User (non-admin).
    Landet automatisch als `status='idea'`, `pending_review=1`.
    Admin kann es danach approven oder rejecten.
    """
    from db import _conn
    if not title.strip() or not submitted_by_user_id:
        return None
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO roadmap_items
               (title, description, status, category, priority, target_version,
                vote_count, created_by, created_at, updated_at,
                submitted_by_user_id, pending_review)
               VALUES (?,?,?,?,?,?,0,?,?,?,?,1)""",
            (title.strip(), description.strip(), "idea", "feature", "medium", "",
             submitted_by_user_id, now, now, submitted_by_user_id),
        )
        new_id = cur.lastrowid
    logger.info("Roadmap: Suggestion #%s von user=%s — '%s' (pending_review)",
                new_id, submitted_by_user_id, title)
    return new_id


def approve_item(item_id: int) -> bool:
    """v6.7.29: Admin gibt ein pending Item frei → für alle sichtbar."""
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE roadmap_items SET pending_review = 0, updated_at = ? "
            "WHERE id = ? AND pending_review = 1",
            (now, item_id),
        )
    logger.info("Roadmap: Item #%s approved (%d rows)", item_id, cur.rowcount)
    return cur.rowcount > 0


def reject_item(item_id: int) -> bool:
    """v6.7.29: Admin lehnt ein pending Item ab → status=rejected, aus Pending-Queue."""
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE roadmap_items SET pending_review = 0, status = 'rejected', "
            "updated_at = ? WHERE id = ? AND pending_review = 1",
            (now, item_id),
        )
    logger.info("Roadmap: Item #%s rejected (%d rows)", item_id, cur.rowcount)
    return cur.rowcount > 0


def count_pending() -> int:
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM roadmap_items WHERE pending_review = 1"
        ).fetchone()
    return int(row["c"]) if row else 0


def get_item(item_id: int) -> Optional[dict]:
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM roadmap_items WHERE id = ?", (item_id,),
        ).fetchone()
    return dict(row) if row else None


def create_item(
    title: str,
    description: str = "",
    status: str = "idea",
    category: str = "feature",
    priority: str = "medium",
    target_version: str = "",
    created_by: str = "",
) -> int:
    from db import _conn
    if status not in STATUS_VALUES:
        status = "idea"
    if category not in CATEGORY_VALUES:
        category = "feature"
    if priority not in PRIORITY_VALUES:
        priority = "medium"
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO roadmap_items
               (title, description, status, category, priority, target_version,
                vote_count, created_by, created_at, updated_at)
               VALUES (?,?,?,?,?,?,0,?,?,?)""",
            (title.strip(), description.strip(), status, category, priority,
             target_version.strip(), created_by, now, now),
        )
        new_id = cur.lastrowid
    logger.info("Roadmap: Item #%s angelegt — '%s' (status=%s)",
                new_id, title, status)
    return new_id


def update_item(
    item_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    category: Optional[str] = None,
    priority: Optional[str] = None,
    target_version: Optional[str] = None,
) -> bool:
    from db import _conn
    updates: list[tuple[str, object]] = []
    if title is not None:
        updates.append(("title", title.strip()))
    if description is not None:
        updates.append(("description", description.strip()))
    if status is not None and status in STATUS_VALUES:
        updates.append(("status", status))
    if category is not None and category in CATEGORY_VALUES:
        updates.append(("category", category))
    if priority is not None and priority in PRIORITY_VALUES:
        updates.append(("priority", priority))
    if target_version is not None:
        updates.append(("target_version", target_version.strip()))
    if not updates:
        return False
    now = datetime.now(timezone.utc).isoformat()
    updates.append(("updated_at", now))
    set_sql = ", ".join(f"{k} = ?" for k, _ in updates)
    params = [v for _, v in updates] + [item_id]
    with _conn() as conn:
        cur = conn.execute(f"UPDATE roadmap_items SET {set_sql} WHERE id = ?", params)
    return cur.rowcount > 0


def delete_item(item_id: int) -> bool:
    from db import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM roadmap_items WHERE id = ?", (item_id,))
    logger.info("Roadmap: Item #%s gelöscht (%d rows)", item_id, cur.rowcount)
    return cur.rowcount > 0


# ─── Voting ──────────────────────────────────────────────────────────────────

def toggle_vote(item_id: int, user_id: str) -> str:
    """Toggle — wenn User schon gevotet hat → vote entfernen, sonst hinzufügen.
    Returns 'added' / 'removed' / 'error'.
    """
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT 1 FROM roadmap_votes WHERE item_id = ? AND user_id = ?",
            (item_id, user_id),
        ).fetchone()
        if existing:
            conn.execute(
                "DELETE FROM roadmap_votes WHERE item_id = ? AND user_id = ?",
                (item_id, user_id),
            )
            conn.execute(
                "UPDATE roadmap_items SET vote_count = MAX(0, vote_count - 1) "
                "WHERE id = ?", (item_id,),
            )
            return "removed"
        else:
            try:
                conn.execute(
                    "INSERT INTO roadmap_votes (item_id, user_id, created_at) "
                    "VALUES (?, ?, ?)", (item_id, user_id, now),
                )
                conn.execute(
                    "UPDATE roadmap_items SET vote_count = vote_count + 1 "
                    "WHERE id = ?", (item_id,),
                )
                return "added"
            except Exception as e:
                logger.warning("Roadmap-Vote failed: %s", e)
                return "error"


def get_user_votes(user_id: str) -> set[int]:
    """Alle Item-IDs, für die ein User gevotet hat."""
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            "SELECT item_id FROM roadmap_votes WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    return {r["item_id"] for r in rows}


def count_items_by_status() -> dict[str, int]:
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS c FROM roadmap_items GROUP BY status"
        ).fetchall()
    result = {s: 0 for s in STATUS_VALUES}
    for r in rows:
        if r["status"] in result:
            result[r["status"]] = r["c"]
    return result
