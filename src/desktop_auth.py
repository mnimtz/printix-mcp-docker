"""
Desktop-Client Authentifizierung + Targets (v6.7.31)
=====================================================
Token-basierte Auth für den „Printix Send"-Windows-Client (und später
andere Desktop-Clients). Ein User kann beliebig viele aktive Tokens
haben (ein Token = ein Gerät); jeder Token trägt optional einen
`device_name` (vom Client beim Login mitgesendet).

Tabelle:
  desktop_tokens
    token         TEXT PRIMARY KEY    — generiert, 32 Bytes URL-safe
    user_id       TEXT REFERENCES users(id) — wem gehört der Token
    device_name   TEXT                 — „Marcus-Laptop", „Empfang-PC" usw.
    created_at    TEXT
    last_used_at  TEXT                 — für UI („zuletzt gesehen am…")

Tokens haben keinen Ablauf in der MVP — der User kann sie explizit
widerrufen (später in `/settings`).
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("printix.desktop_auth")


def init_desktop_schema() -> None:
    """Legt die `desktop_tokens`-Tabelle idempotent an."""
    from db import _conn
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS desktop_tokens (
                token         TEXT PRIMARY KEY,
                user_id       TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                device_name   TEXT NOT NULL DEFAULT '',
                created_at    TEXT NOT NULL,
                last_used_at  TEXT NOT NULL DEFAULT ''
            );
            CREATE INDEX IF NOT EXISTS idx_desktop_tokens_user
                ON desktop_tokens (user_id);
        """)
    logger.info("Migration: desktop_tokens-Tabelle geprüft/erstellt")


def create_token(user_id: str, device_name: str = "") -> str:
    """Generiert einen neuen Token und persistiert ihn."""
    from db import _conn
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO desktop_tokens (token, user_id, device_name, "
            "created_at, last_used_at) VALUES (?, ?, ?, ?, ?)",
            (token, user_id, (device_name or "").strip(), now, now),
        )
    logger.info("Desktop-Token angelegt für user=%s device=%s",
                user_id, device_name or "-")
    return token


def validate_token(token: str) -> Optional[dict]:
    """Prüft ob ein Token gültig ist und liefert User-Info zurück.

    Aktualisiert last_used_at auf „jetzt" (für Audit/UI).
    """
    if not token:
        return None
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT t.token, t.user_id, t.device_name, t.created_at,
                      u.username, u.email, u.full_name, u.role_type,
                      u.status, u.parent_user_id, u.printix_user_id
               FROM desktop_tokens t
               JOIN users u ON u.id = t.user_id
               WHERE t.token = ?""",
            (token,),
        ).fetchone()
        if not row:
            return None
        # last_used_at bumpen
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE desktop_tokens SET last_used_at = ? WHERE token = ?",
            (now, token),
        )
    return dict(row)


def revoke_token(token: str) -> bool:
    """Widerruft einen Token explizit."""
    from db import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM desktop_tokens WHERE token = ?", (token,))
    return cur.rowcount > 0


def list_tokens_for_user(user_id: str) -> list[dict]:
    """Alle aktiven Tokens eines Users (für `/settings` → Desktop-Clients-Liste)."""
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            "SELECT token, device_name, created_at, last_used_at "
            "FROM desktop_tokens WHERE user_id = ? "
            "ORDER BY last_used_at DESC",
            (user_id,),
        ).fetchall()
    # Token maskieren für UI (nur letzte 8 Zeichen zeigen)
    result = []
    for r in rows:
        d = dict(r)
        d["token_preview"] = f"…{d['token'][-8:]}"
        del d["token"]
        result.append(d)
    return result
