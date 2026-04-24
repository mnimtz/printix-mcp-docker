"""
Datenbank — SQLite Multi-Tenant Store für Printix MCP v2.1.0
=============================================================
Datei: /data/printix_multi.db (überlebt Add-on-Updates)

Schema:
  users     — Konten (username, password, status, is_admin)
  tenants   — Printix + SQL + Mail Credentials pro Benutzer (verschlüsselt)
  audit_log — Relevante Aktionen mit Zeitstempel
  settings  — Globale Konfiguration (public_url etc.)

Alle Secrets (client_secrets, passwords, bearer_token) werden mit Fernet
verschlüsselt gespeichert. Der Schlüssel liegt in /data/fernet.key und wird
beim ersten Start generiert.
"""

import hashlib
import logging
import os
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("DB_PATH", "/data/printix_multi.db")


def _normalize_role_type(role_type: str | None, is_admin: bool = False) -> str:
    """Zwei Rollen: admin (Verwalter) und employee (Mitarbeiter / Endbenutzer).

    Die Alt-Rolle "user" aus dem Multi-Tenant-Modell (vor v7.0.0) wird auf
    "employee" gemappt — ein "user" war de facto ein Mitarbeiter ohne
    Admin-Rechte, nur mit eigenem Tenant (den wir nicht mehr anlegen).
    """
    value = (role_type or "").strip().lower()
    if value == "admin":
        return "admin"
    if value in ("employee", "user"):
        return "employee"
    return "admin" if is_admin else "employee"


# ─── Datenbankverbindung ──────────────────────────────────────────────────────

@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
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


# ─── Schema ───────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Erstellt alle Tabellen beim ersten Start (idempotent)."""
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id           TEXT PRIMARY KEY,
                username     TEXT NOT NULL UNIQUE,
                email        TEXT NOT NULL DEFAULT '',
                full_name    TEXT NOT NULL DEFAULT '',
                company      TEXT NOT NULL DEFAULT '',
                password_hash TEXT NOT NULL,
                is_admin     INTEGER NOT NULL DEFAULT 0,
                role_type    TEXT NOT NULL DEFAULT 'user',
                printix_user_id TEXT NOT NULL DEFAULT '',
                status       TEXT NOT NULL DEFAULT 'pending',
                must_change_password INTEGER NOT NULL DEFAULT 0,
                invited_by_user_id TEXT NOT NULL DEFAULT '',
                invitation_language TEXT NOT NULL DEFAULT '',
                invitation_sent_at TEXT NOT NULL DEFAULT '',
                invitation_accepted_at TEXT NOT NULL DEFAULT '',
                created_at   TEXT NOT NULL
            );
            -- Safe migration: add new columns if they don't exist yet
            -- (no-op if columns already exist; PRAGMA table_info used for safety)

            CREATE TABLE IF NOT EXISTS tenants (
                id                   TEXT PRIMARY KEY,
                user_id              TEXT NOT NULL UNIQUE REFERENCES users(id),
                name                 TEXT NOT NULL DEFAULT '',

                -- Printix API (verschlüsselt)
                printix_tenant_id    TEXT NOT NULL DEFAULT '',
                print_client_id      TEXT NOT NULL DEFAULT '',
                print_client_secret  TEXT NOT NULL DEFAULT '',
                card_client_id       TEXT NOT NULL DEFAULT '',
                card_client_secret   TEXT NOT NULL DEFAULT '',
                ws_client_id         TEXT NOT NULL DEFAULT '',
                ws_client_secret     TEXT NOT NULL DEFAULT '',
                um_client_id         TEXT NOT NULL DEFAULT '',
                um_client_secret     TEXT NOT NULL DEFAULT '',
                shared_client_id     TEXT NOT NULL DEFAULT '',
                shared_client_secret TEXT NOT NULL DEFAULT '',

                -- OAuth-Credentials (auto-generiert)
                oauth_client_id      TEXT NOT NULL UNIQUE,
                oauth_client_secret  TEXT NOT NULL,

                -- Bearer Token für MCP
                bearer_token         TEXT NOT NULL,

                -- SQL Reporting (optional, verschlüsselt)
                sql_server           TEXT NOT NULL DEFAULT '',
                sql_database         TEXT NOT NULL DEFAULT 'printix_bi_data_2_1',
                sql_username         TEXT NOT NULL DEFAULT '',
                sql_password         TEXT NOT NULL DEFAULT '',

                -- Mail (optional, verschlüsselt)
                mail_api_key         TEXT NOT NULL DEFAULT '',
                mail_from            TEXT NOT NULL DEFAULT '',

                created_at           TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT,
                action     TEXT NOT NULL,
                details    TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tenant_logs (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id  TEXT NOT NULL,
                timestamp  TEXT NOT NULL,
                level      TEXT NOT NULL,
                category   TEXT NOT NULL,
                message    TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_tenant_logs
                ON tenant_logs (tenant_id, id DESC);
        """)
    # Sichere Migration: neue Spalten hinzufügen falls nicht vorhanden
    with _conn() as conn:
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()}
        if "role_type" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN role_type TEXT NOT NULL DEFAULT 'user'")
            conn.execute("UPDATE users SET role_type='admin' WHERE is_admin=1")
            conn.execute("UPDATE users SET role_type='user' WHERE role_type=''")
        if "printix_user_id" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN printix_user_id TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_printix_user_id ON users (printix_user_id)")
        if "full_name" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN full_name TEXT NOT NULL DEFAULT ''")
        if "company" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN company TEXT NOT NULL DEFAULT ''")
        if "must_change_password" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0")
        if "invited_by_user_id" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN invited_by_user_id TEXT NOT NULL DEFAULT ''")
        if "invitation_language" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN invitation_language TEXT NOT NULL DEFAULT ''")
        if "invitation_sent_at" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN invitation_sent_at TEXT NOT NULL DEFAULT ''")
        if "invitation_accepted_at" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN invitation_accepted_at TEXT NOT NULL DEFAULT ''")
        # v4.1.0: Entra ID (Azure AD) SSO — Object-ID für User-Zuordnung
        if "entra_oid" not in existing_cols:
            conn.execute("ALTER TABLE users ADD COLUMN entra_oid TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_users_entra_oid ON users (entra_oid)")
    # Sichere Migration für tenants-Tabelle: Alert-Spalten hinzufügen
    with _conn() as conn:
        existing_t = {r[1] for r in conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "alert_recipients" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN alert_recipients TEXT NOT NULL DEFAULT ''")
        if "alert_min_level" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN alert_min_level TEXT NOT NULL DEFAULT 'ERROR'")
        if "mail_from_name" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN mail_from_name TEXT NOT NULL DEFAULT ''")
        if "poller_state" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN poller_state TEXT NOT NULL DEFAULT '{}'")
        if "tenant_url" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN tenant_url TEXT NOT NULL DEFAULT ''")
        # User Management API (v5.19.0) — separate Credentials für Benutzerverwaltung
        if "um_client_id" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN um_client_id TEXT NOT NULL DEFAULT ''")
        if "um_client_secret" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN um_client_secret TEXT NOT NULL DEFAULT ''")
        # v6.7.92: Firmen-Default fuer Karten-Transform-Profile — legt fest
        # welches Profil die iOS-App (oder andere Clients) automatisch
        # benutzt, so dass Mitarbeiter nicht selbst waehlen muessen.
        # Wert ist die id eines card_profiles-Eintrags (Builtin oder Custom).
        # Leer = kein Default gesetzt → Client zeigt Picker "Ohne Profil".
        if "default_card_profile_id" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN default_card_profile_id TEXT NOT NULL DEFAULT ''")
    # v3.9.1: bearer_token_hash — indexierter SHA-256-Lookup (O(1) statt
    # Full-Table-Scan über alle Tenants bei jedem authenticated Request).
    # Der Hash ist nicht sensitiv: der Bearer-Token hat 48 Bytes Zufall (>384 Bit),
    # ein Brute-Force des SHA-256-Preimage ist praktisch ausgeschlossen.
    with _conn() as conn:
        existing_t = {r[1] for r in conn.execute("PRAGMA table_info(tenants)").fetchall()}
        if "bearer_token_hash" not in existing_t:
            conn.execute("ALTER TABLE tenants ADD COLUMN bearer_token_hash TEXT NOT NULL DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_tenants_bearer_hash "
            "ON tenants (bearer_token_hash)"
        )
    # Backfill: alle Tenants ohne bearer_token_hash einmal dekodieren und
    # den Hash nachtragen. Läuft nur beim ersten Start nach dem Upgrade.
    with _conn() as conn:
        missing = conn.execute(
            "SELECT id, bearer_token FROM tenants "
            "WHERE bearer_token_hash = '' OR bearer_token_hash IS NULL"
        ).fetchall()
        if missing:
            filled = 0
            for row in missing:
                try:
                    plain = _dec(row["bearer_token"])
                    if not plain:
                        logger.warning(
                            "Migration bearer_token_hash: leerer/ungültiger Token "
                            "für Tenant %s — überspringe", row["id"]
                        )
                        continue
                    conn.execute(
                        "UPDATE tenants SET bearer_token_hash = ? WHERE id = ?",
                        (_bearer_hash(plain), row["id"]),
                    )
                    filled += 1
                except Exception as e:
                    logger.error(
                        "Migration bearer_token_hash: Fehler bei Tenant %s: %s",
                        row["id"], e,
                    )
            if filled:
                logger.info(
                    "Migration bearer_token_hash: %d Tenant(s) nachgetragen", filled
                )
    # Sichere Migration für audit_log (v3.9.0): Objekttyp + Objekt-ID für strukturierten Audit-Trail
    with _conn() as conn:
        existing_a = {r[1] for r in conn.execute("PRAGMA table_info(audit_log)").fetchall()}
        if "object_type" not in existing_a:
            conn.execute("ALTER TABLE audit_log ADD COLUMN object_type TEXT NOT NULL DEFAULT ''")
        if "object_id" not in existing_a:
            conn.execute("ALTER TABLE audit_log ADD COLUMN object_id TEXT NOT NULL DEFAULT ''")
        if "tenant_id" not in existing_a:
            conn.execute("ALTER TABLE audit_log ADD COLUMN tenant_id TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log (created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_log_tenant ON audit_log (tenant_id, created_at DESC)")
    # v6.7.111: Back-fill tenant_id fuer Legacy-Rows. Bis v6.7.110 haben
    # die meisten audit()-Call-Sites in web/app.py den tenant_id-Parameter
    # nicht mitgegeben → alle Zeilen hatten tenant_id=''. Dadurch liefert
    # printix_query_audit_log mit Tenant-Filter 0 Treffer, obwohl Daten
    # da sind. Hier wird einmalig aus users.tenant_id nachgetragen.
    with _conn() as conn:
        try:
            # v6.7.112: korrigierter JOIN-Pfad. users hat keine tenant_id;
            # die Zuordnung kommt aus der tenants-Tabelle via t.user_id.
            updated = conn.execute(
                "UPDATE audit_log SET tenant_id = ("
                "   SELECT t.id FROM tenants t WHERE t.user_id = audit_log.user_id"
                ") "
                "WHERE (tenant_id = '' OR tenant_id IS NULL) "
                "  AND user_id IS NOT NULL "
                "  AND user_id IN (SELECT user_id FROM tenants WHERE id <> '')"
            ).rowcount
            if updated and updated > 0:
                logger.info(
                    "Migration audit_log.tenant_id: %d Legacy-Eintrag/-Eintraege "
                    "via users.tenant_id nachgetragen", updated
                )
        except Exception as e:
            logger.warning("Migration audit_log.tenant_id fehlgeschlagen: %s", e)
    # v7.0.0: Single-Tenant-Refactor — alte Rolle 'user' (Multi-Tenant-Legacy)
    # wird auf 'employee' gemappt und alle Non-Owner-User werden in den
    # einzigen Tenant gehängt (parent_user_id → erster Admin-Tenant-Owner).
    # Orphan-Tenants (Tenants ohne Printix-Credentials, die aus der alten
    # "pro User ein leerer Tenant"-Logik stammen) werden entfernt, damit
    # die DB konsistent wird und Bearer/OAuth-Lookups eindeutig bleiben.
    with _conn() as conn:
        try:
            conn.execute(
                "UPDATE users SET role_type='employee' WHERE role_type='user'"
            )
            owner_row = conn.execute(
                "SELECT t.user_id FROM tenants t JOIN users u ON u.id = t.user_id "
                "WHERE u.is_admin = 1 AND t.printix_tenant_id != '' "
                "ORDER BY t.created_at ASC LIMIT 1"
            ).fetchone()
            if not owner_row:
                owner_row = conn.execute(
                    "SELECT t.user_id FROM tenants t JOIN users u ON u.id = t.user_id "
                    "WHERE u.is_admin = 1 ORDER BY t.created_at ASC LIMIT 1"
                ).fetchone()
            if owner_row:
                owner_uid = owner_row["user_id"]
                updated = conn.execute(
                    "UPDATE users SET parent_user_id = ? "
                    "WHERE id != ? "
                    "  AND (parent_user_id IS NULL OR parent_user_id = '')",
                    (owner_uid, owner_uid),
                ).rowcount
                if updated and updated > 0:
                    logger.info(
                        "Migration v7.0.0 Single-Tenant: %d User an Tenant-Owner "
                        "%s gehaengt (parent_user_id gesetzt)",
                        updated, owner_uid,
                    )
                # Orphan-Tenants aufraeumen: alle Tenants, die NICHT dem Owner
                # gehoeren UND keine Printix-Credentials haben (leere
                # Alt-Tenants aus _create_empty_tenant pro User).
                deleted = conn.execute(
                    "DELETE FROM tenants "
                    "WHERE user_id != ? "
                    "  AND (printix_tenant_id IS NULL OR printix_tenant_id = '')",
                    (owner_uid,),
                ).rowcount
                if deleted and deleted > 0:
                    logger.info(
                        "Migration v7.0.0 Single-Tenant: %d Orphan-Tenant(s) "
                        "ohne Printix-Credentials entfernt", deleted,
                    )
        except Exception as e:
            logger.warning("Migration v7.0.0 Single-Tenant fehlgeschlagen: %s", e)
    # Feature-Requests / Ticketsystem (v3.9.0+)
    with _conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feature_requests (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no   TEXT NOT NULL UNIQUE,
                user_id     TEXT,
                user_email  TEXT NOT NULL DEFAULT '',
                tenant_id   TEXT NOT NULL DEFAULT '',
                title       TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category    TEXT NOT NULL DEFAULT 'feature',
                status      TEXT NOT NULL DEFAULT 'new',
                priority    TEXT NOT NULL DEFAULT 'normal',
                admin_note  TEXT NOT NULL DEFAULT '',
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_requests_status ON feature_requests (status, created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_feature_requests_user ON feature_requests (user_id, created_at DESC)")
    # v4.4.0: Capture Profiles — pro Tenant konfigurierbare Capture-Ziele
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS capture_profiles (
                id               TEXT PRIMARY KEY,
                tenant_id        TEXT NOT NULL REFERENCES tenants(id),
                name             TEXT NOT NULL,
                plugin_type      TEXT NOT NULL DEFAULT 'paperless_ngx',
                secret_key       TEXT NOT NULL DEFAULT '',
                connector_token  TEXT NOT NULL DEFAULT '',
                config_json      TEXT NOT NULL DEFAULT '{}',
                is_active        INTEGER NOT NULL DEFAULT 1,
                created_at       TEXT NOT NULL,
                updated_at       TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_capture_profiles_tenant
                ON capture_profiles (tenant_id);
        """)
        # v4.5.2: Capture Connector Model — erweiterte Profilfelder
        existing_cols = {r[1] for r in conn.execute("PRAGMA table_info(capture_profiles)").fetchall()}
        if "require_signature" not in existing_cols:
            conn.execute("ALTER TABLE capture_profiles ADD COLUMN require_signature INTEGER NOT NULL DEFAULT 0")
        if "metadata_format" not in existing_cols:
            conn.execute("ALTER TABLE capture_profiles ADD COLUMN metadata_format TEXT NOT NULL DEFAULT 'flat'")
        if "index_fields_json" not in existing_cols:
            conn.execute("ALTER TABLE capture_profiles ADD COLUMN index_fields_json TEXT NOT NULL DEFAULT '[]'")

    # v7.1.0: Guest-Print — E-Mail-basierter Secure-Print-Flow fuer Gaeste.
    # Ein ueberwachtes Outlook/Exchange-Postfach (via Entra App-Permissions,
    # Mail.ReadWrite) wird gepollt; Anhaenge von gelisteten Gast-Absendern
    # werden an die konfigurierte Printix-Queue geschickt, Owner via
    # change_job_owner auf den Gast umgeschrieben. Guest-User werden in
    # Printix als GUEST_USER mit expirationTimestamp angelegt (Timebomb).
    with _conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS guestprint_mailbox (
                id                   TEXT PRIMARY KEY,
                tenant_id            TEXT NOT NULL REFERENCES tenants(id),
                name                 TEXT NOT NULL DEFAULT '',
                upn                  TEXT NOT NULL,
                default_printer_id   TEXT NOT NULL DEFAULT '',
                default_queue_id     TEXT NOT NULL DEFAULT '',
                poll_interval_sec    INTEGER NOT NULL DEFAULT 60,
                folder_processed     TEXT NOT NULL DEFAULT 'GuestPrint/Processed',
                folder_skipped       TEXT NOT NULL DEFAULT 'GuestPrint/Skipped',
                on_success           TEXT NOT NULL DEFAULT 'move',
                max_attachment_bytes INTEGER NOT NULL DEFAULT 26214400,
                enabled              INTEGER NOT NULL DEFAULT 1,
                last_poll_at         TEXT NOT NULL DEFAULT '',
                last_error           TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_guestprint_mailbox_tenant
                ON guestprint_mailbox (tenant_id);

            CREATE TABLE IF NOT EXISTS guestprint_guest (
                id                   TEXT PRIMARY KEY,
                mailbox_id           TEXT NOT NULL
                                     REFERENCES guestprint_mailbox(id)
                                     ON DELETE CASCADE,
                sender_email         TEXT NOT NULL,
                full_name            TEXT NOT NULL DEFAULT '',
                printix_user_id      TEXT NOT NULL DEFAULT '',
                printix_guest_email  TEXT NOT NULL DEFAULT '',
                printer_id           TEXT NOT NULL DEFAULT '',
                queue_id             TEXT NOT NULL DEFAULT '',
                expiration_days      INTEGER NOT NULL DEFAULT 7,
                expires_at           TEXT NOT NULL DEFAULT '',
                enabled              INTEGER NOT NULL DEFAULT 1,
                last_match_at        TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_guestprint_guest_unique
                ON guestprint_guest (mailbox_id, sender_email);
            CREATE INDEX IF NOT EXISTS idx_guestprint_guest_printix
                ON guestprint_guest (printix_user_id);

            CREATE TABLE IF NOT EXISTS guestprint_job (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                mailbox_id           TEXT NOT NULL,
                guest_id             TEXT NOT NULL DEFAULT '',
                message_id           TEXT NOT NULL,
                sender_email         TEXT NOT NULL DEFAULT '',
                subject              TEXT NOT NULL DEFAULT '',
                attachment_name      TEXT NOT NULL DEFAULT '',
                attachment_bytes     INTEGER NOT NULL DEFAULT 0,
                printix_job_id       TEXT NOT NULL DEFAULT '',
                status               TEXT NOT NULL DEFAULT 'pending',
                error                TEXT NOT NULL DEFAULT '',
                created_at           TEXT NOT NULL,
                updated_at           TEXT NOT NULL
            );
            -- (mailbox, message, attachment) als Idempotenz-Key:
            -- eine Mail mit N Anhaengen ergibt N Jobs, aber der gleiche
            -- Anhang darf nicht zweimal gedruckt werden (z.B. Crash
            -- zwischen Print und Folder-Move).
            CREATE UNIQUE INDEX IF NOT EXISTS idx_guestprint_job_dedupe
                ON guestprint_job (mailbox_id, message_id, attachment_name);
            CREATE INDEX IF NOT EXISTS idx_guestprint_job_mailbox_created
                ON guestprint_job (mailbox_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_guestprint_job_status
                ON guestprint_job (status, created_at DESC);
        """)

    # v7.1.3: on_success je Postfach — move (Default, Altverhalten) | keep | delete
    with _conn() as conn:
        gp_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(guestprint_mailbox)"
        ).fetchall()}
        if "on_success" not in gp_cols:
            conn.execute(
                "ALTER TABLE guestprint_mailbox "
                "ADD COLUMN on_success TEXT NOT NULL DEFAULT 'move'"
            )

    logger.info("DB initialisiert: %s", DB_PATH)


# ─── Crypto Helpers ───────────────────────────────────────────────────────────

def _enc(value: str) -> str:
    """Verschlüsselt einen String — leer bleibt leer."""
    if not value:
        return ""
    try:
        from crypto import encrypt
        return encrypt(value)
    except Exception:
        return value


def _dec(value: str) -> str:
    """Entschlüsselt einen String — leer bleibt leer."""
    if not value:
        return ""
    try:
        from crypto import decrypt
        return decrypt(value)
    except Exception:
        return value


def _bearer_hash(plain_token: str) -> str:
    """
    Deterministischer SHA-256-Hash eines Bearer-Tokens für den indexierten
    Lookup in der tenants-Tabelle (siehe `get_tenant_by_bearer_token`).

    Der Hash wird zusätzlich zum Fernet-verschlüsselten Token gespeichert.
    Da der Bearer-Token mit `secrets.token_urlsafe(48)` generiert wird (>384
    Bit Zufall), ist der SHA-256-Preimage praktisch nicht brute-force-bar.
    """
    if not plain_token:
        return ""
    return hashlib.sha256(plain_token.encode("utf-8")).hexdigest()


# ─── Settings ────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = "") -> str:
    """Liest einen globalen Einstellungswert."""
    with _conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    """Setzt einen globalen Einstellungswert (upsert)."""
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """, (key, value, now))


# ─── Tenant Logs ─────────────────────────────────────────────────────────────

_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}
_LOG_KEEP = 1000   # Max entries per tenant


def add_tenant_log(tenant_id: str, level: str, category: str, message: str) -> None:
    """Schreibt einen Log-Eintrag für einen Tenant. Hält max. _LOG_KEEP Einträge."""
    if not tenant_id:
        return
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO tenant_logs (tenant_id, timestamp, level, category, message)"
            " VALUES (?,?,?,?,?)",
            (tenant_id, now, level.upper(), category.upper(), message[:2000])
        )
        # Auto-trim: älteste Einträge löschen wenn Limit überschritten
        conn.execute("""
            DELETE FROM tenant_logs
            WHERE tenant_id=? AND id NOT IN (
                SELECT id FROM tenant_logs WHERE tenant_id=? ORDER BY id DESC LIMIT ?
            )
        """, (tenant_id, tenant_id, _LOG_KEEP))


def get_tenant_logs(
    tenant_id: str,
    min_level: str = "DEBUG",
    limit: int = 300,
    category: str = "",
) -> list[dict]:
    """Gibt Log-Einträge eines Tenants zurück, nach Level und optional Kategorie gefiltert."""
    min_val = _LEVEL_ORDER.get(min_level.upper(), 0)
    levels  = [l for l, v in _LEVEL_ORDER.items() if v >= min_val]
    placeholders = ",".join("?" * len(levels))
    params = [tenant_id] + levels
    cat_clause = ""
    if category:
        cat_clause = " AND category=?"
        params.append(category.upper())
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT id, timestamp, level, category, message"
            f" FROM tenant_logs"
            f" WHERE tenant_id=? AND level IN ({placeholders}){cat_clause}"
            f" ORDER BY id DESC LIMIT ?",
            params
        ).fetchall()
    return [dict(r) for r in rows]


def clear_tenant_logs(tenant_id: str) -> int:
    """Löscht alle Log-Einträge eines Tenants. Gibt Anzahl gelöschter Zeilen zurück."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM tenant_logs WHERE tenant_id=?", (tenant_id,))
        return cur.rowcount


# ─── Users ────────────────────────────────────────────────────────────────────

def has_users() -> bool:
    """True wenn mindestens ein Benutzer existiert."""
    with _conn() as conn:
        count = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        return count > 0


def username_exists(username: str, exclude_id: str = "") -> bool:
    with _conn() as conn:
        if exclude_id:
            row = conn.execute("SELECT id FROM users WHERE username=? AND id!=?",
                               (username.strip(), exclude_id)).fetchone()
        else:
            row = conn.execute("SELECT id FROM users WHERE username=?",
                               (username.strip(),)).fetchone()
        return row is not None


def _find_tenant_owner_user_id() -> str:
    """Liefert die user_id des (einzigen) Tenant-Owners im Single-Tenant-Modell.

    Bei mehreren Tenants (Legacy-Daten aus der Multi-Tenant-Zeit) wird der
    älteste Tenant-Eintrag genommen, der zusätzlich einen Admin-User hat.
    Leerer String wenn noch kein Tenant existiert (First-Boot).
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT t.user_id FROM tenants t "
            "JOIN users u ON u.id = t.user_id "
            "WHERE u.is_admin = 1 "
            "ORDER BY t.created_at ASC LIMIT 1"
        ).fetchone()
        if row:
            return row["user_id"]
        # Fallback: irgendein Tenant (falls kein Admin-Join matched)
        row = conn.execute(
            "SELECT user_id FROM tenants ORDER BY created_at ASC LIMIT 1"
        ).fetchone()
    return row["user_id"] if row else ""


def _resolve_tenant_owner_for(user_id: str) -> str:
    """Gibt die user_id des Tenant-Owners zurück, zu dem `user_id` gehört.

    - Wenn user_id selbst ein Tenant-Eintrag besitzt → user_id
    - Sonst: parent_user_id folgen (eine Ebene reicht im Single-Tenant-Modell)
    - Fallback: ältester Tenant-Owner
    """
    uid = (user_id or "").strip()
    if not uid:
        return _find_tenant_owner_user_id()
    with _conn() as conn:
        row = conn.execute(
            "SELECT u.parent_user_id, t.id AS tenant_id "
            "FROM users u LEFT JOIN tenants t ON t.user_id = u.id "
            "WHERE u.id = ?",
            (uid,),
        ).fetchone()
    if not row:
        return _find_tenant_owner_user_id()
    if row["tenant_id"]:
        return uid
    parent = (row["parent_user_id"] or "").strip() if row["parent_user_id"] else ""
    return parent or _find_tenant_owner_user_id()


def create_user(username: str, password: str, email: str = "", is_first: bool = False, full_name: str = "", company: str = "") -> dict:
    """
    Legt einen neuen Benutzer via Registrierungs-Wizard an.
    Erster Benutzer (is_first=True): Admin + automatisch genehmigt + Tenant-Owner.
    Alle weiteren: pending (warten auf Admin-Freischaltung) und werden in den
    bestehenden Tenant gehängt (parent_user_id zeigt auf den Tenant-Owner).
    """
    from crypto import hash_password
    uid = str(uuid.uuid4())
    now = _now()
    status = "approved" if is_first else "pending"
    is_admin = 1 if is_first else 0
    role_type = _normalize_role_type("admin" if is_first else "employee", bool(is_admin))
    parent_user_id = "" if is_first else _find_tenant_owner_user_id()
    pw_hash = hash_password(password)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (id, username, email, full_name, company, password_hash, is_admin, role_type, parent_user_id, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, username.strip(), email.strip(), full_name.strip(), company.strip(), pw_hash, is_admin, role_type, parent_user_id, status, now),
        )
    return get_user_by_id(uid)


def create_user_admin(
    username: str,
    password: str,
    email: str = "",
    is_admin: bool = False,
    role_type: str = "",
    status: str = "approved",
    full_name: str = "",
    company: str = "",
    parent_user_id: str = "",
) -> dict:
    """
    Legt einen Benutzer direkt durch einen Admin an (ohne Wizard-Flow).
    Status und Adminrechte werden explizit gesetzt.

    Seit v7.0.0 (Single-Tenant-Modell): Ein neuer User wird in den bestehenden
    Tenant gehängt — `parent_user_id` zeigt auf den Owner des Tenants. Es
    wird KEIN eigener Tenant mehr für den neuen User angelegt.
    """
    from crypto import hash_password
    uid = str(uuid.uuid4())
    now = _now()
    normalized_role = _normalize_role_type(role_type, is_admin)
    effective_parent = (parent_user_id or "").strip() or _find_tenant_owner_user_id()
    pw_hash = hash_password(password)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users (id, username, email, full_name, company, password_hash, is_admin, role_type, parent_user_id, status, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (uid, username.strip(), email.strip(), full_name.strip(), company.strip(), pw_hash, 1 if normalized_role == 'admin' else 0, normalized_role, effective_parent, status, now),
        )
    return get_user_by_id(uid)


def create_invited_user(
    username: str,
    password: str,
    email: str,
    full_name: str = "",
    company: str = "",
    invited_by_user_id: str = "",
    invitation_language: str = "de",
    is_admin: bool = False,
    role_type: str = "",
    parent_user_id: str = "",
    printix_user_id: str = "",
) -> dict:
    """
    Legt einen Benutzer per Einladungs-Flow an.
    Der Benutzer ist freigeschaltet, muss aber beim ersten Login sein Passwort ändern.

    Seit v7.0.0 (Single-Tenant-Modell): Eingeladene User werden in den
    bestehenden Tenant gehängt — `parent_user_id` zeigt auf den Tenant-Owner.
    Fehlt `parent_user_id`, wird er aus `invited_by_user_id` abgeleitet, sonst
    aus dem Tenant-Owner (erster Admin).
    """
    from crypto import hash_password
    uid = str(uuid.uuid4())
    now = _now()
    normalized_role = _normalize_role_type(role_type, is_admin)
    # Parent-Resolution: übergebene parent_user_id > inviter > tenant-owner
    effective_parent = (parent_user_id or "").strip()
    if not effective_parent and invited_by_user_id.strip():
        effective_parent = _resolve_tenant_owner_for(invited_by_user_id.strip())
    if not effective_parent:
        effective_parent = _find_tenant_owner_user_id()
    pw_hash = hash_password(password)
    with _conn() as conn:
        conn.execute(
            "INSERT INTO users ("
            "id, username, email, full_name, company, password_hash, is_admin, role_type, parent_user_id, printix_user_id, status, "
            "must_change_password, invited_by_user_id, invitation_language, invitation_sent_at, created_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                uid,
                username.strip(),
                email.strip(),
                full_name.strip(),
                company.strip(),
                pw_hash,
                1 if normalized_role == 'admin' else 0,
                normalized_role,
                effective_parent,
                (printix_user_id or "").strip(),
                "approved",
                1,
                invited_by_user_id.strip(),
                invitation_language.strip(),
                now,
                now,
            ),
        )
    return get_user_by_id(uid)


def _create_empty_tenant(user_id: str, name: str = "") -> dict:
    """Erstellt einen leeren Tenant mit generierten Auth-Credentials."""
    tid = str(uuid.uuid4())
    now = _now()
    bearer_plain = secrets.token_urlsafe(48)
    oauth_id = "px-" + secrets.token_hex(8)
    oauth_secret_plain = secrets.token_urlsafe(32)
    with _conn() as conn:
        conn.execute("""
            INSERT INTO tenants (
              id, user_id, name,
              printix_tenant_id,
              print_client_id, print_client_secret,
              card_client_id,  card_client_secret,
              ws_client_id,    ws_client_secret,
              um_client_id,    um_client_secret,
              shared_client_id, shared_client_secret,
              oauth_client_id, oauth_client_secret,
              bearer_token, bearer_token_hash,
              sql_server, sql_database, sql_username, sql_password,
              mail_api_key, mail_from,
              created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tid, user_id, name,
            "", "", "", "", "", "", "", "", "", "", "",
            oauth_id, _enc(oauth_secret_plain),
            _enc(bearer_plain), _bearer_hash(bearer_plain),
            "", "printix_bi_data_2_1", "", "",
            "", "",
            now,
        ))
    return {
        "bearer_token": bearer_plain,
        "oauth_client_id": oauth_id,
        "oauth_client_secret": oauth_secret_plain,
    }


def authenticate_user(username: str, password: str) -> Optional[dict]:
    """Prüft Benutzername + Passwort, gibt User-Dict zurück oder None."""
    from crypto import verify_password
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?",
                           (username.strip(),)).fetchone()
    if not row:
        return None
    user = dict(row)
    if not verify_password(password, user["password_hash"]):
        return None
    return _user_public(user)


def get_user_by_id(user_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return _user_public(dict(row)) if row else None


def get_all_users() -> list:
    with _conn() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall()
        return [_user_public(dict(r)) for r in rows]


def count_tenants() -> int:
    with _conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM tenants").fetchone()[0]


def set_user_status(user_id: str, status: str) -> None:
    with _conn() as conn:
        conn.execute("UPDATE users SET status=? WHERE id=?", (status, user_id))


class LastAdminError(Exception):
    """Wird geworfen, wenn die Aktion den letzten Admin entfernen wuerde."""


def _count_other_admins(user_id: str) -> int:
    """Zaehlt Admins mit status='approved' abgesehen vom gegebenen User."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM users "
            "WHERE is_admin = 1 AND status = 'approved' AND id != ?",
            (user_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def _is_tenant_owner(user_id: str) -> bool:
    """Prueft, ob der User Owner (user_id in tenants) des bestehenden Tenants ist."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tenants WHERE user_id = ? LIMIT 1",
            (user_id,),
        ).fetchone()
    return row is not None


def set_user_admin(user_id: str, is_admin: bool) -> None:
    """Setzt das Admin-Flag. Verhindert das Entfernen des letzten Admins."""
    if not is_admin and _count_other_admins(user_id) == 0:
        raise LastAdminError(
            "Dieser Admin kann nicht degradiert werden — es ist der letzte "
            "aktive Admin. Ernenne zuerst einen anderen User zum Admin."
        )
    with _conn() as conn:
        # is_admin und role_type synchron halten
        new_role = "admin" if is_admin else "employee"
        conn.execute(
            "UPDATE users SET is_admin=?, role_type=? WHERE id=?",
            (1 if is_admin else 0, new_role, user_id),
        )


def update_user(
    user_id: str,
    username: Optional[str] = None,
    email: Optional[str] = None,
    is_admin: Optional[bool] = None,
    role_type: Optional[str] = None,
    status: Optional[str] = None,
    full_name: Optional[str] = None,
    company: Optional[str] = None,
    printix_user_id: Optional[str] = None,
) -> Optional[dict]:
    """Aktualisiert Benutzerdaten (nur gesetzte Felder)."""
    parts, params = [], []
    if username is not None:
        parts.append("username=?"); params.append(username.strip())
    if email is not None:
        parts.append("email=?"); params.append(email.strip())
    if full_name is not None:
        parts.append("full_name=?"); params.append(full_name.strip())
    if company is not None:
        parts.append("company=?"); params.append(company.strip())
    if printix_user_id is not None:
        parts.append("printix_user_id=?"); params.append(printix_user_id.strip())
    normalized_role = None
    if role_type is not None or is_admin is not None:
        normalized_role = _normalize_role_type(role_type, bool(is_admin))
    # Last-Admin-Safeguard: verhindert Runterstufen des letzten Admins.
    if normalized_role is not None and normalized_role != "admin":
        current = get_user_by_id(user_id)
        if current and current.get("is_admin") and _count_other_admins(user_id) == 0:
            raise LastAdminError(
                "Rolle kann nicht geaendert werden — dieser User ist der "
                "letzte Admin. Ernenne zuerst einen anderen User zum Admin."
            )
    if is_admin is False and normalized_role != "admin":
        current = get_user_by_id(user_id)
        if current and current.get("is_admin") and _count_other_admins(user_id) == 0:
            raise LastAdminError(
                "Admin-Rechte koennen nicht entzogen werden — dies ist der "
                "letzte Admin."
            )
    if normalized_role is not None:
        parts.append("role_type=?"); params.append(normalized_role)
        # is_admin IMMER mitpflegen, damit die zwei Felder konsistent bleiben
        parts.append("is_admin=?"); params.append(1 if normalized_role == "admin" else 0)
    elif is_admin is not None:
        parts.append("is_admin=?"); params.append(1 if is_admin else 0)
    if status is not None:
        parts.append("status=?"); params.append(status)
    if not parts:
        return get_user_by_id(user_id)
    params.append(user_id)
    with _conn() as conn:
        conn.execute(f"UPDATE users SET {', '.join(parts)} WHERE id=?", params)
    return get_user_by_id(user_id)


def reset_user_password(user_id: str, new_password: str) -> bool:
    """Setzt Passwort zurück (Admin-Funktion oder Self-Service)."""
    from crypto import hash_password
    pw_hash = hash_password(new_password)
    with _conn() as conn:
        cur = conn.execute("UPDATE users SET password_hash=? WHERE id=?", (pw_hash, user_id))
    return cur.rowcount > 0


def complete_invitation_password_change(user_id: str, new_password: str) -> bool:
    """Setzt ein neues Passwort und markiert die Einladung als angenommen."""
    from crypto import hash_password
    pw_hash = hash_password(new_password)
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE users SET password_hash=?, must_change_password=0, invitation_accepted_at=? WHERE id=?",
            (pw_hash, _now(), user_id),
        )
    return cur.rowcount > 0


def delete_user(user_id: str) -> bool:
    """
    Löscht einen Benutzer.
    Gibt False zurück wenn der Benutzer nicht existiert.

    Seit v7.0.0 (Single-Tenant-Modell):
      - Der Tenant-Owner darf nicht geloescht werden — sonst waere der
        eine Tenant verwaist. Erst via Tenant-Transfer einen neuen Owner
        setzen, dann kann der alte User geloescht werden.
      - Letzter Admin darf nicht geloescht werden (Last-Admin-Safeguard).
      - Fuer alle anderen User: nur den User-Eintrag loeschen, der Tenant
        bleibt unberuehrt.
    """
    current = get_user_by_id(user_id)
    if not current:
        return False
    if current.get("is_admin") and _count_other_admins(user_id) == 0:
        raise LastAdminError(
            "Dieser User ist der letzte Admin und kann nicht geloescht werden. "
            "Ernenne zuerst einen anderen User zum Admin."
        )
    if _is_tenant_owner(user_id):
        raise LastAdminError(
            "Dieser User ist der Tenant-Owner. Das Loeschen wuerde den "
            "Tenant verwaisen. Uebertrage zuerst die Tenant-Ownership "
            "auf einen anderen Admin."
        )
    with _conn() as conn:
        cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return cur.rowcount > 0


def _user_public(user: dict) -> dict:
    """Gibt ein User-Dict ohne password_hash zurück."""
    role_type = _normalize_role_type(user.get("role_type", ""), bool(user["is_admin"]))
    return {
        "id":         user["id"],
        "username":   user["username"],
        "email":      user.get("email", ""),
        "full_name":  user.get("full_name", ""),
        "company":    user.get("company", ""),
        "is_admin":   bool(user["is_admin"]),
        "role_type":  role_type,
        "is_employee": role_type == "employee",
        "printix_user_id": user.get("printix_user_id", ""),
        "parent_user_id": user.get("parent_user_id", ""),

        "status":     user["status"],
        "must_change_password": bool(user.get("must_change_password", 0)),
        "invited_by_user_id": user.get("invited_by_user_id", ""),
        "invitation_language": user.get("invitation_language", ""),
        "invitation_sent_at": user.get("invitation_sent_at", ""),
        "invitation_accepted_at": user.get("invitation_accepted_at", ""),
        "created_at": user.get("created_at", ""),
        "entra_oid":  user.get("entra_oid", ""),
    }


# ─── Entra ID SSO ───────────────────────────────────────────────────────────

def get_or_create_entra_user(
    entra_oid: str,
    email: str,
    display_name: str,
) -> Optional[dict]:
    """
    Findet oder erstellt einen Benutzer anhand der Entra Object-ID.

    Reihenfolge:
      1. User mit passender entra_oid → direkt zurückgeben
      2. User mit passender E-Mail → entra_oid verknüpfen
      3. Neuen User anlegen (Status: pending oder approved je nach Einstellung)
    """
    if not entra_oid:
        return None

    # 1. Suche nach entra_oid
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE entra_oid = ?", (entra_oid,)
        ).fetchone()
        if row:
            return _user_public(dict(row))

    # 2. Suche nach E-Mail und verknüpfe (case-insensitive)
    if email:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email COLLATE NOCASE = ?", (email.strip(),)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE users SET entra_oid = ? WHERE id = ?",
                    (entra_oid, row["id"]),
                )
                user = dict(row)
                user["entra_oid"] = entra_oid
                return _user_public(user)

    # 3. Neuen User anlegen
    uid = str(uuid.uuid4())
    now = _now()

    # Username aus E-Mail ableiten
    username = email.split("@")[0] if email else display_name.replace(" ", ".").lower()
    username = username.strip() or f"entra_{entra_oid[:8]}"
    base = username
    suffix = 1
    while username_exists(username):
        username = f"{base}{suffix}"
        suffix += 1

    # Zufälliges Passwort (User meldet sich via Entra an, nicht per Passwort)
    random_pw = secrets.token_urlsafe(32)
    from crypto import hash_password
    pw_hash = hash_password(random_pw)

    # Auto-Approve prüfen
    auto_approve = get_setting("entra_auto_approve", "0") == "1"
    status = "approved" if auto_approve else "pending"
    # Entra-SSO-User wird Mitarbeiter im bestehenden (einzigen) Tenant
    parent_uid = _find_tenant_owner_user_id()

    with _conn() as conn:
        conn.execute(
            "INSERT INTO users "
            "(id, username, email, full_name, company, password_hash, "
            " is_admin, role_type, parent_user_id, status, created_at, entra_oid) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, username, email.strip(), display_name, "",
             pw_hash, 0, "employee", parent_uid, status, now, entra_oid),
        )

    logger.info("Entra-User angelegt: %s (%s) → status=%s, parent=%s",
                username, email, status, parent_uid or "-")
    return get_user_by_id(uid)


# ─── Tenants ──────────────────────────────────────────────────────────────────

def create_tenant(
    user_id: str,
    printix_tenant_id: str,
    name: str = "",
    print_client_id: str = "",
    print_client_secret: str = "",
    card_client_id: str = "",
    card_client_secret: str = "",
    ws_client_id: str = "",
    ws_client_secret: str = "",
    um_client_id: str = "",
    um_client_secret: str = "",
    shared_client_id: str = "",
    shared_client_secret: str = "",
    sql_server: str = "",
    sql_database: str = "printix_bi_data_2_1",
    sql_username: str = "",
    sql_password: str = "",
    mail_api_key: str = "",
    mail_from: str = "",
) -> dict:
    """
    Legt einen Tenant-Datensatz via Wizard an.
    Generiert automatisch: bearer_token, oauth_client_id, oauth_client_secret.
    Gibt ein Dict mit Klartextwerten zurück (einmaliger Zugriff!).
    """
    tid = str(uuid.uuid4())
    now = _now()
    bearer_plain = secrets.token_urlsafe(48)
    oauth_id = "px-" + secrets.token_hex(8)
    oauth_secret_plain = secrets.token_urlsafe(32)

    with _conn() as conn:
        conn.execute("""
            INSERT INTO tenants (
              id, user_id, name,
              printix_tenant_id,
              print_client_id, print_client_secret,
              card_client_id,  card_client_secret,
              ws_client_id,    ws_client_secret,
              um_client_id,    um_client_secret,
              shared_client_id, shared_client_secret,
              oauth_client_id, oauth_client_secret,
              bearer_token, bearer_token_hash,
              sql_server, sql_database, sql_username, sql_password,
              mail_api_key, mail_from,
              created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            tid, user_id, name or printix_tenant_id,
            printix_tenant_id,
            print_client_id, _enc(print_client_secret),
            card_client_id,  _enc(card_client_secret),
            ws_client_id,    _enc(ws_client_secret),
            um_client_id,    _enc(um_client_secret),
            shared_client_id, _enc(shared_client_secret),
            oauth_id, _enc(oauth_secret_plain),
            _enc(bearer_plain), _bearer_hash(bearer_plain),
            sql_server, sql_database, sql_username, _enc(sql_password),
            _enc(mail_api_key), mail_from,
            now,
        ))

    return {
        "id":                  tid,
        "name":                name or printix_tenant_id,
        "printix_tenant_id":   printix_tenant_id,
        "oauth_client_id":     oauth_id,
        "oauth_client_secret": oauth_secret_plain,
        "bearer_token":        bearer_plain,
    }


def get_tenant_by_user_id(user_id: str) -> Optional[dict]:
    """Gibt Tenant-Infos für das Dashboard zurück (keine Secrets)."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "id":                d["id"],
        "name":              d["name"],
        "printix_tenant_id": d["printix_tenant_id"],
        "oauth_client_id":   d["oauth_client_id"],
        "print_client_id":   d["print_client_id"],
        "card_client_id":    d["card_client_id"],
        "ws_client_id":      d["ws_client_id"],
        "um_client_id":      d.get("um_client_id", ""),
        "shared_client_id":  d.get("shared_client_id", ""),
        "sql_server":        d["sql_server"],
        "sql_database":      d["sql_database"],
        "sql_username":      d["sql_username"],
        "mail_from":         d["mail_from"],
        # Bearer Token für Dashboard-Anzeige (entschlüsselt)
        "bearer_token":      _dec(d.get("bearer_token", "")),
    }


def get_tenant_full_by_user_id(user_id: str) -> Optional[dict]:
    """
    Gibt alle Tenant-Felder für die Einstellungsseite zurück.
    Secrets werden entschlüsselt — nur für den Benutzer selbst verwenden!
    """
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE user_id=?", (user_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "id":                  d["id"],
        "name":                d["name"],
        "tenant_url":          d.get("tenant_url", ""),
        "printix_tenant_id":   d["printix_tenant_id"],
        "print_client_id":     d["print_client_id"],
        "print_client_secret": _dec(d.get("print_client_secret", "")),
        "card_client_id":      d["card_client_id"],
        "card_client_secret":  _dec(d.get("card_client_secret", "")),
        "ws_client_id":        d["ws_client_id"],
        "ws_client_secret":    _dec(d.get("ws_client_secret", "")),
        "um_client_id":        d.get("um_client_id", ""),
        "um_client_secret":    _dec(d.get("um_client_secret", "")),
        "shared_client_id":    d.get("shared_client_id", ""),
        "shared_client_secret": _dec(d.get("shared_client_secret", "")),
        "oauth_client_id":     d["oauth_client_id"],
        "oauth_client_secret": _dec(d.get("oauth_client_secret", "")),
        "bearer_token":        _dec(d.get("bearer_token", "")),
        "sql_server":          d["sql_server"],
        "sql_database":        d["sql_database"],
        "sql_username":        d["sql_username"],
        "sql_password":        _dec(d.get("sql_password", "")),
        "mail_api_key":        _dec(d.get("mail_api_key", "")),
        "mail_from":           d["mail_from"],
        "mail_from_name":      d.get("mail_from_name", ""),
        "alert_recipients":    d.get("alert_recipients", ""),
        "alert_min_level":     d.get("alert_min_level", "ERROR"),
        "poller_state":        d.get("poller_state", "{}"),
        "default_card_profile_id": d.get("default_card_profile_id", ""),
    }


def update_poller_state(user_id: str, state: dict) -> None:
    """Speichert den Event-Poller-Zustand fuer einen Tenant (als JSON)."""
    import json as _json
    state_str = _json.dumps(state)
    with _conn() as conn:
        conn.execute(
            "UPDATE tenants SET poller_state = ? WHERE user_id = ?",
            (state_str, user_id),
        )


def update_tenant_credentials(
    user_id: str,
    printix_tenant_id: Optional[str] = None,
    name: Optional[str] = None,
    tenant_url: Optional[str] = None,
    print_client_id: Optional[str] = None,
    print_client_secret: Optional[str] = None,
    card_client_id: Optional[str] = None,
    card_client_secret: Optional[str] = None,
    ws_client_id: Optional[str] = None,
    ws_client_secret: Optional[str] = None,
    um_client_id: Optional[str] = None,
    um_client_secret: Optional[str] = None,
    shared_client_id: Optional[str] = None,
    shared_client_secret: Optional[str] = None,
    sql_server: Optional[str] = None,
    sql_database: Optional[str] = None,
    sql_username: Optional[str] = None,
    sql_password: Optional[str] = None,
    mail_api_key: Optional[str] = None,
    mail_from: Optional[str] = None,
    mail_from_name: Optional[str] = None,
    alert_recipients: Optional[str] = None,
    alert_min_level: Optional[str] = None,
) -> bool:
    """
    Aktualisiert Tenant-Credentials (nur gesetzte Felder).
    Secrets werden automatisch verschlüsselt.
    """
    parts, params = [], []

    def _add(col: str, val, encrypt: bool = False):
        if val is not None:
            parts.append(f"{col}=?")
            params.append(_enc(val) if encrypt and val else val)

    _add("name",                 name)
    _add("tenant_url",           tenant_url)
    _add("printix_tenant_id",    printix_tenant_id)
    _add("print_client_id",      print_client_id)
    _add("print_client_secret",  print_client_secret, encrypt=True)
    _add("card_client_id",       card_client_id)
    _add("card_client_secret",   card_client_secret,  encrypt=True)
    _add("ws_client_id",         ws_client_id)
    _add("ws_client_secret",     ws_client_secret,    encrypt=True)
    _add("um_client_id",         um_client_id)
    _add("um_client_secret",     um_client_secret,    encrypt=True)
    _add("shared_client_id",     shared_client_id)
    _add("shared_client_secret", shared_client_secret, encrypt=True)
    _add("sql_server",           sql_server)
    _add("sql_database",         sql_database)
    _add("sql_username",         sql_username)
    _add("sql_password",         sql_password,        encrypt=True)
    _add("mail_api_key",         mail_api_key,        encrypt=True)
    _add("mail_from",            mail_from)
    _add("mail_from_name",       mail_from_name)
    _add("alert_recipients",     alert_recipients)
    _add("alert_min_level",      alert_min_level)

    if not parts:
        return True

    params.append(user_id)
    with _conn() as conn:
        cur = conn.execute(
            f"UPDATE tenants SET {', '.join(parts)} WHERE user_id=?", params
        )
    return cur.rowcount > 0


def regenerate_oauth_secret(user_id: str) -> Optional[str]:
    """
    Generiert ein neues OAuth Client-Secret für den Tenant des Benutzers.
    Gibt das neue Secret im Klartext zurück (einmalig!), oder None wenn kein Tenant.
    """
    new_secret = secrets.token_urlsafe(32)
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE tenants SET oauth_client_secret=? WHERE user_id=?",
            (_enc(new_secret), user_id)
        )
    return new_secret if cur.rowcount > 0 else None


def get_tenant_by_bearer_token(bearer_token: str) -> Optional[dict]:
    """
    Sucht Tenant anhand des Bearer Tokens.

    Fast Path (v3.9.1+): Indexierter Lookup über bearer_token_hash (O(1)).
    Wird bei jedem authentifizierten MCP-Request aufgerufen; der vorherige
    Full-Table-Scan mit Fernet-Decrypt pro Zeile war ein harter Bottleneck.

    Fallback: Falls der Hash (noch) nicht gesetzt ist — z.B. während eines
    halb-abgeschlossenen Upgrades oder nach externer DB-Manipulation —
    iterieren wir einmalig über die betroffenen Zeilen und tragen den Hash
    direkt nach. Der Decryption-Fehler wird protokolliert (vorher wurde er
    stumm verschluckt).
    """
    if not bearer_token:
        return None

    token_hash = _bearer_hash(bearer_token)

    # Fast Path: indexierter Lookup
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE bearer_token_hash = ?",
            (token_hash,),
        ).fetchone()
    if row:
        return _tenant_decrypted(dict(row))

    # Legacy Fallback: Zeilen ohne Hash (Backfill verpasst?) scannen + nachtragen
    with _conn() as conn:
        legacy_rows = conn.execute(
            "SELECT * FROM tenants "
            "WHERE bearer_token_hash = '' OR bearer_token_hash IS NULL"
        ).fetchall()
    for row in legacy_rows:
        d = dict(row)
        try:
            plain = _dec(d.get("bearer_token", ""))
        except Exception as e:
            logger.warning(
                "Bearer-Token-Lookup: Entschlüsselung für Tenant %s fehlgeschlagen: %s",
                d.get("id", "?"), e,
            )
            continue
        if not plain:
            continue
        # Hash für diese Zeile nachtragen (einmaliger Kosten, danach fast path)
        try:
            with _conn() as conn:
                conn.execute(
                    "UPDATE tenants SET bearer_token_hash = ? WHERE id = ?",
                    (_bearer_hash(plain), d["id"]),
                )
        except Exception as e:
            logger.warning(
                "Bearer-Token-Lookup: Hash-Backfill für Tenant %s fehlgeschlagen: %s",
                d.get("id", "?"), e,
            )
        if plain == bearer_token:
            return _tenant_decrypted(d)
    return None


def get_tenant_by_oauth_client_id(client_id: str) -> Optional[dict]:
    """Gibt Tenant anhand oauth_client_id zurück (für OAuth Authorize-Seite)."""
    with _conn() as conn:
        row = conn.execute("SELECT * FROM tenants WHERE oauth_client_id=?",
                           (client_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "id":               d["id"],
        "name":             d["name"],
        "oauth_client_id":  d["oauth_client_id"],
        "bearer_token":     _dec(d.get("bearer_token", "")),
    }


def verify_tenant_oauth_secret(tenant_id: str, client_secret: str) -> bool:
    """Prüft das OAuth Client-Secret für einen Tenant."""
    with _conn() as conn:
        row = conn.execute("SELECT oauth_client_secret FROM tenants WHERE id=?",
                           (tenant_id,)).fetchone()
    if not row:
        return False
    return _dec(row["oauth_client_secret"]) == client_secret


def _tenant_decrypted(d: dict) -> dict:
    """Gibt alle Felder eines Tenants entschlüsselt zurück."""
    return {
        "id":                  d["id"],
        "user_id":             d["user_id"],
        "name":                d["name"],
        "printix_tenant_id":   d["printix_tenant_id"],
        "print_client_id":     d["print_client_id"],
        "print_client_secret": _dec(d.get("print_client_secret", "")),
        "card_client_id":      d["card_client_id"],
        "card_client_secret":  _dec(d.get("card_client_secret", "")),
        "ws_client_id":        d["ws_client_id"],
        "ws_client_secret":    _dec(d.get("ws_client_secret", "")),
        "um_client_id":        d.get("um_client_id", ""),
        "um_client_secret":    _dec(d.get("um_client_secret", "")),
        "shared_client_id":    d.get("shared_client_id", ""),
        "shared_client_secret": _dec(d.get("shared_client_secret", "")),
        "oauth_client_id":     d["oauth_client_id"],
        "bearer_token":        _dec(d.get("bearer_token", "")),
        "sql_server":          d["sql_server"],
        "sql_database":        d["sql_database"],
        "sql_username":        d["sql_username"],
        "sql_password":        _dec(d.get("sql_password", "")),
        "mail_api_key":        _dec(d.get("mail_api_key", "")),
        "mail_from":           d["mail_from"],
    }


# ─── Audit Log ────────────────────────────────────────────────────────────────

def audit(
    user_id: Optional[str],
    action: str,
    details: str = "",
    object_type: str = "",
    object_id: str = "",
    tenant_id: str = "",
) -> None:
    """Schreibt einen Audit-Log-Eintrag.

    Rückwärts-kompatibel mit der ursprünglichen 3-Argument-Signatur (v3.8.x).
    Neue optional Felder (v3.9.0): object_type, object_id, tenant_id für den
    strukturierten Admin-Audit-Trail-Report.
    """
    with _conn() as conn:
        # v6.7.111: Wenn kein tenant_id mitgegeben wurde, aus der users-Tabelle
        # auflösen. Vorher wurden alle Einträge mit tenant_id='' geschrieben,
        # wodurch der Audit-Report pro Tenant leer blieb.
        resolved_tenant_id = tenant_id or ""
        if not resolved_tenant_id and user_id:
            # v6.7.112: users hat keine tenant_id-Spalte. Tenant-Zuordnung
            # kommt aus der tenants-Tabelle via t.user_id = users.id.
            row = conn.execute(
                "SELECT id FROM tenants WHERE user_id = ?", (user_id,)
            ).fetchone()
            if row and row["id"]:
                resolved_tenant_id = row["id"]
        conn.execute(
            "INSERT INTO audit_log (user_id, action, details, created_at, object_type, object_id, tenant_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (user_id, action, details, _now(), object_type or "", object_id or "", resolved_tenant_id),
        )


# Alias für klarere Semantik in neuen Call-Sites
audit_write = audit


def get_audit_log(limit: int = 200) -> list:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT a.*, u.username
            FROM audit_log a
            LEFT JOIN users u ON u.id = a.user_id
            ORDER BY a.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]


def query_audit_log_range(
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    tenant_id: str = "",
    action_prefix: str = "",
    limit: int = 1000,
) -> list:
    """Strukturierter Audit-Log-Query für den Report-Engine.

    start_date/end_date: ISO-Datum (YYYY-MM-DD), inklusiv
    tenant_id: wenn gesetzt, nur Einträge dieses Mandanten
    action_prefix: wenn gesetzt, nur Aktionen die damit beginnen (z.B. 'create_', 'delete_')
    """
    where = []
    params: list = []
    if start_date:
        where.append("a.created_at >= ?")
        params.append(f"{start_date}T00:00:00+00:00")
    if end_date:
        where.append("a.created_at <= ?")
        params.append(f"{end_date}T23:59:59+00:00")
    if tenant_id:
        # v6.7.112: Legacy-Rows haben a.tenant_id='' — akzeptiere sie auch
        # wenn der zum user_id gehoerende Tenant denselben Tenant hat.
        # users hat keine tenant_id-Spalte; deshalb separater JOIN auf
        # tenants via t.user_id = a.user_id.
        where.append("(a.tenant_id = ? OR (a.tenant_id = '' AND t.id = ?))")
        params.append(tenant_id)
        params.append(tenant_id)
    if action_prefix:
        where.append("a.action LIKE ?")
        params.append(f"{action_prefix}%")
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(f"""
            SELECT a.id, a.created_at AS timestamp, a.user_id, u.username AS actor,
                   a.action, a.object_type, a.object_id, a.details, a.tenant_id
            FROM audit_log a
            LEFT JOIN users u ON u.id = a.user_id
            LEFT JOIN tenants t ON t.user_id = a.user_id
            {wsql}
            ORDER BY a.created_at DESC
            LIMIT ?
        """, tuple(params)).fetchall()
        return [dict(r) for r in rows]


# ─── Feature-Request / Ticketsystem (v3.9.0) ─────────────────────────────────

def _next_ticket_no() -> str:
    """Erzeugt eine fortlaufende Ticket-Nummer im Format FR-YYYYMM-NNNN."""
    import datetime as _dt
    ym = _dt.datetime.now(timezone.utc).strftime("%Y%m")
    prefix = f"FR-{ym}-"
    with _conn() as conn:
        row = conn.execute(
            "SELECT ticket_no FROM feature_requests WHERE ticket_no LIKE ? "
            "ORDER BY ticket_no DESC LIMIT 1",
            (f"{prefix}%",),
        ).fetchone()
    if row:
        try:
            n = int(row[0].split("-")[-1]) + 1
        except Exception:
            n = 1
    else:
        n = 1
    return f"{prefix}{n:04d}"


def create_feature_request(
    user_id: Optional[str],
    user_email: str,
    title: str,
    description: str = "",
    category: str = "feature",
    tenant_id: str = "",
) -> dict:
    """Legt einen neuen Feature-Request an und liefert das erstellte Ticket."""
    ticket_no = _next_ticket_no()
    now = _now()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO feature_requests (ticket_no, user_id, user_email, tenant_id, "
            "title, description, category, status, priority, admin_note, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                ticket_no, user_id or "", user_email or "", tenant_id or "",
                title.strip(), (description or "").strip(), (category or "feature").strip(),
                "new", "normal", "", now, now,
            ),
        )
        row = conn.execute(
            "SELECT * FROM feature_requests WHERE ticket_no = ?", (ticket_no,)
        ).fetchone()
    return dict(row) if row else {}


def list_feature_requests(
    user_id: Optional[str] = None,
    status: str = "",
    limit: int = 500,
) -> list:
    """Listet Feature-Requests.

    user_id: wenn gesetzt, nur die Tickets dieses Users (für Nicht-Admins).
    status: wenn gesetzt, nur Tickets mit diesem Status.
    """
    where = []
    params: list = []
    if user_id:
        where.append("user_id = ?")
        params.append(user_id)
    if status:
        where.append("status = ?")
        params.append(status)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM feature_requests {wsql} ORDER BY created_at DESC LIMIT ?",
            tuple(params),
        ).fetchall()
    return [dict(r) for r in rows]


def get_feature_request(ticket_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM feature_requests WHERE id = ?", (ticket_id,)
        ).fetchone()
    return dict(row) if row else None


def update_feature_request_status(
    ticket_id: int,
    status: str,
    admin_note: str = "",
    priority: str = "",
) -> bool:
    """Admin-Update eines Tickets. Status: new, planned, in_progress, done, rejected, later."""
    valid = {"new", "planned", "in_progress", "done", "rejected", "later"}
    if status not in valid:
        return False
    with _conn() as conn:
        if priority:
            conn.execute(
                "UPDATE feature_requests SET status = ?, admin_note = ?, priority = ?, updated_at = ? WHERE id = ?",
                (status, admin_note or "", priority, _now(), ticket_id),
            )
        else:
            conn.execute(
                "UPDATE feature_requests SET status = ?, admin_note = ?, updated_at = ? WHERE id = ?",
                (status, admin_note or "", _now(), ticket_id),
            )
        r = conn.execute("SELECT 1 FROM feature_requests WHERE id = ?", (ticket_id,)).fetchone()
    return bool(r)


def count_feature_requests_by_status() -> dict:
    """Zählt Tickets pro Status-Bucket — fürs Admin-Badge."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM feature_requests GROUP BY status"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


# ─── Capture Profiles (v4.4.0) ──────────────────────────────────────────────

def create_capture_profile(
    tenant_id: str,
    name: str,
    plugin_type: str,
    secret_key: str = "",
    connector_token: str = "",
    config_json: str = "{}",
    is_active: bool = True,
    require_signature: bool = False,
    metadata_format: str = "flat",
    index_fields_json: str = "[]",
) -> dict:
    """Erstellt ein neues Capture-Profil für einen Tenant."""
    pid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO capture_profiles
                (id, tenant_id, name, plugin_type, secret_key, connector_token,
                 config_json, is_active, created_at, updated_at,
                 require_signature, metadata_format, index_fields_json)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            pid, tenant_id, name.strip(), plugin_type,
            _enc(secret_key), _enc(connector_token),
            _enc(config_json), 1 if is_active else 0,
            now, now,
            1 if require_signature else 0,
            metadata_format or "flat",
            index_fields_json or "[]",
        ))
    return get_capture_profile(pid)


def get_capture_profile(profile_id: str) -> Optional[dict]:
    """Gibt ein einzelnes Capture-Profil zurück (Secrets entschlüsselt)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM capture_profiles WHERE id = ?", (profile_id,)
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    return {
        "id":                d["id"],
        "tenant_id":         d["tenant_id"],
        "name":              d["name"],
        "plugin_type":       d["plugin_type"],
        "secret_key":        _dec(d.get("secret_key", "")),
        "connector_token":   _dec(d.get("connector_token", "")),
        "config_json":       _dec(d.get("config_json", "{}")),
        "is_active":         bool(d["is_active"]),
        "require_signature": bool(d.get("require_signature", 0)),
        "metadata_format":   d.get("metadata_format", "flat"),
        "index_fields_json": d.get("index_fields_json", "[]"),
        "created_at":        d["created_at"],
        "updated_at":        d["updated_at"],
    }


def get_capture_profiles_by_tenant(tenant_id: str) -> list[dict]:
    """Gibt alle Capture-Profile eines Tenants zurück."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM capture_profiles WHERE tenant_id = ? ORDER BY created_at DESC",
            (tenant_id,),
        ).fetchall()
    results = []
    for row in rows:
        d = dict(row)
        results.append({
            "id":                d["id"],
            "tenant_id":         d["tenant_id"],
            "name":              d["name"],
            "plugin_type":       d["plugin_type"],
            "secret_key":        _dec(d.get("secret_key", "")),
            "connector_token":   _dec(d.get("connector_token", "")),
            "config_json":       _dec(d.get("config_json", "{}")),
            "is_active":         bool(d["is_active"]),
            "require_signature": bool(d.get("require_signature", 0)),
            "metadata_format":   d.get("metadata_format", "flat"),
            "index_fields_json": d.get("index_fields_json", "[]"),
            "created_at":        d["created_at"],
            "updated_at":        d["updated_at"],
        })
    return results


def update_capture_profile(
    profile_id: str,
    name: Optional[str] = None,
    plugin_type: Optional[str] = None,
    secret_key: Optional[str] = None,
    connector_token: Optional[str] = None,
    config_json: Optional[str] = None,
    is_active: Optional[bool] = None,
    require_signature: Optional[bool] = None,
    metadata_format: Optional[str] = None,
    index_fields_json: Optional[str] = None,
) -> Optional[dict]:
    """Aktualisiert ein Capture-Profil (nur gesetzte Felder)."""
    parts, params = [], []
    if name is not None:
        parts.append("name=?"); params.append(name.strip())
    if plugin_type is not None:
        parts.append("plugin_type=?"); params.append(plugin_type)
    if secret_key is not None:
        parts.append("secret_key=?"); params.append(_enc(secret_key))
    if connector_token is not None:
        parts.append("connector_token=?"); params.append(_enc(connector_token))
    if config_json is not None:
        parts.append("config_json=?"); params.append(_enc(config_json))
    if is_active is not None:
        parts.append("is_active=?"); params.append(1 if is_active else 0)
    if require_signature is not None:
        parts.append("require_signature=?"); params.append(1 if require_signature else 0)
    if metadata_format is not None:
        parts.append("metadata_format=?"); params.append(metadata_format)
    if index_fields_json is not None:
        parts.append("index_fields_json=?"); params.append(index_fields_json)
    if not parts:
        return get_capture_profile(profile_id)
    parts.append("updated_at=?"); params.append(_now())
    params.append(profile_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE capture_profiles SET {', '.join(parts)} WHERE id = ?", params
        )
    return get_capture_profile(profile_id)


def delete_capture_profile(profile_id: str) -> bool:
    """Löscht ein Capture-Profil."""
    with _conn() as conn:
        cur = conn.execute("DELETE FROM capture_profiles WHERE id = ?", (profile_id,))
    return cur.rowcount > 0


def get_capture_profile_for_webhook(profile_id: str) -> Optional[dict]:
    """
    Schneller Lookup für den Webhook-Handler — gibt nur die nötigen Felder
    zurück (Secret, Token, Plugin-Config, Auth-Settings). Kein Tenant-Join nötig.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, tenant_id, name, plugin_type, secret_key, connector_token, "
            "config_json, is_active, require_signature, metadata_format, index_fields_json "
            "FROM capture_profiles WHERE id = ?",
            (profile_id,),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    if not d["is_active"]:
        return None
    return {
        "id":                d["id"],
        "tenant_id":         d["tenant_id"],
        "name":              d["name"],
        "plugin_type":       d["plugin_type"],
        "secret_key":        _dec(d.get("secret_key", "")),
        "connector_token":   _dec(d.get("connector_token", "")),
        "config_json":       _dec(d.get("config_json", "{}")),
        "require_signature": bool(d.get("require_signature", 0)),
        "metadata_format":   d.get("metadata_format", "flat"),
        "index_fields_json": d.get("index_fields_json", "[]"),
    }


def add_capture_log(
    tenant_id: str, profile_id: str, profile_name: str,
    event_type: str, status: str, message: str,
    details: str = "",
) -> None:
    """Schreibt einen Capture-Log-Eintrag in die tenant_logs Tabelle."""
    prefix = f"[{profile_name}] [{event_type}] [{status}]"
    full_msg = f"{prefix} {message}"
    if details:
        full_msg += f" | {details[:500]}"
    add_tenant_log(tenant_id, "INFO" if status == "ok" else "ERROR", "CAPTURE", full_msg)


# ─── Guest-Print (v7.1.0) ─────────────────────────────────────────────────────
#
# Drei Tabellen:
#   guestprint_mailbox — ein ueberwachtes Outlook/Exchange-Postfach (UPN)
#   guestprint_guest   — Gast-Allowlist pro Postfach (sender_email UNIQUE)
#   guestprint_job     — Verarbeitungslog, Idempotenz via
#                        (mailbox_id, message_id, attachment_name) UNIQUE
#
# Keine Secrets in den Feldern — die Entra-App-Credentials liegen bereits
# global in settings (entra_*), der Graph-Access-Token wird in-memory
# gecached. Deshalb hier kein _enc()/_dec().

# --- Mailbox ---

def create_guestprint_mailbox(
    tenant_id: str,
    upn: str,
    name: str = "",
    default_printer_id: str = "",
    default_queue_id: str = "",
    poll_interval_sec: int = 60,
    folder_processed: str = "GuestPrint/Processed",
    folder_skipped: str = "GuestPrint/Skipped",
    on_success: str = "move",
    max_attachment_bytes: int = 26214400,
    enabled: bool = True,
) -> dict:
    """Legt ein neues ueberwachtes Postfach an."""
    mid = str(uuid.uuid4())
    now = _now()
    on_success = on_success if on_success in ("move", "keep", "delete") else "move"
    with _conn() as conn:
        conn.execute("""
            INSERT INTO guestprint_mailbox
                (id, tenant_id, name, upn, default_printer_id, default_queue_id,
                 poll_interval_sec, folder_processed, folder_skipped, on_success,
                 max_attachment_bytes, enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            mid, tenant_id, (name or upn).strip(), upn.strip().lower(),
            default_printer_id, default_queue_id,
            int(poll_interval_sec), folder_processed, folder_skipped, on_success,
            int(max_attachment_bytes), 1 if enabled else 0, now, now,
        ))
    return get_guestprint_mailbox(mid)


def get_guestprint_mailbox(mailbox_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM guestprint_mailbox WHERE id = ?", (mailbox_id,)
        ).fetchone()
    return _mailbox_row(row) if row else None


def list_guestprint_mailboxes(tenant_id: str, only_enabled: bool = False) -> list[dict]:
    q = "SELECT * FROM guestprint_mailbox WHERE tenant_id = ?"
    params: list = [tenant_id]
    if only_enabled:
        q += " AND enabled = 1"
    q += " ORDER BY created_at ASC"
    with _conn() as conn:
        rows = conn.execute(q, params).fetchall()
    return [_mailbox_row(r) for r in rows]


def update_guestprint_mailbox(mailbox_id: str, **fields) -> Optional[dict]:
    """Aktualisiert ein Mailbox-Setting. Erlaubt: name, upn, default_printer_id,
    default_queue_id, poll_interval_sec, folder_processed, folder_skipped,
    max_attachment_bytes, enabled, last_poll_at, last_error."""
    allowed = {
        "name", "upn", "default_printer_id", "default_queue_id",
        "poll_interval_sec", "folder_processed", "folder_skipped", "on_success",
        "max_attachment_bytes", "enabled", "last_poll_at", "last_error",
    }
    parts, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "enabled":
            v = 1 if v else 0
        elif k in ("poll_interval_sec", "max_attachment_bytes"):
            v = int(v)
        elif k == "upn" and isinstance(v, str):
            v = v.strip().lower()
        elif k == "on_success":
            v = v if v in ("move", "keep", "delete") else "move"
        parts.append(f"{k}=?"); params.append(v)
    if not parts:
        return get_guestprint_mailbox(mailbox_id)
    parts.append("updated_at=?"); params.append(_now())
    params.append(mailbox_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE guestprint_mailbox SET {', '.join(parts)} WHERE id = ?", params
        )
    return get_guestprint_mailbox(mailbox_id)


def delete_guestprint_mailbox(mailbox_id: str) -> bool:
    # ON DELETE CASCADE auf guestprint_guest wuerde funktionieren, wenn
    # PRAGMA foreign_keys=ON — haben wir. Jobs bleiben erhalten (historisch).
    with _conn() as conn:
        cur = conn.execute("DELETE FROM guestprint_mailbox WHERE id = ?", (mailbox_id,))
    return cur.rowcount > 0


def _mailbox_row(row) -> dict:
    d = dict(row)
    return {
        "id":                   d["id"],
        "tenant_id":            d["tenant_id"],
        "name":                 d.get("name", ""),
        "upn":                  d.get("upn", ""),
        "default_printer_id":   d.get("default_printer_id", ""),
        "default_queue_id":     d.get("default_queue_id", ""),
        "poll_interval_sec":    int(d.get("poll_interval_sec") or 60),
        "folder_processed":     d.get("folder_processed", "GuestPrint/Processed"),
        "folder_skipped":       d.get("folder_skipped", "GuestPrint/Skipped"),
        "on_success":           (d.get("on_success") or "move"),
        "max_attachment_bytes": int(d.get("max_attachment_bytes") or 26214400),
        "enabled":              bool(d.get("enabled", 0)),
        "last_poll_at":         d.get("last_poll_at", ""),
        "last_error":           d.get("last_error", ""),
        "created_at":           d["created_at"],
        "updated_at":           d["updated_at"],
    }


# --- Guest ---

def create_guestprint_guest(
    mailbox_id: str,
    sender_email: str,
    full_name: str = "",
    printix_user_id: str = "",
    printix_guest_email: str = "",
    printer_id: str = "",
    queue_id: str = "",
    expiration_days: int = 7,
    expires_at: str = "",
    enabled: bool = True,
) -> dict:
    """Legt einen Gast in der Allowlist an. (mailbox_id, sender_email) ist UNIQUE."""
    gid = str(uuid.uuid4())
    now = _now()
    with _conn() as conn:
        conn.execute("""
            INSERT INTO guestprint_guest
                (id, mailbox_id, sender_email, full_name,
                 printix_user_id, printix_guest_email, printer_id, queue_id,
                 expiration_days, expires_at, enabled, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            gid, mailbox_id, sender_email.strip().lower(), full_name.strip(),
            printix_user_id, printix_guest_email.strip().lower(),
            printer_id, queue_id,
            int(expiration_days), expires_at,
            1 if enabled else 0, now, now,
        ))
    return get_guestprint_guest(gid)


def get_guestprint_guest(guest_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM guestprint_guest WHERE id = ?", (guest_id,)
        ).fetchone()
    return _guest_row(row) if row else None


def find_guestprint_guest_by_sender(mailbox_id: str, sender_email: str) -> Optional[dict]:
    """Exact-Match-Lookup fuer den Mail-Poller."""
    if not sender_email:
        return None
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM guestprint_guest "
            "WHERE mailbox_id = ? AND sender_email = ? AND enabled = 1",
            (mailbox_id, sender_email.strip().lower()),
        ).fetchone()
    return _guest_row(row) if row else None


def list_guestprint_guests(mailbox_id: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guestprint_guest WHERE mailbox_id = ? "
            "ORDER BY sender_email ASC",
            (mailbox_id,),
        ).fetchall()
    return [_guest_row(r) for r in rows]


def update_guestprint_guest(guest_id: str, **fields) -> Optional[dict]:
    allowed = {
        "sender_email", "full_name", "printix_user_id", "printix_guest_email",
        "printer_id", "queue_id", "expiration_days", "expires_at",
        "enabled", "last_match_at",
    }
    parts, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "enabled":
            v = 1 if v else 0
        elif k == "expiration_days":
            v = int(v)
        elif k in ("sender_email", "printix_guest_email") and isinstance(v, str):
            v = v.strip().lower()
        parts.append(f"{k}=?"); params.append(v)
    if not parts:
        return get_guestprint_guest(guest_id)
    parts.append("updated_at=?"); params.append(_now())
    params.append(guest_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE guestprint_guest SET {', '.join(parts)} WHERE id = ?", params
        )
    return get_guestprint_guest(guest_id)


def delete_guestprint_guest(guest_id: str) -> bool:
    with _conn() as conn:
        cur = conn.execute("DELETE FROM guestprint_guest WHERE id = ?", (guest_id,))
    return cur.rowcount > 0


def _guest_row(row) -> dict:
    d = dict(row)
    return {
        "id":                  d["id"],
        "mailbox_id":          d["mailbox_id"],
        "sender_email":        d.get("sender_email", ""),
        "full_name":           d.get("full_name", ""),
        "printix_user_id":     d.get("printix_user_id", ""),
        "printix_guest_email": d.get("printix_guest_email", ""),
        "printer_id":          d.get("printer_id", ""),
        "queue_id":            d.get("queue_id", ""),
        "expiration_days":     int(d.get("expiration_days") or 7),
        "expires_at":          d.get("expires_at", ""),
        "enabled":             bool(d.get("enabled", 0)),
        "last_match_at":       d.get("last_match_at", ""),
        "created_at":          d["created_at"],
        "updated_at":          d["updated_at"],
    }


# --- Job ---

def create_guestprint_job(
    mailbox_id: str,
    message_id: str,
    attachment_name: str,
    guest_id: str = "",
    sender_email: str = "",
    subject: str = "",
    attachment_bytes: int = 0,
    status: str = "pending",
) -> Optional[dict]:
    """Legt einen Job-Eintrag an. Bei Duplikat (mailbox, message, attachment)
    wird der bestehende Eintrag zurueckgegeben — Idempotenz fuer den Poller.
    """
    now = _now()
    try:
        with _conn() as conn:
            cur = conn.execute("""
                INSERT INTO guestprint_job
                    (mailbox_id, guest_id, message_id, sender_email, subject,
                     attachment_name, attachment_bytes, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (
                mailbox_id, guest_id, message_id,
                (sender_email or "").strip().lower(), subject or "",
                attachment_name or "", int(attachment_bytes or 0),
                status, now, now,
            ))
            jid = cur.lastrowid
        return get_guestprint_job(jid)
    except sqlite3.IntegrityError:
        with _conn() as conn:
            row = conn.execute(
                "SELECT * FROM guestprint_job "
                "WHERE mailbox_id = ? AND message_id = ? AND attachment_name = ?",
                (mailbox_id, message_id, attachment_name or ""),
            ).fetchone()
        return _job_row(row) if row else None


def get_guestprint_job(job_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT * FROM guestprint_job WHERE id = ?", (job_id,)
        ).fetchone()
    return _job_row(row) if row else None


def list_guestprint_jobs(
    mailbox_id: str = "",
    status: str = "",
    limit: int = 200,
) -> list[dict]:
    conds, params = [], []
    if mailbox_id:
        conds.append("mailbox_id = ?"); params.append(mailbox_id)
    if status:
        conds.append("status = ?"); params.append(status)
    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    params.append(int(limit))
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM guestprint_job {where} "
            f"ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
    return [_job_row(r) for r in rows]


def update_guestprint_job(job_id: int, **fields) -> Optional[dict]:
    allowed = {"status", "error", "printix_job_id", "guest_id"}
    parts, params = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        parts.append(f"{k}=?"); params.append(v)
    if not parts:
        return get_guestprint_job(job_id)
    parts.append("updated_at=?"); params.append(_now())
    params.append(job_id)
    with _conn() as conn:
        conn.execute(
            f"UPDATE guestprint_job SET {', '.join(parts)} WHERE id = ?", params
        )
    return get_guestprint_job(job_id)


def _job_row(row) -> dict:
    d = dict(row)
    return {
        "id":               d["id"],
        "mailbox_id":       d["mailbox_id"],
        "guest_id":         d.get("guest_id", ""),
        "message_id":       d.get("message_id", ""),
        "sender_email":     d.get("sender_email", ""),
        "subject":          d.get("subject", ""),
        "attachment_name":  d.get("attachment_name", ""),
        "attachment_bytes": int(d.get("attachment_bytes") or 0),
        "printix_job_id":   d.get("printix_job_id", ""),
        "status":           d.get("status", "pending"),
        "error":            d.get("error", ""),
        "created_at":       d["created_at"],
        "updated_at":       d["updated_at"],
    }


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── DB beim Import initialisieren ────────────────────────────────────────────

try:
    init_db()
except Exception as _e:
    logger.warning("DB init beim Import fehlgeschlagen: %s", _e)
