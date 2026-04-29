"""
MCP Permission Model — Role catalogue, scope catalogue and role resolution.

PR 1 (v7.2.18) — Schema + Persistence + Admin UI only.
PR 2 will add the actual @require_scope decorator, the tools/list filter and
the live Printix-group lookup. Until PR 2 lands, resolve_mcp_role() returns
the explicit per-user override only; group-based resolution is stubbed.

Role hierarchy (highest wins on group-level conflicts):

    end_user (1)  <  helpdesk (2)  <  admin (3)

Auditor and Service Account are explicit-only roles — they are not assigned
via Printix groups but per individual user / token. This matches reality:
the GDPR-mandated DPO is named explicitly, and service tokens are not
human users that belong to Printix groups.

GDPR mapping per role (Art. references):
    end_user          → Art. 15-22 (Betroffenenrechte)
    helpdesk          → Art. 32 TOMs (Funktionstrennung)
    admin             → Art. 24 (Verantwortlicher)
    auditor / DPO     → Art. 37-39 (Datenschutzbeauftragter)
    service_account   → Art. 28 (Auftragsverarbeitung) + Art. 32
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger("printix-mcp.permissions")

# ─── Role constants ──────────────────────────────────────────────────────────

ROLE_END_USER        = "end_user"
ROLE_HELPDESK        = "helpdesk"
ROLE_ADMIN           = "admin"
ROLE_AUDITOR         = "auditor"
ROLE_SERVICE_ACCOUNT = "service_account"

ALL_ROLES: tuple[str, ...] = (
    ROLE_END_USER,
    ROLE_HELPDESK,
    ROLE_ADMIN,
    ROLE_AUDITOR,
    ROLE_SERVICE_ACCOUNT,
)

# Roles that can be inherited from a Printix group. Auditor/Service Account
# are intentionally absent: they must be set explicitly per user/token.
GROUP_ASSIGNABLE_ROLES: tuple[str, ...] = (
    ROLE_END_USER,
    ROLE_HELPDESK,
    ROLE_ADMIN,
)

# Numeric rank for "highest wins" group resolution. Auditor and
# service_account are not part of the rank because they are not group-
# assignable; we assign them an arbitrary low value to keep dict lookups
# safe even if they ever appear in a group-resolve path.
ROLE_RANK: dict[str, int] = {
    ROLE_SERVICE_ACCOUNT: 0,
    ROLE_END_USER: 1,
    ROLE_HELPDESK: 2,
    ROLE_AUDITOR: 2,
    ROLE_ADMIN: 3,
}

ROLE_LABELS_EN: dict[str, str] = {
    ROLE_END_USER: "End User",
    ROLE_HELPDESK: "Helpdesk",
    ROLE_ADMIN: "Admin",
    ROLE_AUDITOR: "Auditor (DPO)",
    ROLE_SERVICE_ACCOUNT: "Service Account",
}

ROLE_LABELS_DE: dict[str, str] = {
    ROLE_END_USER: "Endbenutzer",
    ROLE_HELPDESK: "Helpdesk",
    ROLE_ADMIN: "Administrator",
    ROLE_AUDITOR: "Auditor (DSB)",
    ROLE_SERVICE_ACCOUNT: "Dienstkonto",
}

ROLE_DESCRIPTIONS_EN: dict[str, str] = {
    ROLE_END_USER:
        "Standard employee. Can print own jobs, register own card, "
        "see own print history and quota. Cannot see or modify other users.",
    ROLE_HELPDESK:
        "Support function. Can diagnose other users, reset cards, reassign "
        "job ownership, read reports and view the tenant. Cannot delete "
        "users, modify infrastructure or create backups.",
    ROLE_ADMIN:
        "Customer administrator. Full create/read/update/delete authority "
        "across users, sites, networks, cards, reports, capture profiles "
        "and backups. Equivalent to the current default permission level.",
    ROLE_AUDITOR:
        "DPO / compliance function. Read-only access to the audit log, "
        "reports and user list. No print actions, no mutations. Mandated "
        "by GDPR Art. 37-39 for organisations subject to DPO appointment.",
    ROLE_SERVICE_ACCOUNT:
        "Headless automation token (capture callbacks, scheduled reports, "
        "integrations). Permitted scopes are whitelisted explicitly. Not a "
        "human user; cannot log in to the web UI.",
}

ROLE_DESCRIPTIONS_DE: dict[str, str] = {
    ROLE_END_USER:
        "Normaler Mitarbeiter. Kann eigene Jobs drucken, eigene Karte "
        "registrieren, eigenen Druck-Verlauf und Quota sehen. Sieht oder "
        "ändert keine anderen Benutzer.",
    ROLE_HELPDESK:
        "Support-Funktion. Kann andere Benutzer diagnostizieren, Karten "
        "zurücksetzen, Job-Owner ändern, Reports lesen und den Tenant "
        "einsehen. Kann keine Benutzer löschen, keine Infrastruktur "
        "ändern und keine Backups erstellen.",
    ROLE_ADMIN:
        "Tenant-Administrator. Voller CRUD-Zugriff auf Benutzer, Sites, "
        "Netzwerke, Karten, Reports, Capture-Profile und Backups. "
        "Entspricht der aktuellen Standard-Berechtigung.",
    ROLE_AUDITOR:
        "DSB / Compliance-Funktion. Nur-Lese-Zugriff auf Audit-Log, "
        "Reports und Benutzerliste. Keine Druckaktionen, keine Mutationen. "
        "Gefordert durch DSGVO Art. 37-39 für Organisationen mit DSB-Pflicht.",
    ROLE_SERVICE_ACCOUNT:
        "Headless-Automatisierungs-Token (Capture-Callbacks, geplante "
        "Reports, Integrationen). Erlaubte Scopes sind explizit whitelisted. "
        "Kein menschlicher Benutzer; keine Web-UI-Anmeldung möglich.",
}

# ─── Resolution ──────────────────────────────────────────────────────────────

# Cache TTL for the user→groups membership lookup. Set conservatively low
# enough that role changes propagate within a coffee break, but high enough
# that the Printix API is not hammered on every MCP call.
USER_GROUP_CACHE_TTL_SECONDS = 300  # 5 minutes


def normalize_role(value: str | None) -> str:
    """Returns a known role string, or empty string if unknown / blank."""
    if not value:
        return ""
    v = value.strip().lower()
    return v if v in ALL_ROLES else ""


def highest_role(roles: Iterable[str]) -> str:
    """Returns the role with the highest rank, or empty string if none.

    Used for resolving group-derived role when a user is in multiple
    groups with different role assignments.
    """
    best_role = ""
    best_rank = -1
    for r in roles:
        nr = normalize_role(r)
        if not nr:
            continue
        rank = ROLE_RANK.get(nr, -1)
        if rank > best_rank:
            best_rank = rank
            best_role = nr
    return best_role


def resolve_mcp_role(user_id: str) -> str:
    """Returns the effective MCP role for a user.

    Resolution order:
        1. Explicit per-user override in users.mcp_role
        2. (PR 2) Highest role among the user's Printix-group assignments
        3. Default: end_user

    PR 1 implements only step 1. Step 2 will be wired up in PR 2 once the
    decorator and tools/list filter are in place; that needs a Printix-API
    client at call-time which is currently per-tenant scoped.
    """
    try:
        import db  # local import to avoid circular dependency at module load
        user = db.get_user_by_id(user_id)
    except Exception as e:
        logger.warning("resolve_mcp_role: DB lookup failed for %s: %s", user_id, e)
        return ROLE_END_USER

    if not user:
        return ROLE_END_USER

    explicit = normalize_role(user.get("mcp_role"))
    if explicit:
        return explicit

    # PR 2: query db.get_user_group_cache(user_id) → group_ids; for each
    # group fetch db.get_group_mcp_role(gid); return highest_role(...).
    # For now, fall through to end_user.

    return ROLE_END_USER


def is_role_assignable_to_group(role: str) -> bool:
    """True if a Printix group can be tagged with this role.

    Auditor and Service Account are explicit-only — they must not appear
    as group defaults because they are personal/system designations,
    not organisational scopes.
    """
    return normalize_role(role) in GROUP_ASSIGNABLE_ROLES


def now_iso() -> str:
    """Helper: ISO-8601 UTC timestamp string for DB writes."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_group_id_list(raw: str | None) -> list[str]:
    """Decode the JSON array stored in user_group_cache.group_ids."""
    if not raw:
        return []
    try:
        v = json.loads(raw)
        return [str(x) for x in v] if isinstance(v, list) else []
    except Exception:
        return []
