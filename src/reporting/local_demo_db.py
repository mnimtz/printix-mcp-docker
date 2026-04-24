"""
Local Demo Database — SQLite-basierte Demo-Daten (v4.4.0)
=========================================================
Speichert Demo-Daten lokal in /data/demo_data.db statt in Azure SQL.
Damit reicht ein READ-ONLY Zugriff auf die Printix Azure SQL.

Schema spiegelt die Printix BI-Datenbank (vereinfacht für SQLite):
  demo_networks, demo_users, demo_printers, demo_jobs,
  demo_tracking_data, demo_jobs_scan, demo_jobs_copy,
  demo_jobs_copy_details, demo_sessions
"""

import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DEMO_DB_PATH = os.environ.get("DEMO_DB_PATH", "/data/demo_data.db")


@contextmanager
def demo_conn():
    """Kontextmanager für die Demo-SQLite-DB."""
    os.makedirs(os.path.dirname(DEMO_DB_PATH) if os.path.dirname(DEMO_DB_PATH) else ".", exist_ok=True)
    conn = sqlite3.connect(DEMO_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_demo_db() -> dict:
    """
    Erstellt alle Demo-Tabellen in der lokalen SQLite-DB (idempotent).
    Ersetzt setup_schema() für Azure SQL.
    """
    with demo_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS demo_networks (
                id          TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                name        TEXT NOT NULL,
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_users (
                id          TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                email       TEXT NOT NULL,
                name        TEXT NOT NULL,
                department  TEXT NOT NULL DEFAULT '',
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_printers (
                id          TEXT PRIMARY KEY,
                tenant_id   TEXT NOT NULL,
                name        TEXT NOT NULL,
                model_name  TEXT NOT NULL DEFAULT '',
                vendor_name TEXT NOT NULL DEFAULT '',
                network_id  TEXT NOT NULL DEFAULT '',
                location    TEXT NOT NULL DEFAULT '',
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_jobs (
                id              TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                color           INTEGER NOT NULL DEFAULT 0,
                duplex          INTEGER NOT NULL DEFAULT 0,
                page_count      INTEGER NOT NULL DEFAULT 1,
                paper_size      TEXT NOT NULL DEFAULT 'A4',
                printer_id      TEXT NOT NULL DEFAULT '',
                submit_time     TEXT NOT NULL,
                tenant_user_id  TEXT NOT NULL DEFAULT '',
                filename        TEXT NOT NULL DEFAULT '',
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_tracking_data (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id           TEXT NOT NULL,
                tenant_id        TEXT NOT NULL,
                page_count       INTEGER NOT NULL DEFAULT 1,
                color            INTEGER NOT NULL DEFAULT 0,
                duplex           INTEGER NOT NULL DEFAULT 0,
                print_time       TEXT NOT NULL,
                printer_id       TEXT NOT NULL DEFAULT '',
                print_job_status TEXT NOT NULL DEFAULT 'PRINT_OK',
                demo_session_id  TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_jobs_scan (
                id              TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                printer_id      TEXT NOT NULL DEFAULT '',
                tenant_user_id  TEXT NOT NULL DEFAULT '',
                scan_time       TEXT NOT NULL,
                page_count      INTEGER NOT NULL DEFAULT 1,
                color           INTEGER NOT NULL DEFAULT 0,
                workflow_name   TEXT NOT NULL DEFAULT '',
                filename        TEXT NOT NULL DEFAULT '',
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_jobs_copy (
                id              TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                printer_id      TEXT NOT NULL DEFAULT '',
                tenant_user_id  TEXT NOT NULL DEFAULT '',
                copy_time       TEXT NOT NULL,
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_jobs_copy_details (
                id              TEXT PRIMARY KEY,
                job_id          TEXT NOT NULL,
                page_count      INTEGER NOT NULL DEFAULT 1,
                paper_size      TEXT NOT NULL DEFAULT 'A4',
                duplex          INTEGER NOT NULL DEFAULT 0,
                color           INTEGER NOT NULL DEFAULT 0,
                demo_session_id TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS demo_sessions (
                session_id      TEXT PRIMARY KEY,
                tenant_id       TEXT NOT NULL,
                demo_tag        TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL,
                params_json     TEXT NOT NULL DEFAULT '{}',
                status          TEXT NOT NULL DEFAULT 'active',
                user_count      INTEGER NOT NULL DEFAULT 0,
                printer_count   INTEGER NOT NULL DEFAULT 0,
                network_count   INTEGER NOT NULL DEFAULT 0,
                print_job_count INTEGER NOT NULL DEFAULT 0,
                scan_job_count  INTEGER NOT NULL DEFAULT 0,
                copy_job_count  INTEGER NOT NULL DEFAULT 0
            );

            -- Indexes
            CREATE INDEX IF NOT EXISTS idx_demo_td_tenant
                ON demo_tracking_data (tenant_id, print_time);
            CREATE INDEX IF NOT EXISTS idx_demo_td_session
                ON demo_tracking_data (demo_session_id);
            CREATE INDEX IF NOT EXISTS idx_demo_jobs_tenant
                ON demo_jobs (tenant_id, submit_time);
            CREATE INDEX IF NOT EXISTS idx_demo_users_tenant
                ON demo_users (tenant_id, email);
            CREATE INDEX IF NOT EXISTS idx_demo_printers_tenant
                ON demo_printers (tenant_id, network_id);
            CREATE INDEX IF NOT EXISTS idx_demo_scan_tenant
                ON demo_jobs_scan (tenant_id, scan_time);
            CREATE INDEX IF NOT EXISTS idx_demo_copy_tenant
                ON demo_jobs_copy (tenant_id, copy_time);
            CREATE INDEX IF NOT EXISTS idx_demo_sessions_tenant
                ON demo_sessions (tenant_id, status);
        """)

    logger.info("Demo-DB initialisiert: %s", DEMO_DB_PATH)
    return {"success": True, "message": "Demo-DB bereit."}


def demo_bulk_insert(table: str, columns: list[str], rows: list[tuple]) -> int:
    """Bulk-Insert in die Demo-SQLite-DB."""
    if not rows:
        return 0
    placeholders = ",".join(["?"] * len(columns))
    col_str = ",".join(columns)
    sql = f"INSERT INTO {table} ({col_str}) VALUES ({placeholders})"
    with demo_conn() as conn:
        conn.executemany(sql, rows)
    return len(rows)


def demo_execute(sql: str, params: tuple = ()) -> int:
    """Einzelnes Statement in der Demo-DB ausführen."""
    with demo_conn() as conn:
        cur = conn.execute(sql, params)
        return cur.rowcount if cur.rowcount >= 0 else 0


def demo_query(sql: str, params: tuple = ()) -> list[dict]:
    """Query gegen die Demo-DB — gibt list[dict] zurück."""
    with demo_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_demo_sessions(tenant_id: str) -> list[dict]:
    """Gibt alle Demo-Sessions eines Tenants zurück."""
    return demo_query(
        "SELECT * FROM demo_sessions WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    )


def has_active_demo(tenant_id: str) -> bool:
    """Prüft ob aktive Demo-Daten für einen Tenant existieren."""
    rows = demo_query(
        "SELECT 1 FROM demo_sessions WHERE tenant_id = ? AND status = 'active' LIMIT 1",
        (tenant_id,),
    )
    return len(rows) > 0


def rollback_demo_session(session_id: str) -> dict:
    """Löscht eine spezifische Demo-Session und alle zugehörigen Daten."""
    tables = [
        "demo_tracking_data", "demo_jobs", "demo_jobs_scan",
        "demo_jobs_copy", "demo_jobs_copy_details",
        "demo_users", "demo_printers", "demo_networks",
    ]
    deleted = {}
    with demo_conn() as conn:
        for tbl in tables:
            cur = conn.execute(f"DELETE FROM {tbl} WHERE demo_session_id = ?", (session_id,))
            deleted[tbl] = cur.rowcount
        conn.execute("DELETE FROM demo_sessions WHERE session_id = ?", (session_id,))
    return {"success": True, "deleted": deleted}


def rollback_all_demos(tenant_id: str) -> dict:
    """Löscht ALLE Demo-Daten eines Tenants."""
    tables_with_tenant_id = [
        "demo_tracking_data", "demo_jobs", "demo_jobs_scan",
        "demo_jobs_copy",
        "demo_users", "demo_printers", "demo_networks",
    ]
    deleted = {}
    with demo_conn() as conn:
        # demo_jobs_copy_details hat kein tenant_id — über demo_jobs_copy.id joinen
        cur = conn.execute(
            "DELETE FROM demo_jobs_copy_details WHERE job_id IN "
            "(SELECT id FROM demo_jobs_copy WHERE tenant_id = ?)",
            (tenant_id,),
        )
        deleted["demo_jobs_copy_details"] = cur.rowcount
        for tbl in tables_with_tenant_id:
            cur = conn.execute(f"DELETE FROM {tbl} WHERE tenant_id = ?", (tenant_id,))
            deleted[tbl] = cur.rowcount
        conn.execute("DELETE FROM demo_sessions WHERE tenant_id = ?", (tenant_id,))
    return {"success": True, "deleted": deleted}


def query_demo_tracking_data(tenant_id: str, start_date: str, end_date: str) -> list[dict]:
    """
    Holt Demo-Tracking-Daten für einen Zeitraum (für Merge mit Azure SQL Ergebnissen).
    Gibt Rohdaten zurück — Aggregation passiert im Caller.
    """
    return demo_query(
        """
        SELECT td.*, j.filename, j.paper_size,
               u.name AS user_name, u.email AS user_email, u.department,
               p.name AS printer_name, p.model_name, p.vendor_name,
               p.location, p.network_id,
               n.name AS network_name
        FROM demo_tracking_data td
        LEFT JOIN demo_jobs j ON td.job_id = j.id
        LEFT JOIN demo_users u ON j.tenant_user_id = u.id
        LEFT JOIN demo_printers p ON td.printer_id = p.id
        LEFT JOIN demo_networks n ON p.network_id = n.id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time < ?
          AND EXISTS (
              SELECT 1 FROM demo_sessions ds
              WHERE ds.tenant_id = td.tenant_id AND ds.status = 'active'
          )
        ORDER BY td.print_time
        """,
        (tenant_id, start_date, end_date + "T23:59:59"),
    )


def query_demo_scan_jobs(tenant_id: str, start_date: str, end_date: str) -> list[dict]:
    """Holt Demo-Scan-Jobs mit JOINs für Merge (v4.4.14)."""
    return demo_query(
        """
        SELECT js.id, js.tenant_id, js.printer_id, js.tenant_user_id,
               js.scan_time, js.page_count, js.color, js.workflow_name,
               js.filename, js.demo_session_id,
               u.name AS user_name, u.email AS user_email, u.department,
               p.name AS printer_name, p.model_name, p.vendor_name,
               p.location, p.network_id,
               n.name AS network_name
        FROM demo_jobs_scan js
        LEFT JOIN demo_users u ON js.tenant_user_id = u.id
        LEFT JOIN demo_printers p ON js.printer_id = p.id
        LEFT JOIN demo_networks n ON p.network_id = n.id
        WHERE js.tenant_id = ?
          AND js.scan_time >= ?
          AND js.scan_time < ?
          AND EXISTS (
              SELECT 1 FROM demo_sessions ds
              WHERE ds.tenant_id = js.tenant_id AND ds.status = 'active'
          )
        ORDER BY js.scan_time
        """,
        (tenant_id, start_date, end_date + "T23:59:59"),
    )


def query_demo_copy_jobs(tenant_id: str, start_date: str, end_date: str) -> list[dict]:
    """Holt Demo-Copy-Jobs mit Details und JOINs für Merge (v4.4.14)."""
    return demo_query(
        """
        SELECT jc.id, jc.tenant_id, jc.printer_id, jc.tenant_user_id,
               jc.copy_time, jc.demo_session_id,
               jcd.page_count, jcd.paper_size, jcd.duplex, jcd.color,
               u.name AS user_name, u.email AS user_email, u.department,
               p.name AS printer_name, p.model_name, p.vendor_name,
               p.location, p.network_id,
               n.name AS network_name
        FROM demo_jobs_copy jc
        LEFT JOIN demo_jobs_copy_details jcd ON jcd.job_id = jc.id
        LEFT JOIN demo_users u ON jc.tenant_user_id = u.id
        LEFT JOIN demo_printers p ON jc.printer_id = p.id
        LEFT JOIN demo_networks n ON p.network_id = n.id
        WHERE jc.tenant_id = ?
          AND jc.copy_time >= ?
          AND jc.copy_time < ?
          AND EXISTS (
              SELECT 1 FROM demo_sessions ds
              WHERE ds.tenant_id = jc.tenant_id AND ds.status = 'active'
          )
        ORDER BY jc.copy_time
        """,
        (tenant_id, start_date, end_date + "T23:59:59"),
    )


def query_demo_jobs(tenant_id: str, start_date: str, end_date: str) -> list[dict]:
    """Holt Demo-Jobs mit JOINs (fuer queue_stats, off_hours, sensitive_docs) (v4.4.14)."""
    return demo_query(
        """
        SELECT j.*,
               u.name AS user_name, u.email AS user_email, u.department,
               p.name AS printer_name, p.model_name, p.vendor_name,
               p.location, p.network_id,
               n.name AS network_name
        FROM demo_jobs j
        LEFT JOIN demo_users u ON j.tenant_user_id = u.id
        LEFT JOIN demo_printers p ON j.printer_id = p.id
        LEFT JOIN demo_networks n ON p.network_id = n.id
        WHERE j.tenant_id = ?
          AND j.submit_time >= ?
          AND j.submit_time < ?
          AND EXISTS (
              SELECT 1 FROM demo_sessions ds
              WHERE ds.tenant_id = j.tenant_id AND ds.status = 'active'
          )
        ORDER BY j.submit_time
        """,
        (tenant_id, start_date, end_date + "T23:59:59"),
    )


# ─── DB beim Import initialisieren ────────────────────────────────────────────

try:
    init_demo_db()
except Exception as _e:
    logger.warning("Demo-DB init beim Import fehlgeschlagen: %s", _e)
