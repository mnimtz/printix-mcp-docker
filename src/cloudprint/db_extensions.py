"""
Cloud Print Port — DB-Schema-Erweiterungen
==========================================
Erweitert die bestehende SQLite-DB um:
  - users.role_type      (admin | user | employee)
  - users.parent_user_id (FK → users.id, für Mitarbeiter-Zugehörigkeit)
  - users.printix_user_id (Link auf echten Printix-User)
  - tenants.lpr_target_queue (Ziel-Queue für Cloud Print Weiterleitung)
  - tenants.lpr_port     (LPR-Port, Default 515)
  - delegations-Tabelle  (Owner → Delegate Beziehungen)

Aufruf: init_cloudprint_schema() — idempotent, nutzt PRAGMA table_info.
"""

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("printix.cloudprint.db")


# ─── Schema-Migration ────────────────────────────────────────────────────────

def init_cloudprint_schema() -> None:
    """Erweitert die bestehende DB um Cloud-Print-Felder (idempotent)."""
    from db import _conn

    # 1) users: role_type + parent_user_id + printix_user_id
    with _conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role_type" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN role_type TEXT NOT NULL DEFAULT 'user'"
            )
            # Bestehende Admins korrekt setzen
            conn.execute("UPDATE users SET role_type = 'admin' WHERE is_admin = 1")
            logger.info("Migration: users.role_type hinzugefügt")
        if "parent_user_id" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN parent_user_id TEXT DEFAULT NULL"
            )
            logger.info("Migration: users.parent_user_id hinzugefügt")
        if "printix_user_id" not in cols:
            conn.execute(
                "ALTER TABLE users ADD COLUMN printix_user_id TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_printix_user_id ON users (printix_user_id)"
            )
            logger.info("Migration: users.printix_user_id hinzugefügt")

    # 2) tenants: LPR Cloud Print Forwarding
    with _conn() as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "lpr_target_queue" not in cols:
            conn.execute(
                "ALTER TABLE tenants ADD COLUMN lpr_target_queue TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: tenants.lpr_target_queue hinzugefügt")
        if "lpr_port" not in cols:
            conn.execute(
                "ALTER TABLE tenants ADD COLUMN lpr_port INTEGER NOT NULL DEFAULT 515"
            )
            logger.info("Migration: tenants.lpr_port hinzugefügt")

    # 3) delegations-Tabelle
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS delegations (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_user_id     TEXT NOT NULL REFERENCES users(id),
                delegate_user_id  TEXT NOT NULL REFERENCES users(id),
                status            TEXT NOT NULL DEFAULT 'active',
                created_by        TEXT NOT NULL DEFAULT '',
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                UNIQUE(owner_user_id, delegate_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_deleg_owner
                ON delegations (owner_user_id);
            CREATE INDEX IF NOT EXISTS idx_deleg_delegate
                ON delegations (delegate_user_id);
        """)
        # v6.7.14: Delegate-Identität kann direkt ein Printix-User sein (ohne
        # MCP-Employee-Spiegel). Neue Spalten speichern die Printix-Daten
        # nominativ in der Delegations-Zeile — der Delegate-Picker zieht dann
        # live aus cached_printix_users und beim Hinzufügen werden die Daten
        # hier festgehalten. delegate_user_id wird optional (leer wenn kein
        # MCP-Spiegel existiert).
        # Da SQLite keine nachträgliche Änderung von NOT-NULL/FK-Constraints
        # erlaubt, machen wir einen Table-Rebuild.
        cols_d = {r[1] for r in conn.execute("PRAGMA table_info(delegations)").fetchall()}
        needs_rebuild = (
            "delegate_printix_user_id" not in cols_d
            or "delegate_email" not in cols_d
            or "delegate_full_name" not in cols_d
        )
        if needs_rebuild:
            conn.executescript("""
                PRAGMA foreign_keys = OFF;
                CREATE TABLE delegations_new (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_user_id     TEXT NOT NULL REFERENCES users(id),
                    delegate_user_id  TEXT NOT NULL DEFAULT '',
                    delegate_printix_user_id TEXT NOT NULL DEFAULT '',
                    delegate_email    TEXT NOT NULL DEFAULT '',
                    delegate_full_name TEXT NOT NULL DEFAULT '',
                    status            TEXT NOT NULL DEFAULT 'active',
                    created_by        TEXT NOT NULL DEFAULT '',
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL
                );
            """)
            # Daten aus der alten Tabelle übernehmen — best-effort mit den
            # Spalten die garantiert existieren.
            conn.execute("""
                INSERT INTO delegations_new
                  (id, owner_user_id, delegate_user_id, status,
                   created_by, created_at, updated_at)
                SELECT id, owner_user_id, delegate_user_id, status,
                       created_by, created_at, updated_at
                FROM delegations
            """)
            # Für bestehende Einträge die Printix-Daten nachfüllen
            # (aus users-Tabelle via delegate_user_id).
            conn.execute("""
                UPDATE delegations_new
                SET delegate_printix_user_id = COALESCE(
                    (SELECT printix_user_id FROM users WHERE users.id = delegations_new.delegate_user_id), ''),
                    delegate_email = COALESCE(
                    (SELECT email FROM users WHERE users.id = delegations_new.delegate_user_id), ''),
                    delegate_full_name = COALESCE(
                    (SELECT full_name FROM users WHERE users.id = delegations_new.delegate_user_id), '')
                WHERE delegate_user_id != ''
            """)
            conn.executescript("""
                DROP TABLE delegations;
                ALTER TABLE delegations_new RENAME TO delegations;
                CREATE INDEX IF NOT EXISTS idx_deleg_owner
                    ON delegations (owner_user_id);
                CREATE INDEX IF NOT EXISTS idx_deleg_delegate
                    ON delegations (delegate_user_id);
                CREATE INDEX IF NOT EXISTS idx_deleg_delegate_pxid
                    ON delegations (delegate_printix_user_id);
                CREATE INDEX IF NOT EXISTS idx_deleg_delegate_email
                    ON delegations (LOWER(delegate_email));
                PRAGMA foreign_keys = ON;
            """)
            logger.info(
                "Migration: delegations-Tabelle rebuild abgeschlossen "
                "(neue Spalten delegate_printix_user_id/_email/_full_name)"
            )
        logger.info("Migration: delegations-Tabelle geprüft/erstellt")

    # 4) cloudprint_jobs — empfangene LPR-Jobs nachverfolgen
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cloudprint_jobs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          TEXT NOT NULL,
                tenant_id       TEXT NOT NULL,
                queue_name      TEXT NOT NULL DEFAULT '',
                username        TEXT NOT NULL DEFAULT '',
                hostname        TEXT NOT NULL DEFAULT '',
                job_name        TEXT NOT NULL DEFAULT '',
                data_size       INTEGER NOT NULL DEFAULT 0,
                data_format     TEXT NOT NULL DEFAULT '',
                control_lines_json TEXT NOT NULL DEFAULT '',
                payload_preview TEXT NOT NULL DEFAULT '',
                detected_identity TEXT NOT NULL DEFAULT '',
                identity_source TEXT NOT NULL DEFAULT '',
                status          TEXT NOT NULL DEFAULT 'received',
                printix_job_id  TEXT NOT NULL DEFAULT '',
                target_queue    TEXT NOT NULL DEFAULT '',
                error_message   TEXT NOT NULL DEFAULT '',
                received_at     TEXT NOT NULL,
                forwarded_at    TEXT NOT NULL DEFAULT '',
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_cpjobs_tenant
                ON cloudprint_jobs (tenant_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_cpjobs_username
                ON cloudprint_jobs (username, created_at DESC);
        """)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(cloudprint_jobs)").fetchall()}
        if "control_lines_json" not in cols:
            conn.execute(
                "ALTER TABLE cloudprint_jobs ADD COLUMN control_lines_json TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: cloudprint_jobs.control_lines_json hinzugefügt")
        if "payload_preview" not in cols:
            conn.execute(
                "ALTER TABLE cloudprint_jobs ADD COLUMN payload_preview TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: cloudprint_jobs.payload_preview hinzugefügt")
        if "detected_identity" not in cols:
            conn.execute(
                "ALTER TABLE cloudprint_jobs ADD COLUMN detected_identity TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: cloudprint_jobs.detected_identity hinzugefügt")
        if "identity_source" not in cols:
            conn.execute(
                "ALTER TABLE cloudprint_jobs ADD COLUMN identity_source TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: cloudprint_jobs.identity_source hinzugefügt")
        # v6.4.0 — Delegate-Print: Kind-Einträge pro Delegate
        if "parent_job_id" not in cols:
            conn.execute(
                "ALTER TABLE cloudprint_jobs ADD COLUMN parent_job_id TEXT NOT NULL DEFAULT ''"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_cpjobs_parent ON cloudprint_jobs (parent_job_id)"
            )
            logger.info("Migration: cloudprint_jobs.parent_job_id hinzugefügt")
        if "delegated_from" not in cols:
            conn.execute(
                "ALTER TABLE cloudprint_jobs ADD COLUMN delegated_from TEXT NOT NULL DEFAULT ''"
            )
            logger.info("Migration: cloudprint_jobs.delegated_from hinzugefügt")
        logger.info("Migration: cloudprint_jobs-Tabelle geprüft/erstellt")

    # 5) v6.7.5: cached_printix_users — persistenter Cache der Printix-User
    #     pro Tenant. Ersetzt das In-Memory-Lookup für IPP-Tenant-Resolution
    #     und macht Multi-Tenant-Routing eindeutig.
    #     WICHTIG: Diese Tabelle hat NICHTS mit der MCP-`users`-Tabelle zu tun.
    #     Hier liegen die Endbenutzer aus dem jeweiligen Printix-Tenant
    #     (gespiegelt von der Printix-User-Management-API).
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cached_printix_users (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id         TEXT NOT NULL,         -- = unsere lokale tenants.id
                printix_tenant_id TEXT NOT NULL,         -- = Printix Tenant-UUID
                printix_user_id   TEXT NOT NULL,         -- = Printix User-UUID
                username          TEXT NOT NULL DEFAULT '',
                email             TEXT NOT NULL DEFAULT '',
                full_name         TEXT NOT NULL DEFAULT '',
                role              TEXT NOT NULL DEFAULT '',  -- USER / GUEST_USER / SYSTEM_MANAGER
                raw_json          TEXT NOT NULL DEFAULT '',  -- volles Printix-Objekt für Debug
                synced_at         TEXT NOT NULL,
                UNIQUE(tenant_id, printix_user_id)
            );
            CREATE INDEX IF NOT EXISTS idx_cpu_username
                ON cached_printix_users (LOWER(username));
            CREATE INDEX IF NOT EXISTS idx_cpu_email
                ON cached_printix_users (LOWER(email));
            CREATE INDEX IF NOT EXISTS idx_cpu_tenant
                ON cached_printix_users (tenant_id);
        """)
        logger.info("Migration: cached_printix_users-Tabelle geprüft/erstellt")

    # 6) v6.7.5: cached_sync_status — wann war der letzte Sync pro Tenant pro Entity-Typ?
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS cached_sync_status (
                tenant_id        TEXT NOT NULL,
                entity_type      TEXT NOT NULL,        -- 'users' / 'printers' / 'workstations'
                last_sync_at     TEXT NOT NULL DEFAULT '',
                last_sync_status TEXT NOT NULL DEFAULT '',  -- 'ok' / 'error'
                last_sync_error  TEXT NOT NULL DEFAULT '',
                synced_count     INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (tenant_id, entity_type)
            );
        """)
        logger.info("Migration: cached_sync_status-Tabelle geprüft/erstellt")


# ─── Cloud Print Job Tracking ────────────────────────────────────────────────

def create_cloudprint_job(
    job_id: str,
    tenant_id: str,
    queue_name: str = "",
    username: str = "",
    hostname: str = "",
    job_name: str = "",
    data_size: int = 0,
    data_format: str = "",
    control_lines_json: str = "",
    payload_preview: str = "",
    detected_identity: str = "",
    identity_source: str = "",
    parent_job_id: str = "",
    delegated_from: str = "",
    status: str = "received",
) -> dict:
    """Speichert einen empfangenen LPR-Job in der Tracking-Tabelle.

    v6.4.0: parent_job_id + delegated_from für Delegate-Print-Kind-Einträge.
    Bei Kind-Einträgen referenziert parent_job_id den Haupt-Job und
    delegated_from enthält die E-Mail / den Namen des Original-Owners.
    """
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        conn.execute(
            """INSERT INTO cloudprint_jobs
               (job_id, tenant_id, queue_name, username, hostname, job_name,
                data_size, data_format, control_lines_json, payload_preview,
                detected_identity, identity_source, parent_job_id, delegated_from,
                status, received_at, created_at)
               VALUES (?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?)""",
            (job_id, tenant_id, queue_name, username, hostname, job_name,
             data_size, data_format, control_lines_json[:4000], payload_preview[:4000],
             detected_identity[:255], identity_source[:120],
             parent_job_id[:64], delegated_from[:255],
             status, now, now),
        )
    return {"job_id": job_id, "status": status}


def get_active_delegates_for_identity(tenant_id: str,
                                       owner_identity: str) -> list[dict]:
    """Findet alle aktiven Delegates für einen Owner.

    v6.7.15: Robuste Owner-Auflösung mit drei Fallbacks:
      1. Direkter Match in MCP-users (email / printix_user_id / username)
      2. Printix-Cache-Lookup: identity → Printix-User → Printix-Email →
         MCP-users (bei verschiedener Mail-Form)
      3. Alle gefundenen MCP-User-IDs werden gesammelt — Delegations aus
         jedem davon werden returned

    Das schützt uns gegen den Fall dass der Windows-User mit Printix-Email
    auftaucht aber der MCP-Account eine andere Email hat (nur username
    matched, oder umgekehrt).

    Zurück kommt eine Liste von Dicts mit mindestens `email`, `full_name`,
    `printix_user_id` für spätere Submit-an-Printix mit user=delegate.email.
    """
    from db import _conn
    if not tenant_id or not owner_identity:
        return []
    ident = owner_identity.strip()
    candidate_identities = {ident.lower()}

    # Printix-Cache-Fallback: identity → gecachter Printix-User → mehr Kandidaten
    try:
        from cloudprint.printix_cache_db import find_printix_user_by_identity
        pxuser = find_printix_user_by_identity(ident)
        if pxuser:
            for key in ("email", "printix_user_id", "username"):
                v = (pxuser.get(key) or "").strip().lower()
                if v:
                    candidate_identities.add(v)
    except Exception as _cache_err:
        logger.debug("Delegate-Lookup Printix-Cache-Fallback failed: %s", _cache_err)

    with _conn() as conn:
        # Alle MCP-User finden, deren email/username/printix_user_id
        # mit einer der Identitäten matched.
        placeholders = ",".join("?" * len(candidate_identities))
        params = list(candidate_identities)
        rows_u = conn.execute(
            f"""SELECT id FROM users
                WHERE LOWER(email)           IN ({placeholders})
                   OR LOWER(printix_user_id) IN ({placeholders})
                   OR LOWER(username)        IN ({placeholders})""",
            params + params + params,
        ).fetchall()
        owner_uids = [r["id"] for r in rows_u]
        if not owner_uids:
            logger.debug(
                "Delegate-Lookup: kein MCP-User für identity='%s' (cand=%s)",
                ident, sorted(candidate_identities),
            )
            return []

        # Alle aktiven Delegations für diese Owner-IDs.
        # Delegate-Daten bevorzugt aus der Delegations-Zeile (delegate_email/
        # _name/_printix_user_id). Join auf users optional (LEFT JOIN) für
        # MCP-Mirror-Fall.
        placeholders2 = ",".join("?" * len(owner_uids))
        rows = conn.execute(
            f"""SELECT
                 d.id AS delegation_id,
                 d.owner_user_id,
                 d.delegate_user_id,
                 d.delegate_printix_user_id,
                 COALESCE(NULLIF(d.delegate_email, ''), u.email, '') AS email,
                 COALESCE(NULLIF(d.delegate_full_name, ''),
                          u.full_name, u.username, '') AS full_name,
                 COALESCE(u.username, '') AS username,
                 COALESCE(NULLIF(d.delegate_printix_user_id, ''),
                          u.printix_user_id, '') AS printix_user_id
               FROM delegations d
               LEFT JOIN users u ON u.id = NULLIF(d.delegate_user_id, '')
               WHERE d.owner_user_id IN ({placeholders2})
                 AND d.status = 'active'""",
            owner_uids,
        ).fetchall()
    result = [dict(r) for r in rows if (dict(r).get("email") or "").strip()]
    logger.info(
        "Delegate-Lookup: identity='%s' → owner_uids=%s → %d aktive Delegates",
        ident, owner_uids, len(result),
    )
    return result


def update_cloudprint_job_status(
    job_id: str,
    status: str,
    printix_job_id: str = "",
    target_queue: str = "",
    error_message: str = "",
    detected_identity: str = "",
    identity_source: str = "",
) -> None:
    """Aktualisiert den Status eines empfangenen Cloud-Print-Jobs."""
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        if detected_identity or identity_source:
            conn.execute(
                """UPDATE cloudprint_jobs
                   SET status = ?, printix_job_id = ?, target_queue = ?,
                       error_message = ?, detected_identity = ?, identity_source = ?, forwarded_at = ?
                   WHERE job_id = ?""",
                (status, printix_job_id, target_queue, error_message[:500], detected_identity[:255], identity_source[:120], now, job_id),
            )
        else:
            conn.execute(
                """UPDATE cloudprint_jobs
                   SET status = ?, printix_job_id = ?, target_queue = ?,
                       error_message = ?, forwarded_at = ?
                   WHERE job_id = ?""",
                (status, printix_job_id, target_queue, error_message[:500], now, job_id),
            )


def get_cloudprint_jobs(tenant_id: str = "", username: str = "", limit: int = 50) -> list[dict]:
    """Gibt empfangene Cloud-Print-Jobs zurück, optional nach Tenant oder Username gefiltert."""
    from db import _conn
    with _conn() as conn:
        if tenant_id and username:
            rows = conn.execute(
                """SELECT * FROM cloudprint_jobs
                   WHERE tenant_id = ? AND LOWER(username) = LOWER(?)
                   ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, username, limit),
            ).fetchall()
        elif tenant_id:
            rows = conn.execute(
                """SELECT * FROM cloudprint_jobs
                   WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?""",
                (tenant_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cloudprint_jobs ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


def get_cloudprint_jobs_for_employee(tenant_id: str, employee: dict,
                                       limit: int = 100) -> list[dict]:
    """Liefert LPR-Jobs die dem angegebenen Mitarbeiter zugeordnet werden können.

    Der LPR-Server speichert mehrere Identity-Quellen pro Job:
      - username              (aus dem LPR-Control-File: Feld P)
      - hostname              (aus Feld H)
      - detected_identity     (vom Printix-Owner-Lookup oder LPR-Payload)
      - delegated_from        (bei Delegate-Kind-Jobs: Original-Owner)
      - printix_job_id        (gesetzt wenn Printix den Job angenommen hat)

    v6.4.2: Match gegen 3 Spalten (username + detected_identity +
    delegated_from), damit auch Delegate-Kind-Einträge gefunden werden,
    sowohl für den Owner als auch für die Delegates.
    """
    from db import _conn
    if not tenant_id or not isinstance(employee, dict):
        return []

    candidates = set()
    for key in ("printix_user_id", "email", "username", "full_name"):
        v = (employee.get(key) or "").strip()
        if v:
            candidates.add(v.lower())
    if not candidates:
        return []

    placeholders = ",".join("?" * len(candidates))
    params = [tenant_id]
    params.extend(candidates)
    params.extend(candidates)
    params.extend(candidates)
    params.append(int(limit))
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT * FROM cloudprint_jobs
                WHERE tenant_id = ?
                  AND (LOWER(COALESCE(username, '')) IN ({placeholders})
                       OR LOWER(COALESCE(detected_identity, '')) IN ({placeholders})
                       OR LOWER(COALESCE(delegated_from, '')) IN ({placeholders}))
                ORDER BY COALESCE(forwarded_at, received_at, created_at) DESC
                LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_cloudprint_jobs_debug(tenant_id: str, limit: int = 10) -> list[dict]:
    """Liefert die letzten N Jobs unabhängig vom Owner — für Debug/Admin.

    Wird vom Employee-Portal gezeigt wenn das personenspezifische Match
    0 Treffer ergibt, damit der User sieht was überhaupt empfangen wurde.
    """
    from db import _conn
    if not tenant_id:
        return []
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM cloudprint_jobs
               WHERE tenant_id = ?
               ORDER BY COALESCE(forwarded_at, received_at, created_at) DESC
               LIMIT ?""",
            (tenant_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Cloud Print Forwarding ──────────────────────────────────────────────────

def get_cloudprint_config(user_id: str) -> Optional[dict]:
    """Gibt die Cloud-Print-Konfiguration eines Tenants zurück."""
    from db import _conn
    parent_id = get_parent_user_id(user_id) or user_id
    with _conn() as conn:
        row = conn.execute(
            """SELECT t.id AS tenant_id, t.printix_tenant_id,
                      t.lpr_target_queue, t.lpr_port
               FROM tenants t
               WHERE t.user_id = ?""",
            (parent_id,),
        ).fetchone()
    return dict(row) if row else None


def update_cloudprint_config(user_id: str, target_queue: str, lpr_port: int | None = None) -> None:
    """Speichert die Cloud-Print-Weiterleitungs-Konfiguration.

    lpr_port bleibt aus Kompatibilitätsgründen optional, wird aber nicht mehr
    pro Tenant erzwungen überschrieben, wenn kein Wert übergeben wird.
    """
    from db import _conn
    parent_id = get_parent_user_id(user_id) or user_id
    with _conn() as conn:
        if lpr_port is None:
            conn.execute(
                "UPDATE tenants SET lpr_target_queue = ? WHERE user_id = ?",
                (target_queue.strip(), parent_id),
            )
        else:
            conn.execute(
                "UPDATE tenants SET lpr_target_queue = ?, lpr_port = ? WHERE user_id = ?",
                (target_queue.strip(), lpr_port, parent_id),
            )
    logger.info("Cloud-Print-Config aktualisiert für User %s: Queue=%s", user_id, target_queue)


def get_tenant_by_printix_id(printix_tenant_id: str) -> Optional[dict]:
    """Findet einen Tenant anhand der Printix-Tenant-ID (LPR Queue-Name Lookup)."""
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT t.id AS tenant_id, t.user_id, t.printix_tenant_id,
                      t.lpr_target_queue, t.lpr_port
               FROM tenants t
               WHERE t.printix_tenant_id = ?""",
            (printix_tenant_id,),
        ).fetchone()
    return dict(row) if row else None


def get_admin_tenant_with_queue() -> Optional[dict]:
    """v6.7.36: Last-resort Fallback — findet den Admin-Tenant mit
    konfigurierter `lpr_target_queue`.

    Verwendung: Desktop-API und andere Stellen, die ein User-Request auf
    eine gültige Print-Queue abbilden müssen, wenn der eingeloggte User
    selbst keinen eigenen Tenant-Eintrag hat (z.B. role=user, der manuell
    angelegt wurde, oder im Multi-Tenant-Cluster wo `get_default_single_tenant`
    aussteigt).

    Suchstrategie:
      - tenants-Zeile mit `printix_tenant_id` != '' UND `lpr_target_queue` != ''
      - zugehöriger User hat `is_admin = 1`
      - bei mehreren Admins: der älteste Eintrag (ORDER BY id ASC)

    Returns das tenant-Dict wie `get_tenant_by_printix_id`, oder None.
    """
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT t.id AS tenant_id, t.user_id, t.printix_tenant_id,
                      t.lpr_target_queue, t.lpr_port
               FROM tenants t
               JOIN users u ON u.id = t.user_id
               WHERE t.printix_tenant_id != ''
                 AND t.lpr_target_queue != ''
                 AND u.is_admin = 1
               ORDER BY t.id ASC
               LIMIT 1"""
        ).fetchone()
    return dict(row) if row else None


def get_default_single_tenant() -> Optional[dict]:
    """v6.7.4: Liefert den einzigen Tenant zurück — wenn genau einer existiert.

    Verwendung: Fallback wenn ein eingehender IPP-Request keine Tenant-ID
    im URL-Pfad hat (Printix-Workstation-Client schickt immer auf
    `/ipp/printer`) und keine User-Resolution möglich war. Bei einem
    Single-Tenant-Setup ist das eindeutig und sicher.
    """
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT t.id AS tenant_id, t.user_id, t.printix_tenant_id,
                      t.lpr_target_queue, t.lpr_port
               FROM tenants t
               WHERE t.printix_tenant_id != ''
               LIMIT 2""",
        ).fetchall()
    if len(rows) == 1:
        return dict(rows[0])
    return None


def resolve_tenant_by_user_identity(identity: str) -> Optional[dict]:
    """v6.7.5: Findet den Tenant anhand eines Printix-User-Hinweises.

    Lookup geht jetzt gegen den **persistenten Printix-Cache**
    (`cached_printix_users`), NICHT gegen die MCP-`users`-Tabelle. Damit
    werden die zwei User-Welten sauber getrennt:
      - MCP-Users   = unsere App-Logins (Admin/User/Employee)
      - Printix-Users = Endbenutzer im jeweiligen Printix-Tenant

    Liefert das tenant_info-Dict (wie `get_tenant_by_printix_id`) oder None.
    """
    from cloudprint.printix_cache_db import find_printix_user_by_identity

    user = find_printix_user_by_identity(identity)
    if not user:
        return None

    # tenant_id aus Cache kommt 1:1 aus unserer tenants-Tabelle (wir haben
    # beim Sync die lokale tenant.id mit reingeschrieben).
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT t.id AS tenant_id, t.user_id, t.printix_tenant_id,
                      t.lpr_target_queue, t.lpr_port
               FROM tenants t
               WHERE t.id = ?""",
            (user["tenant_id"],),
        ).fetchone()
    if row:
        # v6.7.5.1: aussagekräftiger Log — wenn username leer ist, nimm email
        display = user.get("username") or user.get("email") or user.get("printix_user_id") or "-"
        logger.info(
            "Tenant-Resolution: '%s' → Printix-User '%s' (id=%s, email=%s) tenant=%s",
            identity, display, user.get("printix_user_id", "-"),
            user.get("email", "-") or "-", row["printix_tenant_id"],
        )
        return dict(row)
    logger.warning(
        "Tenant-Resolution: gecachter User '%s' verweist auf nicht-existenten "
        "tenant_id=%s — Cache stale?", identity, user.get("tenant_id"),
    )
    return None


def resolve_user_email(identity: str) -> str:
    """v6.7.5: Printix-Username → E-Mail aus dem persistenten Printix-Cache.

    Wird vom IPP-Forwarding gebraucht: das `requesting-user-name`-Attribut
    enthält oft nur den Printix-Username (z.B. `marcus.nimtz`), nicht die
    E-Mail. Für `submit_print_job(user=...)` und Delegate-Forwarding brauchen
    wir aber die E-Mail.

    Liefert die E-Mail zurück oder den Original-Identity-String wenn kein
    Match.
    """
    if not identity or not identity.strip():
        return identity
    if "@" in identity:
        return identity  # ist bereits E-Mail
    from cloudprint.printix_cache_db import find_printix_user_by_identity
    user = find_printix_user_by_identity(identity)
    if user and user.get("email"):
        logger.debug("User-Email-Resolution: '%s' → '%s' (Printix-Cache)",
                     identity, user["email"])
        return user["email"]
    return identity


# ─── Employee (Mitarbeiter) ──────────────────────────────────────────────────

def create_employee(
    parent_user_id: str,
    username: str,
    password: str,
    email: str = "",
    full_name: str = "",
    printix_user_id: str = "",
    must_change_password: bool = False,
) -> dict:
    """Erstellt einen Mitarbeiter-Account, verknüpft mit dem Parent-User/Tenant."""
    from crypto import hash_password
    from db import _conn, get_user_by_id
    import uuid

    uid = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    pw_hash = hash_password(password)

    with _conn() as conn:
        conn.execute(
            """INSERT INTO users
               (id, username, email, full_name, company, password_hash,
                is_admin, status, role_type, parent_user_id, printix_user_id, must_change_password, created_at)
               VALUES (?,?,?,?,?,?, 0,'approved','employee',?,?,?,?)""",
            (uid, username.strip(), email.strip(), full_name.strip(), "",
             pw_hash, parent_user_id, (printix_user_id or "").strip(), 1 if must_change_password else 0, now),
        )
    logger.info("Mitarbeiter erstellt: %s (Parent: %s)", username, parent_user_id)
    return get_user_by_id(uid)


def get_employees(parent_user_id: str) -> list[dict]:
    """Gibt alle Mitarbeiter eines Users zurück."""
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, username, email, full_name, status, role_type, created_at, printix_user_id, must_change_password
               FROM users
               WHERE parent_user_id = ? AND role_type = 'employee'
               ORDER BY full_name, username""",
            (parent_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_employee_by_id(employee_id: str, parent_user_id: str) -> Optional[dict]:
    """Gibt einen Mitarbeiter zurück, nur wenn er zum Parent gehört."""
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT id, username, email, full_name, status, role_type,
                      parent_user_id, printix_user_id, must_change_password, created_at
               FROM users
               WHERE id = ? AND parent_user_id = ? AND role_type = 'employee'""",
            (employee_id, parent_user_id),
        ).fetchone()
    return dict(row) if row else None


def get_employee_by_printix_user_id(printix_user_id: str, parent_user_id: str) -> Optional[dict]:
    """Gibt einen bereits importierten Mitarbeiter anhand der Printix-User-ID zurück."""
    if not printix_user_id.strip():
        return None
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            """SELECT id, username, email, full_name, status, role_type,
                      parent_user_id, printix_user_id, must_change_password, created_at
               FROM users
               WHERE printix_user_id = ? AND parent_user_id = ? AND role_type = 'employee'""",
            (printix_user_id.strip(), parent_user_id),
        ).fetchone()
    return dict(row) if row else None


def delete_employee(employee_id: str, parent_user_id: str) -> bool:
    """Löscht einen Mitarbeiter (nur wenn er zum Parent gehört)."""
    from db import _conn
    with _conn() as conn:
        # Erst Delegationen dieses Employees löschen
        conn.execute(
            "DELETE FROM delegations WHERE owner_user_id = ? OR delegate_user_id = ?",
            (employee_id, employee_id),
        )
        cur = conn.execute(
            "DELETE FROM users WHERE id = ? AND parent_user_id = ? AND role_type = 'employee'",
            (employee_id, parent_user_id),
        )
    return cur.rowcount > 0


def get_parent_user_id(user_id: str) -> Optional[str]:
    """Ermittelt den Parent-User eines Mitarbeiters (oder sich selbst für Admin/User)."""
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, role_type, parent_user_id FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    if not row:
        return None
    if row["role_type"] == "employee" and row["parent_user_id"]:
        return row["parent_user_id"]
    return row["id"]


def get_tenant_for_user(user_id: str) -> Optional[dict]:
    """Gibt den Tenant für einen User zurück — auch für Employees (über Parent)."""
    from db import get_tenant_by_user_id
    parent_id = get_parent_user_id(user_id)
    if not parent_id:
        return None
    return get_tenant_by_user_id(parent_id)


# ─── Delegations ──────────────────────────────────────────────────────────────

def create_delegation(
    owner_user_id: str,
    delegate_user_id: str,
    created_by: str = "",
    status: str = "active",
) -> Optional[dict]:
    """Erstellt eine Delegation (Owner → Delegate).

    status: 'active' (Admin-erstellt) oder 'pending' (Mitarbeiter-Vorschlag).
    """
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _conn() as conn:
            conn.execute(
                """INSERT INTO delegations
                   (owner_user_id, delegate_user_id, status, created_by, created_at, updated_at)
                   VALUES (?,?,?,?,?,?)""",
                (owner_user_id, delegate_user_id, status, created_by, now, now),
            )
            row = conn.execute(
                "SELECT * FROM delegations WHERE owner_user_id = ? AND delegate_user_id = ?",
                (owner_user_id, delegate_user_id),
            ).fetchone()
        return dict(row) if row else None
    except sqlite3.IntegrityError:
        logger.warning("Delegation existiert bereits: %s → %s", owner_user_id, delegate_user_id)
        return None


def add_printix_delegate(
    owner_user_id: str,
    printix_user_id: str,
    email: str,
    full_name: str = "",
    created_by: str = "",
) -> Optional[int]:
    """v6.7.14: Fügt einen Printix-User direkt als Delegate hinzu — ohne
    MCP-Employee-Spiegel. Die Printix-Identität wird in der Delegations-Zeile
    selbst festgehalten (delegate_printix_user_id / email / full_name).

    Duplikat-Check: wenn Owner bereits einen Delegate mit derselben
    Printix-User-ID hat → kein Insert, gibt die bestehende ID zurück.

    Returns die neue (oder bestehende) Delegation-ID, oder None bei Fehler.
    """
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    email = (email or "").strip()
    printix_user_id = (printix_user_id or "").strip()
    if not owner_user_id or not printix_user_id:
        return None
    with _conn() as conn:
        # Duplikat-Check über Printix-ID
        existing = conn.execute(
            """SELECT id FROM delegations
               WHERE owner_user_id = ?
                 AND (LOWER(delegate_printix_user_id) = LOWER(?)
                      OR (LOWER(delegate_email) = LOWER(?) AND ? != ''))""",
            (owner_user_id, printix_user_id, email, email),
        ).fetchone()
        if existing:
            logger.info(
                "Delegation existiert bereits — id=%s owner=%s printix=%s",
                existing["id"], owner_user_id, printix_user_id,
            )
            return existing["id"]

        cur = conn.execute(
            """INSERT INTO delegations
               (owner_user_id, delegate_user_id,
                delegate_printix_user_id, delegate_email, delegate_full_name,
                status, created_by, created_at, updated_at)
               VALUES (?, '', ?, ?, ?, 'active', ?, ?, ?)""",
            (owner_user_id, printix_user_id, email, full_name, created_by, now, now),
        )
        new_id = cur.lastrowid
        logger.info(
            "Printix-Delegation angelegt: id=%s owner=%s → Printix-User %s (%s)",
            new_id, owner_user_id, printix_user_id, email,
        )
        return new_id


def get_printix_delegate_candidates(
    tenant_id: str, owner_user_id: str,
) -> list[dict]:
    """v6.7.14: Liefert alle Printix-User des Tenants zurück als potentielle
    Delegate-Kandidaten.

    Excludes:
      - User die der Owner bereits als Delegate hat (gemessen über Printix-ID
        oder Email)
      - System-Manager (können keine Release-Queue haben — die Delegation
        liefe ins Leere)

    Quelle: `cached_printix_users` (via sync_users_for_tenant).
    """
    from db import _conn
    if not tenant_id:
        return []
    with _conn() as conn:
        existing = conn.execute(
            """SELECT LOWER(delegate_printix_user_id) AS pxid,
                      LOWER(delegate_email)           AS em
               FROM delegations
               WHERE owner_user_id = ? AND status = 'active'""",
            (owner_user_id,),
        ).fetchall()
        exclude_pxids = {r["pxid"] for r in existing if r["pxid"]}
        exclude_emails = {r["em"] for r in existing if r["em"]}

        # v6.7.16: SYSTEM_MANAGER-Filter entfernt. Mit dem neuen /changeOwner-
        # Endpoint (v6.7.15) ist Ownership-Transfer an jeden bekannten
        # Printix-User möglich — ob das für SMs technisch klappt, zeigt der
        # Live-Test. Falls SM keine Release-Queue hat, sehen wir's im
        # Printix-Admin-Log (kein Job sichtbar) und können UI-Warnung
        # später wieder anbringen.
        rows = conn.execute(
            """SELECT printix_user_id, email, username, full_name, role
               FROM cached_printix_users
               WHERE tenant_id = ?
                 AND email != ''
               ORDER BY
                 CASE WHEN UPPER(role) = 'SYSTEM_MANAGER' THEN 1 ELSE 0 END,
                 full_name, email""",
            (tenant_id,),
        ).fetchall()

    result = []
    for r in rows:
        d = dict(r)
        if d["printix_user_id"].lower() in exclude_pxids:
            continue
        if d["email"].lower() in exclude_emails:
            continue
        result.append(d)
    return result


def get_delegations_for_owner(owner_user_id: str) -> list[dict]:
    """Gibt alle Delegationen zurück, bei denen der User Owner ist.

    v6.7.14: Delegate-Daten kommen primär aus der Delegations-Zeile selbst
    (für reine Printix-Delegations), fallen optional auf den users-Join
    zurück wenn ein MCP-Mirror existiert.
    """
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT d.id, d.owner_user_id, d.delegate_user_id,
                      d.delegate_printix_user_id,
                      d.status, d.created_at, d.updated_at,
                      COALESCE(NULLIF(d.delegate_email, ''), u.email, '')
                          AS delegate_email,
                      COALESCE(NULLIF(d.delegate_full_name, ''),
                               u.full_name, u.username, '')
                          AS delegate_full_name,
                      COALESCE(u.username, '') AS delegate_username
               FROM delegations d
               LEFT JOIN users u ON u.id = NULLIF(d.delegate_user_id, '')
               WHERE d.owner_user_id = ?
               ORDER BY d.created_at DESC""",
            (owner_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_delegations_for_delegate(delegate_user_id: str) -> list[dict]:
    """Gibt alle Delegationen zurück, bei denen der User Delegate ist."""
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT d.*, u.username AS owner_username, u.email AS owner_email,
                      u.full_name AS owner_full_name, u.printix_user_id AS owner_printix_user_id
               FROM delegations d
               JOIN users u ON u.id = d.owner_user_id
               WHERE d.delegate_user_id = ? AND d.status = 'active'
               ORDER BY d.created_at DESC""",
            (delegate_user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def update_delegation_status(delegation_id: int, status: str) -> bool:
    """Aktualisiert den Status einer Delegation (active/pending/revoked)."""
    from db import _conn
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE delegations SET status = ?, updated_at = ? WHERE id = ?",
            (status, now, delegation_id),
        )
    return cur.rowcount > 0


def delete_delegation(delegation_id: int) -> bool:
    """Löscht eine Delegation."""
    from db import _conn
    with _conn() as conn:
        cur = conn.execute("DELETE FROM delegations WHERE id = ?", (delegation_id,))
    return cur.rowcount > 0


def get_delegation_by_id(delegation_id: int) -> Optional[dict]:
    """Gibt eine Delegation anhand der ID zurück."""
    from db import _conn
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM delegations WHERE id = ?", (delegation_id,)
        ).fetchone()
    return dict(row) if row else None


def get_available_delegates(parent_user_id: str, exclude_user_id: str = "") -> list[dict]:
    """Gibt alle möglichen Delegates zurück (alle Mitarbeiter + den Parent selbst)."""
    from db import _conn
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, username, email, full_name, role_type, printix_user_id
               FROM users
               WHERE (parent_user_id = ? OR id = ?)
                 AND status = 'approved'
                 AND id != ?
               ORDER BY full_name, username""",
            (parent_user_id, parent_user_id, exclude_user_id),
        ).fetchall()
    return [dict(r) for r in rows]


def search_available_delegates(parent_user_id: str, query: str,
                                exclude_user_id: str = "",
                                exclude_ids: Optional[list[str]] = None,
                                limit: int = 20) -> list[dict]:
    """Schnelle Query-basierte Suche nach möglichen Delegates.

    Filtert in SQL via LIKE auf full_name / username / email — liefert
    max. `limit` Treffer. Gedacht für Typeahead-Widgets, damit das UI bei
    vielen Mitarbeitern nicht die komplette Liste laden muss.
    """
    from db import _conn
    q = (query or "").strip().lower()
    like = f"%{q}%" if q else "%"
    excluded = list(exclude_ids or [])
    if exclude_user_id:
        excluded.append(exclude_user_id)
    placeholders = ",".join("?" * len(excluded)) if excluded else ""
    exclude_clause = f" AND id NOT IN ({placeholders})" if placeholders else ""
    params = [parent_user_id, parent_user_id, like, like, like]
    params.extend(excluded)
    params.append(int(limit))
    with _conn() as conn:
        rows = conn.execute(
            f"""SELECT id, username, email, full_name, role_type, printix_user_id
                FROM users
                WHERE (parent_user_id = ? OR id = ?)
                  AND status = 'approved'
                  AND (LOWER(COALESCE(full_name, '')) LIKE ?
                       OR LOWER(COALESCE(username, '')) LIKE ?
                       OR LOWER(COALESCE(email, '')) LIKE ?)
                  {exclude_clause}
                ORDER BY full_name, username
                LIMIT ?""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
