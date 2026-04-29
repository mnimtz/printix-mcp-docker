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


# ─── PR 2: Scope catalogue + permission gate ─────────────────────────────────
#
# Five scopes describe what kind of action a tool performs. Each tool is
# tagged with exactly one scope. The role-to-scopes map below decides
# which roles are allowed to execute which scopes.
#
# Scope semantics:
#   mcp:self    — own data only (user-id arg must equal token holder)
#   mcp:read    — read-only across the tenant
#   mcp:write   — create / update / delete operations
#   mcp:audit   — audit-log access
#   mcp:system  — backups, demo data, time-bomb engine, system commands

SCOPE_SELF   = "mcp:self"
SCOPE_READ   = "mcp:read"
SCOPE_WRITE  = "mcp:write"
SCOPE_AUDIT  = "mcp:audit"
SCOPE_SYSTEM = "mcp:system"

ALL_SCOPES: tuple[str, ...] = (
    SCOPE_SELF, SCOPE_READ, SCOPE_WRITE, SCOPE_AUDIT, SCOPE_SYSTEM,
)

# Role → set of permitted scopes. Auditor gets read + audit (DPO pattern:
# can investigate but not change anything). Service Account is intentionally
# empty — its scopes are whitelisted per-token in a future PR; for now any
# call from a service account is denied unless RBAC is disabled.
ROLE_SCOPES: dict[str, frozenset[str]] = {
    ROLE_END_USER:        frozenset({SCOPE_SELF}),
    ROLE_HELPDESK:        frozenset({SCOPE_SELF, SCOPE_READ}),
    ROLE_ADMIN:           frozenset({SCOPE_SELF, SCOPE_READ, SCOPE_WRITE,
                                     SCOPE_AUDIT, SCOPE_SYSTEM}),
    ROLE_AUDITOR:         frozenset({SCOPE_READ, SCOPE_AUDIT}),
    ROLE_SERVICE_ACCOUNT: frozenset(),
}

# Tool → required scope. Default for unmapped tools = SCOPE_WRITE
# (safe-by-default: deny end_user / auditor on unknown tools until
# explicitly tagged).
TOOL_SCOPES: dict[str, str] = {
    # ─── End-User self-service (truly self by design) ──────────────────────
    # These tools either take no user-id arg (print_self uses tenant context)
    # or are stateless helpers that don't access other users' data.
    "printix_print_self":               SCOPE_SELF,
    "printix_session_print":            SCOPE_SELF,
    "printix_whoami":                   SCOPE_SELF,
    "printix_status":                   SCOPE_SELF,
    "printix_my_role":                  SCOPE_SELF,
    "printix_explain_error":            SCOPE_SELF,
    "printix_suggest_next_action":      SCOPE_SELF,
    "printix_generate_id_code":         SCOPE_SELF,

    # ─── Tools that touch other users' data — require write privileges ─────
    # quick_print and register_card take an explicit user/recipient arg and
    # would let end_user act on behalf of someone else. Mapped to WRITE so
    # only helpdesk+admin can use them.
    "printix_quick_print":              SCOPE_WRITE,
    "printix_register_card":            SCOPE_WRITE,
    "printix_card_enrol_assist":        SCOPE_WRITE,

    # ─── Read-only across the tenant ───────────────────────────────────────
    "printix_list_admins":              SCOPE_READ,
    "printix_list_backups":             SCOPE_READ,
    "printix_list_capture_profiles":    SCOPE_READ,
    "printix_list_card_profiles":       SCOPE_READ,
    "printix_list_cards":               SCOPE_READ,
    "printix_list_cards_by_tenant":     SCOPE_READ,
    "printix_list_design_options":      SCOPE_READ,
    "printix_list_feature_requests":    SCOPE_READ,
    "printix_list_groups":              SCOPE_READ,
    "printix_list_jobs":                SCOPE_READ,
    "printix_list_networks":            SCOPE_READ,
    "printix_list_printers":            SCOPE_READ,
    "printix_list_report_templates":    SCOPE_READ,
    "printix_list_schedules":           SCOPE_READ,
    "printix_list_sites":               SCOPE_READ,
    "printix_list_snmp_configs":        SCOPE_READ,
    "printix_list_timebombs":           SCOPE_READ,
    "printix_list_users":               SCOPE_READ,
    "printix_list_workstations":        SCOPE_READ,
    "printix_get_card_details":         SCOPE_READ,
    "printix_get_card_profile":         SCOPE_READ,
    "printix_get_feature_request":      SCOPE_READ,
    "printix_get_group":                SCOPE_READ,
    "printix_get_group_members":        SCOPE_READ,
    "printix_get_job":                  SCOPE_READ,
    "printix_get_network":              SCOPE_READ,
    "printix_get_network_context":      SCOPE_READ,
    "printix_get_printer":              SCOPE_READ,
    "printix_get_queue_context":        SCOPE_READ,
    "printix_get_report_template":      SCOPE_READ,
    "printix_get_site":                 SCOPE_READ,
    "printix_get_snmp_config":          SCOPE_READ,
    "printix_get_snmp_context":         SCOPE_READ,
    "printix_get_user":                 SCOPE_READ,
    "printix_get_user_card_context":    SCOPE_READ,
    "printix_get_user_groups":          SCOPE_READ,
    "printix_get_workstation":          SCOPE_READ,
    "printix_find_user":                SCOPE_READ,
    "printix_find_orphaned_mappings":   SCOPE_READ,
    "printix_search_card":              SCOPE_READ,
    "printix_search_card_mappings":     SCOPE_READ,
    "printix_query_anomalies":          SCOPE_READ,
    "printix_query_any":                SCOPE_READ,
    "printix_query_cost_report":        SCOPE_READ,
    "printix_query_print_stats":        SCOPE_READ,
    "printix_query_top_printers":       SCOPE_READ,
    "printix_query_top_users":          SCOPE_READ,
    "printix_query_trend":              SCOPE_READ,
    "printix_top_printers":             SCOPE_READ,
    "printix_top_users":                SCOPE_READ,
    "printix_describe_capture_profile": SCOPE_READ,
    "printix_describe_user_print_pattern": SCOPE_READ,
    "printix_resolve_printer":          SCOPE_READ,
    "printix_resolve_recipients":       SCOPE_READ,
    "printix_inactive_users":           SCOPE_READ,
    "printix_jobs_stuck":               SCOPE_READ,
    "printix_compare_periods":          SCOPE_READ,
    "printix_cost_by_department":       SCOPE_READ,
    "printix_print_history_natural":    SCOPE_READ,
    "printix_print_trends":             SCOPE_READ,
    "printix_printer_health_report":    SCOPE_READ,
    "printix_network_printers":         SCOPE_READ,
    "printix_site_summary":             SCOPE_READ,
    "printix_tenant_summary":           SCOPE_READ,
    "printix_permission_matrix":        SCOPE_READ,
    "printix_reporting_status":         SCOPE_READ,
    "printix_sso_status":               SCOPE_READ,
    "printix_capture_status":           SCOPE_READ,
    "printix_card_audit":               SCOPE_READ,
    "printix_decode_card_value":        SCOPE_READ,
    "printix_transform_card_value":     SCOPE_READ,
    "printix_diagnose_user":            SCOPE_READ,
    "printix_quota_guard":              SCOPE_READ,
    "printix_user_360":                 SCOPE_READ,
    "printix_suggest_profile":          SCOPE_READ,
    "printix_natural_query":            SCOPE_READ,
    "printix_preview_report":           SCOPE_READ,
    "printix_get_network_context":      SCOPE_READ,

    # ─── Audit-only ────────────────────────────────────────────────────────
    "printix_query_audit_log":          SCOPE_AUDIT,

    # ─── System administration ─────────────────────────────────────────────
    "printix_create_backup":            SCOPE_SYSTEM,
    "printix_demo_generate":            SCOPE_SYSTEM,
    "printix_demo_rollback":            SCOPE_SYSTEM,
    "printix_demo_setup_schema":        SCOPE_SYSTEM,
    "printix_demo_status":              SCOPE_SYSTEM,
    "printix_defuse_timebomb":          SCOPE_SYSTEM,

    # ─── Mutations (default for all not explicitly listed) ─────────────────
    "printix_create_user":              SCOPE_WRITE,
    "printix_delete_user":              SCOPE_WRITE,
    "printix_offboard_user":            SCOPE_WRITE,
    "printix_onboard_user":             SCOPE_WRITE,
    "printix_welcome_user":             SCOPE_WRITE,
    "printix_create_site":              SCOPE_WRITE,
    "printix_update_site":              SCOPE_WRITE,
    "printix_delete_site":              SCOPE_WRITE,
    "printix_create_network":           SCOPE_WRITE,
    "printix_update_network":           SCOPE_WRITE,
    "printix_delete_network":           SCOPE_WRITE,
    "printix_create_snmp_config":       SCOPE_WRITE,
    "printix_delete_snmp_config":       SCOPE_WRITE,
    "printix_create_group":             SCOPE_WRITE,
    "printix_delete_group":             SCOPE_WRITE,
    "printix_save_report_template":     SCOPE_WRITE,
    "printix_delete_report_template":   SCOPE_WRITE,
    "printix_schedule_report":          SCOPE_WRITE,
    "printix_update_schedule":          SCOPE_WRITE,
    "printix_delete_schedule":          SCOPE_WRITE,
    "printix_run_report_now":           SCOPE_WRITE,
    "printix_bulk_import_cards":        SCOPE_WRITE,
    "printix_delete_card":              SCOPE_WRITE,
    "printix_submit_job":               SCOPE_WRITE,
    "printix_complete_upload":          SCOPE_WRITE,
    "printix_change_job_owner":         SCOPE_WRITE,
    "printix_delete_job":               SCOPE_WRITE,
    "printix_send_to_user":             SCOPE_WRITE,
    "printix_send_to_capture":          SCOPE_WRITE,
    "printix_print_to_recipients":      SCOPE_WRITE,
    "printix_sync_entra_group_to_printix": SCOPE_WRITE,
    "printix_send_test_email":          SCOPE_WRITE,
}


def get_tool_scope(tool_name: str) -> str:
    """Returns the scope a tool requires. Unmapped tools default to WRITE
    (safe-by-default: end_user/auditor cannot execute unknown tools)."""
    return TOOL_SCOPES.get(tool_name, SCOPE_WRITE)


def role_has_scope(role: str, scope: str) -> bool:
    """True if the role grants the given scope."""
    permitted = ROLE_SCOPES.get(normalize_role(role) or ROLE_END_USER, frozenset())
    return scope in permitted


def has_permission(role: str, tool_name: str) -> bool:
    """True if the role is allowed to invoke this tool."""
    return role_has_scope(role, get_tool_scope(tool_name))


def permission_denied_payload(tool_name: str, role: str) -> dict:
    """Standardised denial payload used by the gate wrapper."""
    return {
        "ok": False,
        "error": "permission_denied",
        "message": (
            f"Tool '{tool_name}' requires scope '{get_tool_scope(tool_name)}', "
            f"but your role '{role or 'end_user'}' does not include it."
        ),
        "your_role": role or ROLE_END_USER,
        "required_scope": get_tool_scope(tool_name),
        "hint": (
            "Contact the tenant administrator to elevate your MCP role, "
            "or use a different tool that matches your current scope."
        ),
    }
