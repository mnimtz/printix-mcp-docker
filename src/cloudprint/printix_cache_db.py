"""
Persistenter Printix-Cache (v6.7.5)
====================================
DB-basierter Cache für Printix-Entities (User, später Printer, Workstations).

Architektur:
  - Beim ersten Tenant-Setup → einmaliges sync_all_for_tenant()
  - Beim Login (älter als 24h) → Background-Refresh
  - Manueller Refresh-Button → User-getriggert
  - Lookups gehen IMMER gegen die DB (nie live API beim IPP-Print)

Wichtig: Diese Tabellen haben NICHTS mit der MCP-`users`-Tabelle zu tun.
Hier liegen Spiegel-Daten der Printix-User aus dem jeweiligen Printix-Tenant.

In v6.7.5 implementiert: Users.
Geplant für v6.7.6+: Printers, Queues, Workstations.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("printix.cloudprint.cache_db")


# ─── Sync ────────────────────────────────────────────────────────────────────

def sync_users_for_tenant(tenant_id: str, printix_tenant_id: str, client) -> dict:
    """Pullt alle Printix-User dieses Tenants und schreibt sie in die DB.

    UPSERT-Logik via UNIQUE(tenant_id, printix_user_id).
    User die in Printix gelöscht wurden → bleiben in der DB als „stale"
    (wir löschen NICHT, damit alte Logs noch nachvollziehbar sind).

    Returns: {"count": N, "status": "ok" | "error", "error": str}
    """
    from db import _conn

    now = datetime.now(timezone.utc).isoformat()
    try:
        users = client.list_all_users(page_size=200)
    except Exception as e:
        err = str(e)[:300]
        logger.error("Sync USERS failed for tenant %s: %s", tenant_id, e)
        _update_sync_status(tenant_id, "users", "error", error=err, count=0)
        return {"count": 0, "status": "error", "error": err}

    if not isinstance(users, list):
        users = []

    inserted = 0
    updated = 0
    with _conn() as conn:
        for u in users:
            if not isinstance(u, dict):
                continue
            pid = (u.get("id") or u.get("userId") or "").strip()
            if not pid:
                continue
            username   = (u.get("username") or u.get("userName") or "").strip()
            email      = (u.get("email") or u.get("userPrincipalName") or "").strip()
            full_name  = (u.get("fullName") or u.get("name") or "").strip()
            role       = (u.get("role") or u.get("userRole") or "").strip()
            raw_json   = json.dumps(u, ensure_ascii=True, sort_keys=True)

            existing = conn.execute(
                "SELECT id FROM cached_printix_users "
                "WHERE tenant_id = ? AND printix_user_id = ?",
                (tenant_id, pid),
            ).fetchone()

            if existing:
                conn.execute(
                    """UPDATE cached_printix_users
                       SET username=?, email=?, full_name=?, role=?,
                           raw_json=?, synced_at=?, printix_tenant_id=?
                       WHERE tenant_id=? AND printix_user_id=?""",
                    (username, email, full_name, role, raw_json, now,
                     printix_tenant_id, tenant_id, pid),
                )
                updated += 1
            else:
                conn.execute(
                    """INSERT INTO cached_printix_users
                       (tenant_id, printix_tenant_id, printix_user_id,
                        username, email, full_name, role, raw_json, synced_at)
                       VALUES (?,?,?,?,?,?,?,?,?)""",
                    (tenant_id, printix_tenant_id, pid,
                     username, email, full_name, role, raw_json, now),
                )
                inserted += 1

    # v6.7.8: System-Manager manuell in den Cache einfügen.
    # Die Printix-User-Management-API liefert nur Rollen USER + GUEST_USER —
    # System/Site/Kiosk-Manager werden NIE zurückgegeben. Das führt dazu, dass
    # der MCP-Tenant-Admin (= typisch Printix-System-Manager des Tenants) im
    # Cache fehlt und Delegate-Print an ihn ins Leere läuft.
    # Pragma: den MCP-Tenant-Owner automatisch als SYSTEM_MANAGER einfügen.
    extra = _upsert_system_manager_from_tenant(tenant_id, printix_tenant_id, now)

    total = inserted + updated + extra
    logger.info(
        "Sync USERS tenant=%s OK: %d eingefügt, %d aktualisiert, %d System-Manager (%d total)",
        tenant_id, inserted, updated, extra, total,
    )
    _update_sync_status(tenant_id, "users", "ok", count=total)
    _check_username_collisions(tenant_id)
    return {
        "count": total, "inserted": inserted, "updated": updated,
        "system_managers": extra, "status": "ok",
    }


def _upsert_system_manager_from_tenant(tenant_id: str, printix_tenant_id: str,
                                         now: str) -> int:
    """Nimmt den MCP-Tenant-Owner und legt ihn als SYSTEM_MANAGER in
    cached_printix_users ab. Gibt zurück wieviele Einträge entstanden sind
    (0 oder 1).

    Hintergrund: Die Printix-User-Management-API liefert nie Manager-Rollen,
    dadurch fehlen sie für Delegate-Resolution. Der MCP-Admin kennt seine
    eigene Printix-Identität (Email) — die nehmen wir als autoritative
    Quelle für "System-Manager dieses Tenants".

    Edge case: wenn MCP-Admin-Email != Printix-System-Manager-Email (z.B.
    unterschiedliche Accounts), müsste der Admin die korrekte Email explizit
    konfigurieren — das ist aktuell nicht vorgesehen, kann später ergänzt
    werden über ein Admin-UI "manual Printix-user override".
    """
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT u.email, u.username, u.full_name
               FROM tenants t
               JOIN users u ON u.id = t.user_id
               WHERE t.id = ?""",
            (tenant_id,),
        ).fetchone()
        if not row or not row["email"]:
            return 0
        email = row["email"].strip()
        full_name = (row["full_name"] or email).strip()
        # v6.7.9: Username bewusst LEER lassen. Der MCP-Admin-Username ist NICHT
        # automatisch auch der Printix-Username — die Annahme war falsch und hat
        # in v6.7.8 dazu geführt, dass eingehende Prints mit Windows-Username
        # auf den SYSTEM_MANAGER-Eintrag gerouted wurden statt auf den echten
        # Printix-GUEST/USER. Matching läuft künftig nur noch über E-Mail.

        # Synthetic printix_user_id (nicht echt in Printix, dient nur als DB-Key).
        # Format: "mgr:<email-hash>" — eindeutig und nicht kollidierend
        # mit echten Printix-UUIDs.
        import hashlib
        synth_id = f"mgr:{hashlib.sha1(email.lower().encode()).hexdigest()[:16]}"

        existing = conn.execute(
            """SELECT id, role, username FROM cached_printix_users
               WHERE tenant_id = ? AND LOWER(email) = LOWER(?)""",
            (tenant_id, email),
        ).fetchone()
        if existing:
            # Schon durch die reguläre Sync-Schleife da (als USER oder GUEST_USER)
            # — nicht überschreiben.
            # v6.7.9: Falls ein älterer synthetischer SYSTEM_MANAGER-Eintrag mit
            # falschem Username drin ist (Legacy aus v6.7.8) → Username leeren.
            if (existing["role"] or "").upper() == "SYSTEM_MANAGER" and existing["username"]:
                conn.execute(
                    "UPDATE cached_printix_users SET username = '' WHERE id = ?",
                    (existing["id"],),
                )
                logger.info(
                    "Sync: Legacy-SYSTEM_MANAGER-Eintrag '%s' Username geleert "
                    "(v6.7.8-Fix)", email,
                )
            return 0
        conn.execute(
            """INSERT INTO cached_printix_users
               (tenant_id, printix_tenant_id, printix_user_id,
                username, email, full_name, role, raw_json, synced_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tenant_id, printix_tenant_id, synth_id,
             "", email, full_name, "SYSTEM_MANAGER",
             '{"source":"mcp-tenant-owner","synthetic":true}', now),
        )
        logger.info(
            "Sync: MCP-Tenant-Owner '%s' als SYSTEM_MANAGER in Cache eingefügt "
            "(für Delegate-Resolution)", email,
        )
        return 1


def _update_sync_status(tenant_id: str, entity_type: str, status: str,
                          error: str = "", count: int = 0) -> None:
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO cached_sync_status
               (tenant_id, entity_type, last_sync_at, last_sync_status,
                last_sync_error, synced_count)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(tenant_id, entity_type) DO UPDATE SET
                 last_sync_at=excluded.last_sync_at,
                 last_sync_status=excluded.last_sync_status,
                 last_sync_error=excluded.last_sync_error,
                 synced_count=excluded.synced_count""",
            (tenant_id, entity_type, now, status, error, count),
        )


def _check_username_collisions(tenant_id: str) -> None:
    """Detektiert ob ein Username (oder eine E-Mail) in mehreren Tenants
    auftaucht — würde unser User-basiertes Routing zerschießen.

    Bei Treffer: WARNING ins Log mit klarem Hinweis. Routing schlägt
    dann beim Print-Request fehl (statt falsch zu routen).
    """
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT LOWER(username) AS un, COUNT(DISTINCT tenant_id) AS tc
               FROM cached_printix_users
               WHERE username != ''
               GROUP BY LOWER(username)
               HAVING tc > 1
               LIMIT 20"""
        ).fetchall()
    for row in rows:
        logger.warning(
            "Cache: Username-Kollision — '%s' existiert in %d Tenants. "
            "IPP-Routing kann diese User nicht eindeutig zuordnen.",
            row["un"], row["tc"],
        )


# ─── Lookups (für IPP-Routing + UI) ──────────────────────────────────────────

def find_printix_user_by_identity(identity: str) -> Optional[dict]:
    """Sucht über alle gecachten Printix-User nach einem User-Identifier.

    Identifier kann sein:
      - Username       (z.B. 'marcus.nimtz')
      - Volle E-Mail   (z.B. 'marcus@nimtz.email')
      - Lokal-Part     (z.B. 'marcus.nimtz' → matcht 'marcus.nimtz@firma.de')

    Liefert None wenn kein Match oder ambiguous (mehrere Tenants).
    Liefert das User-Dict + Tenant-Info bei eindeutigem Treffer.
    """
    if not identity or not identity.strip():
        return None
    identity = identity.strip()
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT tenant_id, printix_tenant_id, printix_user_id,
                      username, email, full_name, role, raw_json
               FROM cached_printix_users
               WHERE LOWER(username) = LOWER(?)
                  OR LOWER(email)    = LOWER(?)
                  OR LOWER(email) LIKE LOWER(?)""",
            (identity, identity, f"{identity}@%"),
        ).fetchall()

    if not rows:
        logger.debug("Printix-User-Lookup: kein Match für '%s'", identity)
        return None

    # Eindeutigkeitscheck: alle Treffer müssen zum selben Tenant gehören.
    tenant_ids = {r["tenant_id"] for r in rows}
    if len(tenant_ids) > 1:
        logger.warning(
            "Printix-User-Lookup: AMBIGUOUS — '%s' matched in %d Tenants (%s). "
            "Routing wird abgelehnt.",
            identity, len(tenant_ids), tenant_ids,
        )
        return None

    # v6.7.9: Priorisierung bei mehreren Treffern im gleichen Tenant:
    #   1. Exact username-Match (bei regulären Rollen)
    #   2. Reguläre Rollen (USER, GUEST_USER) vor Management-Rollen (SYSTEM_MANAGER
    #      etc.) — System-Manager haben in Printix keine Print-Queue im klassischen
    #      Sinne, daher sollen eingehende Prints bevorzugt auf User-Accounts
    #      geroutet werden. Management-Rollen kommen nur zum Zug, wenn kein
    #      regulärer Match da ist (typisch: Delegate-an-System-Manager).
    def _rank(r):
        role = (r["role"] or "").upper()
        role_penalty = 0 if role in ("USER", "GUEST_USER") else 1
        exact_username = 0 if (r["username"] or "").lower() == identity.lower() else 1
        return (role_penalty, exact_username)

    best = sorted(rows, key=_rank)[0]
    return dict(best)


def get_cached_user_count(tenant_id: str) -> int:
    """Anzahl der gecachten Printix-User für einen Tenant."""
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM cached_printix_users WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
    return int(row["c"]) if row else 0


def get_sync_status(tenant_id: str, entity_type: str = "users") -> Optional[dict]:
    """Letzter Sync-Status für ein Tenant + Entity."""
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT last_sync_at, last_sync_status, last_sync_error, synced_count
               FROM cached_sync_status
               WHERE tenant_id = ? AND entity_type = ?""",
            (tenant_id, entity_type),
        ).fetchone()
    return dict(row) if row else None
