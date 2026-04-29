"""
Printix MCP Server — Home Assistant Add-on (Multi-Tenant)
=================================================================
Model Context Protocol server for the Printix Cloud Print API.

v2.0.0: Multi-Tenant Betrieb — alle Zugangsdaten werden per Tenant in der SQLite-DB
(/data/printix_multi.db) verwaltet. Konfiguration erfolgt über die Web-UI (Port 8080).

Env vars (aus run.sh):
  MCP_PORT        — Listen port (default: 8765)
  MCP_HOST        — Listen host (default: 0.0.0.0)
  MCP_LOG_LEVEL   — debug/info/warning/error/critical
  MCP_PUBLIC_URL  — Öffentliche URL (für OAuth Discovery)

Pro Request wird der Tenant anhand des Bearer Tokens aus der DB nachgeschlagen.
Die Tenant-Credentials werden über ContextVars weitergegeben (thread-safe).

Transports:
  POST /mcp   → Streamable HTTP (claude.ai)
  GET  /sse   → SSE Transport   (ChatGPT)
"""

import os
import re
import sys
import json
import logging
from typing import Any, Optional
from collections import OrderedDict

from mcp.types import ToolAnnotations
from mcp.server.fastmcp import FastMCP
from printix_client import PrintixClient, PrintixAPIError
from auth import BearerAuthMiddleware, current_tenant
from oauth import OAuthMiddleware
from app_version import APP_VERSION


# ─── Logging Setup ────────────────────────────────────────────────────────────

LOG_LEVEL = os.environ.get("MCP_LOG_LEVEL", "info").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)

# Drittbibliotheken auf WARNING festhalten — auch bei DEBUG-Modus
# (MCP-intern loggt sonst komplette JSON-Payloads, urllib3 jeden TCP-Handshake)
for _noisy in (
    "mcp.server.sse",
    "mcp.server.lowlevel.server",
    "mcp.server.fastmcp.server",
    "urllib3.connectionpool",
    "httpx",
    "httpcore",
    "python_multipart",
    "python_multipart.multipart",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# uvicorn.access bleibt auf INFO — wichtig für Debugging eingehender Requests
# (v4.4.9: war vorher auf WARNING unterdrückt → Webhooks unsichtbar)

logger = logging.getLogger("printix.mcp")
logger.info("Log-Level: %s", LOG_LEVEL)


# ─── Tenant-aware DB Log Handler ──────────────────────────────────────────────

class _TenantDBHandler(logging.Handler):
    """
    Leitet Log-Einträge in die tenant_logs SQLite-Tabelle weiter,
    sofern ein Tenant-Kontext (current_tenant ContextVar) aktiv ist.
    Kategorien: PRINTIX_API | SQL | AUTH | SYSTEM
    """
    _CATEGORY_MAP = {
        "printix_client": "PRINTIX_API",
        "printix.api":    "PRINTIX_API",
        "reporting":      "SQL",
        "sql":            "SQL",
        "auth":           "AUTH",
        "oauth":          "AUTH",
    }

    def emit(self, record: logging.LogRecord) -> None:
        try:
            tenant = current_tenant.get()
            if not tenant:
                return
            tid = tenant.get("id", "")
            if not tid:
                return
            name_lower = record.name.lower()
            category = "SYSTEM"
            for key, cat in self._CATEGORY_MAP.items():
                if key in name_lower:
                    category = cat
                    break
            msg = self.format(record)
            from db import add_tenant_log
            add_tenant_log(tid, record.levelname, category, msg)
        except Exception:
            pass  # Niemals den Server wegen Logging crashen


_tenant_handler = _TenantDBHandler()
_tenant_handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
_tenant_handler.setLevel(logging.DEBUG)
logging.getLogger().addHandler(_tenant_handler)  # Root-Logger → alle Tenants


# ─── Setup ────────────────────────────────────────────────────────────────────
# host="0.0.0.0" deaktiviert die Auto-Aktivierung des DNS-Rebinding-Schutzes.
# FastMCP aktiviert ihn nur wenn host in ("127.0.0.1", "localhost", "::1").
# Mit "0.0.0.0" bleibt transport_security=None → keine Host-Validierung.
mcp = FastMCP("Printix", host="0.0.0.0")


# ─── v7.2.23: MCP Permission Gate (PR 2 enforcement) ──────────────────────────
#
# Wraps every `@mcp.tool(...)` registration to enforce role-based access
# control. The role of the calling user is resolved from `current_tenant`
# (set by BearerAuthMiddleware/OAuthMiddleware before any tool call), and
# checked against the tool's required scope (see permissions.TOOL_SCOPES).
#
# Activation: env var MCP_RBAC_ENABLED=1. When unset/false the gate is a
# pass-through and behaviour matches v7.2.22 exactly. When enabled, denied
# calls return a structured permission_denied JSON payload and are logged
# to the audit table with action='mcp_permission_denied'.
#
# Failure mode is intentional:
#   - RBAC disabled               → always allow (PR 1 compatibility mode)
#   - RBAC enabled, no tenant ctx → deny (auth middleware should have set it)
#   - RBAC enabled, db error      → deny + log (fail-closed for safety)
import asyncio as _aio_for_gate
import functools as _functools
import inspect as _inspect

_RBAC_ENABLED = (os.getenv("MCP_RBAC_ENABLED", "0").strip().lower()
                 in ("1", "true", "yes", "on"))


def _check_tool_permission(tool_name: str) -> str | None:
    """Returns None when the call is permitted. Returns a JSON-serialised
    denial payload (string) when the call must be rejected — to be returned
    from the wrapped tool as if it were the tool's own response.
    """
    if not _RBAC_ENABLED:
        return None
    try:
        from permissions import (
            resolve_mcp_role, has_permission, permission_denied_payload,
        )
    except Exception as e:
        logger.error("RBAC: permissions module import failed: %s", e)
        return _ok({"ok": False, "error": "rbac_init_failed",
                    "message": str(e)})

    tenant = current_tenant.get()
    if not tenant:
        # Authenticated calls always have a tenant. None here means the
        # auth middleware did not run — refuse rather than leak data.
        logger.warning("RBAC: no tenant context for tool '%s'", tool_name)
        return _ok({
            "ok": False, "error": "no_tenant_context",
            "message": ("Authentication missing. The MCP server requires a "
                        "valid bearer token or OAuth session."),
        })

    user_id = tenant.get("user_id") or ""
    if not user_id:
        # Tenant exists but isn't bound to a user — historical edge case
        # for service-tenant rows from very old installations. Allow only
        # admin-equivalent scopes (legacy compat); deny self-only tools.
        logger.warning("RBAC: tenant has no user_id (legacy?), tool='%s'", tool_name)
        return None  # treat as legacy admin

    try:
        role = resolve_mcp_role(user_id)
    except Exception as e:
        logger.error("RBAC: resolve_mcp_role failed for user %s: %s", user_id, e)
        return _ok({"ok": False, "error": "role_resolution_failed",
                    "message": "Could not resolve MCP role for caller."})

    if has_permission(role, tool_name):
        return None  # allowed

    # Denied — record for the audit trail and return structured response.
    try:
        import db as _db
        _db.audit(
            user_id=user_id,
            action="mcp_permission_denied",
            details=f"Tool '{tool_name}' denied for role '{role}'.",
            object_type="mcp_tool",
            object_id=tool_name,
            tenant_id=tenant.get("id", ""),
        )
    except Exception as e:
        logger.warning("RBAC: audit insert for denied call failed: %s", e)

    logger.info("RBAC: denied tool='%s' role='%s' user=%s",
                tool_name, role, user_id)
    return _ok(permission_denied_payload(tool_name, role))


# Save and replace mcp.tool so every subsequent registration gets gated.
_orig_mcp_tool = mcp.tool


def _guarded_mcp_tool(*dec_args, **dec_kwargs):
    """Replacement for `@mcp.tool(...)` that adds an automatic permission
    check. Behaves identically to the original decorator in every other
    respect — same args, same return value, same descriptor wrapping.
    """
    def _wrap(fn):
        tool_name = fn.__name__
        if _aio_for_gate.iscoroutinefunction(fn):
            @_functools.wraps(fn)
            async def _async_guarded(*args, **kwargs):
                denial = _check_tool_permission(tool_name)
                if denial is not None:
                    return denial
                return await fn(*args, **kwargs)
            wrapped = _async_guarded
        else:
            @_functools.wraps(fn)
            def _sync_guarded(*args, **kwargs):
                denial = _check_tool_permission(tool_name)
                if denial is not None:
                    return denial
                return fn(*args, **kwargs)
            wrapped = _sync_guarded
        # Hand the wrapped function to the original decorator so FastMCP
        # registers it under the same name with all annotations intact.
        return _orig_mcp_tool(*dec_args, **dec_kwargs)(wrapped)
    return _wrap


mcp.tool = _guarded_mcp_tool

if _RBAC_ENABLED:
    logger.info("RBAC: MCP_RBAC_ENABLED=1 — permission gate ACTIVE on all tools")
else:
    logger.info("RBAC: MCP_RBAC_ENABLED=0 (default) — permission gate inactive (PR 1 compat)")


def client() -> PrintixClient:
    """
    Gibt einen PrintixClient für den aktuellen Request-Tenant zurück.

    v2.0.0: Tenant-Credentials kommen aus der SQLite-DB (via current_tenant ContextVar).
    Jeder Request bekommt seinen eigenen Client mit den Credentials des anfragenden Tenants.
    """
    tenant = current_tenant.get()
    if not tenant:
        logger.error("client() aufgerufen ohne Tenant-Kontext — kein Bearer Token?")
        raise RuntimeError("Kein Tenant-Kontext. Bearer Token fehlt oder ungültig.")

    logger.debug("Client für Tenant '%s' (ID: %s)", tenant.get("name", "?"), tenant.get("id", "?"))

    return PrintixClient(
        tenant_id=tenant.get("printix_tenant_id", ""),
        print_client_id=tenant.get("print_client_id") or None,
        print_client_secret=tenant.get("print_client_secret") or None,
        card_client_id=tenant.get("card_client_id") or None,
        card_client_secret=tenant.get("card_client_secret") or None,
        ws_client_id=tenant.get("ws_client_id") or None,
        ws_client_secret=tenant.get("ws_client_secret") or None,
        um_client_id=tenant.get("um_client_id") or None,
        um_client_secret=tenant.get("um_client_secret") or None,
        shared_client_id=tenant.get("shared_client_id") or None,
        shared_client_secret=tenant.get("shared_client_secret") or None,
    )


def _json_default(o):
    """Fallback-Serializer fuer Typen die json.dumps nicht kennt.

    v6.7.108: Reports/Queries vom SQL Server liefern NUMERIC-Spalten als
    decimal.Decimal zurueck — ohne Hook kippt die gesamte Response mit
    'Object of type Decimal is not JSON serializable'. Betroffen waren
    printix_top_users, printix_query_top_users, printix_query_any und
    alle anderen Reporting-Tools.
    """
    from decimal import Decimal
    from datetime import date, datetime, time
    if isinstance(o, Decimal):
        # float statt str, damit Consumer numerisch rechnen koennen.
        return float(o)
    if isinstance(o, (datetime, date, time)):
        return o.isoformat()
    if isinstance(o, (bytes, bytearray)):
        try:
            return o.decode("utf-8")
        except Exception:
            import base64 as _b64
            return _b64.b64encode(bytes(o)).decode("ascii")
    if isinstance(o, set):
        return sorted(o) if o and all(isinstance(x, str) for x in o) else list(o)
    # Letzter Versuch: str() — besser als Crash.
    return str(o)


def _ok(data) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=_json_default)


def _err(e: PrintixAPIError) -> str:
    logger.error("API-Fehler %d: %s (ErrorID: %s)", e.status_code, e.message, e.error_id)
    return json.dumps({
        "error": True,
        "status_code": e.status_code,
        "message": e.message,
        "error_id": e.error_id,
    }, ensure_ascii=False, indent=2, default=_json_default)


def _extract_resource_id_from_href(href: str) -> str:
    href = (href or "").strip().rstrip("/")
    return href.split("/")[-1] if href else ""


def _extract_card_id_from_api(card_obj: dict) -> str:
    if not isinstance(card_obj, dict):
        return ""
    # v6.7.113: manche API-Shapes packen die eigentliche Karte in ein
    # Sub-Object (z.B. {"card": {...}} oder {"cards": [{...}]}).
    # Vorher liefen die Top-Level-Extractoren ins Leere und lieferten "".
    primary = (
        _extract_resource_id_from_href((((card_obj.get("_links") or {}).get("self") or {}).get("href", "")))
        or card_obj.get("cardId", "")
        or card_obj.get("card_id", "")
        or card_obj.get("id", "")
    )
    if primary:
        return primary
    for key in ("card", "data", "result"):
        sub = card_obj.get(key)
        if isinstance(sub, dict):
            inner = _extract_card_id_from_api(sub)
            if inner:
                return inner
    items = card_obj.get("cards") or card_obj.get("items") or card_obj.get("content")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        inner = _extract_card_id_from_api(items[0])
        if inner:
            return inner
    return ""


def _card_items(data) -> list[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("cards", "content", "items"):
            value = data.get(key)
            if isinstance(value, list):
                return [item for item in value if isinstance(item, dict)]
    return []


def _extract_owner_id_from_card(card_obj: dict) -> str:
    if not isinstance(card_obj, dict):
        return ""
    owner_link = (((card_obj.get("_links") or {}).get("owner") or {}).get("href", ""))
    owner_obj = card_obj.get("owner") if isinstance(card_obj.get("owner"), dict) else {}
    primary = (
        _extract_resource_id_from_href(owner_link)
        or owner_obj.get("id", "")
        or owner_obj.get("userId", "")
        or card_obj.get("userId", "")
        or card_obj.get("ownerId", "")
    )
    if primary:
        return primary
    # v6.7.113: rekursiv in Sub-Objects nachsehen (analog _extract_card_id_from_api)
    for key in ("card", "data", "result"):
        sub = card_obj.get(key)
        if isinstance(sub, dict):
            inner = _extract_owner_id_from_card(sub)
            if inner:
                return inner
    items = card_obj.get("cards") or card_obj.get("items") or card_obj.get("content")
    if isinstance(items, list) and items and isinstance(items[0], dict):
        inner = _extract_owner_id_from_card(items[0])
        if inner:
            return inner
    return ""


def _merge_mapping_hits(*mapping_lists) -> list[dict]:
    merged = OrderedDict()
    for items in mapping_lists:
        for item in items or []:
            mid = item.get("id")
            key = f"id:{mid}" if mid is not None else json.dumps(item, sort_keys=True, ensure_ascii=False)
            merged[key] = item
    return list(merged.values())


def _enrich_card_with_local_data(card_obj: dict, tenant_id: str, printix_user_id: str = "", requested_card_number: str = "") -> dict:
    if not isinstance(card_obj, dict):
        return {"raw": card_obj}

    enriched = dict(card_obj)
    card_id = _extract_card_id_from_api(enriched)
    owner_id = _extract_owner_id_from_card(enriched) or (printix_user_id or "")
    secret_value = (
        enriched.get("secret")
        or enriched.get("cardNumber")
        or enriched.get("number")
        or requested_card_number
        or ""
    )
    enriched["card_id"] = card_id
    enriched["owner_id"] = owner_id

    local_mapping = None
    local_matches = []
    decoded_secret = None
    try:
        from cards.store import get_mapping_by_card, search_mappings
        from cards.transform import decode_printix_secret_value

        if owner_id and card_id:
            local_mapping = get_mapping_by_card(tenant_id, owner_id, card_id)

        queries = []
        for candidate in (card_id, secret_value, requested_card_number, enriched.get("secret", ""), enriched.get("cardNumber", "")):
            candidate = (candidate or "").strip()
            if candidate and candidate not in queries:
                queries.append(candidate)

        all_hits = []
        for candidate in queries:
            hits = search_mappings(tenant_id, candidate)
            if owner_id:
                hits = [m for m in hits if not m.get("printix_user_id") or m.get("printix_user_id") == owner_id]
            all_hits.append(hits)
        local_matches = _merge_mapping_hits(*all_hits)

        if not local_mapping and local_matches:
            local_mapping = next(
                (m for m in local_matches if m.get("printix_card_id") == card_id and (not owner_id or m.get("printix_user_id") == owner_id)),
                local_matches[0],
            )

        if secret_value:
            decoded_secret = decode_printix_secret_value(secret_value)
    except Exception as enrich_err:
        enriched["local_enrichment_error"] = str(enrich_err)

    if local_mapping:
        enriched["local_mapping"] = local_mapping
    enriched["local_mappings"] = local_matches
    enriched["local_mappings_count"] = len(local_matches)
    if decoded_secret:
        enriched["decoded_secret"] = decoded_secret
    return enriched


def _extract_printer_queue_ids(printer_obj: dict) -> tuple[str, str]:
    href = (((printer_obj.get("_links") or {}).get("self") or {}).get("href", ""))
    parts = [p for p in (href or "").split("/") if p]
    printer_id = printer_obj.get("printerId", "") or printer_obj.get("printer_id", "")
    queue_id = printer_obj.get("queueId", "") or printer_obj.get("queue_id", "") or printer_obj.get("id", "")
    if "printers" in parts and "queues" in parts:
        try:
            printer_id = printer_id or parts[parts.index("printers") + 1]
            queue_id = queue_id or parts[parts.index("queues") + 1]
        except Exception:
            pass
    return str(printer_id or ""), str(queue_id or "")


# ─── Status / Info ────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_status() -> str:
    """
        Health-Check des MCP-Servers — laeuft alles, ist Tenant erreichbar?

        Wann nutzen: "Laeuft Printix?" • "Is everything up?" • "Status check"
        Wann NICHT — stattdessen: User-Stammdaten → printix_whoami ;
            Inventar-Counts → printix_tenant_summary
        Returns: {print_api, card_management, workstation_monitoring, tenant_id}.
        Args: keine.

    """
    try:
        result = client().get_credential_status()
        logger.info("Status abgefragt: %s", result)
        return _ok(result)
    except Exception as e:
        logger.error("Status-Fehler: %s", e)
        return _ok({"error": str(e)})


# ─── Drucker / Print Queues ───────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_printers(search: str = "", page: int = 0, size: int = 50) -> str:
    """
        Listet alle Drucker-Queues. Pro physischem Drucker oft mehrere Queues!

        Wann nutzen: "Welche Drucker haben wir?" • "Show all queues" •
            "List of printers"
        Wann NICHT — stattdessen: Fuzzy-Match by Name → printix_resolve_printer ;
            Detail eines Druckers → printix_get_printer ; Drucker eines Netzwerks → printix_network_printers
        Returns: printers Liste — pro Item: name (Queue-Name), vendor, location,
            connectionStatus, _links.self.href (printer_id/queue_id darin).
        Args: search Substring | page 0-basiert | size max 100.

    """
    try:
        logger.debug("list_printers(search=%s, page=%d, size=%d)", search, page, size)
        return _ok(client().list_printers(search=search or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_printer(printer_id: str, queue_id: str) -> str:
    """
        Details + Faehigkeiten einer konkreten Drucker-Queue.

        Wann nutzen: "Details zu Drucker X" • "Show printer abc"
        Wann NICHT — stattdessen: Fuzzy-Suche → printix_resolve_printer ;
            Health → printix_printer_health_report ; Queue + letzte Jobs → printix_get_queue_context
        Returns: vollstaendiges Drucker-Objekt mit capabilities.
        Args: printer_id  UUID aus list_printers _links.self.href.
            queue_id    UUID — selber Pfad.

    """
    try:
        logger.debug("get_printer(printer_id=%s, queue_id=%s)", printer_id, queue_id)
        return _ok(client().get_printer(printer_id, queue_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Print Jobs ───────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_jobs(queue_id: str = "", page: int = 0, size: int = 50) -> str:
    """
        Druckjobs auflisten, optional nach Queue gefiltert.

        Wann nutzen: "Welche Jobs sind in Queue X?" • "Recent print jobs"
        Wann NICHT — stattdessen: ein Job-Detail → printix_get_job ;
            haengende Jobs → printix_jobs_stuck ; eigene Historie → printix_print_history_natural
        Returns: jobs Liste mit id, title, ownerEmail, status, createdAt.
        Args: queue_id optional | page 0-basiert | size max 50.

    """
    try:
        logger.debug("list_jobs(queue_id=%s, page=%d, size=%d)", queue_id, page, size)
        return _ok(client().list_print_jobs(queue_id=queue_id or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_job(job_id: str) -> str:
    """
        Details zu einem konkreten Druckjob.

        Wann nutzen: "Status von Job X" • "Show job abc"
        Wann NICHT — stattdessen: gesamte Liste → printix_list_jobs ;
            haengende Jobs → printix_jobs_stuck
        Returns: vollstaendiges Job-Objekt mit Stage, Owner, Pages.
        Args: job_id  UUID.

    """
    try:
        logger.debug("get_job(job_id=%s)", job_id)
        return _ok(client().get_print_job(job_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_submit_job(
    printer_id: str,
    queue_id: str,
    title: str,
    user: str = "",
    pdl: str = "",
    color: Optional[bool] = None,
    duplex: str = "",
    copies: int = 0,
    paper_size: str = "",
    orientation: str = "",
    scaling: str = "",
) -> str:
    """
        Low-Level-Druckjob einreichen (API v1.1, Schritt 1 des 5-Stage-Submits).

        Wann nutzen: NUR fuer manuelle Multi-Step-Workflows.
        Wann NICHT — stattdessen: KI generiert PDF → printix_print_self ;
            Eine Datei an User → printix_send_to_user ; Multi-Recipient → printix_print_to_recipients
        Returns: {job:{id,…}, uploadLinks:[{url,headers:{x-ms-blob-type}}]}.
        Args: printer_id, queue_id  UUIDs aus list_printers.
            title  Job-Titel.
            user, pdl, color, duplex, copies, paper_size, orientation, scaling  alle optional.

    """
    try:
        logger.info("submit_job(printer=%s, queue=%s, title=%s)", printer_id, queue_id, title)
        return _ok(client().submit_print_job(
            printer_id=printer_id,
            queue_id=queue_id,
            title=title,
            user=user or None,
            pdl=pdl or None,
            color=color,
            duplex=duplex or None,
            copies=copies if copies > 0 else None,
            paper_size=paper_size or None,
            orientation=orientation or None,
            scaling=scaling or None,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_complete_upload(job_id: str) -> str:
    """
        Schritt 3 des Submit-Flows: Upload als komplett markieren, Drucken triggern.

        Wann nutzen: NUR im manuellen 5-Stage-Submit.
        Wann NICHT — stattdessen: High-Level-Tools → printix_print_self / _send_to_user / _print_to_recipients
        Returns: completion-status.
        Args: job_id  aus submit_job.

    """
    try:
        logger.info("complete_upload(job_id=%s)", job_id)
        return _ok(client().complete_upload(job_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_job(job_id: str) -> str:
    """
        Druckjob stornieren / loeschen.

        Wann nutzen: "Stornier Job X" • "Cancel job abc"
        Wann NICHT — stattdessen: nur Owner wechseln → printix_change_job_owner
        Returns: {ok}.
        Args: job_id  UUID.

    """
    try:
        logger.info("delete_job(job_id=%s)", job_id)
        return _ok(client().delete_print_job(job_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_change_job_owner(job_id: str, new_owner_email: str) -> str:
    """
        Job-Owner wechseln — Druckjob delegieren an anderen User.

        Wann nutzen: "Gib Job X an User Y ab" • "Delegate job to colleague"
        Wann NICHT — stattdessen: erst submitten dann delegieren ist redundant —
            beim Submit gleich user_email direkt setzen → printix_send_to_user
        Returns: {ok, job_id, new_owner}.
        Args: job_id  UUID. user_email_or_uuid  Empfaenger.

    """
    try:
        logger.info("change_job_owner(job_id=%s, new_owner=%s)", job_id, new_owner_email)
        return _ok(client().change_job_owner(job_id, new_owner_email))
    except PrintixAPIError as e:
        return _err(e)


# ─── Card Management ──────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_cards(user_id: str) -> str:
    """
        Karten eines bestimmten Users.

        Wann nutzen: "Welche Karten hat Marcus?" • "List cards for user X"
        Wann NICHT — stattdessen: User + Karten + Profile aggregiert → printix_get_user_card_context ;
            tenant-weit → printix_list_cards_by_tenant
        Returns: cards Liste mit card_id, _links.
        Args: user_id  UUID.

    """
    try:
        logger.debug("list_cards(user_id=%s)", user_id)
        c = client()
        data = c.list_user_cards(user_id=user_id)
        tenant_id = _get_card_tenant_id()
        cards = [_enrich_card_with_local_data(card, tenant_id, printix_user_id=user_id) for card in _card_items(data)]
        return _ok({
            "user_id": user_id,
            "cards": cards,
            "count": len(cards),
            "raw": data,
        })
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_search_card(card_id: str = "", card_number: str = "") -> str:
    """
        Karte per ID oder Kartennummer suchen.

        Wann nutzen: "Such Karte mit ID X" • "Find card abc"
        Wann NICHT — stattdessen: dekodieren ohne DB-Lookup → printix_decode_card_value ;
            Profil-Vorschlag → printix_suggest_profile
        Returns: matches Liste.
        Args: search  card_id ODER Kartennummer (Hex/Dec).

    """
    try:
        logger.debug("search_card(card_id=%s, card_number=%s)", card_id, card_number or "***")
        api_card = client().search_card(card_id=card_id or None,
                                        card_number=card_number or None)
        enriched = _enrich_card_with_local_data(api_card, _get_card_tenant_id(), requested_card_number=card_number)
        return _ok(enriched)
    except (PrintixAPIError, ValueError) as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_register_card(user_id: str, card_number: str) -> str:
    """
        Karte einem User zuordnen (low-level). Card-Number wird base64-encodiert.

        Wann nutzen: NUR direkt wenn UID schon transformiert ist.
        Wann NICHT — stattdessen: AI-Onboarding mit Auto-Transform → printix_card_enrol_assist ;
            CSV-Bulk → printix_bulk_import_cards
        Returns: created card-Objekt mit card_id.
        Args: user_id  UUID. card_number  bereits-transformierte Kartenwert.

    """
    try:
        logger.info("register_card(user_id=%s)", user_id)
        return _ok(client().register_card(user_id, card_number))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_card(card_id: str) -> str:
    """
        Karten-Zuordnung entfernen.

        Wann nutzen: "Loesch Karte X" • "Remove card assignment"
        Wann NICHT — stattdessen: ganzen User offboarden → printix_offboard_user (entfernt alle Karten)
        Returns: {ok}.
        Args: card_id  UUID. user_id optional.

    """
    try:
        logger.info("delete_card(card_id=%s)", card_id)
        return _ok(client().delete_card(card_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── User Management ──────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_users(
    role: str = "USER",
    query: str = "",
    page: int = 0,
    page_size: int = 50,
) -> str:
    """
        Alle User des Tenants mit Pagination + Rollen-Filter.

        Wann nutzen: "Welche User haben wir?" • "List users" • "Show all guests"
        Wann NICHT — stattdessen: nur einen User → printix_get_user / printix_find_user ;
            komplette 360-Sicht → printix_user_360
        Returns: users Liste, pagination meta.
        Args: role "USER" (Default) | "GUEST_USER" | "USER,GUEST_USER".
            query Email-/Namen-Substring.
            page, page_size.

    """
    try:
        logger.debug("list_users(role=%s, query=%s, page=%d)", role, query, page)
        return _ok(client().list_users(
            role=role or None,
            query=query or None,
            page=page,
            page_size=page_size,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_user(user_id: str) -> str:
    """
        Details eines konkreten Users.

        Wann nutzen: "Detail User X" • "Show user abc"
        Wann NICHT — stattdessen: 360-Sicht (Karten + Gruppen + Workstations) → printix_user_360 ;
            Diagnose warum etwas nicht funktioniert → printix_diagnose_user
        Returns: user-Objekt.
        Args: user_id  UUID.

    """
    try:
        logger.debug("get_user(user_id=%s)", user_id)
        return _ok(client().get_user(user_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_create_user(
    email: str,
    display_name: str,
    pin: str = "",
    password: str = "",
) -> str:
    """
        Low-Level User-anlegen (DB-Eintrag in Printix).

        Wann nutzen: NUR direkt — meistens lieber Wrapper.
        Wann NICHT — stattdessen: Kompletter Onboarding-Flow → printix_onboard_user ;
            AI-Welcome-Workflow → printix_welcome_user (nach create)
        Returns: created user-Objekt mit id.
        Args: email, display_name, pin (optional), password (optional), id_code, expiration_timestamp.

    """
    try:
        logger.info("create_user(email=%s, name=%s)", email, display_name)
        return _ok(client().create_user(
            email=email,
            display_name=display_name,
            pin=pin or None,
            password=password or None,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_user(user_id: str) -> str:
    """
        User loeschen (USER oder GUEST_USER).

        Wann nutzen: NUR mit Vorsicht — endgueltig.
        Wann NICHT — stattdessen: kompletter Offboarding-Workflow → printix_offboard_user
        Returns: {ok}.
        Args: user_id  UUID.

    """
    try:
        logger.info("delete_user(user_id=%s)", user_id)
        return _ok(client().delete_user(user_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_generate_id_code(user_id: str) -> str:
    """
        Neuen 6-stelligen ID-Code fuer einen User erzeugen (Self-Service-Token).

        Wann nutzen: "Neuer ID-Code fuer User X" • "Generate self-service code"
        Wann NICHT — stattdessen: kompletter Onboarding → printix_onboard_user
        Returns: {id_code, expires_at}.
        Args: user_id  UUID.

    """
    try:
        logger.info("generate_id_code(user_id=%s)", user_id)
        return _ok(client().generate_id_code(user_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Groups ───────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_groups(search: str = "", page: int = 0, size: int = 50) -> str:
    """
        Alle Gruppen des Tenants.

        Wann nutzen: "Welche Gruppen haben wir?" • "List groups"
        Wann NICHT — stattdessen: Mitglieder einer Gruppe → printix_get_group_members ;
            Gruppen eines Users → printix_get_user_groups ;
            einzelne Group-Details → printix_get_group
        Returns: groups Liste mit name, queueCount, userCount, _links.self.href (UUID darin).
        Args: search optional | page | size.

    """
    try:
        logger.debug("list_groups(search=%s, page=%d, size=%d)", search, page, size)
        return _ok(client().list_groups(search=search or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_group(group_id: str) -> str:
    """
        Details einer Gruppe.

        Wann nutzen: "Detail Group X" • "Show group abc"
        Wann NICHT — stattdessen: Mitglieder → printix_get_group_members ;
            komplette Liste → printix_list_groups
        Returns: group-Objekt.
        Args: group_id  UUID.

    """
    try:
        logger.debug("get_group(group_id=%s)", group_id)
        return _ok(client().get_group(group_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_create_group(name: str, external_id: str) -> str:
    """
        Neue Printix-Gruppe anlegen. VORAUSSETZUNG: Tenant hat Directory-Anbindung (Entra/AD).

        Wann nutzen: "Neue Gruppe Z anlegen" • "Create group X"
        Wann NICHT — stattdessen: schon vorhandene checken → printix_list_groups ;
            AD-Sync abgleichen → printix_sync_entra_group_to_printix
        Returns: created group-Objekt.
        Args: name  Anzeigename. external_id  externe ID (AD/Entra).
            identity_provider, description optional.

    """
    try:
        logger.info("create_group(name=%s, external_id=%s)", name, external_id)
        return _ok(client().create_group(
            name=name,
            external_id=external_id,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_group(group_id: str) -> str:
    """
        Gruppe loeschen.

        Wann nutzen: NUR mit Vorsicht.
        Wann NICHT — stattdessen: erst Mitglieder pruefen → printix_get_group_members
        Returns: {ok}.
        Args: group_id  UUID.

    """
    try:
        logger.info("delete_group(group_id=%s)", group_id)
        return _ok(client().delete_group(group_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Workstation Monitoring ───────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_workstations(
    search: str = "",
    site_id: str = "",
    page: int = 0,
    size: int = 50,
) -> str:
    """
        Verbundene Workstations des Tenants.

        Wann nutzen: "Welche Workstations sind online?" • "List workstations"
        Wann NICHT — stattdessen: einzelne Details → printix_get_workstation
        Returns: workstations Liste.
        Args: search, site_id, page, size optional.

    """
    try:
        logger.debug("list_workstations(search=%s, site_id=%s)", search, site_id)
        return _ok(client().list_workstations(
            search=search or None,
            site_id=site_id or None,
            page=page,
            size=size,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_workstation(workstation_id: str) -> str:
    """
        Details einer Workstation.

        Wann nutzen: "Detail Workstation X"
        Wann NICHT — stattdessen: gesamte Liste → printix_list_workstations
        Returns: workstation-Objekt.
        Args: workstation_id  UUID.

    """
    try:
        logger.debug("get_workstation(workstation_id=%s)", workstation_id)
        return _ok(client().get_workstation(workstation_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Sites ────────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_sites(search: str = "", page: int = 0, size: int = 50) -> str:
    """
        Alle Standorte des Tenants.

        Wann nutzen: "Welche Standorte haben wir?" • "List sites"
        Wann NICHT — stattdessen: Site-Details + Networks + Drucker → printix_site_summary ;
            einzelne Site → printix_get_site
        Returns: sites Liste mit address, timezone.
        Args: keine.

    """
    try:
        logger.debug("list_sites(search=%s, page=%d, size=%d)", search, page, size)
        return _ok(client().list_sites(search=search or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_site(site_id: str) -> str:
    """
        Details einer einzelnen Site.

        Wann nutzen: "Detail zu Site X" • "Show site abc"
        Wann NICHT — stattdessen: aggregiert mit Networks + Druckern → printix_site_summary ;
            komplette Liste → printix_list_sites
        Returns: site-Objekt.
        Args: site_id  UUID.

    """
    try:
        logger.debug("get_site(site_id=%s)", site_id)
        return _ok(client().get_site(site_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_create_site(
    name: str,
    path: str,
    admin_group_ids: str = "",
    network_ids: str = "",
) -> str:
    """
        Neuen Standort anlegen.

        Wann nutzen: "Leg Standort 'Hamburg' an" • "Create site Y"
        Wann NICHT — stattdessen: vorhandene anschauen → printix_list_sites ;
            bestehende editieren → printix_update_site
        Returns: created site mit id.
        Args: name, address, timezone optional, country_code optional.

    """
    try:
        logger.info("create_site(name=%s, path=%s)", name, path)
        agids = [x.strip() for x in admin_group_ids.split(",") if x.strip()] \
            if admin_group_ids else []
        nids = [x.strip() for x in network_ids.split(",") if x.strip()] \
            if network_ids else []
        return _ok(client().create_site(
            name=name,
            path=path,
            admin_group_ids=agids or None,
            network_ids=nids or None,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_update_site(
    site_id: str,
    name: str = "",
    path: str = "",
    admin_group_ids: str = "",
    network_ids: str = "",
) -> str:
    """
        Site-Stammdaten editieren.

        Wann nutzen: "Aktualisier die Adresse von Site X" • "Update site Y"
        Wann NICHT — stattdessen: neue anlegen → printix_create_site ;
            nur ansehen → printix_get_site
        Returns: updated site.
        Args: site_id  UUID. + alle aenderbaren Felder optional.

    """
    try:
        logger.info("update_site(site_id=%s, name=%s, path=%s)", site_id, name, path)
        agids = [x.strip() for x in admin_group_ids.split(",") if x.strip()] \
            if admin_group_ids else None
        nids = [x.strip() for x in network_ids.split(",") if x.strip()] \
            if network_ids else None
        return _ok(client().update_site(
            site_id=site_id,
            name=name or None,
            path=path or None,
            admin_group_ids=agids,
            network_ids=nids,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_site(site_id: str) -> str:
    """
        Site loeschen — VORSICHT, betrifft auch zugehoerige Networks.

        Wann nutzen: NUR mit Vorlauf.
        Wann NICHT — stattdessen: erst Site-Inventory pruefen → printix_site_summary
        Returns: {ok}.
        Args: site_id  UUID.

    """
    try:
        logger.info("delete_site(site_id=%s)", site_id)
        return _ok(client().delete_site(site_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Networks ─────────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_networks(site_id: str = "", page: int = 0, size: int = 50) -> str:
    """
        Alle Netzwerke, optional nach Site gefiltert.

        Wann nutzen: "Welche Netzwerke?" • "Networks at site X"
        Wann NICHT — stattdessen: einzelnes Network mit Druckern → printix_get_network_context ;
            nur Detail → printix_get_network
        Returns: networks Liste mit subnets, gateways.
        Args: site_id  optional.

    """
    try:
        logger.debug("list_networks(site_id=%s, page=%d, size=%d)", site_id, page, size)
        return _ok(client().list_networks(site_id=site_id or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_network(network_id: str) -> str:
    """
        Details eines einzelnen Netzwerks.

        Wann nutzen: "Detail Network X" • "Show network abc"
        Wann NICHT — stattdessen: + Drucker + Site aggregiert → printix_get_network_context ;
            Drucker direkt → printix_network_printers
        Returns: network-Objekt.
        Args: network_id  UUID.

    """
    try:
        logger.debug("get_network(network_id=%s)", network_id)
        return _ok(client().get_network(network_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_create_network(
    name: str,
    home_office: bool = False,
    client_migrate_print_queues: str = "GLOBAL_SETTING",
    air_print: bool = False,
    site_id: str = "",
    gateway_mac: str = "",
    gateway_ip: str = "",
) -> str:
    """
        Neues Netzwerk innerhalb einer Site anlegen.

        Wann nutzen: "Leg Network mit Subnet X an" • "Create network for site Y"
        Wann NICHT — stattdessen: bestehende ansehen → printix_list_networks
        Returns: created network mit id.
        Args: site_id, name, subnet, gateway optional, dns optional.

    """
    try:
        logger.info("create_network(name=%s, site_id=%s)", name, site_id)
        return _ok(client().create_network(
            name=name,
            home_office=home_office,
            client_migrate_print_queues=client_migrate_print_queues,
            air_print=air_print,
            site_id=site_id or None,
            gateway_mac=gateway_mac or None,
            gateway_ip=gateway_ip or None,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_update_network(
    network_id: str,
    name: str = "",
    subnet: str = "",
    home_office: Optional[bool] = None,
    client_migrate_print_queues: str = "",
    air_print: Optional[bool] = None,
    site_id: str = "",
) -> str:
    """
        Netzwerk-Stammdaten editieren.

        Wann nutzen: "Aenderung Subnet von Network X" • "Update network Y"
        Wann NICHT — stattdessen: neu anlegen → printix_create_network
        Returns: updated network.
        Args: network_id  UUID. + Felder optional.

    """
    try:
        logger.info("update_network(network_id=%s, name=%s)", network_id, name)
        return _ok(client().update_network(
            network_id=network_id,
            name=name or None,
            subnet=subnet or None,
            home_office=home_office,
            client_migrate_print_queues=client_migrate_print_queues or None,
            air_print=air_print,
            site_id=site_id or None,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_network(network_id: str) -> str:
    """
        Netzwerk loeschen.

        Wann nutzen: NUR mit Vorlauf.
        Wann NICHT — stattdessen: aktuelle Inhalte pruefen → printix_get_network_context
        Returns: {ok}.
        Args: network_id  UUID.

    """
    try:
        logger.info("delete_network(network_id=%s)", network_id)
        return _ok(client().delete_network(network_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── SNMP Configurations ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_snmp_configs(page: int = 0, size: int = 50) -> str:
    """
        Alle SNMP-Konfigurationen (v1/v2c/v3) des Tenants.

        Wann nutzen: "SNMP-Konfigs?" • "List SNMP profiles"
        Wann NICHT — stattdessen: einzelne Config + Drucker → printix_get_snmp_context
        Returns: snmp_configs Liste.
        Args: keine.

    """
    try:
        logger.debug("list_snmp_configs(page=%d, size=%d)", page, size)
        return _ok(client().list_snmp_configs(page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_snmp_config(config_id: str) -> str:
    """
        Details einer SNMP-Konfiguration.

        Wann nutzen: "Detail SNMP X" • "Show snmp abc"
        Wann NICHT — stattdessen: aggregiert mit Druckern + Network → printix_get_snmp_context
        Returns: snmp-Objekt.
        Args: snmp_id  UUID.

    """
    try:
        logger.debug("get_snmp_config(config_id=%s)", config_id)
        return _ok(client().get_snmp_config(config_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_create_snmp_config(
    name: str,
    get_community_name: str = "",
    set_community_name: str = "",
    tenant_default: Optional[bool] = None,
    security_level: str = "",
    version: str = "",
    username: str = "",
    context_name: str = "",
    authentication: str = "",
    authentication_key: str = "",
    privacy: str = "",
    privacy_key: str = "",
) -> str:
    """
        SNMP-Profil (v1/v2c/v3) anlegen.

        Wann nutzen: "Erstell SNMP-Config Y" • "Create SNMP profile"
        Wann NICHT — stattdessen: bestehende ansehen → printix_list_snmp_configs
        Returns: created snmp_config mit id.
        Args: name, version "v1"/"v2c"/"v3", community, auth_user/auth_protocol/auth_password (v3),
            priv_protocol/priv_password (v3-priv).

    """
    try:
        logger.info("create_snmp_config(name=%s, version=%s)", name, version)
        return _ok(client().create_snmp_config(
            name=name,
            get_community_name=get_community_name or None,
            set_community_name=set_community_name or None,
            tenant_default=tenant_default,
            security_level=security_level or None,
            version=version or None,
            username=username or None,
            context_name=context_name or None,
            authentication=authentication or None,
            authentication_key=authentication_key or None,
            privacy=privacy or None,
            privacy_key=privacy_key or None,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_snmp_config(config_id: str) -> str:
    """
        SNMP-Konfiguration loeschen.

        Wann nutzen: NUR mit Vorlauf.
        Wann NICHT — stattdessen: erst Drucker pruefen die sie nutzen → printix_get_snmp_context
        Returns: {ok}.
        Args: snmp_id  UUID.

    """
    try:
        logger.info("delete_snmp_config(config_id=%s)", config_id)
        return _ok(client().delete_snmp_config(config_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Reporting: Import ────────────────────────────────────────────────────────
# Lazy-Import: Reporting-Module sind optional — fehlen sie (z.B. pyodbc nicht
# installiert), arbeitet der MCP-Server trotzdem normal weiter.

try:
    from reporting import query_tools, template_store, report_engine, scheduler as rep_scheduler
    from reporting.mail_client import send_report as _send_report, is_configured as _mail_configured
    from reporting.sql_client  import is_configured as _sql_configured, get_tenant_id as _get_sql_tenant_id
    from reporting.log_alert_handler import register_alert_handler as _register_alert_handler
    from reporting.event_poller      import register_event_poller as _register_event_poller
    _register_alert_handler()
    _register_event_poller()
    _REPORTING_AVAILABLE = True
    logger.info("Reporting-Modul geladen")
except ImportError as _e:
    _REPORTING_AVAILABLE = False
    logger.warning("Reporting-Modul nicht verfügbar (%s) — SQL-Pakete fehlen?", _e)


def _reporting_check() -> str | None:
    """Gibt eine Fehlermeldung zurück wenn Reporting nicht verfügbar/konfiguriert ist."""
    if not _REPORTING_AVAILABLE:
        return ("Reporting-Modul nicht verfügbar. "
                "Bitte Container neu bauen — pyodbc/jinja2/apscheduler fehlen.")
    if not _sql_configured():
        return ("SQL nicht konfiguriert. "
                "Bitte sql_server, sql_database, sql_username, sql_password "
                "in den Add-on-Einstellungen ergänzen.")
    return None


# ─── Reporting: Datenabfrage-Tools ────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_reporting_status() -> str:
    """
        Status der Reports-Engine — DB-Verbindung, letzter Nightly-Run, Preset-Count.

        Wann nutzen: "Laeuft Reports?" • "Reporting status"
        Wann NICHT — stattdessen: konkretes Reports-Tool aufrufen → printix_query_*
        Returns: {sql_connected, last_run_at, preset_count}.
        Args: keine.

    """
    status: dict = {"reporting_available": _REPORTING_AVAILABLE}

    if not _REPORTING_AVAILABLE:
        status["error"] = "Reporting-Modul nicht geladen — pyodbc/jinja2/apscheduler fehlen?"
        return _ok(status)

    try:
        import pyodbc
        drivers = pyodbc.drivers()
        status["odbc_drivers_found"] = drivers
    except Exception as e:
        status["odbc_drivers_found"] = []
        status["odbc_error"] = str(e)

    from reporting.sql_client import _detect_driver, is_configured as sql_configured
    from auth import current_sql_config
    status["odbc_driver_selected"] = _detect_driver()

    # SQL-Config aus Tenant-Kontext (v2.0.0 Multi-Tenant)
    sql_cfg = current_sql_config.get() or {}
    status["sql_configured"] = bool(sql_cfg.get("server") and sql_cfg.get("username"))
    status["sql_server"]     = sql_cfg.get("server", "")
    status["sql_database"]   = sql_cfg.get("database", "")
    status["sql_username"]   = sql_cfg.get("username", "")
    status["tenant_id"]      = sql_cfg.get("tenant_id", "")

    # Mail aus Tenant-Kontext
    tenant = current_tenant.get() or {}
    from reporting.mail_client import is_configured as mail_configured
    status["mail_configured"] = bool(tenant.get("mail_api_key") and tenant.get("mail_from"))
    status["mail_from"]       = tenant.get("mail_from", "")

    if not status["odbc_drivers_found"]:
        status["hint"] = (
            "Keine ODBC-Treiber registriert. "
            "Container muss mit der neuen build.yaml (Debian-Base) neu gebaut werden: "
            "HA → Add-on → Neu bauen."
        )
    elif not status["sql_configured"]:
        status["hint"] = ("SQL-Parameter fehlen. "
                          "Bitte sql_server, sql_database, sql_username und sql_password "
                          "in der Web-UI (Port 8080) für diesen Tenant eintragen.")
    else:
        status["hint"] = "Alles konfiguriert — SQL-Abfragen sollten funktionieren."

    return _ok(status)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_print_stats(
    start_date: str,
    end_date: str,
    group_by: str = "month",
    site_id: str = "",
    user_email: str = "",
    printer_id: str = "",
) -> str:
    """
        Druckvolumen nach beliebiger Dimension (User, Site, Drucker, Zeit).

        Wann nutzen: "Druckvolumen pro X" • "Print stats grouped by Y"
        Wann NICHT — stattdessen: Trend ueber Zeit → printix_query_trend / printix_print_trends ;
            Top-N → printix_query_top_users / printix_query_top_printers ;
            Kosten → printix_query_cost_report
        Returns: rows mit count, pages, color/bw breakdown.
        Args: dimension, days, filters.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_print_stats(
            start_date=start_date, end_date=end_date, group_by=group_by,
            site_id=site_id or None, user_email=user_email or None,
            printer_id=printer_id or None,
        )
        return _ok({"rows": rows, "count": len(rows)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_cost_report(
    start_date: str,
    end_date: str,
    cost_per_sheet: float = 0.01,
    cost_per_mono: float = 0.02,
    cost_per_color: float = 0.08,
    group_by: str = "month",
    site_id: str = "",
    currency: str = "€",
) -> str:
    """
        Druckkosten, optional nach Abteilung oder User.

        Wann nutzen: "Was kostet uns das Drucken?" • "Cost report by department"
        Wann NICHT — stattdessen: Volumina ohne Preis → printix_query_print_stats ;
            Kurz-Wrapper Abteilungsvergleich → printix_cost_by_department
        Returns: cost rows mit price_per_page, total.
        Args: days, group_by.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_cost_report(
            start_date=start_date, end_date=end_date,
            cost_per_sheet=cost_per_sheet, cost_per_mono=cost_per_mono,
            cost_per_color=cost_per_color, group_by=group_by,
            site_id=site_id or None,
        )
        # Gesamtsumme berechnen
        total = {
            "total_pages":        sum(r.get("total_pages", 0) or 0 for r in rows),
            "total_cost":         round(sum(r.get("total_cost", 0) or 0 for r in rows), 2),
            "toner_cost_color":   round(sum(r.get("toner_cost_color", 0) or 0 for r in rows), 2),
            "toner_cost_bw":      round(sum(r.get("toner_cost_bw", 0) or 0 for r in rows), 2),
            "sheet_cost":         round(sum(r.get("sheet_cost", 0) or 0 for r in rows), 2),
        }
        return _ok({"rows": rows, "totals": total, "currency": currency, "count": len(rows)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_top_users(
    start_date: str,
    end_date: str,
    top_n: int = 10,
    metric: str = "pages",
    cost_per_sheet: float = 0.01,
    cost_per_mono: float = 0.02,
    cost_per_color: float = 0.08,
    site_id: str = "",
) -> str:
    """
        Top-N User mit Zeitfenster — vollstaendiges Filter-Set.

        Wann nutzen: nur fuer komplexe Filter — sonst Kurzform.
        Wann NICHT — stattdessen: einfache Top-Liste → printix_top_users
        Returns: users Liste mit metric.
        Args: days, limit, metric "pages"/"jobs", filters.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_top_users(
            start_date=start_date, end_date=end_date,
            top_n=top_n, metric=metric,
            cost_per_sheet=cost_per_sheet, cost_per_mono=cost_per_mono,
            cost_per_color=cost_per_color, site_id=site_id or None,
        )
        return _ok({"rows": rows, "count": len(rows), "metric": metric})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_top_printers(
    start_date: str,
    end_date: str,
    top_n: int = 10,
    metric: str = "pages",
    cost_per_sheet: float = 0.01,
    cost_per_mono: float = 0.02,
    cost_per_color: float = 0.08,
    site_id: str = "",
) -> str:
    """
        Top-N Drucker mit Zeitfenster — vollstaendiges Filter-Set.

        Wann nutzen: nur fuer komplexe Filter — sonst Kurzform.
        Wann NICHT — stattdessen: einfache Top-Liste → printix_top_printers
        Returns: printers Liste mit metric.
        Args: days, limit, metric, filters.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_top_printers(
            start_date=start_date, end_date=end_date,
            top_n=top_n, metric=metric,
            cost_per_sheet=cost_per_sheet, cost_per_mono=cost_per_mono,
            cost_per_color=cost_per_color, site_id=site_id or None,
        )
        return _ok({"rows": rows, "count": len(rows), "metric": metric})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_anomalies(
    start_date: str,
    end_date: str,
    threshold_multiplier: float = 2.5,
) -> str:
    """
        Anomalie-Erkennung (Volumen-Spikes, ungewoehnliche Drucker-Nutzung).

        Wann nutzen: "Gibt es Anomalien?" • "Detect outliers" • "Auffaellige Muster?"
        Wann NICHT — stattdessen: Trend → printix_query_trend ;
            Burst eines Users → printix_quota_guard
        Returns: anomalies Liste mit user, dimension, deviation.
        Args: days, sensitivity.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_anomalies(
            start_date=start_date, end_date=end_date,
            threshold_multiplier=threshold_multiplier,
        )
        return _ok({"anomalies": rows, "count": len(rows)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_trend(
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    cost_per_sheet: float = 0.01,
    cost_per_mono: float = 0.02,
    cost_per_color: float = 0.08,
) -> str:
    """
        Trendlinien ueber Zeit — eine Dimension nach Tag/Woche/Monat.

        Wann nutzen: "Wie entwickelt sich X?" • "Trend over time" mit Filtern.
        Wann NICHT — stattdessen: Kurzform → printix_print_trends ;
            Vergleich Periode A vs B → printix_compare_periods
        Returns: timeseries.
        Args: dimension, group_by, days, filters.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        result = query_tools.query_trend(
            period1_start=period1_start, period1_end=period1_end,
            period2_start=period2_start, period2_end=period2_end,
            cost_per_sheet=cost_per_sheet, cost_per_mono=cost_per_mono,
            cost_per_color=cost_per_color,
        )
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Reporting: Template-Management ───────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_save_report_template(
    name: str,
    query_type: str,
    query_params: str,
    recipients: str,
    mail_subject: str,
    output_formats: str = "html",
    schedule_frequency: str = "",
    schedule_day: int = 1,
    schedule_time: str = "08:00",
    company_name: str = "",
    primary_color: str = "#0078D4",
    footer_text: str = "",
    created_prompt: str = "",
    report_id: str = "",
    logo_base64: str = "",
    logo_mime: str = "image/png",
    logo_url: str = "",
    theme_id: str = "",
    chart_type: str = "",
    header_variant: str = "",
    density: str = "",
    font_family: str = "",
    currency: str = "",
    show_env_impact: str = "",
    logo_position: str = "",
) -> str:
    """
        Speichert eine Query + Design-Konfig als wiederverwendbares Report-Template.

        Wann nutzen: "Speicher als Template 'X'" • "Save report config"
        Wann NICHT — stattdessen: einmaligen Run ausfuehren → printix_run_report_now ;
            bestehende ansehen → printix_list_report_templates
        Returns: template_id.
        Args: name, preset, filters, design (Farben/Logo/Layout), recipients optional.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    import json as _json
    try:
        params = _json.loads(query_params) if isinstance(query_params, str) else query_params
    except Exception:
        return _ok({"error": f"query_params ist kein gültiges JSON: {query_params}"})

    recipient_list = [r.strip() for r in recipients.split(",") if r.strip()]
    format_list    = [f.strip() for f in output_formats.split(",") if f.strip()]

    schedule = None
    if schedule_frequency in ("monthly", "weekly", "daily"):
        schedule = {
            "frequency": schedule_frequency,
            "day":       schedule_day,
            "time":      schedule_time,
        }

    # v3.7.10: Logo-Auflösung analog zum Web-Formular (_resolve_logo).
    # Reihenfolge: Base64 hat Vorrang vor URL; 1MB-Cap; MIME-Safety.
    _lb64  = (logo_base64 or "").strip()
    _lmime = (logo_mime   or "image/png").strip() or "image/png"
    _lurl  = (logo_url    or "").strip()
    if _lb64:
        if not _lmime.startswith("image/"):
            _lmime = "image/png"
        # Base64-Größen-Cap (Rohbytes ≈ 0.75 × len(b64))
        _approx_raw = int(len(_lb64) * 0.75)
        if _approx_raw > 1024 * 1024:
            return _ok({"error": f"Logo zu groß ({_approx_raw} bytes > 1MB). Max 1 MB Rohgröße."})
        _lurl = ""  # Base64 gewinnt gegen URL
    else:
        _lb64  = ""
        _lmime = "image/png"

    layout = {
        "company_name":  company_name,
        "primary_color": primary_color,
        "footer_text":   footer_text,
        "logo_base64":   _lb64,
        "logo_mime":     _lmime,
        "logo_url":      _lurl,
    }
    # v4.2.0: Erweiterte Design-Parameter
    if theme_id:
        layout["theme_id"] = theme_id
    if chart_type:
        layout["chart_style"] = chart_type  # maps to design_presets key
    if header_variant:
        layout["header_variant"] = header_variant
    if density:
        layout["density"] = density
    if font_family:
        layout["font_family"] = font_family
    if currency:
        layout["currency"] = currency
    if show_env_impact:
        layout["show_env_impact"] = show_env_impact.lower() in ("true", "1", "yes", "ja")
    if logo_position:
        layout["logo_position"] = logo_position

    try:
        # owner_user_id aus Tenant-Kontext holen — nötig damit der Scheduler
        # später die korrekten Mail-Credentials aus der DB laden kann
        _t = current_tenant.get() or {}
        owner_user_id = _t.get("user_id", "") or _t.get("id", "")

        template = template_store.save_template(
            name=name, query_type=query_type, query_params=params,
            recipients=recipient_list, mail_subject=mail_subject,
            output_formats=format_list, layout=layout,
            schedule=schedule, created_prompt=created_prompt,
            report_id=report_id or None,
            owner_user_id=owner_user_id or None,
        )

        # Schedule registrieren wenn vorhanden
        if schedule and _REPORTING_AVAILABLE:
            rep_scheduler.schedule_report(template["report_id"], schedule)

        return _ok({
            "saved":     True,
            "report_id": template["report_id"],
            "name":      template["name"],
            "scheduled": schedule is not None,
            "next_run":  _next_run_info(template["report_id"]) if schedule else None,
        })
    except Exception as e:
        return _ok({"error": str(e)})


def _next_run_info(report_id: str) -> str | None:
    """Gibt den nächsten geplanten Ausführungszeitpunkt zurück."""
    try:
        jobs = rep_scheduler.list_scheduled_jobs()
        for j in jobs:
            if j["job_id"] == report_id:
                return j.get("next_run_utc")
    except Exception:
        pass
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_report_templates() -> str:
    """
        Alle gespeicherten Report-Templates des Tenants.

        Wann nutzen: "Welche Templates haben wir?" • "List saved reports"
        Wann NICHT — stattdessen: Detail eines Templates → printix_get_report_template ;
            aktive Schedules → printix_list_schedules
        Returns: templates Liste.
        Args: keine.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        _t = current_tenant.get() or {}
        owner_id = _t.get("user_id", "") or ""
        all_templates = template_store.list_templates()
        # Per-Tenant-Filter: nur eigene Templates anzeigen
        if owner_id:
            templates = [t for t in all_templates if t.get("owner_user_id", "") == owner_id]
        else:
            templates = all_templates
        scheduled_ids = {j["job_id"] for j in rep_scheduler.list_scheduled_jobs()}
        for t in templates:
            t["is_scheduled"] = t.get("report_id", "") in scheduled_ids
            t["next_run"] = _next_run_info(t["report_id"]) if t.get("report_id") in scheduled_ids else None
        return _ok({"templates": templates, "count": len(templates),
                    "hint": "report_id aus dieser Liste an printix_run_report_now übergeben"})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_report_template(report_id: str) -> str:
    """
        Details eines gespeicherten Report-Templates.

        Wann nutzen: "Detail Template X" • "Show saved report Y"
        Wann NICHT — stattdessen: Vorschau-Rendering → printix_preview_report ;
            einmal ausfuehren → printix_run_report_now
        Returns: template-Objekt.
        Args: report_id  UUID.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        template = template_store.get_template(report_id)
        if not template:
            return _ok({"error": f"Template {report_id} nicht gefunden."})
        return _ok(template)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_report_template(report_id: str) -> str:
    """
        Report-Template loeschen.

        Wann nutzen: NUR mit Vorsicht — auch verbundene Schedules werden ungueltig.
        Wann NICHT — stattdessen: nur Schedule entfernen → printix_delete_schedule
        Returns: {ok}.
        Args: report_id  UUID.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        rep_scheduler.unschedule_report(report_id)
        deleted = template_store.delete_template(report_id)
        if not deleted:
            return _ok({"error": f"Template {report_id} nicht gefunden."})
        return _ok({"deleted": True, "report_id": report_id})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_run_report_now(report_id: str = "", report_name: str = "") -> str:
    """
        Template einmalig ausfuehren und sofort zustellen (Test-Run oder Ad-hoc).

        Wann nutzen: "Schick Template X jetzt einmalig an y@firma.de" • "Run report ad-hoc"
        Wann NICHT — stattdessen: regelmaessig einplanen → printix_schedule_report ;
            nur PDF-Vorschau → printix_preview_report
        Returns: run_id, delivery_status.
        Args: report_id, recipients optional (Override aus Template).

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        _t = current_tenant.get() or {}
        owner_id = _t.get("user_id", "") or ""

        # Name-basierter Lookup wenn keine ID angegeben
        if not report_id and report_name:
            all_t = template_store.list_templates()
            own_t = [t for t in all_t if not owner_id or t.get("owner_user_id","") == owner_id]
            needle = report_name.lower()
            matches = [t for t in own_t if needle in t.get("name","").lower()]
            if not matches:
                names = [t.get("name","?") for t in own_t]
                return _ok({"error": f"Kein Template mit Name '{report_name}' gefunden.",
                            "available_templates": names})
            if len(matches) > 1:
                return _ok({"error": f"Mehrere Templates passen zu '{report_name}' — bitte genauer angeben.",
                            "matches": [{"report_id": t["report_id"], "name": t["name"]} for t in matches]})
            report_id = matches[0]["report_id"]

        if not report_id:
            # Kein ID und kein Name — Templates auflisten damit der Nutzer wählen kann
            all_t = template_store.list_templates()
            own_t = [t for t in all_t if not owner_id or t.get("owner_user_id","") == owner_id]
            return _ok({"error": "Bitte report_id oder report_name angeben.",
                        "available_templates": [{"report_id": t["report_id"], "name": t["name"]} for t in own_t]})

        result = rep_scheduler.run_report_now(
            report_id,
            mail_api_key=_t.get("mail_api_key", "") or "",
            mail_from=_t.get("mail_from", "") or "",
            mail_from_name=_t.get("mail_from_name", "") or "",
        )
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Reporting: Schedule-Management ───────────────────────────────────────────


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_send_test_email(recipient: str) -> str:
    """
        Schickt eine Test-Mail an eine Adresse — prueft SMTP/Resend-Konfig.

        Wann nutzen: "Test-Mail" • "SMTP check" • "Verify mail setup"
        Wann NICHT — stattdessen: Reports zustellen → printix_run_report_now
        Returns: {ok, message_id}.
        Args: to_email  Empfaenger.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    from reporting.mail_client import send_alert
    _t = current_tenant.get() or {}
    api_key        = _t.get("mail_api_key", "") or ""
    mail_from      = _t.get("mail_from", "") or ""
    mail_from_name = _t.get("mail_from_name", "") or ""
    if not api_key:
        return _ok({"error": "Kein mail_api_key konfiguriert."})
    try:
        send_alert(recipients=[recipient], subject="✅ Printix MCP Test-E-Mail",
                   text_body="Test OK — Resend-Konfiguration funktioniert.",
                   api_key=api_key, mail_from=mail_from, mail_from_name=mail_from_name)
        return _ok({"sent": True, "recipient": recipient})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_schedule_report(
    report_id: str,
    frequency: str,
    day: int = 1,
    time: str = "08:00",
) -> str:
    """
        Template als Cron-Job einplanen — wiederkehrender Versand.

        Wann nutzen: "Schick X jeden Montag" • "Schedule report monthly"
        Wann NICHT — stattdessen: einmalig ausfuehren → printix_run_report_now ;
            Template selbst editieren → printix_save_report_template (neu speichern)
        Returns: schedule_id, next_run_at.
        Args: report_id, cron z.B. "0 8 1 * *", recipients Liste, timezone optional.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    if frequency not in ("monthly", "weekly", "daily"):
        return _ok({"error": "frequency muss monthly, weekly oder daily sein."})
    try:
        template = template_store.get_template(report_id)
        if not template:
            return _ok({"error": f"Template {report_id} nicht gefunden."})

        schedule = {"frequency": frequency, "day": day, "time": time}

        # Im Template speichern
        template_store.save_template(
            report_id=report_id,
            name=template["name"],
            query_type=template["query_type"],
            query_params=template["query_params"],
            recipients=template.get("recipients", []),
            mail_subject=template.get("mail_subject", ""),
            output_formats=template.get("output_formats", ["html"]),
            layout=template.get("layout"),
            schedule=schedule,
            created_prompt=template.get("created_prompt", ""),
        )

        # Im Scheduler registrieren
        rep_scheduler.schedule_report(report_id, schedule)

        return _ok({
            "scheduled":  True,
            "report_id":  report_id,
            "frequency":  frequency,
            "day":        day,
            "time_utc":   time,
            "next_run":   _next_run_info(report_id),
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_schedules() -> str:
    """
        Alle aktiven Schedules.

        Wann nutzen: "Was ist eingeplant?" • "List active schedules"
        Wann NICHT — stattdessen: Templates → printix_list_report_templates ;
            Schedule editieren/loeschen → printix_update_schedule / _delete_schedule
        Returns: schedules Liste mit cron, next_run_at, recipients.
        Args: keine.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        jobs = rep_scheduler.list_scheduled_jobs()
        return _ok({"schedules": jobs, "count": len(jobs)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_delete_schedule(report_id: str) -> str:
    """
        Schedule entfernen — Template bleibt.

        Wann nutzen: "Stopp den geplanten Versand"
        Wann NICHT — stattdessen: Template auch weg → printix_delete_report_template
        Returns: {ok}.
        Args: schedule_id  UUID.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        removed = rep_scheduler.unschedule_report(report_id)

        # Schedule im Template auf None setzen
        template = template_store.get_template(report_id)
        if template:
            template_store.save_template(
                report_id=report_id,
                name=template["name"],
                query_type=template["query_type"],
                query_params=template["query_params"],
                recipients=template.get("recipients", []),
                mail_subject=template.get("mail_subject", ""),
                output_formats=template.get("output_formats", ["html"]),
                layout=template.get("layout"),
                schedule=None,
            )

        return _ok({"removed": removed, "report_id": report_id})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_update_schedule(
    report_id: str,
    frequency: str = "",
    day: int = 0,
    time: str = "",
    recipients: str = "",
) -> str:
    """
        Schedule-Konfig aendern (Cron, Empfaenger, etc.).

        Wann nutzen: "Aender den Schedule X" • "Modify schedule"
        Wann NICHT — stattdessen: komplett neu → printix_schedule_report ;
            loeschen → printix_delete_schedule
        Returns: updated schedule.
        Args: schedule_id  UUID. + aenderbare Felder optional.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        template = template_store.get_template(report_id)
        if not template:
            return _ok({"error": f"Template {report_id} nicht gefunden."})

        current_schedule = template.get("schedule") or {}
        new_schedule = {
            "frequency": frequency or current_schedule.get("frequency", "monthly"),
            "day":       day       or current_schedule.get("day", 1),
            "time":      time      or current_schedule.get("time", "08:00"),
        }
        new_recipients = (
            [r.strip() for r in recipients.split(",") if r.strip()]
            if recipients else template.get("recipients", [])
        )

        template_store.save_template(
            report_id=report_id,
            name=template["name"],
            query_type=template["query_type"],
            query_params=template["query_params"],
            recipients=new_recipients,
            mail_subject=template.get("mail_subject", ""),
            output_formats=template.get("output_formats", ["html"]),
            layout=template.get("layout"),
            schedule=new_schedule,
        )
        rep_scheduler.schedule_report(report_id, new_schedule)

        return _ok({
            "updated":   True,
            "report_id": report_id,
            "schedule":  new_schedule,
            "recipients": new_recipients,
            "next_run":  _next_run_info(report_id),
        })
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Reporting: Design & Preview ─────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_design_options() -> str:
    """
        Verfuegbare Farbschemata, Logos, Layout-Varianten fuer Reports.

        Wann nutzen: "Welche Designs habe ich?" • "List report styles"
        Wann NICHT — stattdessen: Vorschau-Render mit gewaehltem Design → printix_preview_report
        Returns: color_schemes, logos, layouts.
        Args: keine.

    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})

    from reporting.design_presets import (
        THEMES, FONTS, HEADER_VARIANTS, CHART_STYLES, CURRENCIES, DEFAULT_LAYOUT,
    )

    themes = {}
    for key, t in THEMES.items():
        themes[key] = {
            "name": t["name"],
            "primary_color": t["primary_color"],
            "accent_color": t["accent_color"],
            "background_color": t["background_color"],
        }

    fonts = [{"key": f["key"], "name": f.get("name", f["key"])} for f in FONTS]

    return _ok({
        "themes": themes,
        "chart_types": [
            {"key": "auto",     "description": "Automatisch — Engine wählt basierend auf Daten"},
            {"key": "bar",      "description": "Horizontale Balkendiagramme"},
            {"key": "line",     "description": "Liniendiagramm mit Fläche (ideal für Zeitreihen)"},
            {"key": "donut",    "description": "Kreisdiagramm (ideal für Anteile/Prozent)"},
            {"key": "heatmap",  "description": "Heatmap (ideal für Stunde×Wochentag-Daten)"},
            {"key": "sparkline","description": "Mini-Trend-Linien in KPI-Karten"},
        ],
        "chart_styles": [cs["key"] for cs in CHART_STYLES],
        "fonts": fonts,
        "header_variants": [hv["key"] for hv in HEADER_VARIANTS],
        "densities": ["compact", "normal", "airy"],
        "currencies": [{"key": c["key"], "symbol": c["symbol"]} for c in CURRENCIES],
        "logo_positions": ["left", "right", "center"],
        "default_layout": DEFAULT_LAYOUT,
        "available_query_types": [
            "print_stats", "cost_report", "top_users", "top_printers",
            "anomalies", "trend", "hour_dow_heatmap",
            "printer_history", "device_readings", "job_history", "queue_stats",
            "user_detail", "user_copy_detail", "user_scan_detail",
            "workstation_overview", "workstation_detail",
            "tree_meter", "service_desk", "sensitive_documents",
            "off_hours_print", "audit_log",
        ],
    })


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_preview_report(
    query_type: str,
    start_date: str = "last_month_start",
    end_date: str = "last_month_end",
    query_params_json: str = "",
    theme_id: str = "",
    primary_color: str = "",
    chart_type: str = "auto",
    company_name: str = "",
    header_variant: str = "",
    density: str = "",
    font_family: str = "",
    logo_base64: str = "",
    logo_mime: str = "image/png",
    footer_text: str = "",
    currency: str = "",
    show_env_impact: bool = False,
    output_format: str = "html",
    report_id: str = "",
) -> str:
    """
        Rendert PDF-Vorschau eines Reports OHNE zu versenden.

        Wann nutzen: "Zeig mir wie X aussieht" • "Render preview"
        Wann NICHT — stattdessen: tatsaechlich versenden → printix_run_report_now
        Returns: {pdf_b64, page_count}.
        Args: report_id  UUID.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})

    import json as _json
    from reporting.design_presets import apply_theme, normalize_layout

    # ── Layout bauen ──────────────────────────────────────────────────────────
    layout = {}

    # Template als Basis laden?
    template = None
    if report_id:
        template = template_store.get_template(report_id)
        if template:
            layout = dict(template.get("layout", {}))
            if not query_type:
                query_type = template.get("query_type", "print_stats")

    # Explizite Werte überschreiben Template-Werte
    if theme_id:
        layout = apply_theme(layout, theme_id)
    if primary_color:
        layout["primary_color"] = primary_color
    if company_name:
        layout["company_name"] = company_name
    if header_variant:
        layout["header_variant"] = header_variant
    if density:
        layout["density"] = density
    if font_family:
        layout["font_family"] = font_family
    if footer_text:
        layout["footer_text"] = footer_text
    if currency:
        layout["currency"] = currency
    if logo_base64:
        layout["logo_base64"] = logo_base64
        layout["logo_mime"] = logo_mime
    if show_env_impact:
        layout["show_env_impact"] = True
    if chart_type and chart_type != "auto":
        layout["preferred_chart_type"] = chart_type
    layout["charts_enabled"] = True

    layout = normalize_layout(layout)

    # ── Query-Parameter ───────────────────────────────────────────────────────
    params = {"start_date": start_date, "end_date": end_date}
    if query_params_json:
        try:
            extra = _json.loads(query_params_json)
            params.update(extra)
        except Exception:
            return _ok({"error": f"query_params_json ist kein gültiges JSON: {query_params_json}"})

    # Template-Parameter als Fallback
    if template and not query_params_json:
        tp = template.get("query_params", {})
        for k, v in tp.items():
            if k not in params:
                params[k] = v

    # Dynamische Datums-Presets auflösen
    from reporting.scheduler import _resolve_dynamic_dates
    params = _resolve_dynamic_dates(params)

    # ── Daten abfragen ────────────────────────────────────────────────────────
    try:
        data = query_tools.run_query(query_type=query_type, **params)
    except Exception as e:
        return _ok({"error": f"Query fehlgeschlagen: {e}", "query_type": query_type})

    period = f"{params.get('start_date', '?')} — {params.get('end_date', '?')}"

    if output_format == "json":
        return _ok({
            "query_type": query_type,
            "period": period,
            "row_count": len(data) if isinstance(data, list) else "n/a",
            "data": data,
        })

    # ── Report rendern ────────────────────────────────────────────────────────
    try:
        outputs = report_engine.generate_report(
            query_type=query_type,
            data=data,
            period=period,
            layout=layout,
            output_formats=[output_format],
            currency=layout.get("currency", "EUR"),
            query_params=params,
        )
    except Exception as e:
        return _ok({"error": f"Report-Rendering fehlgeschlagen: {e}"})

    html = outputs.get("html", outputs.get(output_format, ""))

    # Für MCP-Antwort: HTML-Größe begrenzen (sehr große Reports > 100KB)
    if len(html) > 120_000:
        html = html[:120_000] + "\n<!-- ... (gekürzt, Report zu groß für Chat) -->"

    return _ok({
        "query_type": query_type,
        "period": period,
        "row_count": len(data) if isinstance(data, list) else "n/a",
        "format": output_format,
        "html": html,
        "layout_used": {
            "theme_id": layout.get("theme_id", ""),
            "primary_color": layout.get("primary_color", ""),
            "header_variant": layout.get("header_variant", ""),
            "density": layout.get("density", ""),
            "font_family": layout.get("font_family", ""),
            "chart_type": chart_type,
            "currency": layout.get("currency", ""),
        },
        "hint": "Zufrieden? → printix_save_report_template() zum Speichern als Template.",
    })


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_any(
    query_type: str,
    start_date: str = "last_month_start",
    end_date: str = "last_month_end",
    query_params_json: str = "",
) -> str:
    """
        Universal-Reports-Endpunkt: Preset + Filter -> Tabelle.

        Wann nutzen: "Frag X aus dem Reports-Warehouse" • "Run query Y with filters"
        Wann NICHT — stattdessen: spezialisierte Tools sind kuerzer → printix_query_print_stats /
            printix_top_users / printix_query_anomalies / printix_print_trends ; etc.
        Returns: rows Liste, columns, total.
        Args: preset  Preset-Name. filters dict mit User/Site/Time-Window.

    """
    err = _reporting_check()
    if err:
        return _ok({"error": err})

    import json as _json
    from reporting.scheduler import _resolve_dynamic_dates

    params = {"start_date": start_date, "end_date": end_date}
    if query_params_json:
        try:
            extra = _json.loads(query_params_json)
            params.update(extra)
        except Exception:
            return _ok({"error": f"query_params_json ist kein gültiges JSON: {query_params_json}"})

    params = _resolve_dynamic_dates(params)

    try:
        data = query_tools.run_query(query_type=query_type, **params)
    except ValueError as e:
        return _ok({"error": str(e), "hint": "printix_list_design_options() zeigt alle query_types."})
    except Exception as e:
        return _ok({"error": f"Query fehlgeschlagen: {e}"})

    return _ok({
        "query_type": query_type,
        "period": f"{params.get('start_date','?')} — {params.get('end_date','?')}",
        "row_count": len(data) if isinstance(data, list) else "n/a",
        "data": data,
    })


# ─── Demo Data Generator ─────────────────────────────────────────────────────

try:
    from reporting import demo_generator as _demo_gen
    _DEMO_AVAILABLE = True
except Exception as _de:
    _demo_gen = None  # type: ignore
    _DEMO_AVAILABLE = False
    logger.warning("Demo-Generator nicht verfügbar: %s", _de)


def _demo_check() -> str | None:
    if not _DEMO_AVAILABLE:
        return "Demo-Generator nicht verfügbar — bitte Container neu bauen."
    # v4.4.7: Demo läuft auf lokaler SQLite — kein Azure SQL nötig.
    # Nur prüfen ob tenant_id verfügbar ist.
    tid = _get_sql_tenant_id() if _REPORTING_AVAILABLE else ""
    if not tid:
        return "Kein Tenant-Kontext — bitte mit gültigem Bearer Token authentifizieren."
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_demo_setup_schema() -> str:
    """
        Erzeugt Demo-Schema in der Reports-DB (Sandbox-Tabellen) — einmaliger Setup.

        Wann nutzen: "Setup Demo-Datenbank" • "Init demo schema"
        Wann NICHT — stattdessen: Daten erzeugen (nach Setup) → printix_demo_generate
        Returns: {ok, tables_created}.
        Args: keine.

    """
    err = _demo_check()
    if err:
        return _ok({"error": err})
    try:
        result = _demo_gen.setup_schema()
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_demo_generate(
    user_count: int = 15,
    printer_count: int = 6,
    months: int = 12,
    languages: str = "de,en,fr",
    sites: str = "Hauptsitz,Niederlassung",
    demo_tag: str = "",
    jobs_per_user_day: float = 3.0,
) -> str:
    """
        Erzeugt synthetische Demo-Daten (User, Drucker, Druckjobs, Karten).

        Wann nutzen: "Setze Demo-Umgebung mit 50 Usern und 500 Jobs auf" • "Generate demo data"
        Wann NICHT — stattdessen: nur Schema ohne Daten → printix_demo_setup_schema ;
            Daten wieder weg → printix_demo_rollback
        Returns: {users_created, printers_created, jobs_created, demo_tag}.
        Args: users 50, printers 10, jobs 500, days_of_history 30, demo_tag (Auto).

    """
    err = _demo_check()
    if err:
        return _ok({"error": err})
    try:
        tenant_id = _get_sql_tenant_id()
        lang_list = [l.strip() for l in languages.split(",") if l.strip()]
        site_list = [s.strip() for s in sites.split(",") if s.strip()]
        result = _demo_gen.generate_demo_dataset(
            tenant_id        = tenant_id,
            user_count       = user_count,
            printer_count    = printer_count,
            months           = months,
            languages        = lang_list,
            sites            = site_list,
            demo_tag         = demo_tag,
            jobs_per_user_day= jobs_per_user_day,
        )
        return _ok(result)
    except Exception as e:
        logger.error("Demo-Generator Fehler: %s", e, exc_info=True)
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_demo_rollback(demo_tag: str) -> str:
    """
        Entfernt Demo-Daten anhand des demo_tag — wieder cleane Datenbank.

        Wann nutzen: "Demo wieder weg" • "Rollback demo"
        Wann NICHT — stattdessen: erst pruefen welche Tags aktiv sind → printix_demo_status
        Returns: {removed_count, tables_cleaned}.
        Args: demo_tag  aus printix_demo_status.

    """
    err = _demo_check()
    if err:
        return _ok({"error": err})
    if not demo_tag.strip():
        return _ok({"error": "demo_tag darf nicht leer sein. Verfügbare Tags via printix_demo_status."})
    try:
        tenant_id = _get_sql_tenant_id()
        result = _demo_gen.rollback_demo(tenant_id, demo_tag.strip())
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_demo_status() -> str:
    """
        Welche Demo-Sets sind aktuell aktiv?

        Wann nutzen: "Welche Demo-Daten haben wir?" • "Demo state"
        Wann NICHT — stattdessen: Daten neu erzeugen → printix_demo_generate
        Returns: active_tags Liste mit counts.
        Args: keine.

    """
    err = _demo_check()
    if err:
        return _ok({"error": err})
    try:
        tenant_id = _get_sql_tenant_id()
        result = _demo_gen.get_demo_status(tenant_id)
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Card Management: Profiles & Mappings (Local DB) ────────────────────────

def _get_card_tenant_id() -> str:
    """Tenant-ID aus current_tenant für Card-DB-Operationen."""
    t = current_tenant.get()
    return t.get("id", "") if t else ""


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_card_profiles() -> str:
    """
        Alle Card-Profile (Transform-Regeln) des Tenants.

        Wann nutzen: "Welche Profile haben wir?" • "List card profiles"
        Wann NICHT — stattdessen: einzelnes Detail → printix_get_card_profile ;
            Profil zu einer UID vorschlagen → printix_suggest_profile
        Returns: profiles Liste.
        Args: keine.

    """
    try:
        from cards.store import list_profiles
        tid = _get_card_tenant_id()
        profiles = list_profiles(tid)
        return _ok({"profiles": profiles, "count": len(profiles)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_card_profile(profile_id: str) -> str:
    """
        Details eines Card-Profils inkl. Transform-Regeln.

        Wann nutzen: "Detail Profil X"
        Wann NICHT — stattdessen: alle Profile → printix_list_card_profiles ;
            passendes Profil zu UID finden → printix_suggest_profile
        Returns: profile mit rules_json.
        Args: profile_id  UUID.

    """
    try:
        from cards.store import get_profile
        tid = _get_card_tenant_id()
        p = get_profile(profile_id, tid)
        if not p:
            return _ok({"error": f"Profil '{profile_id}' nicht gefunden."})
        return _ok(p)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_search_card_mappings(search: str = "", printix_user_id: str = "") -> str:
    """
        Lokale Card-Mapping-DB durchsuchen.

        Wann nutzen: "Hat User X eine Mapping mit Wert Y?" • "Find local mapping"
        Wann NICHT — stattdessen: orphaned-Cleanup → printix_find_orphaned_mappings ;
            Karten gegen Printix → printix_search_card
        Returns: matches Liste mit user, card_value, profile.
        Args: query  Substring oder Wert.

    """
    try:
        from cards.store import search_mappings
        tid = _get_card_tenant_id()
        q = search or printix_user_id or ""
        results = search_mappings(tid, q)
        return _ok({"mappings": results, "count": len(results)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_card_details(card_id: str = "", card_number: str = "") -> str:
    """
        Karte + lokales Mapping + Owner-Details in einem Block.

        Wann nutzen: "Detail Karte X" • "Show card abc"
        Wann NICHT — stattdessen: User + alle Karten → printix_get_user_card_context ;
            Audit-Trail → printix_card_audit
        Returns: card, mapping, owner.
        Args: card_id  UUID. user_id optional.

    """
    try:
        c = client()
        api_data = c.search_card(card_id=card_id or None,
                                  card_number=card_number or None)
        enriched = _enrich_card_with_local_data(api_data, _get_card_tenant_id(), requested_card_number=card_number)
        owner_id = enriched.get("owner_id", "")
        if owner_id:
            try:
                enriched["owner"] = c.get_user(owner_id)
            except Exception as owner_err:
                enriched["owner_lookup_error"] = str(owner_err)
        return _ok(enriched)
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_decode_card_value(card_value: str) -> str:
    """
        Raw-Kartenwert dekodieren (Base64, Hex, YSoft/Konica-Varianten).

        Wann nutzen: "Was ist die Karte mit UID 04:5F:F0:…?" • "Decode card value"
        Wann NICHT — stattdessen: durch Profil schicken → printix_transform_card_value ;
            Profil suchen → printix_suggest_profile
        Returns: decoded_bytes_hex, profile_hint, parsed_variants.
        Args: card_value  raw String mit oder ohne Trennzeichen.

    """
    try:
        from cards.transform import decode_printix_secret_value
        result = decode_printix_secret_value(card_value)
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_transform_card_value(
    card_value: str,
    profile_id: str = "",
    strip_separators: bool = False,
    submit_mode: str = "",
) -> str:
    """
        Wert durch Transformations-Pipeline schicken (Hex<->Dec, Reverse, Prefix/Suffix …).

        Wann nutzen: "Konvertier UID X zu Y-Format" • "Transform card value"
        Wann NICHT — stattdessen: nur Erkennung ohne Transform → printix_decode_card_value ;
            komplette Enrolment-Kette → printix_card_enrol_assist
        Returns: final, hex, decimal, plus alle Zwischenstufen.
        Args: raw_value  Eingabe. + viele Profile-Parameter (siehe cards.transform).

    """
    try:
        from cards.transform import transform_card_value
        from cards.store import get_profile
        import json as _json

        rules = {}
        if profile_id:
            tid = _get_card_tenant_id()
            p = get_profile(profile_id, tid)
            if p:
                rules = _json.loads(p.get("rules_json", "{}")) if isinstance(p.get("rules_json"), str) else p.get("rules_json", {})
            else:
                return _ok({"error": f"Profil '{profile_id}' nicht gefunden."})
        else:
            if strip_separators:
                rules["strip_separators"] = True
            if submit_mode:
                rules["submit_mode"] = submit_mode

        result = transform_card_value(card_value, rules)
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_user_card_context(user_id: str) -> str:
    """
        User + alle seine Karten + verwendete Profile in einem Block.

        Wann nutzen: "Karten + Profile von Marcus" • "User card context"
        Wann NICHT — stattdessen: nur User-Stamm → printix_user_360 ;
            nur Karten-Liste → printix_list_cards
        Returns: user, cards, profiles.
        Args: email ODER user_id.

    """
    try:
        c = client()
        user = c.get_user(user_id)
        cards_data = c.list_user_cards(user_id=user_id)
        tenant_id = _get_card_tenant_id()
        cards = [_enrich_card_with_local_data(card, tenant_id, printix_user_id=user_id) for card in _card_items(cards_data)]
        local_mappings = []
        try:
            from cards.store import search_mappings
            local_mappings = [m for m in search_mappings(tenant_id, user_id) if m.get("printix_user_id") == user_id]
        except Exception as mapping_err:
            return _ok({
                "user": user,
                "cards": cards,
                "card_count": len(cards),
                "local_mapping_error": str(mapping_err),
            })
        return _ok({
            "user": user,
            "cards": cards,
            "card_count": len(cards),
            "local_mappings": local_mappings,
            "local_mappings_count": len(local_mappings),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Audit Log & Feature Requests (Local SQLite) ────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_query_audit_log(
    start_date: str = "",
    end_date: str = "",
    action_prefix: str = "",
    object_type: str = "",
    limit: int = 200,
) -> str:
    """
        Strukturierter Audit-Trail des MCP-Servers (Aktionen, Objekte, Actor).

        Wann nutzen: "Was hat User X im MCP gemacht?" • "Audit trail server-side"
        Wann NICHT — stattdessen: Karten-Audit → printix_card_audit ;
            Druckhistorie → printix_print_history_natural
        Returns: events Liste mit timestamp, actor, action, target.
        Args: start_date, end_date, actor_email, action.

    """
    try:
        from datetime import datetime, timedelta
        import db

        if not start_date:
            start_date = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        if not end_date:
            end_date = datetime.utcnow().strftime("%Y-%m-%d")

        tid = _get_card_tenant_id()
        rows = db.query_audit_log_range(
            start_date=start_date,
            end_date=end_date,
            tenant_id=tid if tid else "",
            action_prefix=action_prefix,
            limit=limit,
        )
        # Optionaler Filter auf object_type (db-Funktion hat den Parameter evtl. nicht)
        if object_type and rows:
            rows = [r for r in rows if r.get("object_type", "") == object_type]

        return _ok({"audit_entries": rows, "count": len(rows),
                     "filter": {"start_date": start_date, "end_date": end_date,
                                "action_prefix": action_prefix, "object_type": object_type}})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_feature_requests(status: str = "", limit: int = 100) -> str:
    """
        Ticketsystem fuer Feature-Wuensche (interner Tracker).

        Wann nutzen: "Welche Wuensche stehen offen?" • "List feature requests"
        Wann NICHT — stattdessen: Detail eines Tickets → printix_get_feature_request
        Returns: requests Liste mit id, title, status, votes.
        Args: status "open"/"closed"/"all" optional.

    """
    try:
        import db
        rows = db.list_feature_requests(status=status, limit=limit)
        counts = db.count_feature_requests_by_status()
        return _ok({"feature_requests": rows, "count": len(rows),
                     "status_counts": counts})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_feature_request(ticket_id: int) -> str:
    """
        Details eines Feature-Request-Tickets.

        Wann nutzen: "Detail Feature X"
        Wann NICHT — stattdessen: gesamte Liste → printix_list_feature_requests
        Returns: request mit body, comments.
        Args: request_id  numerisch oder UUID.

    """
    try:
        import db
        row = db.get_feature_request(ticket_id)
        if not row:
            return _ok({"error": f"Ticket {ticket_id} nicht gefunden."})
        return _ok(row)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Backup Management ──────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_backups() -> str:
    """
        Alle vorhandenen Backups (DB + Konfig + Metadaten).

        Wann nutzen: "Welche Backups gibt's?" • "List backups"
        Wann NICHT — stattdessen: neues anlegen → printix_create_backup
        Returns: backups Liste mit timestamp, size, contents-Summary.
        Args: keine.

    """
    try:
        from backup_manager import list_backups
        backups = list_backups()
        return _ok({"backups": backups, "count": len(backups)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_create_backup() -> str:
    """
        Erzeugt ein Backup-Zip mit DB + Verschluesselungs-Key + Konfiguration.

        Wann nutzen: "Backup vor Aenderung" • "Create backup before X"
        Wann NICHT — stattdessen: ueber HA-UI auch moeglich (alternativer Pfad)
        Returns: {filename, size, timestamp}.
        Args: keine.

    """
    try:
        from backup_manager import create_backup
        result = create_backup()
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Capture Profile Management ─────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_capture_profiles() -> str:
    """
        Alle Capture-Profile (Scan-Weiterleitungs-Regeln) des Tenants.

        Wann nutzen: "Welche Capture-Profile habe ich?" • "List capture configs"
        Wann NICHT — stattdessen: Plugin-Schema eines Profils → printix_describe_capture_profile ;
            Datei direkt einspeisen → printix_send_to_capture ;
            Capture-Server-Status → printix_capture_status
        Returns: capture_profiles Liste mit id, name, plugin_type, webhook_url.
        Args: keine.

    """
    try:
        import db
        tid = _get_card_tenant_id()
        profiles = db.get_capture_profiles_by_tenant(tid)
        # Webhook-Base-URL ergänzen (v7.0.0: DB > Env)
        try:
            base_url = (db.get_setting("capture_public_url", "") or "").strip().rstrip("/")
        except Exception:
            base_url = ""
        if not base_url:
            try:
                base_url = (db.get_setting("public_url", "") or "").strip().rstrip("/")
            except Exception:
                base_url = ""
        if not base_url:
            base_url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/") or "http://localhost:8765"
        result = []
        for p in profiles:
            pd = dict(p) if not isinstance(p, dict) else p
            pd["webhook_url_full"] = f"{base_url}/capture/webhook/{pd.get('id', '')}"
            result.append(pd)
        return _ok({"capture_profiles": result, "count": len(result)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_capture_status() -> str:
    """
        Server-Status der Capture-Pipeline: Port, Webhook-URL, verfuegbare Plugins.

        Wann nutzen: "Laeuft Capture?" • "Capture status" • "Welche Plugins sind installiert?"
        Wann NICHT — stattdessen: konkrete Profile → printix_list_capture_profiles ;
            Plugin-Schema → printix_describe_capture_profile
        Returns: server_port, webhook_base_url, plugins Liste, profiles_count.
        Args: keine.

    """
    try:
        capture_enabled = os.environ.get("CAPTURE_ENABLED", "false").strip().lower() == "true"
        try:
            import db as _db
            cap_url = (_db.get_setting("capture_public_url", "") or "").strip().rstrip("/")
            mcp_url = (_db.get_setting("public_url", "") or "").strip().rstrip("/")
        except Exception:
            cap_url = ""
            mcp_url = ""
        if not mcp_url:
            mcp_url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")

        # Plugins ermitteln — v6.7.113: zwei Bugs hier:
        # 1) Import von `capture.base_plugin` alleine triggert die
        #    Plugin-Registrierung nicht; erst das Importieren von
        #    `capture.plugins` fuehrt die @register_plugin-Decorators aus.
        # 2) Das Klassenattribut heisst `plugin_name` (lowercase), nicht
        #    `PLUGIN_NAME` — `getattr(..., "PLUGIN_NAME", pid)` liefert
        #    deshalb immer den Fallback.
        available_plugins = []
        try:
            import capture.plugins  # noqa: F401  (Seiteneffekt: register_plugin)
            from capture.base_plugin import get_all_plugins
            available_plugins = [
                {
                    "id": pid,
                    "name": getattr(pcls, "plugin_name", "") or pid,
                    "icon": getattr(pcls, "plugin_icon", ""),
                    "description": getattr(pcls, "plugin_description", ""),
                }
                for pid, pcls in get_all_plugins().items()
            ]
        except Exception as plugin_err:
            # Nicht mehr still verschlucken — Fehlerursache im Response
            # sichtbar machen, damit der naechste Bug-Report nicht raten muss.
            available_plugins = []
            _plugin_load_error = str(plugin_err)
        else:
            _plugin_load_error = ""

        # Profile zählen
        import db
        tid = _get_card_tenant_id()
        try:
            profiles = db.get_capture_profiles_by_tenant(tid)
            profile_count = len(profiles)
        except Exception:
            profile_count = 0

        response = {
            "capture_separate_server": capture_enabled,
            "capture_port": 8775 if capture_enabled else "shared with MCP",
            "webhook_base_url": cap_url or mcp_url or "not configured",
            "available_plugins": available_plugins,
            "configured_profiles": profile_count,
        }
        if _plugin_load_error:
            response["plugin_load_error"] = _plugin_load_error
        return _ok(response)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Site & Network: Aggregierte Ansichten ───────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_site_summary(site_id: str) -> str:
    """
        Aggregierte Sicht: Site + Networks + Drucker in einem Block.

        Wann nutzen: "Komplettsicht Site DACH" • "Full site overview"
        Wann NICHT — stattdessen: nur Site-Meta → printix_get_site ;
            nur Drucker einer Site → printix_network_printers(network_id=site)
        Returns: site, networks Liste, printers Liste.
        Args: site_id  UUID.

    """
    try:
        c = client()
        site = c.get_site(site_id)
        networks = c.list_networks(site_id=site_id)
        printers = c.list_printers()

        # v6.7.113: Erst auf Liste normalisieren, dann zaehlen. Vorher wurde
        # network_ids nur befuellt wenn die API direkt eine Liste zurueckgab —
        # bei Dict-Shape ({"networks": [...]}) blieb der Counter auf 0.
        def _listify(payload, *keys):
            if isinstance(payload, list):
                return payload
            if isinstance(payload, dict):
                for k in keys:
                    v = payload.get(k)
                    if isinstance(v, list):
                        return v
            return []

        networks_list = _listify(networks, "networks", "content", "items")
        printers_list = _listify(printers, "printers", "content", "items")

        network_ids = set()
        for n in networks_list:
            if not isinstance(n, dict):
                continue
            nid = n.get("networkId") or n.get("id") or ""
            if nid:
                network_ids.add(nid)

        # Fallback: wenn IDs nicht extrahiert werden konnten, nimm die Listenlaenge.
        network_count = len(network_ids) if network_ids else len(networks_list)

        return _ok({
            "site": site,
            "networks": networks_list,
            "network_count": network_count,
            "printers": printers_list,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_network_printers(network_id: str = "", site_id: str = "") -> str:
    """
        Alle Drucker eines Netzwerks oder einer Site (mit Strategy-Fallbacks).

        Wann nutzen: "Drucker im Netzwerk X" • "Printers at site Y"
        Wann NICHT — stattdessen: Tenant-weite Liste → printix_list_printers ;
            Fuzzy-Suche → printix_resolve_printer
        Returns: printers + resolution_strategy (network_id_or_link |
            network_site_match | network_name_match | site_fallback).
        Args: network_id  UUID des Netzwerks (oder Site falls bekannt).

    """
    try:
        c = client()
        all_printers = c.list_printers()
        printers_list = all_printers if isinstance(all_printers, list) else (
            all_printers.get("printers") or all_printers.get("content") or all_printers.get("items") or []
        )

        # v6.7.114: Die Printix-API liefert auf dem Printer-Objekt oft KEIN
        # direktes networkId-Feld. Wir probieren mehrere Strategien und
        # dokumentieren im Response, welche gegriffen hat.
        def _printer_network_refs(p: dict) -> set[str]:
            """Extrahiert alle Network-Referenzen eines Printers aus diversen Feldnamen + _links."""
            refs: set[str] = set()
            for key in ("networkId", "network_id", "networkID"):
                v = p.get(key)
                if v:
                    refs.add(str(v))
            nets = p.get("networks")
            if isinstance(nets, list):
                for n in nets:
                    if isinstance(n, dict):
                        nid = n.get("id") or n.get("networkId")
                        if nid:
                            refs.add(str(nid))
                    elif isinstance(n, str):
                        refs.add(n)
            # HAL-Links: _links.network.href oder _links.networks[].href
            links = p.get("_links") or {}
            net_link = links.get("network") or {}
            if isinstance(net_link, dict):
                href = net_link.get("href", "")
                rid = _extract_resource_id_from_href(href)
                if rid:
                    refs.add(rid)
            net_links = links.get("networks") or []
            if isinstance(net_links, list):
                for nl in net_links:
                    href = (nl or {}).get("href", "") if isinstance(nl, dict) else ""
                    rid = _extract_resource_id_from_href(href)
                    if rid:
                        refs.add(rid)
            return refs

        def _filter_by_networks(target_ids: set[str]) -> tuple[list, str, dict]:
            """Liefert Drucker + Strategie + Diagnose-Dict.

            Strategien (der Reihe nach):
              1. network_id_or_link   — Printer-Feld/HAL-Link matched direkt
              2. network_site_match   — Network hat eine siteId, Printer via siteId matchen
              3. network_name_match   — Printer-Location/siteName/networkName enthaelt den Network-Namen
            """
            diag = {"target_network_ids": sorted(target_ids)}

            # Strategie 1: direkter Feld/Link-Match
            hits = [p for p in printers_list
                    if isinstance(p, dict) and (_printer_network_refs(p) & target_ids)]
            if hits:
                return hits, "network_id_or_link", diag

            # Network-Details fuer die Fallback-Strategien nachladen
            net_details: list[dict] = []
            for nid in target_ids:
                try:
                    nd = c.get_network(nid)
                    if isinstance(nd, dict):
                        net_details.append(nd)
                except Exception as e:
                    diag.setdefault("get_network_errors", []).append(f"{nid}: {e}")

            net_names = {(nd.get("name") or "").strip().lower() for nd in net_details}
            net_names.discard("")
            net_site_ids = set()
            for nd in net_details:
                sid = (
                    nd.get("siteId")
                    or nd.get("site_id")
                    or _extract_resource_id_from_href((((nd.get("_links") or {}).get("site") or {}).get("href", "")))
                    or ""
                )
                if sid:
                    net_site_ids.add(str(sid))
            diag["resolved_network_names"] = sorted(net_names)
            diag["resolved_network_site_ids"] = sorted(net_site_ids)

            # Strategie 2: via siteId
            if net_site_ids:
                hits = []
                for p in printers_list:
                    if not isinstance(p, dict):
                        continue
                    sid = str(
                        p.get("siteId")
                        or p.get("site_id")
                        or _extract_resource_id_from_href((((p.get("_links") or {}).get("site") or {}).get("href", "")))
                        or ""
                    )
                    if sid and sid in net_site_ids:
                        hits.append(p)
                if hits:
                    return hits, "network_site_match", diag

            # Strategie 3: Name-Match gegen Printer-Location / siteName / networkName
            if net_names:
                hits = []
                for p in printers_list:
                    if not isinstance(p, dict):
                        continue
                    hay = " ".join([
                        str(p.get("location", "")),
                        str(p.get("siteName", "")),
                        str(p.get("networkName", "")),
                    ]).lower()
                    if any(nm in hay for nm in net_names):
                        hits.append(p)
                if hits:
                    return hits, "network_name_match", diag

            # Strategie 4: Site-Fallback. v6.7.116 — ground-truth aus dem
            # Delta-Test: die Printix-API liefert auf Printer-Objekten
            # weder networkId noch siteId; strukturelle Referenzen fehlen
            # komplett. Kein client-seitiger Filter kann daher "Printer
            # gehoert zu Network X" exakt ermitteln. Als Naeherung: Network
            # → Site aufloesen und alle Printer des Tenants als
            # site-scoped Ergebnis liefern, mit ehrlichem Disclaimer.
            if net_site_ids:
                diag["strategy4_disclaimer"] = (
                    "Printix-API liefert weder networkId noch siteId auf "
                    "Printer-Objekten. Fallback: alle Printer des Tenants, "
                    "vermutlich scoped auf Site(s) " + ", ".join(sorted(net_site_ids)) + "."
                )
                diag["total_printers_scanned"] = len(printers_list or [])
                return (
                    [p for p in printers_list if isinstance(p, dict)],
                    "site_fallback",
                    diag,
                )

            # Wirklich nichts ging — Diagnose-Sample der Printer-Felder
            # mitgeben, damit der naechste Bug-Report nicht raten muss.
            sample = []
            for p in (printers_list[:3] if printers_list else []):
                if not isinstance(p, dict):
                    continue
                sample.append({
                    "keys": sorted(list(p.keys()))[:25],
                    "networkId": p.get("networkId"),
                    "siteId": p.get("siteId"),
                    "networkName": p.get("networkName"),
                    "location": p.get("location"),
                })
            diag["printer_sample"] = sample
            diag["total_printers_scanned"] = len(printers_list or [])
            return [], "no_strategy_matched", diag

        if network_id:
            filtered, strategy, diag = _filter_by_networks({str(network_id)})
            return _ok({"printers": filtered, "count": len(filtered),
                         "filter": {"network_id": network_id},
                         "resolution_strategy": strategy,
                         "diagnostics": diag})

        if site_id:
            networks = c.list_networks(site_id=site_id)
            net_list = networks if isinstance(networks, list) else (
                (networks or {}).get("networks") or (networks or {}).get("content") or []
            )
            net_ids = {str(n.get("networkId") or n.get("id", "")) for n in net_list if isinstance(n, dict)}
            net_ids.discard("")
            filtered, strategy, diag = _filter_by_networks(net_ids)
            return _ok({"printers": filtered, "count": len(filtered),
                         "filter": {"site_id": site_id, "network_ids": list(net_ids)},
                         "resolution_strategy": strategy,
                         "diagnostics": diag})

        return _ok({"error": "Bitte network_id oder site_id angeben."})
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_queue_context(queue_id: str, printer_id: str = "") -> str:
    """
        Aggregierte Sicht: Queue + Drucker-Objekt + letzte Jobs in einem Aufruf.

        Wann nutzen: "Komplettsicht auf Queue X" • "Was ist mit dieser Queue los?"
        Wann NICHT — stattdessen: nur Drucker-Details → printix_get_printer ;
            nur Jobs einer Queue → printix_list_jobs(queue_id=…)
        Returns: queue, printer, recent_jobs.
        Args: queue_id  UUID.

    """
    try:
        c = client()
        printers_data = c.list_printers(size=200)
        printer_items = printers_data if isinstance(printers_data, list) else printers_data.get("printers", [])
        match = None
        for item in printer_items:
            pid, qid = _extract_printer_queue_ids(item)
            if qid == queue_id and (not printer_id or pid == printer_id):
                match = dict(item)
                match["printer_id"] = pid
                match["queue_id"] = qid
                break
        if not match:
            return _ok({"error": f"Queue '{queue_id}' nicht gefunden."})

        detailed = None
        if match.get("printer_id"):
            try:
                detailed = c.get_printer(match["printer_id"], queue_id)
            except Exception as detail_err:
                detailed = {"detail_lookup_error": str(detail_err)}

        jobs = []
        jobs_error = ""
        try:
            jobs_data = c.list_print_jobs(queue_id=queue_id, size=20)
            jobs = jobs_data if isinstance(jobs_data, list) else jobs_data.get("jobs", jobs_data.get("content", []))
        except Exception as job_err:
            jobs_error = str(job_err)

        return _ok({
            "queue_id": queue_id,
            "printer_id": match.get("printer_id", ""),
            "queue_entry": match,
            "details": detailed,
            "recent_jobs": jobs,
            "recent_job_count": len(jobs),
            "recent_jobs_error": jobs_error,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_network_context(network_id: str) -> str:
    """
        Aggregierte Sicht: Network + Site + Drucker in einem Block.

        Wann nutzen: "Komplettsicht Network X" • "Network details with printers"
        Wann NICHT — stattdessen: nur Detail → printix_get_network ;
            nur Drucker → printix_network_printers
        Returns: network, site, printers.
        Args: network_id  UUID.

    """
    try:
        c = client()
        network = c.get_network(network_id)
        site = None
        site_id = (network.get("siteId", "") if isinstance(network, dict) else "") or ""
        if site_id:
            try:
                site = c.get_site(site_id)
            except Exception as site_err:
                site = {"site_id": site_id, "lookup_error": str(site_err)}

        printers_data = c.list_printers(size=200)
        printers_list = printers_data if isinstance(printers_data, list) else printers_data.get("printers", [])
        printers = []
        for item in printers_list:
            pid, qid = _extract_printer_queue_ids(item)
            item_network_id = str(item.get("networkId", "") or item.get("network_id", ""))
            if item_network_id == str(network_id):
                enriched = dict(item)
                enriched["printer_id"] = pid
                enriched["queue_id"] = qid
                printers.append(enriched)

        snmp_data = c.list_snmp_configs(size=200)
        snmp_list = snmp_data if isinstance(snmp_data, list) else snmp_data.get("snmp", snmp_data.get("snmpConfigurations", []))
        snmp_matches = []
        for config in snmp_list:
            config_network_ids = list(config.get("networkIds", []) or [])
            if not config_network_ids:
                for link in ((config.get("_links") or {}).get("networks") or []):
                    href = link.get("href", "")
                    cid = _extract_resource_id_from_href(href)
                    if cid:
                        config_network_ids.append(cid)
            if str(network_id) in {str(x) for x in config_network_ids}:
                snmp_matches.append(config)

        return _ok({
            "network": network,
            "site": site,
            "printers": printers,
            "printer_count": len(printers),
            "snmp_configs": snmp_matches,
            "snmp_config_count": len(snmp_matches),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_snmp_context(config_id: str) -> str:
    """
        Aggregierte Sicht: SNMP-Config + Drucker die sie nutzen + Network.

        Wann nutzen: "Was nutzt SNMP X?" • "SNMP impact view"
        Wann NICHT — stattdessen: nur Detail → printix_get_snmp_config
        Returns: snmp, printers, network.
        Args: snmp_id  UUID.

    """
    try:
        c = client()
        snmp_config = c.get_snmp_config(config_id)
        network_ids = list(snmp_config.get("networkIds", []) or []) if isinstance(snmp_config, dict) else []
        if not network_ids and isinstance(snmp_config, dict):
            for link in ((snmp_config.get("_links") or {}).get("networks") or []):
                href = link.get("href", "")
                nid = _extract_resource_id_from_href(href)
                if nid:
                    network_ids.append(nid)

        networks = []
        sites_by_id = OrderedDict()
        printers = []
        for network_id in network_ids:
            try:
                network = c.get_network(network_id)
            except Exception as network_err:
                network = {"network_id": network_id, "lookup_error": str(network_err)}
            networks.append(network)
            site_id = network.get("siteId", "") if isinstance(network, dict) else ""
            if site_id and site_id not in sites_by_id:
                try:
                    sites_by_id[site_id] = c.get_site(site_id)
                except Exception as site_err:
                    sites_by_id[site_id] = {"site_id": site_id, "lookup_error": str(site_err)}

        printers_data = c.list_printers(size=200)
        printers_list = printers_data if isinstance(printers_data, list) else printers_data.get("printers", [])
        network_id_set = {str(nid) for nid in network_ids}
        for item in printers_list:
            pid, qid = _extract_printer_queue_ids(item)
            item_network_id = str(item.get("networkId", "") or item.get("network_id", ""))
            if item_network_id in network_id_set:
                enriched = dict(item)
                enriched["printer_id"] = pid
                enriched["queue_id"] = qid
                printers.append(enriched)

        return _ok({
            "snmp_config": snmp_config,
            "networks": networks,
            "network_count": len(networks),
            "sites": list(sites_by_id.values()),
            "site_count": len(sites_by_id),
            "printers": printers,
            "printer_count": len(printers),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Cross-Source Insights, Governance & Agent Workflows (v6.7.107+) ─────────
#
# Diese Tools sind High-Level-Aggregationen ueber die bestehenden
# Printix-Endpunkte + die lokale DB. Sie sparen Agents (claude.ai, ChatGPT)
# Round-Trips und liefern "Frage beantworten"-Antworten statt nur
# "API-Response".

def _extract_list(raw, *keys) -> list[dict]:
    """Gemeinsamer Extractor fuer Printix-List-Responses.

    v6.7.109: API variiert zwischen `{"printers": [...]}`, `{"content": [...]}`
    und `{"_embedded": {"printers": [...]}}`. Der silent-except-Pfad von v6.7.107
    hat Fehler geschluckt und 0-Counts geliefert — jetzt tolerant + loud.
    """
    if isinstance(raw, list):
        return [x for x in raw if isinstance(x, dict)]
    if not isinstance(raw, dict):
        return []
    for k in keys:
        v = raw.get(k)
        if isinstance(v, list):
            return [x for x in v if isinstance(x, dict)]
    emb = raw.get("_embedded") or {}
    if isinstance(emb, dict):
        for k in keys:
            v = emb.get(k)
            if isinstance(v, list):
                return [x for x in v if isinstance(x, dict)]
    return []


def _is_printer_online(p: dict) -> bool:
    """Printix nutzt connectionStatus='CONNECTED' — nicht bool online=true."""
    status = (p.get("connectionStatus") or p.get("status") or "").lower()
    return status in ("connected", "online", "ok", "ready")


def _is_workstation_online(w: dict) -> bool:
    return bool(w.get("active") or w.get("online") or
                 (w.get("status") or "").lower() in ("online", "active", "connected"))


def _fuzzy_match_user(users: list[dict], query: str) -> list[dict]:
    """Case-insensitive substring match ueber E-Mail, displayName, name, id."""
    q = (query or "").strip().lower()
    if not q:
        return users
    out = []
    for u in users:
        if not isinstance(u, dict):
            continue
        haystack = " ".join([
            str(u.get("email", "")),
            str(u.get("displayName", "")),
            str(u.get("name", "")),
            str(u.get("fullName", "")),
            str(u.get("id", "")),
        ]).lower()
        if q in haystack:
            out.append(u)
    return out


def _collect_all_users(c: PrintixClient) -> list[dict]:
    """Liefert USER + GUEST_USER als eine flache Liste."""
    out: list[dict] = []
    seen: set[str] = set()
    for role in ("USER", "GUEST_USER"):
        try:
            data = c.list_users(role=role, page=0, page_size=200)
            users = []
            if isinstance(data, dict):
                users = data.get("users") or data.get("content") or []
            for u in users or []:
                uid = u.get("id", "")
                if uid and uid not in seen:
                    seen.add(uid)
                    out.append(u)
        except Exception:
            continue
    return out


# ─── Cross-Source Insights ────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_find_user(query: str) -> str:
    """
        User-Fuzzy-Suche per Email-Fragment oder Name.

        Wann nutzen: "Such Marcus" • "Find user by email" • "Wer heisst Mueller?"
        Wann NICHT — stattdessen: ID schon bekannt → printix_get_user ;
            komplette Liste → printix_list_users
        Returns: matches Liste.
        Args: query  Substring von Email oder Name.

    """
    try:
        users = _collect_all_users(client())
        matches = _fuzzy_match_user(users, query)
        return _ok({
            "query": query,
            "matches": matches,
            "match_count": len(matches),
            "searched_total": len(users),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_user_360(query: str) -> str:
    """
        360-Grad-Sicht eines Users: Stammdaten + Karten + Gruppen + Workstations + letzte Jobs.

        Wann nutzen: "Alles ueber marcus@firma.de" • "Full view of user X"
        Wann NICHT — stattdessen: gezielte Helpdesk-Diagnose → printix_diagnose_user ;
            nur Karten → printix_get_user_card_context ;
            nur Gruppen → printix_get_user_groups
        Returns: aggregated dict.
        Args: query  Email oder UUID.

    """
    try:
        c = client()
        users = _collect_all_users(c)
        matches = _fuzzy_match_user(users, query)
        if not matches:
            return _ok({"error": "no user found", "query": query})
        if len(matches) > 1:
            return _ok({
                "error": "multiple users match — please refine query or use printix_get_user(user_id)",
                "candidates": matches,
                "candidate_count": len(matches),
            })
        user = matches[0]
        user_id = user.get("id", "")
        tenant_id = _get_card_tenant_id()

        # Karten (enriched)
        cards = []
        try:
            cards_data = c.list_user_cards(user_id=user_id)
            cards = [_enrich_card_with_local_data(card, tenant_id, printix_user_id=user_id)
                     for card in _card_items(cards_data)]
        except Exception as e:
            cards = [{"error": str(e)}]

        # Workstations (best-effort — nicht jeder Tenant hat WS-API)
        workstations = []
        try:
            ws_data = c.list_workstations(page=0, size=200)
            ws_list = ws_data.get("workstations") or ws_data.get("content") or [] if isinstance(ws_data, dict) else []
            email = (user.get("email") or "").lower()
            for ws in ws_list:
                if isinstance(ws, dict):
                    owner = (ws.get("userEmail") or ws.get("user") or "").lower()
                    if email and owner == email:
                        workstations.append(ws)
        except Exception:
            pass

        return _ok({
            "user": user,
            "cards": cards,
            "card_count": len(cards),
            "workstations": workstations,
            "workstation_count": len(workstations),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_printer_health_report() -> str:
    """
        Drucker-Status grupiert: online / offline / Fehlerzustaende.

        Wann nutzen: "Welche Drucker sind offline?" • "Printer health" •
            "Status aller Drucker"
        Wann NICHT — stattdessen: konkrete Liste → printix_list_printers ;
            Detail-Health eines Druckers → printix_get_printer
        Returns: groups: online[], offline[], error[]; counts.
        Args: keine.

    """
    try:
        c = client()
        data = c.list_printers(page=0, size=200)
        printers = _extract_list(data, "printers")

        online = [p for p in printers if _is_printer_online(p)]
        offline = [p for p in printers if not _is_printer_online(p)]
        by_site: dict[str, dict] = {}
        for p in printers:
            site = (p.get("siteName") or p.get("site") or p.get("location") or "unknown") if isinstance(p, dict) else "unknown"
            bucket = by_site.setdefault(site, {"total": 0, "online": 0, "offline": 0})
            bucket["total"] += 1
            if p in online:
                bucket["online"] += 1
            else:
                bucket["offline"] += 1

        return _ok({
            "total": len(printers),
            "online": len(online),
            "offline": len(offline),
            "offline_ratio": round(len(offline) / len(printers), 3) if printers else 0,
            "by_site": by_site,
            "offline_printers": offline[:50],
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_tenant_summary() -> str:
    """
        Kompakter Inventar-Overview: Drucker / User / Sites / Cards / offene Jobs.

        Wann nutzen: "Gib mir einen Ueberblick" • "Tenant overview" •
            "Wieviel was haben wir?"
        Wann NICHT — stattdessen: Drucker-Liste → printix_list_printers ;
            User-Liste → printix_list_users ; Health-Status → printix_printer_health_report
        Returns: counts pro Resource-Typ.
        Args: keine.

    """
    try:
        c = client()
        tenant = current_tenant.get() or {}

        # Users
        users = _collect_all_users(c)
        role_counts: dict[str, int] = {}
        for u in users:
            r = u.get("role") or u.get("roleType") or "USER"
            role_counts[r] = role_counts.get(r, 0) + 1

        # Printers
        printer_count = 0
        online_count = 0
        printer_error = None
        try:
            pdata = c.list_printers(page=0, size=200)
            plist = _extract_list(pdata, "printers")
            printer_count = len(plist)
            online_count = sum(1 for p in plist if _is_printer_online(p))
        except Exception as e:
            printer_error = str(e)[:200]
            logger.warning("tenant_summary: list_printers failed: %s", e)

        # Workstations
        ws_count = 0
        ws_error = None
        try:
            wdata = c.list_workstations(page=0, size=200)
            wlist = _extract_list(wdata, "workstations")
            ws_count = len(wlist)
        except Exception as e:
            ws_error = str(e)[:200]
            logger.warning("tenant_summary: list_workstations failed: %s", e)

        # Groups
        group_count = 0
        group_error = None
        try:
            gdata = c.list_groups(page=0, size=200)
            glist = _extract_list(gdata, "groups")
            group_count = len(glist)
        except Exception as e:
            group_error = str(e)[:200]
            logger.warning("tenant_summary: list_groups failed: %s", e)

        # Lokale Karten
        local_cards_count = 0
        try:
            from cards.store import search_mappings
            local_cards_count = len(search_mappings(_get_card_tenant_id(), ""))
        except Exception:
            pass

        errors = {k: v for k, v in {
            "printers": printer_error,
            "workstations": ws_error,
            "groups": group_error,
        }.items() if v}

        return _ok({
            "tenant": {
                "id": tenant.get("id"),
                "name": tenant.get("name"),
                "printix_tenant_id": tenant.get("printix_tenant_id"),
            },
            "users": {"total": len(users), "by_role": role_counts},
            "printers": {"total": printer_count, "online": online_count},
            "workstations": {"total": ws_count},
            "groups": {"total": group_count},
            "local_cards": {"total": local_cards_count},
            **({"errors": errors} if errors else {}),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_diagnose_user(email: str) -> str:
    """
        Helpdesk-Diagnose: warum funktioniert was bei User X nicht?

        Wann nutzen: "Anna kann nicht drucken" • "Why is user X failing?" •
            "Helpdesk-Diagnose fuer Y"
        Wann NICHT — stattdessen: vollstaendiges Profil → printix_user_360 ;
            Server-Health → printix_status
        Returns: findings Liste mit Befund-Texten + Loesungs-Hinweisen.
        Args: email  User-Email.

    """
    try:
        c = client()
        findings: list[dict] = []
        users = _collect_all_users(c)
        matches = [u for u in users if (u.get("email") or "").lower() == email.lower()]
        if not matches:
            findings.append({
                "severity": "critical",
                "check": "user_exists",
                "result": "User nicht im Tenant gefunden",
                "suggestion": "Account ueber /desktop Entra SSO oder manuell anlegen.",
            })
            return _ok({"email": email, "findings": findings, "status": "failed"})

        user = matches[0]
        user_id = user.get("id", "")
        findings.append({"severity": "ok", "check": "user_exists", "result": f"User gefunden: id={user_id}"})

        role = user.get("role") or user.get("roleType") or ""
        if role in ("SYSTEM_MANAGER", "SITE_MANAGER", "KIOSK_MANAGER"):
            findings.append({
                "severity": "warning",
                "check": "role_printable",
                "result": f"Rolle {role} kann keine Karten registrieren",
                "suggestion": "Rolle auf USER aendern oder mit anderem Account arbeiten.",
            })

        # Karten
        try:
            cards_data = c.list_user_cards(user_id=user_id)
            cards = _card_items(cards_data)
            if not cards:
                findings.append({
                    "severity": "warning",
                    "check": "has_card",
                    "result": "Keine Karte registriert",
                    "suggestion": "Karte ueber iOS-App / Self-Service-Portal registrieren.",
                })
            else:
                findings.append({"severity": "ok", "check": "has_card", "result": f"{len(cards)} Karte(n) registriert"})
        except PrintixAPIError as e:
            findings.append({
                "severity": "warning", "check": "has_card",
                "result": f"Karten-API-Fehler: {e.message}",
                "suggestion": "Card Management OAuth-Credentials im Portal pruefen.",
            })

        # Workstation
        try:
            wdata = c.list_workstations(page=0, size=200)
            wlist = wdata.get("workstations") or wdata.get("content") or [] if isinstance(wdata, dict) else []
            own = [w for w in wlist if (w.get("userEmail") or "").lower() == email.lower()]
            if own:
                findings.append({"severity": "ok", "check": "has_workstation", "result": f"{len(own)} Workstation(s)"})
            else:
                findings.append({
                    "severity": "info", "check": "has_workstation",
                    "result": "Keine Workstation zugeordnet",
                    "suggestion": "Printix-Client installieren falls am Arbeitsplatz gedruckt werden soll.",
                })
        except Exception:
            pass

        severities = {f["severity"] for f in findings}
        if "critical" in severities:
            status = "failed"
        elif "warning" in severities:
            status = "issues"
        else:
            status = "healthy"

        return _ok({"email": email, "user_id": user_id, "findings": findings, "status": status})
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Card Management (tenant-wide) ───────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_cards_by_tenant(status: str = "all") -> str:
    """
        Alle Karten des Tenants — quer ueber alle User.

        Wann nutzen: "Alle Karten" • "Tenant-wide card list" • "Find orphaned cards"
        Wann NICHT — stattdessen: Karten EINES Users → printix_list_cards ;
            nur lokale Mappings ohne Printix-User → printix_find_orphaned_mappings
        Returns: cards Liste, optional gefiltert.
        Args: filter "all" (Default) | "registered" | "orphaned".

    """
    try:
        c = client()
        tenant_id = _get_card_tenant_id()
        users = _collect_all_users(c)
        all_cards: list[dict] = []
        for u in users:
            uid = u.get("id", "")
            if not uid:
                continue
            try:
                data = c.list_user_cards(user_id=uid)
                for card in _card_items(data):
                    enriched = _enrich_card_with_local_data(card, tenant_id, printix_user_id=uid)
                    enriched["user_email"] = u.get("email", "")
                    enriched["user_name"] = u.get("displayName") or u.get("fullName") or ""
                    all_cards.append(enriched)
            except Exception:
                continue

        s = (status or "all").lower()
        if s == "unmapped":
            filtered = [c_ for c_ in all_cards if not c_.get("local_mapping")]
        elif s == "mapped":
            filtered = [c_ for c_ in all_cards if c_.get("local_mapping")]
        else:
            filtered = all_cards

        return _ok({
            "cards": filtered,
            "count": len(filtered),
            "total_tenant_cards": len(all_cards),
            "status_filter": s,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_find_orphaned_mappings() -> str:
    """
        Lokale Card-Mappings ohne zugehoerigen Printix-User — Cleanup-Kandidaten.

        Wann nutzen: "Welche Mappings sind orphan?" • "Find dead card mappings"
        Wann NICHT — stattdessen: tenant-weite Cards → printix_list_cards_by_tenant
        Returns: orphans Liste.
        Args: keine.

    """
    try:
        c = client()
        tenant_id = _get_card_tenant_id()
        from cards.store import search_mappings
        local_mappings = search_mappings(tenant_id, "")

        # Alle Printix-Card-IDs einsammeln
        users = _collect_all_users(c)
        printix_card_ids: set[str] = set()
        for u in users:
            uid = u.get("id", "")
            if not uid:
                continue
            try:
                data = c.list_user_cards(user_id=uid)
                for card in _card_items(data):
                    cid = _extract_card_id_from_api(card)
                    if cid:
                        printix_card_ids.add(cid)
            except Exception:
                continue

        orphans = [m for m in local_mappings if m.get("printix_card_id") and m["printix_card_id"] not in printix_card_ids]
        return _ok({
            "orphans": orphans,
            "orphan_count": len(orphans),
            "local_total": len(local_mappings),
            "printix_total": len(printix_card_ids),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_bulk_import_cards(
    csv_data: str,
    profile_id: str = "",
    dry_run: bool = True,
) -> str:
    """
        CSV-Massenimport mit Profil + Dry-Run-Modus.

        Wann nutzen: "Importier 500 Karten" • "Bulk-import from CSV"
        Wann NICHT — stattdessen: einzelne Karte → printix_card_enrol_assist
        Returns: imported_count, skipped, errors, preview (bei dry_run).
        Args: csv_data  CSV-String mit Header "email,card_uid[,notes]".
            profile_id  Transform-Profil.
            dry_run True (Default) — nur validieren, keine API-Calls.

    """
    import csv as _csv
    import io as _io
    try:
        c = client()
        tenant_id = _get_card_tenant_id()
        reader = _csv.DictReader(_io.StringIO(csv_data))
        results: list[dict] = []
        users = _collect_all_users(c) if not dry_run else []
        email_to_uid = {(u.get("email") or "").lower(): u.get("id", "") for u in users}

        from cards.transform import apply_profile_transform
        from cards.store import get_profile, save_mapping
        profile = get_profile(profile_id, tenant_id) if profile_id else None
        rules = {}
        if profile and profile.get("rules_json"):
            try:
                rules = json.loads(profile["rules_json"])
            except Exception:
                rules = {}

        for row in reader:
            email = (row.get("email") or "").strip()
            uid = (row.get("card_uid") or "").strip()
            notes = (row.get("notes") or "").strip()
            if not email or not uid:
                results.append({"row": row, "status": "skipped", "reason": "missing email/uid"})
                continue
            try:
                transformed = apply_profile_transform(uid, rules) if rules else {"final": uid, "working": uid}
            except Exception as te:
                results.append({"email": email, "uid": uid, "status": "transform_error", "error": str(te)})
                continue
            final_value = transformed.get("final") if isinstance(transformed, dict) else uid
            if dry_run:
                results.append({
                    "email": email, "uid": uid, "final": final_value,
                    "status": "dry_run_ok",
                })
                continue
            user_id = email_to_uid.get(email.lower(), "")
            if not user_id:
                results.append({"email": email, "status": "user_not_found"})
                continue
            try:
                reg = c.register_card(user_id, final_value)
                card_id = _extract_card_id_from_api(reg) if isinstance(reg, dict) else ""
                save_mapping(tenant_id, user_id, card_id, uid, final_value,
                             normalized_value=uid, source="bulk_import", notes=notes,
                             profile_id=profile_id)
                results.append({"email": email, "card_id": card_id, "status": "registered"})
            except Exception as re:
                results.append({"email": email, "status": "register_error", "error": str(re)})

        return _ok({
            "dry_run": dry_run,
            "total_rows": len(results),
            "registered": sum(1 for r in results if r.get("status") == "registered"),
            "errors": sum(1 for r in results if "error" in r.get("status", "")),
            "results": results,
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_suggest_profile(sample_uid: str) -> str:
    """
        Schlaegt anhand einer Beispiel-UID das passende Card-Profil vor (Top-10-Ranking).

        Wann nutzen: "Welches Profil passt zu UID X?" • "Suggest profile from sample"
        Wann NICHT — stattdessen: alle Profile sehen → printix_list_card_profiles ;
            gleich registrieren → printix_card_enrol_assist
        Returns: top-10 mit score, best_match.
        Args: sample_uid  Hex-UID, z.B. "045FF002".

    """
    try:
        from cards.store import list_profiles
        from cards.transform import apply_profile_transform
        tenant_id = _get_card_tenant_id()
        profiles = list_profiles(tenant_id)
        results = []
        for p in profiles:
            rules = {}
            try:
                rules = json.loads(p.get("rules_json", "{}"))
            except Exception:
                pass
            try:
                out = apply_profile_transform(sample_uid, rules) if rules else None
                final_value = out.get("final") if isinstance(out, dict) else ""
                score = 0
                if out:
                    score += 1
                if final_value and final_value != sample_uid:
                    score += 2
                if final_value and 4 <= len(final_value) <= 64:
                    score += 1
                results.append({
                    "profile_id": p.get("id"),
                    "name": p.get("name"),
                    "vendor": p.get("vendor"),
                    "mode": p.get("mode"),
                    "score": score,
                    "final_value": final_value,
                })
            except Exception as te:
                results.append({
                    "profile_id": p.get("id"),
                    "name": p.get("name"),
                    "score": 0,
                    "error": str(te),
                })
        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        return _ok({
            "sample_uid": sample_uid,
            "ranking": results[:10],
            "best": results[0] if results else None,
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_card_audit(user_email: str) -> str:
    """
        Audit-Trail aller Karten-Aenderungen fuer einen User.

        Wann nutzen: "Was ist mit Marcus Karten passiert?" • "Card history for user"
        Wann NICHT — stattdessen: Audit des MCP allgemein → printix_query_audit_log
        Returns: audit Liste mit timestamp, action, before/after.
        Args: user_id  UUID.

    """
    try:
        c = client()
        users = _collect_all_users(c)
        matches = [u for u in users if (u.get("email") or "").lower() == user_email.lower()]
        if not matches:
            return _ok({"error": "user not found", "email": user_email})
        user = matches[0]
        user_id = user.get("id", "")
        tenant_id = _get_card_tenant_id()

        cards = []
        try:
            data = c.list_user_cards(user_id=user_id)
            cards = [_enrich_card_with_local_data(card, tenant_id, printix_user_id=user_id)
                     for card in _card_items(data)]
        except Exception as e:
            return _ok({"error": str(e)})

        audit: list[dict] = []
        for card in cards:
            mapping = card.get("local_mapping") or {}
            audit.append({
                "card_id": card.get("card_id"),
                "printix_registered": True,
                "local_mapping_present": bool(mapping),
                "raw_value": mapping.get("raw_value", ""),
                "profile_id": mapping.get("profile_id", ""),
                "notes": mapping.get("notes", ""),
                "source": mapping.get("source", "unknown"),
                "created_at": mapping.get("created_at", ""),
                "updated_at": mapping.get("updated_at", ""),
            })
        return _ok({
            "user_email": user_email,
            "user_id": user_id,
            "card_count": len(audit),
            "audit": audit,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Print Jobs & Reporting (High-Level Wrapper) ─────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_top_printers(days: int = 7, limit: int = 10, metric: str = "pages") -> str:
    """
        Top-N Drucker — Kurzform-Wrapper.

        Wann nutzen: "Top-Drucker letzte 30 Tage" • "Most used printers"
        Wann NICHT — stattdessen: komplexe Filter → printix_query_top_printers
        Returns: top Liste mit metric.
        Args: days 30, limit 10, metric "pages".

    """
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    start = end - timedelta(days=max(days, 1))
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_top_printers(
            start_date=start.isoformat(), end_date=end.isoformat(),
            top_n=limit, metric=metric,
        )
        return _ok({"rows": rows, "count": len(rows), "metric": metric,
                    "period": {"start": start.isoformat(), "end": end.isoformat()}})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_top_users(days: int = 7, limit: int = 10, metric: str = "pages") -> str:
    """
        Top-N User — Kurzform-Wrapper.

        Wann nutzen: "Wer druckt am meisten?" • "Top users last week"
        Wann NICHT — stattdessen: komplexe Filter → printix_query_top_users
        Returns: top Liste mit metric.
        Args: days, limit, metric.

    """
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    start = end - timedelta(days=max(days, 1))
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_top_users(
            start_date=start.isoformat(), end_date=end.isoformat(),
            top_n=limit, metric=metric,
        )
        return _ok({"rows": rows, "count": len(rows), "metric": metric,
                    "period": {"start": start.isoformat(), "end": end.isoformat()}})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_jobs_stuck(minutes: int = 15) -> str:
    """
        Jobs die laenger als N Minuten haengen — Helpdesk-Diagnose.

        Wann nutzen: "Welche Jobs haengen seit langem?" • "Stuck jobs"
        Wann NICHT — stattdessen: alle Jobs einer Queue → printix_list_jobs
        Returns: stuck Liste mit age + owner.
        Args: minutes  Schwellwert in Minuten (Default 30).

    """
    try:
        from datetime import datetime, timezone, timedelta
        c = client()
        data = c.list_print_jobs(page=0, size=200)
        jobs = []
        if isinstance(data, dict):
            jobs = data.get("jobs") or data.get("content") or []
        threshold = datetime.now(timezone.utc) - timedelta(minutes=max(minutes, 1))
        stuck = []
        for j in jobs or []:
            if not isinstance(j, dict):
                continue
            created = j.get("createdAt") or j.get("created") or j.get("submittedAt") or ""
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00")) if created else None
            except Exception:
                dt = None
            status = (j.get("status") or "").lower()
            if dt and dt < threshold and status not in ("done", "completed", "printed", "cancelled"):
                stuck.append({"job": j, "age_minutes": int((datetime.now(timezone.utc) - dt).total_seconds() // 60)})
        stuck.sort(key=lambda x: x.get("age_minutes", 0), reverse=True)
        return _ok({
            "threshold_minutes": minutes,
            "stuck_jobs": stuck,
            "stuck_count": len(stuck),
            "total_jobs_checked": len(jobs),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_print_trends(group_by: str = "day", days: int = 30) -> str:
    """
        Druck-Trend nach Tag/Woche/Monat — Kurzform.

        Wann nutzen: "Trend der letzten 90 Tage" • "Monthly print trend"
        Wann NICHT — stattdessen: vollstaendige Filter → printix_query_trend ;
            A vs B Vergleich → printix_compare_periods
        Returns: timeseries.
        Args: group_by "day"/"week"/"month", days.

    """
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    start = end - timedelta(days=max(days, 1))
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_trend(
            start_date=start.isoformat(), end_date=end.isoformat(),
            group_by=group_by,
        )
        return _ok({"rows": rows, "count": len(rows), "group_by": group_by,
                    "period": {"start": start.isoformat(), "end": end.isoformat()}})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_cost_by_department(
    department_field: str = "department",
    days: int = 30,
    cost_per_mono: float = 0.02,
    cost_per_color: float = 0.08,
) -> str:
    """
        Druckkosten aggregiert pro Abteilung.

        Wann nutzen: "Welche Abteilung verursacht hohe Kosten?" • "Cost by department"
        Wann NICHT — stattdessen: nach User → printix_query_cost_report ;
            ohne Preis-Werte (nur Volumen) → printix_query_print_stats
        Returns: rows {department, jobs, pages, color/bw, cost}.
        Args: department_field, days, cost_per_mono, cost_per_color.

    """
    from datetime import datetime, timedelta
    end = datetime.utcnow().date()
    start = end - timedelta(days=max(days, 1))
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        rows = query_tools.query_top_users(
            start_date=start.isoformat(), end_date=end.isoformat(),
            top_n=500, metric="cost",
            cost_per_mono=cost_per_mono, cost_per_color=cost_per_color,
        )
        users = _collect_all_users(client())
        email_to_dept: dict[str, str] = {}
        for u in users:
            attrs = u.get("attributes") or u.get("customAttributes") or {}
            dept = ""
            if isinstance(attrs, dict):
                dept = attrs.get(department_field) or ""
            dept = dept or u.get(department_field) or "Unassigned"
            email_to_dept[(u.get("email") or "").lower()] = dept

        by_dept: dict[str, dict] = {}
        for r in rows:
            email = (r.get("user_email") or r.get("email") or "").lower()
            dept = email_to_dept.get(email, "Unassigned")
            bucket = by_dept.setdefault(dept, {"department": dept, "pages": 0, "cost": 0.0, "user_count": 0, "users": set()})
            bucket["pages"] += int(r.get("pages") or 0)
            bucket["cost"] += float(r.get("cost") or 0)
            if email:
                bucket["users"].add(email)
        ranked = []
        for v in by_dept.values():
            v["user_count"] = len(v["users"])
            v["users"] = sorted(v["users"])
            ranked.append(v)
        ranked.sort(key=lambda x: x.get("cost", 0), reverse=True)
        return _ok({
            "period": {"start": start.isoformat(), "end": end.isoformat()},
            "department_field": department_field,
            "rows": ranked,
            "department_count": len(ranked),
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_compare_periods(
    days_a: int = 30,
    days_b: int = 30,
    offset_b: int = 30,
) -> str:
    """
        Periode A gegen Periode B stellen — Delta-KPIs.

        Wann nutzen: "Vergleich letzte 30 vs 30 Tage davor" • "Period A vs B"
        Wann NICHT — stattdessen: kontinuierlicher Trend → printix_query_trend / _print_trends
        Returns: a, b, deltas, percent_changes.
        Args: days_a, days_b, dimension.

    """
    from datetime import datetime, timedelta
    err = _reporting_check()
    if err:
        return _ok({"error": err})
    try:
        today = datetime.utcnow().date()
        a_end = today
        a_start = a_end - timedelta(days=max(days_a, 1))
        b_end = a_start - timedelta(days=1)
        b_start = b_end - timedelta(days=max(days_b, 1))

        def totals(start, end):
            rows = query_tools.query_print_stats(
                start_date=start.isoformat(), end_date=end.isoformat(),
                group_by="total",
            )
            if rows and isinstance(rows, list):
                r = rows[0]
                return {
                    "pages": int(r.get("pages") or 0),
                    "jobs": int(r.get("jobs") or 0),
                    "color_pages": int(r.get("color_pages") or 0),
                }
            return {"pages": 0, "jobs": 0, "color_pages": 0}

        a = totals(a_start, a_end)
        b = totals(b_start, b_end)

        def pct(new, old):
            if old == 0:
                return None
            return round((new - old) / old * 100, 2)

        return _ok({
            "period_a": {"start": a_start.isoformat(), "end": a_end.isoformat(), **a},
            "period_b": {"start": b_start.isoformat(), "end": b_end.isoformat(), **b},
            "delta_pct": {
                "pages": pct(a["pages"], b["pages"]),
                "jobs": pct(a["jobs"], b["jobs"]),
                "color_pages": pct(a["color_pages"], b["color_pages"]),
            },
        })
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Access & Governance ─────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_admins() -> str:
    """
        Alle Admins des Tenants.

        Wann nutzen: "Welche Admins gibt's?" • "Tenant admin list"
        Wann NICHT — stattdessen: alle User → printix_list_users ;
            Berechtigungs-Matrix → printix_permission_matrix
        Returns: admins Liste.
        Args: keine.

    """
    try:
        c = client()
        admins: list[dict] = []
        for role in ("SYSTEM_MANAGER", "SITE_MANAGER", "KIOSK_MANAGER"):
            try:
                data = c.list_users(role=role, page=0, page_size=200)
                users = []
                if isinstance(data, dict):
                    users = data.get("users") or data.get("content") or []
                for u in users or []:
                    u = dict(u) if isinstance(u, dict) else {}
                    u["role"] = role
                    admins.append(u)
            except Exception:
                continue
        return _ok({"admins": admins, "count": len(admins)})
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_permission_matrix() -> str:
    """
        Matrix User × Berechtigungen — wer darf was?

        Wann nutzen: "Wer hat welche Rechte?" • "Permission overview"
        Wann NICHT — stattdessen: nur Admin-Liste → printix_list_admins ;
            einzelne User-Rechte → printix_get_user
        Returns: matrix table.
        Args: keine.

    """
    try:
        c = client()
        users = _collect_all_users(c)
        groups = []
        try:
            gdata = c.list_groups(page=0, size=200)
            groups = gdata.get("groups") or gdata.get("content") or [] if isinstance(gdata, dict) else []
        except Exception:
            pass

        matrix: list[dict] = []
        for u in users:
            ugroups = u.get("groups") or []
            if isinstance(ugroups, list):
                matrix.append({
                    "user_id": u.get("id"),
                    "email": u.get("email"),
                    "display_name": u.get("displayName") or u.get("fullName"),
                    "group_count": len(ugroups),
                    "groups": ugroups,
                })
        return _ok({
            "users": matrix,
            "user_count": len(matrix),
            "group_count": len(groups),
            "groups": groups,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_inactive_users(days: int = 90) -> str:
    """
        User die seit N Tagen nicht mehr gedruckt haben — Offboarding-Kandidaten.

        Wann nutzen: "Wer ist seit 180 Tagen inaktiv?" • "Idle users since X days"
        Wann NICHT — stattdessen: Druck-Pattern eines Users → printix_describe_user_print_pattern ;
            Pruefung ob User existiert → printix_find_user
        Returns: inactive Liste mit last_print_at.
        Args: days  Schwelle (Default 90).

    """
    try:
        from datetime import datetime, timezone, timedelta
        c = client()
        users = _collect_all_users(c)
        threshold = datetime.now(timezone.utc) - timedelta(days=max(days, 1))
        inactive: list[dict] = []
        unknown: list[dict] = []
        for u in users:
            last = u.get("lastSignIn") or u.get("lastActivity") or u.get("lastLogin") or ""
            try:
                dt = datetime.fromisoformat(last.replace("Z", "+00:00")) if last else None
            except Exception:
                dt = None
            if dt is None:
                unknown.append({"user": u, "last_seen": None})
            elif dt < threshold:
                inactive.append({"user": u, "last_seen": last,
                                  "days_inactive": (datetime.now(timezone.utc) - dt).days})
        return _ok({
            "threshold_days": days,
            "inactive": inactive,
            "inactive_count": len(inactive),
            "unknown_last_seen": unknown,
            "unknown_count": len(unknown),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_sso_status(email: str) -> str:
    """
        Prueft SSO/Entra-Mapping fuer eine User-Email.

        Wann nutzen: "SSO-Status fuer marcus@firma.de" • "Is user X SSO-mapped?"
        Wann NICHT — stattdessen: Helpdesk-Allround → printix_diagnose_user
        Returns: {sso_provider, mapped, attributes}.
        Args: email  User-Email.

    """
    try:
        c = client()
        users = _collect_all_users(c)
        matches = [u for u in users if (u.get("email") or "").lower() == email.lower()]
        if not matches:
            return _ok({"error": "user not found", "email": email})
        user = matches[0]
        return _ok({
            "email": email,
            "user_id": user.get("id"),
            "auth_provider": user.get("authProvider") or user.get("identityProvider") or "unknown",
            "sso_id": user.get("externalId") or user.get("idpUserId") or "",
            "last_sign_in": user.get("lastSignIn") or user.get("lastLogin") or "",
            "is_sso_linked": bool(user.get("externalId") or user.get("idpUserId")),
            "raw_user": user,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Agent Workflow Helpers ──────────────────────────────────────────────────

_ERROR_KB = {
    "no_printix_user": "Der lokale User ist nicht mit einer Printix-UUID verknuepft. Loesung: im Portal Printix-Login verbinden oder UUID manuell eintragen.",
    "printix_uuid_invalid": "Die gespeicherte UUID ist ein Platzhalter, keine echte Printix-User-UUID. Aus der Admin-URL von Printix die UUID kopieren.",
    "manager_cannot_register_cards": "System/Site/Kiosk-Manager koennen keine Karten registrieren. Normalen USER-Account nutzen.",
    "no_tenant": "Kein Printix-Tenant konfiguriert. Im Portal OAuth-Credentials eintragen und Tenant zuweisen.",
    "forbidden": "Die verwendeten OAuth-Credentials haben keinen Scope fuer diese Operation. Im Portal Credentials erweitern.",
    "auth_required": "Bearer-Token fehlt oder ist abgelaufen. Im Desktop/Mobile-Client neu anmelden.",
    "printix_error": "Printix-API hat den Request abgelehnt. Der Response-Body enthaelt die konkrete Ursache.",
    "transform_error": "Die Kartentransformation schlug fehl. Profil-Rules pruefen oder anderes Profil waehlen.",
    "409": "Konflikt: Ressource existiert bereits (z.B. Karte mit gleichem Secret). Idempotenz-Flow triggern oder bestehende Ressource suchen.",
    "404": "Ressource nicht gefunden. ID pruefen — bei Cards-DELETE: id kam eventuell als String rein und wurde nicht auf int gecastet.",
    "502": "Upstream Printix-Fehler — die Printix-API selbst war nicht erreichbar oder hat 5xx geworfen. Retry mit Backoff.",
}


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_explain_error(code_or_message: str) -> str:
    """
        Uebersetzt einen Printix-Fehlercode oder Error-Message in Klartext + Loesungsvorschlag.

        Wann nutzen: "Was bedeutet 'AADSTS700025'?" • "Erklaer mir Fehler X" •
            "Why did this fail?"
        Wann NICHT — stattdessen: User druckt nicht → printix_diagnose_user ;
            Server prueft → printix_status
        Returns: explanation, root_causes, fix_suggestions.
        Args: code_or_message  Fehlercode ("auth_required", "AADSTS700025") ODER Teil-Message.

    """
    key = (code_or_message or "").strip().lower()
    matches = []
    for k, v in _ERROR_KB.items():
        if k in key or key in k:
            matches.append({"code": k, "explanation": v})
    if not matches:
        return _ok({
            "input": code_or_message,
            "matches": [],
            "hint": "Kein bekannter Code. Pruefe die tenant_logs (printix_query_audit_log) oder die Printix-Dashboard-Audit-Logs.",
        })
    return _ok({"input": code_or_message, "matches": matches})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_suggest_next_action(context: str) -> str:
    """
        Schlaegt anhand eines Kontext-Strings einen sinnvollen naechsten Schritt vor.

        Wann nutzen: "Was sollte ich als naechstes tun?" • "Suggest next step"
        Wann NICHT — stattdessen: konkretes Reports-Tool gesucht → printix_natural_query
        Returns: suggested_actions Liste.
        Args: context  Freitext-Kontext.

    """
    c = (context or "").lower()
    suggestions: list[str] = []
    if "fail" in c or "fehl" in c or "error" in c:
        suggestions.append("printix_query_audit_log mit action_prefix='job.' fuer letzte 24h ausfuehren.")
        suggestions.append("printix_diagnose_user(email) fuer den betroffenen User laufen lassen.")
    if "stuck" in c or "haeng" in c or "queue" in c:
        suggestions.append("printix_jobs_stuck(minutes=15) aufrufen und betroffene Jobs identifizieren.")
    if "karte" in c or "card" in c or "rfid" in c:
        suggestions.append("printix_card_audit(user_email) fuer den User laufen lassen.")
        suggestions.append("printix_find_orphaned_mappings() falls 'Karte geloescht aber Mapping noch da'.")
    if "druck" in c and "offline" in c:
        suggestions.append("printix_printer_health_report() fuer Offline-Uebersicht.")
    if "neuer" in c and ("user" in c or "mitarbeiter" in c):
        suggestions.append("printix_onboard_user(email, role, printers) verwenden.")
    if not suggestions:
        suggestions = [
            "printix_tenant_summary() fuer Ueberblick.",
            "printix_user_360(query) falls es um einen konkreten User geht.",
            "printix_query_anomalies fuer auffaellige Muster.",
        ]
    return _ok({"context": context, "suggested_actions": suggestions})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_send_to_user(
    user_email: str,
    file_url: str = "",
    file_content_b64: str = "",
    filename: str = "document.pdf",
    target_printer: str = "",
    copies: int = 1,
    pdl: str = "auto",
    color: bool = True,
) -> str:
    """
        High-Level: druckt Dokument als User X. Auto-PDL-Conversion (PDF→PCL XL).

        Wann nutzen: "Schick X an marcus@firma.de" • "Send PDF to user Y" •
            "Druck das fuer Anna"
        Wann NICHT — stattdessen: an sich selbst → printix_print_self ;
            mehrere Empfaenger → printix_print_to_recipients ;
            URL statt Base64 + simple use case → printix_quick_print ;
            archivieren statt drucken → printix_send_to_capture
        Returns: {ok, job_id, owner_email, filename, size_*, pdl, printer_id, queue_id}.
        Args: user_email  Ziel-Email.
            file_url HTTP(S)-URL  | file_content_b64 Base64 (eines von beiden!).
            filename  Anzeigename.
            target_printer  "" | Name | "pid:qid".
            copies, pdl ("auto"), color (True).

    """
    import base64 as _b64
    import requests as _req
    from print_conversion import prepare_for_print, ConversionError
    try:
        c = client()
        if not file_url and not file_content_b64:
            return _ok({"error": "file_url or file_content_b64 required"})
        if file_url:
            r = _req.get(file_url, timeout=60)
            r.raise_for_status()
            file_bytes = r.content
        else:
            file_bytes = _b64.b64decode(file_content_b64)

        # Printer aufloesen
        printer_id, queue_id = "", ""
        if ":" in target_printer:
            printer_id, queue_id = target_printer.split(":", 1)
        else:
            pdata = c.list_printers(search=target_printer or None, page=0, size=50)
            plist = pdata.get("printers") or pdata.get("content") or [] if isinstance(pdata, dict) else []
            if not plist:
                return _ok({"error": "no printer found", "query": target_printer})
            printer_id, queue_id = _extract_printer_queue_ids(plist[0])
        if not (printer_id and queue_id):
            return _ok({"error": "could not resolve printer_id/queue_id"})

        # PDL-Detection + Konvertierung (v6.8.8+)
        target_pdl = (pdl or "auto").upper()
        if target_pdl == "AUTO":
            target_pdl = "PCLXL"
        try:
            converted_bytes, final_pdl = prepare_for_print(
                file_bytes, target=target_pdl, color=color)
        except ConversionError as ce:
            return _ok({"error": f"conversion failed: {ce}",
                         "hint": "pdl='passthrough' ueberspringt die Konvertierung"})

        # Job submit
        # NOTE v6.8.4: PrintixClient.submit_print_job() hat kein size_bytes-Argument.
        # NOTE v6.8.5: API-Response ist nested ({job:{id:...}, uploadLinks:[...]}).
        # NOTE v6.8.8: pdl wird jetzt aus der Konvertierung uebergeben.
        job = c.submit_print_job(printer_id=printer_id, queue_id=queue_id,
                                  title=filename, copies=copies, pdl=final_pdl)
        job_id, upload_url, upload_headers = _extract_job_id_and_upload(job)
        if not (job_id and upload_url):
            return _ok({"error": "submit_print_job missing job_id or upload_url", "raw": job})

        c.upload_file_to_url(upload_url, converted_bytes, extra_headers=upload_headers)
        c.complete_upload(job_id)
        c.change_job_owner(job_id, user_email)
        return _ok({
            "ok": True, "job_id": job_id, "owner_email": user_email,
            "filename": filename,
            "size_input": len(file_bytes),
            "size_after_conversion": len(converted_bytes),
            "pdl": final_pdl,
            "printer_id": printer_id, "queue_id": queue_id,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_onboard_user(
    email: str,
    display_name: str,
    role: str = "USER",
    pin: str = "",
    password: str = "",
    groups: str = "",
) -> str:
    """
        Komplett-Onboarding: User anlegen, optional in Gruppen stecken.

        Wann nutzen: "Leg neuen Mitarbeiter an" • "Onboard new user" •
            "Create user with full setup"
        Wann NICHT — stattdessen: Welcome-PDF + Reminder-Time-Bombs ergaenzend → printix_welcome_user
        Returns: ok, user, group_assignments, next_steps.
        Args: email, display_name, role ("USER"), pin, password, groups (csv).

    """
    try:
        c = client()
        created = c.create_user(email=email, display_name=display_name,
                                 pin=pin or None, password=password or None)
        created_info = PrintixClient.extract_created_user(created) if hasattr(PrintixClient, "extract_created_user") else created
        user_id = created_info.get("id") if isinstance(created_info, dict) else ""
        group_assignments: list[dict] = []
        if groups:
            for gid in [g.strip() for g in groups.split(",") if g.strip()]:
                # Printix-API: best effort — wenn's keine Gruppen-Add-API gibt, loggen
                group_assignments.append({"group_id": gid, "status": "not_implemented"})
        return _ok({
            "ok": True,
            "user": created_info,
            "user_id": user_id,
            "group_assignments": group_assignments,
            "next_steps": [
                "printix_generate_id_code(user_id) wenn ID-Code gewuenscht",
                "Karte via iOS/Web registrieren oder printix_bulk_import_cards()",
            ],
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── GDPR Data Subject Rights (v7.2.30) ──────────────────────────────────────

def _resolve_data_subject(c: PrintixClient, user_email_or_id: str) -> dict | None:
    """Findet einen Printix-User über E-Mail ODER User-UUID. Nutzt
    `_collect_all_users` Helper (tenant-weit, max 200 User).
    Returns das Printix-User-Dict oder None.
    """
    if not user_email_or_id:
        return None
    needle = user_email_or_id.strip().lower()
    if "-" in needle and len(needle) >= 32:
        try:
            ud = c.get_user(needle)
            if isinstance(ud, dict):
                return ud
        except Exception:
            pass
    try:
        all_users = _collect_all_users(c)
    except Exception:
        all_users = []
    for u in all_users:
        if not isinstance(u, dict):
            continue
        if (u.get("email") or "").strip().lower() == needle:
            return u
        if (u.get("id") or "") == user_email_or_id:
            return u
    return None


def _gather_personal_data(c: PrintixClient, target_user: dict) -> dict:
    """Sammelt alles was wir über den User wissen — Quelle für DSGVO Art. 15.

    Trägt aus:
      - Printix-User-Profil (Name, E-Mail, Status)
      - Gruppen-Mitgliedschaften
      - Druckhistorie (letzte 365 Tage falls SQL-Reporting konfiguriert)
      - Karten-Mappings
      - Welcome-PDFs / aktive Time-Bombs (lokale DB)
      - Audit-Log (lokale DB, alle Aktionen wo user_id = X)
      - MCP-Rolle / Override
    """
    from datetime import datetime, timezone, timedelta
    pid = target_user.get("id") or ""
    email = target_user.get("email") or ""

    # Gruppen via existing helper
    groups = []
    try:
        ugroups_raw = printix_get_user_groups(email or pid)
        gd = json.loads(ugroups_raw)
        if isinstance(gd, dict):
            groups = gd.get("groups") or []
    except Exception:
        pass

    # Karten
    cards: list[dict] = []
    try:
        raw = c.list_cards(page=0, size=200)
        all_cards = (raw.get("cards") or raw.get("content") or []) if isinstance(raw, dict) else []
        for crd in all_cards or []:
            owner = (crd.get("userId") or
                     ((crd.get("_links") or {}).get("user") or {}).get("href", "").rstrip("/").split("/")[-1])
            if owner == pid:
                cards.append({
                    "card_id":   _extract_card_id_from_api(crd),
                    "value":     crd.get("value") or crd.get("cardId"),
                    "profile":   crd.get("profileName") or "",
                    "created":   crd.get("createdAt") or "",
                })
    except Exception:
        pass

    # Audit-Log (lokal — nur eigene Tenant-Aktionen)
    audit_entries: list[dict] = []
    try:
        # Lokale User-ID falls vorhanden (über printix_user_id)
        with db._conn() as conn:
            row = conn.execute(
                "SELECT id FROM users WHERE printix_user_id = ?", (pid,)
            ).fetchone()
            local_uid = row["id"] if row else None
            if local_uid:
                rows = conn.execute(
                    "SELECT created_at, action, object_type, object_id, details "
                    "FROM audit_log WHERE user_id = ? "
                    "ORDER BY created_at DESC LIMIT 500",
                    (local_uid,),
                ).fetchall()
                audit_entries = [dict(r) for r in rows]
    except Exception:
        pass

    # MCP-Rolle (lokal)
    mcp_role = ""
    try:
        with db._conn() as conn:
            row = conn.execute(
                "SELECT mcp_role FROM users WHERE printix_user_id = ?", (pid,)
            ).fetchone()
            if row:
                mcp_role = row["mcp_role"] or ""
    except Exception:
        pass

    # Time-Bombs (lokal)
    timebombs: list[dict] = []
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM user_timebombs WHERE printix_user_id = ? "
                "ORDER BY created_at DESC LIMIT 100",
                (pid,),
            ).fetchall()
            timebombs = [dict(r) for r in rows]
    except Exception:
        pass

    # Druck-Statistik (SQL Reporting falls verfügbar)
    print_stats = None
    try:
        # Versuche query_print_stats aufzurufen
        period_to = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        period_from = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
        raw = printix_query_print_stats(
            user_filter=email, date_from=period_from, date_to=period_to,
        )
        ps = json.loads(raw)
        if isinstance(ps, dict) and not ps.get("error"):
            print_stats = ps
    except Exception:
        pass

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "subject": {
            "printix_user_id": pid,
            "email": email,
            "name":  target_user.get("name") or target_user.get("displayName") or "",
            "status": target_user.get("status") or "",
            "tenant_id": (current_tenant.get() or {}).get("printix_tenant_id"),
        },
        "groups": groups,
        "cards": cards,
        "mcp_role_override": mcp_role,
        "timebombs": timebombs,
        "audit_log": audit_entries,
        "print_statistics_last_365d": print_stats,
        "_note": (
            "This export is generated under GDPR Article 15 (right of "
            "access). Data not present here either does not exist for this "
            "subject or is held by an upstream system (Printix cloud, "
            "AI assistant vendor) outside this MCP server's scope."
        ),
    }


def _build_personal_data_zip(data: dict) -> bytes:
    """Baut ein ZIP mit JSON pro Datenkategorie + README für den Empfänger."""
    import io, zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        readme = (
            "Printix MCP — Personal Data Export\n"
            "==================================\n\n"
            f"Exported at: {data.get('exported_at')}\n"
            f"Subject:     {data.get('subject', {}).get('email', '?')}\n"
            f"Tenant:      {data.get('subject', {}).get('tenant_id', '?')}\n\n"
            "Files in this archive:\n"
            "  manifest.json         — top-level summary\n"
            "  user_profile.json     — Printix user profile\n"
            "  groups.json           — group memberships\n"
            "  cards.json            — RFID/HID/Mifare cards mapped to subject\n"
            "  audit_log.json        — actions performed by subject (last 500)\n"
            "  timebombs.json        — pending and resolved onboarding triggers\n"
            "  print_statistics.json — print volume / cost (last 365 days, if SQL reporting is enabled)\n"
            "  mcp_role.json         — MCP role override (if set)\n\n"
            "Generated under GDPR Article 15 by Printix MCP.\n"
            "If you believe this export is incomplete, contact the data\n"
            "controller (your tenant administrator).\n"
        )
        zf.writestr("README.txt", readme)
        zf.writestr("manifest.json", json.dumps({
            "exported_at": data.get("exported_at"),
            "subject":     data.get("subject"),
            "_note":       data.get("_note"),
        }, indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("user_profile.json", json.dumps(data.get("subject", {}),
                    indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("groups.json",       json.dumps(data.get("groups", []),
                    indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("cards.json",        json.dumps(data.get("cards", []),
                    indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("audit_log.json",    json.dumps(data.get("audit_log", []),
                    indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("timebombs.json",    json.dumps(data.get("timebombs", []),
                    indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("print_statistics.json", json.dumps(
            data.get("print_statistics_last_365d") or {"note": "no SQL reporting configured"},
            indent=2, ensure_ascii=False, default=_json_default))
        zf.writestr("mcp_role.json",     json.dumps({
            "mcp_role_override": data.get("mcp_role_override", ""),
        }, indent=2, ensure_ascii=False))
    return buf.getvalue()


def _caller_email() -> str:
    """Liefert die E-Mail des aktuellen MCP-Aufrufers aus dem Tenant-Kontext."""
    tenant = current_tenant.get() or {}
    # In den meisten Tenant-Records steht email/username — der Tenant-User
    # ist der Account, der den Bearer-Token besitzt
    return (tenant.get("email") or tenant.get("username") or "").strip().lower()


def _caller_is_admin_or_helpdesk() -> bool:
    """True wenn die Rolle des Aufrufers Helpdesk oder Admin ist —
    erlaubt damit Aktionen auf andere User."""
    try:
        from permissions import resolve_mcp_role
        tenant = current_tenant.get() or {}
        uid = tenant.get("user_id") or ""
        if not uid:
            return False
        role = resolve_mcp_role(uid)
        return role in ("helpdesk", "admin")
    except Exception:
        return False


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_personal_data_export(user_email_or_id: str = "") -> str:
    """
        DSGVO Art. 15 — vollständiger Export aller persönlichen Daten als ZIP (Base64).

        Wann nutzen:
          • End-User: "Welche Daten haben Sie über mich gespeichert?" → leer
            lassen oder eigene E-Mail eingeben
          • Helpdesk/Admin: "Bitte erstelle einen Datenexport für anna@firma.de"
            für ein DSGVO-Auskunftsersuchen

        Wann NICHT — stattdessen:
          • Daten löschen (Art. 17) → printix_personal_data_purge_request
          • Tenant-weiter Audit-Trail → printix_query_audit_log

        Returns dict mit:
          - status:       "ok"
          - filename:     "personal_data_export_<email>.zip"
          - file_b64:     Base64-kodiertes ZIP zum Download
          - size_bytes:   ZIP-Größe
          - subject:      Profil-Zusammenfassung
          - included:     Liste der enthaltenen Datenkategorien

        Args:
          user_email_or_id:  E-Mail oder Printix-UUID. Leer = eigener Account
                             (nur für End-User; Helpdesk/Admin müssen explizit
                             angeben).

    """
    try:
        c = client()
        caller_email = _caller_email()
        is_elevated = _caller_is_admin_or_helpdesk()

        # Default für End-User: eigene Daten
        target_email_or_id = (user_email_or_id or "").strip()
        if not target_email_or_id:
            if not caller_email:
                return _ok({"error": "no caller email in tenant context — please pass user_email_or_id"})
            target_email_or_id = caller_email

        # End-User darf nur eigene Daten exportieren
        if not is_elevated:
            tgt_lower = target_email_or_id.lower()
            if tgt_lower != caller_email:
                return _ok({
                    "error": "permission_denied",
                    "message": (
                        "End users can only export their own data under GDPR "
                        "Art. 15. Helpdesk or admin role required to export "
                        "another user's data."
                    ),
                    "your_email": caller_email,
                    "requested_for": target_email_or_id,
                })

        # User auflösen
        target = _resolve_data_subject(c, target_email_or_id)
        if not target:
            return _ok({
                "error": "user_not_found",
                "message": f"No Printix user found for '{target_email_or_id}'.",
            })

        # Daten sammeln + ZIP bauen
        data = _gather_personal_data(c, target)
        zip_bytes = _build_personal_data_zip(data)

        # Audit-Trail
        try:
            tenant = current_tenant.get() or {}
            db.audit(
                user_id=tenant.get("user_id") or "",
                action="gdpr_data_exported",
                details=f"GDPR Art. 15 export for subject '{target.get('email', target.get('id'))}'",
                object_type="data_subject",
                object_id=target.get("id", ""),
                tenant_id=tenant.get("id", ""),
            )
        except Exception:
            pass

        import base64
        return _ok({
            "status": "ok",
            "subject": data["subject"],
            "filename": f"personal_data_export_{(target.get('email') or target.get('id') or 'subject')}.zip",
            "file_b64": base64.b64encode(zip_bytes).decode("ascii"),
            "size_bytes": len(zip_bytes),
            "included": [
                "user_profile", "groups", "cards", "audit_log",
                "timebombs", "print_statistics", "mcp_role",
            ],
            "next_step": (
                "Save the file_b64 contents as a .zip file. The archive "
                "contains one JSON per data category plus a README.txt."
            ),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("personal_data_export failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_personal_data_purge_request(
    user_email_or_id: str = "",
    reason: str = "",
) -> str:
    """
        DSGVO Art. 17 — Antrag auf Löschung der eigenen Daten ("Recht auf Vergessenwerden").

        Dieses Tool LÖSCHT NICHTS direkt. Es:
          1. Sammelt eine Übersicht der betroffenen Daten (wie der Export)
          2. Schreibt einen audit_log-Eintrag mit action='gdpr_purge_requested'
          3. Sendet eine Mail an die alert_recipients (Tenant-Admins) mit
             dem Lösch-Antrag und der Datenübersicht
          4. Liefert Bestätigungs-Token zurück

        Der Admin entscheidet anschließend manuell über die Löschung
        (typisch via printix_offboard_user oder printix_delete_user).

        Wann nutzen:
          • End-User: "Bitte alle Daten von mir löschen lassen"
          • Helpdesk/Admin: Antrag im Namen eines Users einreichen

        Wann NICHT — stattdessen:
          • Sofortige Löschung als Admin     → printix_offboard_user
          • Nur Daten ansehen ohne Löschung  → printix_personal_data_export
          • Audit-Log-Recherche              → printix_query_audit_log

        Returns dict mit:
          - status:        "request_recorded" | "permission_denied" | "user_not_found"
          - request_id:    eindeutige Referenz für Rückfragen
          - notified_admins: Liste der angeschriebenen Admin-Mails
          - data_summary:  Was würde gelöscht (Cards, Groups, Audit-Einträge, etc.)
          - next_step:     Hinweis was passiert ("Ihr Antrag wurde an X
                           weitergeleitet. Sie erhalten eine Bestätigung
                           sobald die Löschung erfolgt ist.")

        Args:
          user_email_or_id:  E-Mail/UUID. Leer = eigener Account.
          reason:            Optional Begründung — wird in der Admin-Mail
                             zitiert. Hilft der DSB-Bewertung des Antrags.

    """
    try:
        c = client()
        caller_email = _caller_email()
        is_elevated = _caller_is_admin_or_helpdesk()

        target_email_or_id = (user_email_or_id or "").strip()
        if not target_email_or_id:
            target_email_or_id = caller_email

        # End-User darf nur eigenen Antrag stellen
        if not is_elevated:
            if target_email_or_id.lower() != caller_email:
                return _ok({
                    "error": "permission_denied",
                    "message": (
                        "End users can only file a deletion request for "
                        "their own data. Helpdesk or admin role required "
                        "to file on behalf of another user."
                    ),
                })

        target = _resolve_data_subject(c, target_email_or_id)
        if not target:
            return _ok({
                "status": "user_not_found",
                "message": f"No Printix user found for '{target_email_or_id}'.",
            })

        # Daten zusammenfassen (für die Mail-Übersicht — kein voller Export)
        data = _gather_personal_data(c, target)
        summary = {
            "subject_email":       data["subject"].get("email"),
            "subject_name":        data["subject"].get("name"),
            "subject_id":          data["subject"].get("printix_user_id"),
            "groups_count":        len(data.get("groups") or []),
            "cards_count":         len(data.get("cards") or []),
            "audit_entries_count": len(data.get("audit_log") or []),
            "timebombs_count":     len(data.get("timebombs") or []),
            "has_print_statistics": bool(data.get("print_statistics_last_365d")),
            "mcp_role_override":   data.get("mcp_role_override"),
        }

        # Eindeutige Request-ID
        import secrets as _secrets
        request_id = "gdpr-" + _secrets.token_hex(8)

        # Audit-Eintrag
        tenant = current_tenant.get() or {}
        try:
            db.audit(
                user_id=tenant.get("user_id") or "",
                action="gdpr_purge_requested",
                details=(f"GDPR Art. 17 deletion request for "
                         f"'{target.get('email', target.get('id'))}'. "
                         f"Reason: {reason or '(none)'}. "
                         f"Request: {request_id}"),
                object_type="data_subject",
                object_id=target.get("id", ""),
                tenant_id=tenant.get("id", ""),
            )
        except Exception as e:
            logger.warning("audit insert for gdpr_purge_requested failed: %s", e)

        # Admin-Mail bauen + senden
        notified: list[str] = []
        try:
            from db import get_tenant_full_by_user_id
            tenant_full = get_tenant_full_by_user_id(tenant.get("user_id") or "")
            if not tenant_full:
                # Single-Tenant-Fallback — Owner finden
                from db import _find_tenant_owner_user_id
                owner_uid = _find_tenant_owner_user_id()
                if owner_uid:
                    tenant_full = get_tenant_full_by_user_id(owner_uid)

            if tenant_full:
                recipients_str = (tenant_full.get("alert_recipients") or "").strip()
                notified = [r.strip() for r in recipients_str.split(",") if r.strip()]

            if tenant_full and notified:
                rows_html = "".join(
                    f"<tr><td style='padding:4px 10px;'><strong>{k}</strong></td>"
                    f"<td style='padding:4px 10px;'>{v}</td></tr>"
                    for k, v in summary.items()
                )
                html = f"""
                <div style="font-family:Arial,sans-serif;color:#1a1a1a;">
                  <h2 style="color:#003366;">GDPR Art. 17 — Deletion Request</h2>
                  <p>A user has requested deletion of their personal data
                     under GDPR Article 17 (right to erasure).</p>
                  <p><strong>Request ID:</strong> <code>{request_id}</code><br>
                     <strong>Filed by:</strong> {caller_email or '(unknown)'}<br>
                     <strong>Subject:</strong> {target.get('email') or target.get('id')}<br>
                     <strong>Reason given:</strong> {reason or '<em>(none)</em>'}</p>
                  <h3 style="color:#003366;">Data summary</h3>
                  <table style="border-collapse:collapse;">{rows_html}</table>
                  <h3 style="color:#003366;">What to do</h3>
                  <ol>
                    <li>Review the request and verify the requester's identity.</li>
                    <li>If approved, execute deletion via the MCP tool
                        <code>printix_offboard_user</code> (preserves audit
                        trail) or <code>printix_delete_user</code> (full).
                        Both are recorded in the audit log against this
                        request ID.</li>
                    <li>Notify the data subject of the outcome
                        within one month (GDPR Art. 12(3)).</li>
                  </ol>
                  <p style="font-size:.85em;color:#666;">
                    Generated by Printix MCP. Audit trail entry:
                    <code>action='gdpr_purge_requested', request='{request_id}'</code>
                  </p>
                </div>
                """
                from reporting.notify_helper import send_event_notification
                send_event_notification(
                    tenant_full,
                    event_type="gdpr_purge_request",
                    subject=f"[Printix MCP] GDPR Deletion Request — {target.get('email', target.get('id'))} ({request_id})",
                    html_body=html,
                    check_enabled=False,  # always send, regardless of notify_events
                )
        except Exception as e:
            logger.warning("admin notification for gdpr_purge_request failed: %s", e)

        return _ok({
            "status": "request_recorded",
            "request_id": request_id,
            "subject": data["subject"],
            "data_summary": summary,
            "notified_admins": notified,
            "next_step": (
                "Your deletion request has been logged and forwarded to the "
                "tenant administrators. You will be notified of the outcome "
                "within one month as required by GDPR Art. 12(3). Keep the "
                f"request_id ({request_id}) for follow-up correspondence."
            ),
            "_note": (
                "This tool does NOT delete any data directly — end users "
                "are not authorised to remove records. The administrator "
                "reviews each request and executes the deletion manually "
                "to ensure auditability and protect against malicious "
                "self-purge attempts."
            ),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("personal_data_purge_request failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True))
def printix_offboard_user(email: str, force: bool = False) -> str:
    """
        Leaver-Flow: alle Karten loeschen + offene Jobs canceln + User deaktivieren/loeschen.

        Wann nutzen: "Offboard X" • "Mitarbeiter scheidet aus" • "Delete user with cleanup"
        Wann NICHT — stattdessen: nur User-Loeschung ohne Cleanup → printix_delete_user (selten ratsam)
        Returns: report mit steps Liste pro Phase.
        Args: email  User-Email. force True wenn role != GUEST_USER.

    """
    try:
        c = client()
        users = _collect_all_users(c)
        matches = [u for u in users if (u.get("email") or "").lower() == email.lower()]
        if not matches:
            return _ok({"error": "user not found", "email": email})
        user = matches[0]
        user_id = user.get("id", "")
        tenant_id = _get_card_tenant_id()

        report = {"email": email, "user_id": user_id, "steps": []}

        # Karten loeschen
        try:
            data = c.list_user_cards(user_id=user_id)
            for card in _card_items(data):
                cid = _extract_card_id_from_api(card)
                if cid:
                    try:
                        c.delete_card(cid, user_id=user_id)
                        report["steps"].append({"card": cid, "status": "deleted"})
                    except Exception as ce:
                        report["steps"].append({"card": cid, "status": "error", "error": str(ce)})
        except Exception as e:
            report["steps"].append({"phase": "list_cards", "error": str(e)})

        # Lokale Mappings loeschen
        try:
            from cards.store import delete_mappings_for_user
            delete_mappings_for_user(tenant_id, user_id)
            report["steps"].append({"phase": "local_mappings", "status": "deleted"})
        except Exception as e:
            report["steps"].append({"phase": "local_mappings", "error": str(e)})

        # User loeschen (nur wenn GUEST_USER oder explizit force)
        role = user.get("role") or user.get("roleType") or ""
        if role == "GUEST_USER" or force:
            try:
                c.delete_user(user_id)
                report["steps"].append({"phase": "delete_user", "status": "deleted"})
            except Exception as e:
                report["steps"].append({"phase": "delete_user", "error": str(e)})
        else:
            report["steps"].append({
                "phase": "delete_user",
                "status": "skipped",
                "reason": f"role={role}; use force=True",
            })

        return _ok(report)
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Quality of Life ─────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_whoami() -> str:
    """
        Aktueller Tenant + eigener Printix-User + Admin-Status.

        Wann nutzen: "Wer bin ich?" • "Who am I logged in as?"
        Wann NICHT — stattdessen: nur Server-Health → printix_status ;
            Tenant-Kennzahlen → printix_tenant_summary
        Returns: tenant_name, user_email, is_admin.
        Args: keine.

    """
    try:
        tenant = current_tenant.get() or {}
        scopes = {
            "print": bool(tenant.get("print_client_id")),
            "card":  bool(tenant.get("card_client_id")),
            "workstation": bool(tenant.get("ws_client_id")),
            "user_management": bool(tenant.get("um_client_id")),
            "shared": bool(tenant.get("shared_client_id")),
        }
        return _ok({
            "tenant_id": tenant.get("id"),
            "tenant_name": tenant.get("name"),
            "printix_tenant_id": tenant.get("printix_tenant_id"),
            "configured_scopes": scopes,
            "server_version": APP_VERSION,
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
def printix_my_role() -> str:
    """
        Eigene MCP-Rolle und welche Tools damit erlaubt sind.

        Wann nutzen: "Was darf ich?" • "Welche Rolle habe ich?" •
            "Why was I denied access to tool X?"
        Wann NICHT — stattdessen: vollstaendige Berechtigungs-Matrix
            aller User → printix_permission_matrix (admin only)
        Returns: dict mit role, permitted_scopes, rbac_enabled,
            denied_tool_count.
        Args: keine.

    """
    try:
        from permissions import (
            resolve_mcp_role, ROLE_SCOPES, TOOL_SCOPES, has_permission,
            ROLE_LABELS_EN,
        )
        tenant = current_tenant.get() or {}
        user_id = tenant.get("user_id") or ""
        role = resolve_mcp_role(user_id) if user_id else "end_user"
        permitted = sorted(ROLE_SCOPES.get(role, frozenset()))

        # Sample of allowed/denied tools for transparency
        allowed_tools = sorted(
            t for t in TOOL_SCOPES.keys() if has_permission(role, t)
        )
        denied_tools = sorted(
            t for t in TOOL_SCOPES.keys() if not has_permission(role, t)
        )

        return _ok({
            "role": role,
            "role_label": ROLE_LABELS_EN.get(role, role),
            "permitted_scopes": permitted,
            "rbac_enabled": _RBAC_ENABLED,
            "tools_allowed_count": len(allowed_tools),
            "tools_denied_count":  len(denied_tools),
            "tools_allowed_sample": allowed_tools[:10],
            "tools_denied_sample":  denied_tools[:10],
            "user_id": user_id,
            "tenant_id": tenant.get("id"),
            "note": (
                "RBAC is currently inactive (MCP_RBAC_ENABLED=0). All tools "
                "are reachable regardless of role."
            ) if not _RBAC_ENABLED else (
                "RBAC is active. Tools outside your scopes return a "
                "permission_denied response."
            ),
        })
    except Exception as e:
        logger.error("printix_my_role failed: %s", e)
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_quick_print(recipient_email: str, file_url: str, filename: str = "document.pdf") -> str:
    """
        Single-shot-Print: URL + Empfaenger → fertig (Wrapper um send_to_user).

        Wann nutzen: "Druck mir https://… als marcus@firma.de" • "Quick print URL to user"
        Wann NICHT — stattdessen: KI-generiertes PDF (Base64) → printix_send_to_user mit file_content_b64 ;
            an sich selbst → printix_print_self ; Mehrere Empfaenger → printix_print_to_recipients
        Returns: gleich wie send_to_user.
        Args: recipient_email  Empfaenger.
            file_url  HTTP(S)-URL der Datei.
            filename  optional, Default "document.pdf".

    """
    return printix_send_to_user(user_email=recipient_email, file_url=file_url,
                                 filename=filename, target_printer="", copies=1)


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_resolve_printer(name_or_location: str) -> str:
    """
        Findet besten Drucker per Token-Fuzzy-Match (Name + Location + Vendor + Site).

        Wann nutzen: "Brother Drucker in Duesseldorf" • "Welcher HP M577 in DACH?"
        Wann NICHT — stattdessen: schon ID bekannt → printix_get_printer ;
            gesamte Liste → printix_list_printers
        Returns: matches mit Score; bester Treffer first.
        Args: query  freier Text mit beliebigen Tokens (Vendor + Location + Modell …).

    """
    try:
        c = client()
        data = c.list_printers(page=0, size=200)
        if isinstance(data, list):
            plist = data
        elif isinstance(data, dict):
            plist = data.get("printers") or data.get("content") or data.get("items") or []
        else:
            plist = []

        raw_query = (name_or_location or "").strip()
        q_lower = raw_query.lower()
        # v6.7.114: Token-basierter Fuzzy-Match. Vorher wurde "Brother Duesseldorf"
        # als zusammenhaengender Substring gesucht — das matcht keinen Printer,
        # dessen name="Brother-MFP-01" und siteName="Duesseldorf Office" ist,
        # weil die Tokens in verschiedenen Feldern stehen. Jetzt: ALLE Tokens
        # muessen irgendwo im kombinierten Haystack (name+model+vendor+location+
        # siteName+networkName) vorkommen.
        tokens = [t for t in re.split(r"\s+", q_lower) if t]

        matches = []
        for p in plist or []:
            if not isinstance(p, dict):
                continue
            hay_parts = [
                str(p.get("name", "")),
                str(p.get("model", "")),
                str(p.get("vendor", "")),
                str(p.get("location", "")),
                str(p.get("siteName", "")),
                str(p.get("networkName", "")),
                str(p.get("hostname", "")),
            ]
            hay = " ".join(hay_parts).lower()
            if not hay.strip():
                continue

            # Substring-Match bleibt als Schnellpfad fuer Einzelwort-Queries
            hit = False
            score = 0
            if q_lower and q_lower in hay:
                hit = True
                score = 100
            elif tokens and all(t in hay for t in tokens):
                # Alle Tokens vorhanden — Fuzzy-Treffer.
                hit = True
                score = 50 + min(40, 10 * len(tokens))

            if hit:
                pid, qid = _extract_printer_queue_ids(p)
                matches.append({
                    "printer": p, "printer_id": pid, "queue_id": qid,
                    "compact": f"{pid}:{qid}",
                    "score": score,
                })

        # Beste Matches nach Score oben
        matches.sort(key=lambda m: m.get("score", 0), reverse=True)
        return _ok({"query": name_or_location, "matches": matches, "match_count": len(matches),
                    "best_match": matches[0] if matches else None})
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_natural_query(question: str) -> str:
    """
        Nimmt natuerlich-sprachige Frage und schlaegt das passende Reports-Tool vor.

        Wann nutzen: "Welches Tool fuer …?" • "Wie frage ich X ab?"
        Wann NICHT — stattdessen: direkt querien → printix_query_any oder spezialisiertes query_*
        Returns: question, suggested_tools.
        Args: question  natuerlich-sprachige Frage.

    """
    q = (question or "").lower()
    hints = []
    if "wer" in q or "who" in q or "top" in q or "meist" in q:
        hints.append("printix_top_users(days, limit) oder printix_query_top_users(start_date, end_date).")
    if "welch" in q and "druck" in q:
        hints.append("printix_top_printers(days, limit).")
    if "trend" in q or "entwick" in q or "ueber zeit" in q:
        hints.append("printix_print_trends(group_by, days).")
    if "kosten" in q or "cost" in q:
        hints.append("printix_query_cost_report oder printix_cost_by_department.")
    if "vergleich" in q or "versus" in q or "gegenueber" in q:
        hints.append("printix_compare_periods(days_a, days_b).")
    if "anomal" in q or "auffaell" in q:
        hints.append("printix_query_anomalies.")
    if "karte" in q or "card" in q:
        hints.append("printix_list_cards_by_tenant, printix_card_audit, printix_find_orphaned_mappings.")
    if not hints:
        hints = [
            "printix_tenant_summary() fuer Ueberblick",
            "printix_list_report_templates() fuer gespeicherte Reports",
            "printix_query_print_stats fuer rohes Druckvolumen",
        ]
    return _ok({"question": question, "suggested_tools": hints})


# ════════════════════════════════════════════════════════════════════════════
# v6.8.0 Workflow-Tools — High-Level Composition aus den Low-Level-Bausteinen
# ════════════════════════════════════════════════════════════════════════════
# Diese Sektion buendelt 14 neue Tools die die existierenden 5-stage-Submits,
# Capture-Plugins und User/Group-Lookups zu AI-natuerlichen Workflows
# kombinieren. Jedes Tool hier komponiert nur — keine neuen Printix-API-Calls
# unter der Haube, alles laeuft ueber printix_client.py / capture/plugins/.


# ─── v6.8.5 Helpers fuer Submit-Job Response-Extraktion ─────────────────────

def _extract_job_id_and_upload(job: Any) -> tuple[str, str, dict]:
    """Holt job_id + upload_url + Pflicht-Headers aus einer
    submit_print_job-Response.

    Echte Printix-API-Response (v1.1):
      {
        "job":         {"id": "...", "_links": {...}, ...},
        "_links":      {"uploadCompleted": {...}, "changeOwner": {...}},
        "uploadLinks": [
          {
            "url":     "https://prodenv2printjobs.blob.core.windows.net/...",
            "headers": {"x-ms-blob-type": "BlockBlob"}   ← Pflicht beim PUT
          }
        ],
        ...
      }

    Azure-Blob-Storage verlangt fuer PUT auf einen BlockBlob den Header
    `x-ms-blob-type: BlockBlob` — sonst HTTP 400 "MissingRequiredHeader".
    Die API gibt uns die noetigen Headers im Response, wir muessen sie
    nur an upload_file_to_url(extra_headers=...) durchreichen.

    Returns: (job_id, upload_url, upload_headers).
    """
    if not isinstance(job, dict):
        return "", "", {}
    inner = job.get("job") if isinstance(job.get("job"), dict) else {}
    job_id = (inner.get("id")
              or inner.get("jobId")
              or job.get("jobId")
              or job.get("id")
              or "")
    upload_url = ""
    upload_headers: dict = {}
    ul = job.get("uploadLinks")
    if isinstance(ul, list) and ul and isinstance(ul[0], dict):
        upload_url = ul[0].get("url", "") or ul[0].get("href", "")
        h = ul[0].get("headers")
        if isinstance(h, dict):
            upload_headers = {str(k): str(v) for k, v in h.items()}
    if not upload_url:
        # alternativer Pfad falls API normalisiert wird
        links = job.get("_links") or {}
        upload_url = ((links.get("upload") or {}).get("href")
                      or job.get("uploadUrl") or "")
    return str(job_id), str(upload_url), upload_headers


# ─── Phase 1a: print_self ────────────────────────────────────────────────────

def _resolve_self_user(c: PrintixClient) -> dict | None:
    """Loest den aufrufenden MCP-User auf seine Printix-Identitaet.

    Strategie:
    1) `current_tenant` ContextVar liefert tenant.email/username (Printix-
       Login). Falls nicht: tenant.user_id → users-Tabelle joinen.
    2) Mit Email: list_users mit query=email, exakter Match.
    3) Wenn `printix_user_id` schon gemappt: get_user(uuid) direkt.
    """
    t = current_tenant.get() or {}
    email = (t.get("email") or t.get("username") or "").strip()
    # Tenant-Row hat kein email-Feld; ueber user_id auf users-Tabelle joinen.
    if not email and t.get("user_id"):
        try:
            from db import get_user_by_id
            urow = get_user_by_id(t["user_id"])
            if urow:
                email = (urow.get("email") or urow.get("username") or "").strip()
        except Exception:
            pass
    pre_id = t.get("printix_user_id") or ""
    if pre_id:
        try:
            u = c.get_user(pre_id)
            if isinstance(u, dict):
                return u
        except Exception:
            pass
    if email:
        try:
            data = c.list_users(query=email, page=0, page_size=10)
            users = data.get("users") if isinstance(data, dict) else None
            users = users or (data.get("content") if isinstance(data, dict) else None) or []
            for u in users:
                if isinstance(u, dict) and (u.get("email") or "").lower() == email.lower():
                    return u
            if users:
                return users[0]
        except Exception:
            pass
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_print_self(
    file_b64: str,
    filename: str,
    title: str = "",
    target_printer: str = "",
    copies: int = 1,
    pdl: str = "auto",
    color: bool = True,
) -> str:
    """
        Druckt eine Datei in die EIGENE Secure-Print-Queue des aufrufenden MCP-Users.
        Auto-PDL-Conversion (Default: PDF→PCL XL via Ghostscript).

        Wann nutzen — typische User-Prompts:
          • "Druck mir das"
          • "Drucke das fuer mich zur Abholung"
          • "Print this to my queue"
          • "Send this to my own printer"
          • "Erstelle einen Bericht und schick ihn an meinen Drucker"
          • "Generate a PDF and queue it on my printer"

        Wann NICHT — stattdessen:
          • Anderer Empfaenger (E-Mail bekannt)   → printix_send_to_user
          • Mehrere Empfaenger gleichzeitig       → printix_print_to_recipients
          • Datei in Paperless archivieren        → printix_send_to_capture
          • URL statt Base64                      → printix_quick_print

        Returns dict mit:
          - ok:                  bool
          - job_id:              Printix-Job-UUID (→ printix_get_job, _delete_job, _change_job_owner)
          - owner_email:         bestaetigter Besitzer
          - owner_user_id:       Printix-User-UUID
          - filename:            Anzeigename
          - size_input:          Original-Bytes
          - size_after_conversion: Bytes nach Ghostscript
          - pdl:                 tatsaechliches PDL ("PCLXL" | "PCL5" | "POSTSCRIPT")
          - printer_id, queue_id: zur Diagnose
          - next_step:           Hinweis fuer den User

        Args:
          file_b64:        Base64-kodierte Bytes. PDF / PostScript / PCL / Plaintext.
                           Beispiel: AI generiert ein PDF und encodet es vor dem Aufruf.
          filename:        "Bericht_Q1.pdf" — Anzeigename, NICHT Datei-Pfad.
          title:           Job-Titel an der Drucker-Konsole. Default = filename.
          target_printer:  "" (leer) = erster verfuegbarer Drucker
                           "HP M577 Düsseldorf" = Fuzzy-Match (Name/Location/Model)
                           "abc-123:def-456" = printer_id:queue_id direkt aus list_printers
          copies:          1 (Default), 2, 3, ...
          pdl:             "auto" (Default = PCLXL) | "PCLXL" | "PCL5" | "POSTSCRIPT" | "passthrough"
                           passthrough = ohne Ghostscript-Konvertierung; bei PDF auf nicht-RIP-Druckern Hieroglyphen.
          color:           True (Default, pxlcolor) | False (pxlmono)

    """
    import base64 as _b64
    from print_conversion import prepare_for_print, ConversionError
    try:
        c = client()
        # 1) Datei-Bytes
        try:
            file_bytes = _b64.b64decode(file_b64)
        except Exception as e:
            return _ok({"error": f"invalid base64: {e}"})
        if not file_bytes:
            return _ok({"error": "empty file"})

        # 2) Self-User aufloesen
        me = _resolve_self_user(c)
        if not me:
            return _ok({
                "error": "could not resolve self-user",
                "hint": "Tenant.email ist im MCP-Server nicht zu einem Printix-User mappbar. "
                        "Pruefe Settings > Mapping oder nutze printix_send_to_user(user_email=...).",
            })
        my_email = me.get("email") or ""

        # 3) Drucker aufloesen (wie in send_to_user)
        printer_id, queue_id = "", ""
        if ":" in target_printer:
            printer_id, queue_id = target_printer.split(":", 1)
        else:
            pdata = c.list_printers(search=target_printer or None, page=0, size=50)
            plist = pdata.get("printers") or pdata.get("content") or [] if isinstance(pdata, dict) else []
            if not plist:
                return _ok({"error": "no printer found", "query": target_printer})
            printer_id, queue_id = _extract_printer_queue_ids(plist[0])
        if not (printer_id and queue_id):
            return _ok({"error": "could not resolve printer_id/queue_id"})

        # 4) PDL-Detection + Konvertierung
        target_pdl = (pdl or "auto").upper()
        if target_pdl == "AUTO":
            target_pdl = "PCLXL"
        if target_pdl == "PASSTHROUGH":
            target_pdl = "PASSTHROUGH"
        try:
            converted_bytes, final_pdl = prepare_for_print(
                file_bytes, target=target_pdl, color=color)
        except ConversionError as ce:
            return _ok({"error": f"conversion failed: {ce}",
                         "hint": "pdl='passthrough' ueberspringt die Konvertierung — "
                                  "aber PDF wird dann von vielen Druckern als "
                                  "Hieroglyphen gedruckt."})

        # 5) 5-Stage-Submit mit korrektem PDL
        job = c.submit_print_job(printer_id=printer_id, queue_id=queue_id,
                                  title=title or filename, copies=copies,
                                  pdl=final_pdl)
        job_id, upload_url, upload_headers = _extract_job_id_and_upload(job)
        if not (job_id and upload_url):
            return _ok({"error": "submit_print_job missing job_id or upload_url", "raw": job})
        c.upload_file_to_url(upload_url, converted_bytes, extra_headers=upload_headers)
        c.complete_upload(job_id)
        if my_email:
            try:
                c.change_job_owner(job_id, my_email)
            except Exception:
                pass

        return _ok({
            "ok": True,
            "job_id": job_id,
            "owner_email": my_email,
            "owner_user_id": me.get("id", ""),
            "filename": filename,
            "size_input": len(file_bytes),
            "size_after_conversion": len(converted_bytes),
            "pdl": final_pdl,
            "copies": copies,
            "printer_id": printer_id,
            "queue_id": queue_id,
            "next_step": "Job liegt jetzt in der Secure-Print-Queue — am Drucker mit Karte/Code releasen.",
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("print_self failed")
        return _ok({"error": str(e)})


# ─── Phase 1b: send_to_capture + describe_capture_profile ────────────────────

def _resolve_capture_profile(profile: str, tenant_id: str) -> dict | None:
    """Findet ein Capture-Profil per Name ODER UUID (case-insensitive)."""
    import db
    profiles = db.get_capture_profiles_by_tenant(tenant_id) or []
    for p in profiles:
        if str(p.get("id", "")).lower() == profile.lower():
            return p
    for p in profiles:
        if (p.get("name") or "").lower() == profile.lower():
            return p
    return None


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
async def printix_send_to_capture(
    profile: str,
    file_b64: str,
    filename: str,
    metadata_json: str = "{}",
) -> str:
    """
        Schickt eine Datei direkt in einen Capture-Workflow (Paperless/SharePoint/DMS).
        Gleicher Pfad wie ein eingehender Webhook, aber ohne Drucker- oder
        Azure-Blob-Umweg — die Datei geht direkt ins Plugin (z.B. Paperless-API).

        Wann nutzen — typische User-Prompts:
          • "Archiviere das in Paperless"
          • "Speicher den Vertrag im DMS"
          • "File this in Paperless with tags X, Y"
          • "Save this to my document workflow"
          • "Capture diesen Scan und tag mit X"

        Wann NICHT — stattdessen:
          • Datei drucken (statt archivieren)         → printix_print_self / _send_to_user
          • Erst pruefen welche Felder akzeptiert werden → printix_describe_capture_profile
          • Profile auflisten                          → printix_list_capture_profiles

        Returns dict mit:
          - ok:               bool
          - profile:          aufgeloester Profil-Name oder UUID
          - plugin:           "paperless_ngx" | ...
          - filename:         der Name unter dem es im Ziel-System erscheint
          - size:             Bytes
          - result_message:   Plugin-spezifische Antwort (z.B. "Document uploaded HTTP 200")

        Args:
          profile:        Capture-Profil-Name ("Paperless (Marcus)") ODER UUID.
                          Wenn unsicher: vorher printix_list_capture_profiles aufrufen.
          file_b64:       Base64-Bytes der Datei. PDF empfohlen — die meisten DMS-Systeme
                          mögen PDF mehr als PCL/PS.
          filename:       "vertrag_acme_2026-04.pdf" — wird als Original-Dateiname gespeichert.
          metadata_json:  JSON-String mit Plugin-Feldern. Pruefe mit
                          printix_describe_capture_profile welche akzeptiert werden.
                          Paperless-Beispiel:
                            '{"tags":["Q1","Vertrag"], "correspondent":"Acme Corp",
                              "document_type":"Vertrag"}'
                          Default "{}" = nur Defaults aus Profil-Konfig.

    """
    import base64 as _b64
    import json as _json
    try:
        # 1) Profil finden
        tid = _get_card_tenant_id()
        prof = _resolve_capture_profile(profile, tid)
        if not prof:
            return _ok({"error": "capture profile not found", "profile": profile})

        # 2) Datei-Bytes
        try:
            data = _b64.b64decode(file_b64)
        except Exception as e:
            return _ok({"error": f"invalid base64: {e}"})
        if not data:
            return _ok({"error": "empty file"})

        # 3) Metadata
        try:
            meta = _json.loads(metadata_json) if metadata_json else {}
            if not isinstance(meta, dict):
                return _ok({"error": "metadata_json must be a JSON object"})
        except Exception as e:
            return _ok({"error": f"invalid metadata_json: {e}"})

        # 4) Plugin laden
        # WICHTIG: capture.plugins importieren, damit das Auto-Discovery
        # in capture/plugins/__init__.py laeuft und @register_plugin
        # alle Plugins im _PLUGINS-Registry eintraegt. Sonst ist
        # get_plugin_class(...) immer None.
        import capture.plugins  # noqa: F401  triggers auto-discovery
        from capture.base_plugin import get_plugin_class
        plugin_id = prof.get("plugin_type") or prof.get("plugin_id") or ""
        cls = get_plugin_class(plugin_id)
        if not cls:
            return _ok({"error": "plugin not found", "plugin_id": plugin_id})
        plugin = cls(prof.get("config_json") or "{}")
        ok_cfg, err_cfg = plugin.validate_config()
        if not ok_cfg:
            return _ok({"error": f"plugin config invalid: {err_cfg}"})

        # 5) Direct-Ingest — Plugin liefert async, wir sind selbst async
        ok, msg = await plugin.ingest_bytes(data, filename, meta)

        return _ok({
            "ok": bool(ok),
            "profile": prof.get("name") or prof.get("id"),
            "plugin": plugin_id,
            "filename": filename,
            "size": len(data),
            "result_message": msg,
        })
    except Exception as e:
        logger.exception("send_to_capture failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_describe_capture_profile(profile: str) -> str:
    """
        Zeigt das Plugin-Schema eines Capture-Profils — welche metadata-Felder
        erlaubt/erwartet sind, plus aktuelle Konfiguration (Secrets maskiert).
        Diagnose-Tool — vor dem eigentlichen send_to_capture aufrufen, damit
        das Modell das richtige metadata_json baut.

        Wann nutzen — typische User-Prompts:
          • "Was nimmt das Paperless-Profil an Metadaten an?"
          • "Welche Felder erwartet mein DMS-Profil?"
          • "Show me what fields the capture profile accepts"
          • "Schema des Capture-Profils X"

        Wann NICHT — stattdessen:
          • Datei wirklich archivieren                 → printix_send_to_capture
          • Liste aller Profile                        → printix_list_capture_profiles
          • Capture-Server-Status                      → printix_capture_status

        Returns dict mit:
          - profile, plugin_id, plugin_name, plugin_description
          - config_schema:           Liste der Konfig-Felder mit type/required/hint/default
          - current_config:          aktuelle Werte (Tokens/Passwords als "***")
          - supports_direct_ingest:  bool — ob Plugin send_to_capture-faehig ist
          - accepts_metadata_fields: erwartete Index-Felder

        Args:
          profile:  Profil-Name oder UUID. Beispiel "Paperless (Marcus)" oder "d7ab98ac-…".

    """
    try:
        tid = _get_card_tenant_id()
        prof = _resolve_capture_profile(profile, tid)
        if not prof:
            return _ok({"error": "capture profile not found", "profile": profile})

        # Auto-discovery anstossen (siehe Kommentar in send_to_capture)
        import capture.plugins  # noqa: F401
        from capture.base_plugin import get_plugin_class
        plugin_id = prof.get("plugin_type") or prof.get("plugin_id") or ""
        cls = get_plugin_class(plugin_id)
        if not cls:
            return _ok({"error": "plugin not found", "plugin_id": plugin_id})
        plugin = cls(prof.get("config_json") or "{}")

        # Sensible Felder (Token/Password) maskieren
        _SENSITIVE = {"password", "token", "secret", "api_key"}
        cfg_safe: dict = {}
        for k, v in (plugin.config or {}).items():
            if any(s in k.lower() for s in _SENSITIVE):
                cfg_safe[k] = "***" if v else ""
            else:
                cfg_safe[k] = v

        return _ok({
            "profile": prof.get("name") or prof.get("id"),
            "plugin_id": plugin_id,
            "plugin_name": getattr(cls, "plugin_name", plugin_id),
            "plugin_description": getattr(cls, "plugin_description", ""),
            "config_schema": plugin.config_schema(),
            "current_config": cfg_safe,
            "supports_direct_ingest": (
                cls.ingest_bytes is not __import__("capture.base_plugin", fromlist=["CapturePlugin"]).CapturePlugin.ingest_bytes
            ),
            "accepts_metadata_fields": prof.get("index_fields_json") or "[]",
        })
    except Exception as e:
        logger.exception("describe_capture_profile failed")
        return _ok({"error": str(e)})


# ─── Phase 2a: get_group_members + get_user_groups ───────────────────────────

def _follow_hal_link(c: PrintixClient, obj: dict, rel: str) -> Any | None:
    """Folgt einem HAL-Link `_links.<rel>.href` falls vorhanden.
    Returns geparstes JSON oder None."""
    link = (((obj or {}).get("_links") or {}).get(rel) or {}).get("href", "")
    if not link:
        return None
    try:
        # Printix-Client hat keinen "raw GET URL" Helper — wir nutzen requests
        # mit dem Print-API-TM token-manager.
        tm = c._require_tm(c._print_tm, "Print API")
        token = tm.get_token() if hasattr(tm, "get_token") else None
        import requests as _r
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        r = _r.get(link, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        logger.debug("_follow_hal_link(%s) failed: %s", rel, e)
    return None


def _group_members_from_obj(c: PrintixClient, group_obj: dict) -> list[dict]:
    """Extrahiert Mitglieder aus einem get_group-Response. Probiert mehrere
    Felder/Formen, faellt auf HAL-Link `_links.users` zurueck."""
    if not isinstance(group_obj, dict):
        return []
    # Direkt im Group-Objekt?
    for key in ("members", "users", "memberUsers"):
        v = group_obj.get(key)
        if isinstance(v, list) and v and isinstance(v[0], dict):
            return v
    # HAL: _links.users → eigene Sub-Resource
    sub = _follow_hal_link(c, group_obj, "users")
    if isinstance(sub, dict):
        for key in ("users", "content", "members"):
            v = sub.get(key)
            if isinstance(v, list):
                return [u for u in v if isinstance(u, dict)]
    if isinstance(sub, list):
        return [u for u in sub if isinstance(u, dict)]
    return []


def _group_id(g: dict) -> str:
    """Holt die Group-UUID. Printix-API liefert die ID nicht im Body
    sondern nur als HAL-Link `_links.self.href` — Body-`id` ist meist
    None. Wir probieren beides der Reihe nach."""
    if not isinstance(g, dict):
        return ""
    return (g.get("id")
            or _extract_resource_id_from_href(
                ((g.get("_links") or {}).get("self") or {}).get("href", "")
            )
            or "")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_group_members(group_id_or_name: str) -> str:
    """
        Listet alle Mitglieder einer Printix-Gruppe (per UUID oder Anzeigename,
        case-insensitive). Bei mehrdeutigen Namen kommt eine Kandidatenliste.

        Wann nutzen — typische User-Prompts:
          • "Wer ist alles in der Marketing-Gruppe?"
          • "Show me all members of group X"
          • "List users in Sales-DACH"
          • "Wer gehört zur Gruppe …?"

        Wann NICHT — stattdessen:
          • Liste ALLER Gruppen                        → printix_list_groups
          • Gruppen EINES Users                        → printix_get_user_groups
          • Beim Drucken Empfaenger aufloesen          → printix_resolve_recipients
            (akzeptiert "group:Name" als Eingabe)

        Returns dict mit:
          - group:        {id, name}
          - member_count: int
          - members:      Liste {id, email, name, role}
          - note:         Hinweistext wenn API keine Members liefert (z.B. wegen
                          Directory-Sync-Lag im Printix-Admin)

        Args:
          group_id_or_name:  "Marketing-DACH" (case-insensitive Name-Match)
                             ODER "abc-123-uuid" (Group-UUID, ueber 32 Zeichen)

    """
    try:
        c = client()
        # ID-vs-Name Heuristik: UUID hat Bindestriche und ~32 hex-Chars
        gid = ""
        if "-" in group_id_or_name and len(group_id_or_name) >= 32:
            gid = group_id_or_name
        else:
            data = c.list_groups(search=group_id_or_name, page=0, size=100)
            groups = (data.get("groups") if isinstance(data, dict) else None) or \
                     (data.get("content") if isinstance(data, dict) else None) or []
            matches = [g for g in groups
                       if (g.get("name") or "").lower() == group_id_or_name.lower()]
            if not matches:
                return _ok({"error": "group not found", "query": group_id_or_name,
                            "candidates": [{"id": _group_id(g), "name": g.get("name")} for g in groups[:10]]})
            # Bei mehreren Treffern mit gleicher Group-UUID = nur ein Eintrag
            unique_by_id = {}
            for g in matches:
                _gid = _group_id(g)
                if _gid and _gid not in unique_by_id:
                    unique_by_id[_gid] = g
            if len(unique_by_id) > 1:
                return _ok({"error": "ambiguous group name",
                            "candidates": [{"id": _gid, "name": g.get("name")}
                                              for _gid, g in unique_by_id.items()]})
            gid = next(iter(unique_by_id.keys()), "") if unique_by_id else _group_id(matches[0])
        if not gid:
            return _ok({"error": "could not resolve group_id"})

        gobj = c.get_group(gid)
        members = _group_members_from_obj(c, gobj if isinstance(gobj, dict) else {})
        return _ok({
            "group": {
                "id": gid,
                "name": (gobj.get("name") if isinstance(gobj, dict) else ""),
            },
            "member_count": len(members),
            "members": [
                {
                    "id":    u.get("id", ""),
                    "email": u.get("email", ""),
                    "name":  u.get("name") or u.get("displayName") or "",
                    "role":  u.get("role") or u.get("roleType") or "",
                }
                for u in members
            ],
            "note": "" if members else
                    "Keine Mitglieder im API-Response. Printix liefert nicht "
                    "alle Group-Memberships ueber den Public-API-Endpoint — "
                    "fuer vollstaendige Mitgliederlisten ggf. Directory-Sync "
                    "(Entra/AD) im Printix-Admin pruefen.",
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("get_group_members failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_get_user_groups(user_email_or_id: str) -> str:
    """
        Reverse-Lookup: in welchen Gruppen ist User X Mitglied?
        Versucht zuerst das User-Objekt (Felder `groups`/`memberOf`),
        faellt sonst auf einen Gruppen-Scan zurueck (langsamer aber zuverlaessig).

        Wann nutzen — typische User-Prompts:
          • "In welchen Gruppen ist Anna?"
          • "Show me Marcus's group memberships"
          • "Welche Gruppen hat User X?"
          • "Group memberships for alice@firma.de"

        Wann NICHT — stattdessen:
          • Mitglieder EINER Gruppe                    → printix_get_group_members
          • Komplette User-Sicht inkl. Gruppen          → printix_user_360
          • User generell suchen                        → printix_find_user

        Returns dict mit:
          - user:        {id, email}
          - group_count: int
          - groups:      Liste {id, name}
          - method:      "user_object_direct" | "groups_scan"
          - note:        Hinweis bei Performance-Cap (max 50 Gruppen gescannt)

        Args:
          user_email_or_id:  "alice@firma.de" oder Printix-User-UUID.
                             Email wird per _collect_all_users aufgeloest.

    """
    try:
        c = client()
        # 1) User-ID auflösen
        uid = ""
        email = ""
        if "@" in user_email_or_id:
            email = user_email_or_id.lower()
            users = _collect_all_users(c)
            for u in users:
                if (u.get("email") or "").lower() == email:
                    uid = u.get("id", "")
                    break
            if not uid:
                return _ok({"error": "user not found", "query": user_email_or_id})
        else:
            uid = user_email_or_id

        # 2) get_user → direkt aus Feldern
        try:
            uobj = c.get_user(uid)
        except Exception:
            uobj = {}
        direct_groups: list[dict] = []
        for key in ("groups", "memberOf", "memberGroups"):
            v = uobj.get(key) if isinstance(uobj, dict) else None
            if isinstance(v, list) and v and isinstance(v[0], dict):
                direct_groups = v
                break

        if direct_groups:
            return _ok({
                "user": {"id": uid, "email": uobj.get("email", email)},
                "group_count": len(direct_groups),
                "groups": [
                    {"id": _group_id(g), "name": g.get("name", "")}
                    for g in direct_groups
                ],
                "method": "user_object_direct",
            })

        # 3) Fallback: alle Gruppen scannen, Membership pruefen
        gdata = c.list_groups(page=0, size=200)
        groups = (gdata.get("groups") if isinstance(gdata, dict) else None) or \
                 (gdata.get("content") if isinstance(gdata, dict) else None) or []
        matched: list[dict] = []
        for g in groups[:50]:  # safety cap
            try:
                gid = _group_id(g)
                if not gid:
                    continue
                gobj = c.get_group(gid)
                members = _group_members_from_obj(c, gobj if isinstance(gobj, dict) else {})
                if any((m.get("id") or "") == uid or
                       (m.get("email") or "").lower() == email for m in members):
                    matched.append({"id": gid, "name": g.get("name", "")})
            except Exception:
                continue

        return _ok({
            "user": {"id": uid, "email": email},
            "group_count": len(matched),
            "groups": matched,
            "method": "groups_scan",
            "note": (
                "Fallback-Methode: alle Gruppen durchgegangen. Nur die "
                "ersten 50 Gruppen wurden gescannt (Performance-Cap)."
                if len(groups) > 50 else ""
            ),
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("get_user_groups failed")
        return _ok({"error": str(e)})


# ─── Phase 2b: resolve_recipients ────────────────────────────────────────────

def _resolve_recipients_internal(c: PrintixClient,
                                  recipients: list[str]) -> dict:
    """Loest eine gemischte Liste (Emails, group:Name, entra:OID, upn:UPN)
    zu einer flachen User-Liste auf.

    Returns dict mit keys:
      - resolved:   list[{user_id, email, name, source}]
      - not_found:  list[str] — was nicht aufloesbar war
      - ambiguous:  list[{input, candidates}]
    """
    out: list[dict] = []
    not_found: list[str] = []
    ambiguous: list[dict] = []
    seen_ids: set[str] = set()

    def _add(u: dict, source: str) -> None:
        uid = u.get("id", "")
        if uid and uid not in seen_ids:
            seen_ids.add(uid)
            out.append({
                "user_id": uid,
                "email":   u.get("email", ""),
                "name":    u.get("name") or u.get("displayName") or "",
                "source":  source,
            })

    all_users_cache: list[dict] | None = None

    def _all_users() -> list[dict]:
        nonlocal all_users_cache
        if all_users_cache is None:
            all_users_cache = _collect_all_users(c)
        return all_users_cache

    for raw in recipients:
        item = (raw or "").strip()
        if not item:
            continue

        # group:Name → Mitglieder einer Printix-Gruppe
        if item.lower().startswith("group:"):
            name = item.split(":", 1)[1].strip()
            try:
                gdata = c.list_groups(search=name, page=0, size=50)
                groups = (gdata.get("groups") if isinstance(gdata, dict) else None) or \
                         (gdata.get("content") if isinstance(gdata, dict) else None) or []
                matches = [g for g in groups if (g.get("name") or "").lower() == name.lower()]
                if not matches:
                    not_found.append(item)
                    continue
                # Dedupliziere ueber tatsaechliche Group-UUID (Printix
                # liefert id=null im Body, ID nur in _links.self.href).
                unique_by_id: dict[str, dict] = {}
                for g in matches:
                    _gid = _group_id(g)
                    if _gid and _gid not in unique_by_id:
                        unique_by_id[_gid] = g
                if len(unique_by_id) > 1:
                    ambiguous.append({
                        "input": item,
                        "candidates": [{"id": _gid, "name": g.get("name")}
                                         for _gid, g in unique_by_id.items()],
                    })
                    continue
                if not unique_by_id:
                    not_found.append(item + " (no resolvable group_id)")
                    continue
                gid = next(iter(unique_by_id.keys()))
                gobj = c.get_group(gid)
                members = _group_members_from_obj(c, gobj if isinstance(gobj, dict) else {})
                if not members:
                    not_found.append(item + " (group has no members in API)")
                    continue
                for m in members:
                    _add(m, source=item)
            except Exception as e:
                logger.debug("group:%s resolve failed: %s", name, e)
                not_found.append(item)
            continue

        # entra:OID → MS-Graph Membership-Lookup, Email-Match in Printix
        if item.lower().startswith("entra:"):
            oid = item.split(":", 1)[1].strip()
            members = _entra_group_members(oid)
            if members is None:
                not_found.append(item + " (graph-call failed)")
                continue
            if not members:
                not_found.append(item + " (entra group empty)")
                continue
            users = _all_users()
            email_idx = {(u.get("email") or "").lower(): u for u in users if u.get("email")}
            for m in members:
                em = (m.get("mail") or m.get("userPrincipalName") or "").lower()
                if em and em in email_idx:
                    _add(email_idx[em], source=item)
            continue

        # upn:foo@bar → Email-Match
        if item.lower().startswith("upn:"):
            em = item.split(":", 1)[1].strip().lower()
            users = _all_users()
            for u in users:
                if (u.get("email") or "").lower() == em:
                    _add(u, source=item)
                    break
            else:
                not_found.append(item)
            continue

        # Default: als Email behandeln (mit oder ohne @)
        if "@" in item:
            users = _all_users()
            for u in users:
                if (u.get("email") or "").lower() == item.lower():
                    _add(u, source=item)
                    break
            else:
                not_found.append(item)
        else:
            # Letzter Versuch: Name-Suche
            try:
                data = c.list_users(query=item, page=0, page_size=10)
                users = (data.get("users") if isinstance(data, dict) else None) or \
                        (data.get("content") if isinstance(data, dict) else None) or []
                exact = [u for u in users
                         if (u.get("name") or u.get("displayName") or "").lower() == item.lower()]
                if exact:
                    _add(exact[0], source=item)
                elif len(users) > 1:
                    ambiguous.append({
                        "input": item,
                        "candidates": [{"id": u.get("id"), "email": u.get("email"),
                                          "name": u.get("name") or u.get("displayName")}
                                         for u in users[:5]],
                    })
                elif users:
                    _add(users[0], source=item)
                else:
                    not_found.append(item)
            except Exception:
                not_found.append(item)

    return {
        "resolved":  out,
        "not_found": not_found,
        "ambiguous": ambiguous,
    }


def _entra_group_members(group_oid: str) -> list[dict] | None:
    """MS-Graph: GET /groups/{id}/members. Nutzt entra.get_admin_token oder
    schlaegt sauber fehl wenn Entra nicht konfiguriert ist."""
    try:
        from entra import get_config
    except ImportError:
        return None
    cfg = get_config()
    if not (cfg.get("enabled") and cfg.get("client_id") and cfg.get("client_secret")):
        return None
    tenant = cfg.get("tenant_id") or "common"
    # Client-Credentials-Flow fuer App-only Graph-Zugriff
    import requests as _r
    try:
        token_resp = _r.post(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data={
                "client_id":     cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "grant_type":    "client_credentials",
                "scope":         "https://graph.microsoft.com/.default",
            },
            timeout=15,
        )
        if token_resp.status_code != 200:
            logger.warning("Entra app-token fail: %s %s",
                            token_resp.status_code, token_resp.text[:300])
            return None
        access = token_resp.json().get("access_token", "")
    except Exception as e:
        logger.warning("Entra app-token exception: %s", e)
        return None

    members: list[dict] = []
    url = f"https://graph.microsoft.com/v1.0/groups/{group_oid}/members?$top=200"
    pages = 0
    while url and pages < 20:
        try:
            r = _r.get(url, headers={"Authorization": f"Bearer {access}"}, timeout=20)
            if r.status_code != 200:
                logger.warning("Graph /groups/.../members fail: %s %s",
                                r.status_code, r.text[:300])
                return None
            j = r.json()
            for m in j.get("value", []):
                if isinstance(m, dict):
                    members.append(m)
            url = j.get("@odata.nextLink", "")
            pages += 1
        except Exception as e:
            logger.warning("Graph members page exception: %s", e)
            break
    return members


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_resolve_recipients(recipients_csv: str) -> str:
    """
        Diagnose-Tool: loest eine gemischte Empfaengerliste zu einer flachen
        Printix-User-Liste auf — OHNE zu drucken. Vor printix_print_to_recipients
        nutzen um zu pruefen wer wirklich angeschrieben wird.

        Wann nutzen — typische User-Prompts:
          • "An wen wuerde das gehen?"
          • "Resolve these recipients first: …"
          • "Wieviele User sind in group:X?"
          • "Pruef vorher die Empfaenger"
          • "Show me who's in this list"

        Wann NICHT — stattdessen:
          • Tatsaechlich drucken                       → printix_print_to_recipients
          • Nur EINE Group-Membership pruefen          → printix_get_group_members
          • User suchen                                 → printix_find_user

        Returns dict mit:
          - input_count, resolved_count: Eingabe vs Treffer
          - resolved:    Liste {user_id, email, name, source}
          - not_found:   Liste der nicht aufloesbaren Eingaben
          - ambiguous:   Liste {input, candidates} bei Mehrdeutigkeit (z.B. zwei Gruppen
                         mit gleichem Namen)

        Args:
          recipients_csv:  Komma-getrennte Liste mit gemischten Eingaben:
                           "alice@firma.de"          (Email-Lookup)
                           "group:Marketing-DACH"    (Printix-Gruppe → Members)
                           "entra:abc-uuid"          (Entra-/AD-Gruppe via Graph API)
                           "upn:alice@firma.de"      (forciert UPN-Match)
                           "Alice Mueller"           (Name-Suche)
                           Beispiel: "alice@firma.de, group:Sales, entra:abc-123"

    """
    try:
        items = [x.strip() for x in (recipients_csv or "").split(",") if x.strip()]
        if not items:
            return _ok({"error": "no recipients given"})
        result = _resolve_recipients_internal(client(), items)
        result["input_count"] = len(items)
        result["resolved_count"] = len(result["resolved"])
        return _ok(result)
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("resolve_recipients failed")
        return _ok({"error": str(e)})


# ─── Phase 2c: print_to_recipients ───────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_print_to_recipients(
    recipients_csv: str,
    file_b64: str,
    filename: str,
    target_printer: str = "",
    copies: int = 1,
    fail_on_unresolved: bool = True,
    pdl: str = "auto",
    color: bool = True,
) -> str:
    """
        Druckt EIN Dokument als individuelle Secure-Print-Jobs an MEHRERE
        Empfaenger. Jeder bekommt einen eigenen Job in seiner Queue. Konvertiert
        PDF/PS/Text einmalig vor der Schleife (spart Ghostscript-Calls).

        Wann nutzen — typische User-Prompts:
          • "Schick das an alle aus Marketing"
          • "Send this PDF to Marcus and Anna"
          • "An mehrere Leute drucken"
          • "Distribute this to group X"
          • "Print this for everyone in Sales-DACH"

        Wann NICHT — stattdessen:
          • Nur EIN Empfaenger                          → printix_send_to_user
          • An sich selbst                              → printix_print_self
          • Vorab pruefen wer aufgeloest wird           → printix_resolve_recipients
          • Ein gemeinsamer Pickup-Job (statt N)        → nicht implementiert; jeder
                                                           bekommt seinen eigenen Job

        Returns dict mit:
          - ok:                bool (true wenn ALLE submissions erfolgreich)
          - summary.input_count, resolved_count, submitted_count, failed_count
          - summary.not_found, ambiguous: Diagnose-Listen
          - filename, size_input, size_after_conversion, pdl
          - results:           Liste pro Empfaenger {recipient, user_id, ok, job_id|error}

        Args:
          recipients_csv:        Wie printix_resolve_recipients
                                 ("alice@firma.de, group:Marketing, entra:oid").
          file_b64:              Base64-Datei. PDF empfohlen.
          filename:              Anzeigename (gilt fuer alle Jobs).
          target_printer:        Wie in printix_print_self.
          copies:                Kopien PRO EMPFAENGER.
          fail_on_unresolved:    True (Default!) = abbrechen wenn unaufloesbar.
                                 False = nur die aufloesbaren drucken (best effort).
          pdl, color:            Wie in printix_print_self.

    """
    import base64 as _b64
    from print_conversion import prepare_for_print, ConversionError
    try:
        c = client()
        items = [x.strip() for x in (recipients_csv or "").split(",") if x.strip()]
        if not items:
            return _ok({"error": "no recipients given"})

        # 1) Datei
        try:
            file_bytes = _b64.b64decode(file_b64)
        except Exception as e:
            return _ok({"error": f"invalid base64: {e}"})
        if not file_bytes:
            return _ok({"error": "empty file"})

        # 1b) PDL-Detection + Konvertierung (einmalig fuer alle Empfaenger)
        target_pdl = (pdl or "auto").upper()
        if target_pdl == "AUTO":
            target_pdl = "PCLXL"
        try:
            converted_bytes, final_pdl = prepare_for_print(
                file_bytes, target=target_pdl, color=color)
        except ConversionError as ce:
            return _ok({"error": f"conversion failed: {ce}"})

        # 2) Empfaenger aufloesen
        resolved = _resolve_recipients_internal(c, items)
        users = resolved["resolved"]
        if fail_on_unresolved and (resolved["not_found"] or resolved["ambiguous"]):
            return _ok({
                "error": "unresolved recipients (set fail_on_unresolved=false to ignore)",
                **resolved,
            })
        if not users:
            return _ok({"error": "no recipients resolved", **resolved})

        # 3) Printer auflösen — einmal, fuer alle
        printer_id, queue_id = "", ""
        if ":" in target_printer:
            printer_id, queue_id = target_printer.split(":", 1)
        else:
            pdata = c.list_printers(search=target_printer or None, page=0, size=50)
            plist = pdata.get("printers") or pdata.get("content") or [] if isinstance(pdata, dict) else []
            if not plist:
                return _ok({"error": "no printer found", "query": target_printer})
            printer_id, queue_id = _extract_printer_queue_ids(plist[0])
        if not (printer_id and queue_id):
            return _ok({"error": "could not resolve printer_id/queue_id"})

        # 4) Pro Empfaenger: 5-Stage-Submit
        results: list[dict] = []
        for u in users:
            email = u.get("email") or ""
            try:
                job = c.submit_print_job(printer_id=printer_id, queue_id=queue_id,
                                          title=filename, copies=copies,
                                          pdl=final_pdl)
                job_id, upload_url, upload_headers = _extract_job_id_and_upload(job)
                if not (job_id and upload_url):
                    results.append({"recipient": email, "ok": False,
                                     "error": "no job_id/upload_url in response"})
                    continue
                c.upload_file_to_url(upload_url, converted_bytes, extra_headers=upload_headers)
                c.complete_upload(job_id)
                if email:
                    try:
                        c.change_job_owner(job_id, email)
                    except Exception as ce:
                        logger.warning("change_owner failed for %s: %s", email, ce)
                results.append({"recipient": email, "user_id": u.get("user_id"),
                                 "ok": True, "job_id": job_id})
            except Exception as e:
                results.append({"recipient": email, "ok": False, "error": str(e)[:300]})

        ok_count = sum(1 for r in results if r.get("ok"))
        return _ok({
            "ok": ok_count == len(users),
            "summary": {
                "input_count": len(items),
                "resolved_count": len(users),
                "submitted_count": ok_count,
                "failed_count": len(users) - ok_count,
                "not_found": resolved["not_found"],
                "ambiguous": resolved["ambiguous"],
            },
            "filename": filename,
            "size_input": len(file_bytes),
            "size_after_conversion": len(converted_bytes),
            "pdl": final_pdl,
            "printer_id": printer_id,
            "queue_id": queue_id,
            "results": results,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("print_to_recipients failed")
        return _ok({"error": str(e)})


# ─── Phase 3a: Time-Bomb engine ──────────────────────────────────────────────

def _ensure_timebomb_table() -> None:
    """Idempotente Schema-Erweiterung. Wird beim ersten Tool-Aufruf gerufen."""
    import db
    with db._conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS user_timebombs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id     TEXT NOT NULL,
                user_id       TEXT NOT NULL,
                user_email    TEXT NOT NULL DEFAULT '',
                bomb_type     TEXT NOT NULL,
                trigger_at    TEXT NOT NULL,
                action_json   TEXT NOT NULL DEFAULT '{}',
                status        TEXT NOT NULL DEFAULT 'pending',
                created_at    TEXT NOT NULL,
                resolved_at   TEXT,
                last_message  TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_timebomb_pending
                ON user_timebombs (status, trigger_at);
            CREATE INDEX IF NOT EXISTS idx_timebomb_user
                ON user_timebombs (tenant_id, user_id);
        """)


def _check_timebomb_condition(c: PrintixClient, bomb: dict) -> bool:
    """Prueft ob die Bedingung der Bombe noch erfuellt ist (also: muss
    sie wirklich zuenden?). Default-Logik:
      - first_print_reminder: feuert nur wenn User noch keinen Print-Job hat
      - card_enrol: feuert nur wenn keine Karte enrolled ist
      - generic: feuert immer
    """
    bomb_type = bomb.get("bomb_type", "")
    user_id = bomb.get("user_id", "")
    if bomb_type == "first_print_reminder":
        try:
            jobs = c.list_print_jobs(page=0, size=10)
            items = (jobs.get("jobs") if isinstance(jobs, dict) else None) or \
                    (jobs.get("content") if isinstance(jobs, dict) else None) or []
            for j in items:
                owner = (j.get("ownerId") or
                          (((j.get("_links") or {}).get("owner") or {}).get("href", "")).rsplit("/", 1)[-1])
                if owner == user_id:
                    return False  # Schon gedruckt → Bombe entschaerfen
        except Exception:
            pass
        return True
    if bomb_type == "card_enrol":
        try:
            data = c.list_user_cards(user_id=user_id)
            items = _card_items(data)
            if items:
                return False
        except Exception:
            pass
        return True
    return True  # generic: immer feuern


def _execute_timebomb(c: PrintixClient, bomb: dict) -> tuple[bool, str]:
    """Fuehrt die Action der Bombe aus.

    Action-JSON-Felder:
      - "kind": "print_reminder" | "log" | "noop"
      - "filename": optional, bei print_reminder
      - "file_b64": optional, bei print_reminder
      - "message":  optional, fuer log
    """
    import json as _json
    try:
        action = _json.loads(bomb.get("action_json") or "{}")
    except Exception:
        action = {}
    kind = action.get("kind", "noop")
    if kind == "print_reminder":
        # Generischer Reminder-Druck — neutraler Text, ein Tipp den User
        # zu engagieren. Bytes erzeugen wir on-the-fly via einem Mini-PDF
        # falls keine eigene Datei mitgegeben wurde.
        file_b64 = action.get("file_b64", "")
        filename = action.get("filename") or "reminder.pdf"
        if not file_b64:
            file_b64 = _generate_reminder_pdf_b64(
                title=action.get("title", "Reminder"),
                body=action.get("body", "Dies ist eine automatische Erinnerung."),
            )
        try:
            r = printix_print_self(file_b64=file_b64, filename=filename,
                                     title=action.get("title", "Reminder"))
            return True, f"reminder sent: {r[:200]}"
        except Exception as e:
            return False, f"reminder failed: {e}"
    if kind == "log":
        return True, action.get("message", "noop")
    return True, "noop"


def _generate_reminder_pdf_b64(title: str, body: str) -> str:
    """Erzeugt ein minimales A4-PDF ohne externe Dependencies. Reines
    PDF-1.4-Skeleton mit einem Helvetica-Textblock. ~700 Bytes."""
    import base64 as _b64
    safe_title = (title or "").replace("(", "").replace(")", "")[:80]
    safe_body  = (body or "").replace("(", "").replace(")", "")[:400]
    content = (
        f"BT /F1 18 Tf 72 750 Td ({safe_title}) Tj ET "
        f"BT /F1 11 Tf 72 720 Td ({safe_body}) Tj ET"
    )
    objs = [
        b"1 0 obj<< /Type /Catalog /Pages 2 0 R >>endobj\n",
        b"2 0 obj<< /Type /Pages /Kids [3 0 R] /Count 1 >>endobj\n",
        b"3 0 obj<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>endobj\n",
        f"4 0 obj<< /Length {len(content)} >>stream\n{content}\nendstream endobj\n".encode(),
        b"5 0 obj<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>endobj\n",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for o in objs:
        offsets.append(len(out))
        out += o
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (b"trailer<< /Size 6 /Root 1 0 R >>\nstartxref\n"
             + str(xref_pos).encode() + b"\n%%EOF\n")
    return _b64.b64encode(out).decode("ascii")


def _run_timebomb_tick() -> dict:
    """Wird vom Scheduler regelmaessig aufgerufen. Geht alle pending Bomben
    durch deren trigger_at <= now ist, prueft die Bedingung, fuehrt Action
    aus, markiert den Eintrag als 'fired'/'defused'/'error'.

    WICHTIG: Diese Funktion wird OHNE current_tenant-Context aufgerufen
    (Cron-Job). Wir muessen pro Bombe den Tenant explizit setzen.
    """
    from datetime import datetime, timezone
    import db
    _ensure_timebomb_table()
    now_iso = datetime.now(timezone.utc).isoformat()
    summary = {"checked": 0, "fired": 0, "defused": 0, "errors": 0}
    with db._conn() as conn:
        rows = conn.execute(
            "SELECT * FROM user_timebombs "
            "WHERE status = 'pending' AND trigger_at <= ? "
            "ORDER BY trigger_at ASC LIMIT 200",
            (now_iso,),
        ).fetchall()
    for row in rows:
        summary["checked"] += 1
        bomb = dict(row)
        # Tenant-Kontext setzen
        try:
            tenants = db.get_tenants_by_id(bomb["tenant_id"]) \
                       if hasattr(db, "get_tenants_by_id") else None
            tenant = tenants if isinstance(tenants, dict) else None
        except Exception:
            tenant = None
        if not tenant:
            try:
                with db._conn() as c2:
                    r2 = c2.execute(
                        "SELECT * FROM tenants WHERE id = ?", (bomb["tenant_id"],)
                    ).fetchone()
                    tenant = dict(r2) if r2 else None
            except Exception:
                tenant = None
        if not tenant:
            with db._conn() as c2:
                c2.execute(
                    "UPDATE user_timebombs SET status='error', resolved_at=?, last_message=? WHERE id=?",
                    (now_iso, "tenant not found", bomb["id"]),
                )
            summary["errors"] += 1
            continue
        ctx_tok = current_tenant.set(tenant)
        try:
            cli = client()
            still_active = _check_timebomb_condition(cli, bomb)
            if not still_active:
                with db._conn() as c2:
                    c2.execute(
                        "UPDATE user_timebombs SET status='defused', resolved_at=?, last_message=? WHERE id=?",
                        (now_iso, "condition no longer matches", bomb["id"]),
                    )
                summary["defused"] += 1
                continue
            ok, msg = _execute_timebomb(cli, bomb)
            with db._conn() as c2:
                c2.execute(
                    "UPDATE user_timebombs SET status=?, resolved_at=?, last_message=? WHERE id=?",
                    ("fired" if ok else "error", now_iso, msg[:500], bomb["id"]),
                )
            if ok:
                summary["fired"] += 1
            else:
                summary["errors"] += 1
        except Exception as e:
            with db._conn() as c2:
                c2.execute(
                    "UPDATE user_timebombs SET status='error', resolved_at=?, last_message=? WHERE id=?",
                    (now_iso, str(e)[:500], bomb["id"]),
                )
            summary["errors"] += 1
        finally:
            current_tenant.reset(ctx_tok)
    return summary


def _ensure_timebomb_scheduler() -> None:
    """Registriert einen stuendlichen APScheduler-Job, wenn der Reporting-
    Scheduler aktiv ist. Idempotent (rufender Code prueft ob Job existiert)."""
    try:
        from reporting.scheduler import _scheduler  # type: ignore
    except Exception:
        return
    if _scheduler is None or not getattr(_scheduler, "running", False):
        return
    if _scheduler.get_job("timebomb_tick"):
        return
    try:
        from apscheduler.triggers.cron import CronTrigger  # type: ignore
        _scheduler.add_job(
            _run_timebomb_tick,
            trigger=CronTrigger(minute=7),  # einmal pro Stunde, Minute 7
            id="timebomb_tick",
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger.info("Timebomb-Scheduler registriert (cron: minute=7).")
    except Exception as e:
        logger.warning("Timebomb-Scheduler-Registrierung fehlgeschlagen: %s", e)


# ─── Phase 3b: welcome_user + list/defuse timebombs ──────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_welcome_user(
    user_email: str,
    template: str = "default",
    auto_print_to_self: bool = True,
    timebombs: str = "card_enrol_7d,first_print_reminder_3d",
) -> str:
    """
        Onboarding-Begleiter fuer einen frisch angelegten Printix-User: erzeugt
        ein Welcome-PDF, optional in dessen Secure-Print-Queue, und scharft
        Time-Bombs (verzoegerte Auto-Reminder mit Bedingungs-Check).

        Wann nutzen — typische User-Prompts:
          • "Onboard Peter mit Welcome-Workflow"
          • "Set up new user with reminders"
          • "Mach das Welcome-Paket fuer Neuen X"
          • "Welcome flow fuer marcus@firma.de"

        Wann NICHT — stattdessen:
          • User erst anlegen                           → printix_onboard_user / _create_user
          • Time-Bombs anschauen                        → printix_list_timebombs
          • Time-Bomb manuell deaktivieren              → printix_defuse_timebomb

        Returns dict mit:
          - ok:               bool
          - user:             {id, email, name}
          - welcome_print:    Resultat des send_to_user oder None bei auto_print_to_self=False
          - timebombs_armed:  Liste {id, type, spec, trigger_at}
          - next_steps:       Hinweistexte

        Args:
          user_email:         Email des User (muss bereits in Printix existieren).
          template:           "default" (aktuell nur dieser).
          auto_print_to_self: True (Default) = Welcome-PDF in seine Queue;
                              False = nur Time-Bombs anlegen, kein Druckjob.
          timebombs:          CSV-Liste der Bomben-Typen, default
                              "card_enrol_7d,first_print_reminder_3d".
                              Verfuegbar:
                                card_enrol_7d           — 7d Reminder ohne Karte
                                first_print_reminder_3d — 3d Reminder ohne ersten Druck
                                card_enrol_30d          — 30d Final-Reminder

    """
    import json as _json
    from datetime import datetime, timezone, timedelta
    try:
        c = client()
        users = _collect_all_users(c)
        match = next((u for u in users
                       if (u.get("email") or "").lower() == user_email.lower()), None)
        if not match:
            return _ok({"error": "user not found", "email": user_email})
        user_id = match.get("id", "")
        display = match.get("name") or match.get("displayName") or user_email

        # Welcome-PDF
        welcome_b64 = _generate_reminder_pdf_b64(
            title=f"Willkommen, {display}!",
            body=("Dein Printix-Account ist bereit. Naechste Schritte: "
                  "1) Mobile-App installieren (TestFlight) "
                  "2) NFC-Karte am Phone enrollen "
                  "3) Erster Job per Share-Sheet senden."),
        )
        printed_job: dict | None = None
        if auto_print_to_self:
            # Wir tunen current_tenant kurz NICHT — printix_print_self loest
            # den Self-User aus dem Tenant; hier wollen wir aber an den
            # *neuen* User schicken. Also direkt send_to_user.
            try:
                r_str = printix_send_to_user(user_email=user_email,
                                              file_content_b64=welcome_b64,
                                              filename="welcome.pdf")
                printed_job = _json.loads(r_str)
            except Exception as e:
                printed_job = {"error": str(e)}

        # Time-Bombs setzen
        _ensure_timebomb_table()
        _ensure_timebomb_scheduler()
        tid = _get_card_tenant_id()
        now = datetime.now(timezone.utc)
        created: list[dict] = []
        bomb_specs = {
            "card_enrol_7d": {
                "type": "card_enrol", "delta": timedelta(days=7),
                "action": {"kind": "print_reminder", "filename": "card_reminder.pdf",
                            "title": "Karte noch nicht registriert",
                            "body": ("Du hast deine ID-Karte noch nicht enrolled. "
                                     "Tap mit der Karte ans iPhone (NFC) und folge "
                                     "den Anweisungen in der Printix-App.")},
            },
            "first_print_reminder_3d": {
                "type": "first_print_reminder", "delta": timedelta(days=3),
                "action": {"kind": "print_reminder", "filename": "first_print.pdf",
                            "title": "Bereit fuer den ersten Druck?",
                            "body": ("Wir haben noch keinen Druckjob von dir gesehen. "
                                     "Probier's am besten mit der Mobile-App oder dem "
                                     "Desktop-Send-Tool aus dem Mitarbeiter-Portal.")},
            },
            "card_enrol_30d": {
                "type": "card_enrol", "delta": timedelta(days=30),
                "action": {"kind": "print_reminder", "filename": "card_final.pdf",
                            "title": "Letzte Erinnerung: Karte enrollen",
                            "body": ("Vor 30 Tagen wurde dein Account angelegt. "
                                     "Bitte enrole deine ID-Karte fuer Secure-Print.")},
            },
        }
        for spec_name in [s.strip() for s in (timebombs or "").split(",") if s.strip()]:
            spec = bomb_specs.get(spec_name)
            if not spec:
                continue
            trigger_at = (now + spec["delta"]).isoformat()
            with __import__("db")._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO user_timebombs "
                    "(tenant_id, user_id, user_email, bomb_type, trigger_at, "
                    " action_json, created_at) VALUES (?,?,?,?,?,?,?)",
                    (tid, user_id, user_email, spec["type"], trigger_at,
                     _json.dumps(spec["action"]), now.isoformat()),
                )
                bomb_id = cur.lastrowid
            created.append({
                "id": bomb_id, "type": spec["type"], "spec": spec_name,
                "trigger_at": trigger_at,
            })

        return _ok({
            "ok": True,
            "user": {"id": user_id, "email": user_email, "name": display},
            "welcome_print": printed_job,
            "timebombs_armed": created,
            "next_steps": [
                "User informieren (Welcome-Mail laeuft via Onboarding separat).",
                "User druckt erstes Dokument → first_print_reminder defused sich automatisch.",
                "User enrolled Karte → card_enrol_* defused sich automatisch.",
            ],
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("welcome_user failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_list_timebombs(
    user_email: str = "",
    status: str = "pending",
) -> str:
    """
        Listet aktive (oder historische) Time-Bombs des Tenants — verzoegerte
        Auto-Reminder die mit dem Onboarding (welcome_user) oder Session-Print
        angelegt werden.

        Wann nutzen — typische User-Prompts:
          • "Welche Reminder sind gerade aktiv?"
          • "Show me pending timebombs"
          • "Was steht fuer Anna noch aus?"
          • "List active timebombs"

        Wann NICHT — stattdessen:
          • Bombe entschaerfen                          → printix_defuse_timebomb
          • Neue Bombe anlegen (per Onboarding)         → printix_welcome_user
          • Session-Print mit Auto-Expire               → printix_session_print

        Returns dict mit:
          - count:     int
          - timebombs: Liste {id, tenant_id, user_id, user_email, bomb_type,
                              trigger_at, action_json, status, created_at,
                              resolved_at, last_message}

        Args:
          user_email:  Optional auf einen User filtern. Leer = alle.
          status:      "pending" (Default) | "fired" | "defused" | "error" | "all".

    """
    try:
        _ensure_timebomb_table()
        import db
        tid = _get_card_tenant_id()
        sql = "SELECT * FROM user_timebombs WHERE tenant_id = ?"
        params: list = [tid]
        if status and status != "all":
            sql += " AND status = ?"
            params.append(status)
        if user_email:
            sql += " AND lower(user_email) = ?"
            params.append(user_email.lower())
        sql += " ORDER BY trigger_at ASC LIMIT 500"
        with db._conn() as conn:
            rows = [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]
        return _ok({"count": len(rows), "timebombs": rows})
    except Exception as e:
        logger.exception("list_timebombs failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_defuse_timebomb(bomb_id: int, reason: str = "manual") -> str:
    """
        Markiert eine geplante Time-Bomb als "defused" (deaktiviert), ohne ihre
        Action auszufuehren. Tenant-Filter aktiv — nur eigene Bomben.

        Wann nutzen — typische User-Prompts:
          • "Stell die Erinnerung fuer Anna ab"
          • "Defuse timebomb 42"
          • "Anna ist im Urlaub, deaktivier die Reminder"
          • "Cancel the reminder for user X"

        Wann NICHT — stattdessen:
          • Erst gucken welche Bomben da sind           → printix_list_timebombs

        Returns dict mit:
          - ok:        bool (false wenn ID nicht im Tenant)
          - bomb_id:   numerische ID
          - status:    "defused"

        Args:
          bomb_id:  Numerische ID aus printix_list_timebombs.
          reason:   Freitext fuer Audit-Trail. Default "manual".
                    Beispiel: "User im Urlaub bis 2026-05-12"

    """
    from datetime import datetime, timezone
    try:
        _ensure_timebomb_table()
        import db
        tid = _get_card_tenant_id()
        now_iso = datetime.now(timezone.utc).isoformat()
        with db._conn() as conn:
            cur = conn.execute(
                "UPDATE user_timebombs SET status='defused', resolved_at=?, "
                "last_message=? WHERE id = ? AND tenant_id = ?",
                (now_iso, f"manual:{reason}", bomb_id, tid),
            )
            count = cur.rowcount
        if count == 0:
            return _ok({"error": "bomb not found or not in this tenant",
                         "bomb_id": bomb_id})
        return _ok({"ok": True, "bomb_id": bomb_id, "status": "defused"})
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Phase 3c: sync_entra_group_to_printix ───────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_sync_entra_group_to_printix(
    entra_group_oid: str,
    printix_group_id: str = "",
    sync_mode: str = "report_only",
) -> str:
    """
        Pulled Mitglieder einer Entra-/AD-Gruppe via Microsoft Graph (App-Permission
        Group.Read.All) und vergleicht sie mit einer Printix-Gruppe. Default
        `report_only` — keine Schreib-Operationen ohne Vorlauf.

        Wann nutzen — typische User-Prompts:
          • "Sync die Entra-Gruppe X mit Printix"
          • "Compare Entra group to Printix"
          • "Was muesste man synchronisieren?"
          • "Show diff between Azure AD and Printix"

        Wann NICHT — stattdessen:
          • Nur Printix-Members anschauen                → printix_get_group_members
          • Group ohne AD-Bezug                          → printix_create_group / _delete_group

        Returns dict mit:
          - entra_group_oid, printix_group_id
          - entra_member_count, printix_member_count
          - to_add:    Liste Emails (in Entra, nicht in Printix)
          - to_remove: Liste Emails (in Printix, nicht in Entra)
          - sync_mode: "report_only" | "additive" | "mirror"
          - note:      Hinweis dass write paths derzeit nicht implementiert sind

        Args:
          entra_group_oid:   Microsoft-Graph Group-Object-ID (UUID).
                             Format: "abc12345-..."
          printix_group_id:  Ziel-Printix-Group-UUID. Leer = nicht implementiert
                             (Auto-Resolve by Name kommt spaeter).
          sync_mode:         "report_only" (Default!) — nur Diff zeigen
                             "additive"   — fehlende User adden (best-effort)
                             "mirror"     — additive + extras entfernen

    """
    try:
        c = client()
        entra_members = _entra_group_members(entra_group_oid)
        if entra_members is None:
            return _ok({
                "error": "graph call failed — Entra not configured or "
                         "Group.Read.All app-permission missing",
            })
        entra_emails = {(m.get("mail") or m.get("userPrincipalName") or "").lower()
                         for m in entra_members if (m.get("mail") or m.get("userPrincipalName"))}

        # Printix-Gruppe finden
        if not printix_group_id:
            return _ok({
                "error": "printix_group_id required (auto-resolve "
                         "by name not yet implemented)",
                "entra_member_count": len(entra_emails),
            })
        gobj = c.get_group(printix_group_id)
        printix_members = _group_members_from_obj(c, gobj if isinstance(gobj, dict) else {})
        printix_emails = {(u.get("email") or "").lower()
                           for u in printix_members if u.get("email")}

        to_add = sorted(entra_emails - printix_emails)
        to_remove = sorted(printix_emails - entra_emails)

        result = {
            "entra_group_oid": entra_group_oid,
            "printix_group_id": printix_group_id,
            "entra_member_count": len(entra_emails),
            "printix_member_count": len(printix_emails),
            "to_add":    to_add,
            "to_remove": to_remove,
            "sync_mode": sync_mode,
            "note": "Printix-API hat aktuell keinen direkten 'add user "
                     "to group'-Endpoint im Public-API-Set — additive/"
                     "mirror sind 'best effort' und schreiben nur wenn "
                     "der Endpoint verfuegbar ist.",
        }

        if sync_mode == "report_only":
            return _ok(result)

        # Schreib-Pfade — derzeit nicht im PrintixClient implementiert.
        # Wir loggen die Intention fuer manuelles Nachpflegen.
        result["status"] = "writes not implemented — use report_only"
        return _ok(result)
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("sync_entra_group_to_printix failed")
        return _ok({"error": str(e)})


# ─── Bonus: B1-B5 ────────────────────────────────────────────────────────────

@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_card_enrol_assist(
    user_email: str,
    card_uid_raw: str,
    profile_id: str = "",
) -> str:
    """
        AI-gefuehrtes Karten-Onboarding: nimmt eine rohe Card-UID (z.B. von der
        iOS-App nach NFC-Scan), laeuft sie durch das Card-Profile-Transform und
        ordnet sie einem User zu — alles in einem Aufruf.

        Wann nutzen — typische User-Prompts:
          • "Marcus hat seine Karte gescannt, UID 04A1B2…"
          • "Register card UID … for user X"
          • "Karte X dem User Y zuweisen"
          • "NFC tag enrolment for marcus@firma.de"

        Wann NICHT — stattdessen:
          • Massenimport aus CSV                         → printix_bulk_import_cards
          • Karte ohne Transform direkt registrieren     → printix_register_card
          • Profil ermitteln vor dem Enrolment           → printix_suggest_profile

        Returns dict mit:
          - ok:                          bool
          - user:                        {id, email}
          - card_uid_raw:                Eingabe (zur Verifikation)
          - card_value_after_transform:  Ergebnis nach Profil-Transform
          - profile_id:                  "default" oder die uebergebene ID
          - register_response:           Printix-API-Antwort mit card_id

        Args:
          user_email:    Email des User dem die Karte gehoert.
          card_uid_raw:  Rohe UID als HEX, z.B. "04A1B2C3D4E5F6".
                         Trennzeichen werden im Transform automatisch entfernt.
          profile_id:    Card-Transform-Profil. Leer = Default-Profil des Tenants.
                         Suche per printix_suggest_profile bei unbekannter Karte.

    """
    try:
        c = client()
        users = _collect_all_users(c)
        match = next((u for u in users
                       if (u.get("email") or "").lower() == user_email.lower()), None)
        if not match:
            return _ok({"error": "user not found", "email": user_email})
        user_id = match.get("id", "")

        # Profil laden — printix_transform_card_value-Tool existiert; wir
        # rufen die zugrundeliegende Funktion direkt auf wenn moeglich,
        # sonst Fallback: Raw verwenden.
        # Card-Transformer via Profil ODER mit Default-Regeln.
        # `apply_profile_transform` kennt das rules_json-Format der Profile.
        try:
            from cards.transform import apply_profile_transform, transform_card_value  # type: ignore
            from cards.store import get_profile  # type: ignore
            tenant_id_db = _get_card_tenant_id()
            rules = {}
            if profile_id:
                prof = get_profile(profile_id, tenant_id_db)
                if prof and isinstance(prof, dict):
                    rules = prof.get("rules_json") or prof.get("rules") or {}
            tdict = apply_profile_transform(card_uid_raw, rules) if rules \
                    else {"final": transform_card_value(card_uid_raw).get(
                            "final", card_uid_raw)}
            transformed = tdict.get("final") or card_uid_raw
        except Exception as te:
            logger.warning("card transform fallback (raw): %s", te)
            transformed = card_uid_raw

        # Karte registrieren via Printix (Signatur: user_id, card_number)
        result = c.register_card(user_id=user_id, card_number=transformed)
        return _ok({
            "ok": True,
            "user": {"id": user_id, "email": user_email},
            "card_uid_raw": card_uid_raw,
            "card_value_after_transform": transformed,
            "profile_id": profile_id or "default",
            "register_response": result,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        logger.exception("card_enrol_assist failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_describe_user_print_pattern(user_email: str, days: int = 30) -> str:
    """
        Profiliert das Druck-Verhalten eines Users: Top-Drucker, Farb-Quote,
        Ø-Seitenzahl. Versucht zuerst SQL-Reports, faellt auf API-Job-Scan zurueck.

        Wann nutzen — typische User-Prompts:
          • "Wie druckt Marcus normalerweise?"
          • "Show me Anna's print pattern"
          • "Welche Drucker nutzt User X meistens?"
          • "Print habits for marcus@firma.de"

        Wann NICHT — stattdessen:
          • Letzte Jobs als Liste                        → printix_print_history_natural
          • Tenant-weite Top-Listen                      → printix_top_users
          • Komplette User-Sicht                         → printix_user_360

        Returns dict mit:
          - user_email:        Eingabe
          - method:            "sql_report" oder "api_scan_fallback"
          - jobs_found:        int (bei API-Scan)
          - top_printers:      Liste [(name, count)]  (bei API-Scan)
          - color_breakdown:   {"color": n, "bw": m}
          - average_pages:     float
          - stats:             SQL-Block (bei sql_report)

        Args:
          user_email:  "marcus@firma.de"
          days:        Zeitraum in Tagen (Default 30).
                       Bei API-Scan begrenzt durch Rolling-Window von Printix.

    """
    try:
        # Wir benutzen die existierende SQL-basierte Reports-Pipeline.
        # Hinweis: Es gibt kein dediziertes "user_print_pattern" Preset im
        # query_tools-Modul. Wir versuchen es trotzdem (zukunftsoffen) und
        # fallen dann sauber auf die API-Scan-Variante unten zurueck.
        try:
            from reporting.query_tools import run_query  # type: ignore
            stats = run_query("user_print_pattern",
                               tenant_id=_get_card_tenant_id(),
                               user_email=user_email, days=days)
        except Exception:
            stats = None
        if not stats:
            # Fallback: Printix-API list_print_jobs scannen
            c = client()
            jobs_data = c.list_print_jobs(page=0, size=200)
            items = (jobs_data.get("jobs") if isinstance(jobs_data, dict) else None) or \
                    (jobs_data.get("content") if isinstance(jobs_data, dict) else None) or []
            mine = [j for j in items
                    if (j.get("ownerEmail") or "").lower() == user_email.lower()]
            if not mine:
                return _ok({"user_email": user_email, "jobs_found": 0,
                            "note": "no jobs in scanned window"})
            from collections import Counter
            printers = Counter(j.get("printerName", "?") for j in mine)
            colors  = Counter(("color" if j.get("colorMode") == "COLOR" else "bw") for j in mine)
            return _ok({
                "user_email": user_email,
                "method": "api_scan_fallback",
                "jobs_found": len(mine),
                "top_printers": printers.most_common(5),
                "color_breakdown": dict(colors),
                "average_pages":   round(
                    sum(j.get("pages", 0) or 0 for j in mine) / max(len(mine), 1), 1),
            })
        return _ok({"user_email": user_email, "method": "sql_report", "stats": stats})
    except Exception as e:
        logger.exception("describe_user_print_pattern failed")
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True))
def printix_session_print(
    user_email: str,
    file_b64: str,
    filename: str,
    expires_in_hours: int = 24,
) -> str:
    """
        Druckjob mit Time-Bomb: Job geht sofort an den Empfaenger raus, und
        nach `expires_in_hours` wird ein Audit-Log-Eintrag erzeugt
        (tatsaechliches Auto-Delete erfordert manuell printix_delete_job).
        Praktisch fuer zeitkritische Dokumente / Gaeste / Externe.

        Wann nutzen — typische User-Prompts:
          • "Schick X an Y, soll aber nach 4 Stunden weg"
          • "Send this with auto-expire"
          • "Gast bekommt das Dokument fuer 2h"
          • "Time-limited print for guest"

        Wann NICHT — stattdessen:
          • Normaler Druck ohne Expire                   → printix_send_to_user
          • An sich selbst                               → printix_print_self
          • Mehrere Empfaenger                           → printix_print_to_recipients

        Returns dict mit:
          - ok:           bool
          - job_id:       Printix-Job-UUID
          - user_email:   bestaetigter Empfaenger
          - expires_at:   ISO-Timestamp
          - timebomb_id:  numerische ID (→ printix_defuse_timebomb falls verfrueht)
          - note:         Hinweis dass Auto-Delete manuell erfolgen muss

        Args:
          user_email:        Empfaenger-Email.
          file_b64:          Base64-Datei.
          filename:          Anzeigename.
          expires_in_hours:  Lifetime in Stunden (Default 24, Beispiel 4).

    """
    import json as _json
    from datetime import datetime, timezone, timedelta
    try:
        # 1) Job senden via send_to_user
        result_str = printix_send_to_user(
            user_email=user_email, file_content_b64=file_b64, filename=filename
        )
        result = _json.loads(result_str)
        job_id = result.get("job_id", "")
        if not job_id:
            return _ok({"error": "submit failed", "details": result})

        # 2) Time-Bomb: Auto-Delete in N Stunden
        _ensure_timebomb_table()
        _ensure_timebomb_scheduler()
        tid = _get_card_tenant_id()
        now = datetime.now(timezone.utc)
        trigger_at = (now + timedelta(hours=expires_in_hours)).isoformat()
        action = {"kind": "log",
                   "message": f"session_print expired — job_id={job_id} for {user_email}"}
        with __import__("db")._conn() as conn:
            cur = conn.execute(
                "INSERT INTO user_timebombs "
                "(tenant_id, user_id, user_email, bomb_type, trigger_at, "
                " action_json, created_at) VALUES (?,?,?,?,?,?,?)",
                (tid, "", user_email, "session_print_expire", trigger_at,
                 _json.dumps(action), now.isoformat()),
            )
            bomb_id = cur.lastrowid

        return _ok({
            "ok": True,
            "job_id": job_id,
            "user_email": user_email,
            "expires_at": trigger_at,
            "timebomb_id": bomb_id,
            "note": ("Job liegt in der Secure-Print-Queue und wird nach Ablauf "
                      "der Time-Bomb geloggt; tatsaechliches Auto-Delete via "
                      "Printix erfordert manuell printix_delete_job."),
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_quota_guard(
    user_email: str = "",
    window_minutes: int = 5,
    max_jobs: int = 10,
) -> str:
    """
        Pre-flight-Burst-Check vor Print-Submits: schaut wie viele Jobs ein User
        in den letzten X Minuten gesendet hat und gibt verdict allow/throttle/block
        zurueck. Defensive AI-Funktion gegen Bot-Loops oder Cost-Bursts.

        Wann nutzen — typische User-Prompts:
          • "Vor dem naechsten Druck pruefen"
          • "Check user X's print rate"
          • "Hat User X zu viele Jobs in letzter Zeit?"
          • "Quota check before submitting"

        Wann NICHT — stattdessen:
          • Tenant-weite Top-Volumen                     → printix_top_users
          • Druckhistorie eines Users mit Details        → printix_print_history_natural
          • Anomalie-Erkennung allgemein                  → printix_query_anomalies

        Returns dict mit:
          - user_email:        aufgeloester User
          - recent_count:      Jobs im Window
          - window_minutes, max_jobs: Eingabe-Parameter (zur Diagnose)
          - verdict:           "allow" | "throttle" | "block"
          - recommendation:    Klartext-Hinweis fuer den AI-Assistenten

        Args:
          user_email:      Default leer = Self-User aus Tenant-Email.
          window_minutes:  Zeitfenster, Default 5.
          max_jobs:        Block-Schwelle, Default 10.
                           Throttle-Schwelle = max_jobs / 2.

    """
    from datetime import datetime, timezone, timedelta
    try:
        c = client()
        if not user_email:
            t = current_tenant.get() or {}
            user_email = t.get("email") or t.get("username") or ""
            if not user_email and t.get("user_id"):
                try:
                    from db import get_user_by_id
                    urow = get_user_by_id(t["user_id"])
                    if urow:
                        user_email = urow.get("email") or urow.get("username") or ""
                except Exception:
                    pass
        if not user_email:
            return _ok({"error": "user_email required and could not be inferred"})

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        try:
            jobs_data = c.list_print_jobs(page=0, size=100)
        except Exception as e:
            return _ok({"error": str(e)})
        items = (jobs_data.get("jobs") if isinstance(jobs_data, dict) else None) or \
                (jobs_data.get("content") if isinstance(jobs_data, dict) else None) or []
        recent: list[dict] = []
        for j in items:
            owner = (j.get("ownerEmail") or "").lower()
            if owner != user_email.lower():
                continue
            ts = j.get("createdAt") or j.get("submittedAt") or ""
            try:
                jt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if jt >= cutoff:
                    recent.append(j)
            except Exception:
                continue

        n = len(recent)
        if n >= max_jobs:
            verdict, hint = "block", f"User has sent {n} jobs in last {window_minutes}min — likely automation gone wrong"
        elif n >= max_jobs // 2:
            verdict, hint = "throttle", f"User at {n}/{max_jobs} — ask for confirmation before next submit"
        else:
            verdict, hint = "allow", f"Normal volume ({n} jobs in {window_minutes}min)"
        return _ok({
            "user_email": user_email,
            "recent_count": n,
            "window_minutes": window_minutes,
            "max_jobs": max_jobs,
            "verdict": verdict,
            "recommendation": hint,
        })
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True))
def printix_print_history_natural(
    user_email: str = "",
    when: str = "today",
    limit: int = 50,
) -> str:
    """
        Druckhistorie mit natuerlich-sprachlichen Zeitangaben — kein expliziter
        ISO-Datums-Range noetig.

        Wann nutzen — typische User-Prompts:
          • "Was hat Anna heute gedruckt?"
          • "Show me Marcus's prints last week"
          • "Druckverlauf fuer Q1"
          • "Was wurde gestern gedruckt?"
          • "Last 7 days of print history"

        Wann NICHT — stattdessen:
          • Aggregate Statistiken (top, count)           → printix_query_print_stats /
                                                           printix_top_users
          • Druck-Profil eines Users (Pattern)           → printix_describe_user_print_pattern
          • Spezifische Job-IDs                          → printix_get_job

        Returns dict mit:
          - user_email
          - when:              Eingabe-Wert
          - interpreted_as:    {start, end} ISO-Timestamps zur Verifikation
          - count:             int
          - jobs:              Liste {job_id, title, printer, pages, color_mode, submitted}

        Args:
          user_email:  Default leer = Self-User aus Tenant-Email.
          when:        Akzeptierte Werte (case-insensitive):
                         "today" / "heute"
                         "yesterday" / "gestern"
                         "this_week" / "diese_woche"
                         "last_week" / "letzte_woche"
                         "this_month" / "diesen_monat"
                         "last_month" / "letzten_monat"
                         "Q1" | "Q2" | "Q3" | "Q4"   (aktuelles Jahr)
                         "<n>d"  z.B. "7d" = letzte 7 Tage, "30d", "90d"
          limit:       Max. Eintraege (Default 50).

    """
    from datetime import datetime, timezone, timedelta
    try:
        if not user_email:
            t = current_tenant.get() or {}
            user_email = t.get("email") or t.get("username") or ""
            if not user_email and t.get("user_id"):
                try:
                    from db import get_user_by_id
                    urow = get_user_by_id(t["user_id"])
                    if urow:
                        user_email = urow.get("email") or urow.get("username") or ""
                except Exception:
                    pass
        now = datetime.now(timezone.utc)
        w = (when or "today").lower()
        if w in ("today", "heute"):
            start, end = now.replace(hour=0, minute=0, second=0, microsecond=0), now
        elif w in ("yesterday", "gestern"):
            y = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            start, end = y, y + timedelta(days=1)
        elif w in ("this_week", "diese_woche"):
            start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            end = now
        elif w in ("last_week", "letzte_woche"):
            this_mon = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
            start = this_mon - timedelta(days=7)
            end = this_mon
        elif w in ("this_month", "diesen_monat"):
            start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = now
        elif w in ("last_month", "letzten_monat"):
            first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            last_prev = first_this - timedelta(seconds=1)
            start = last_prev.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            end = first_this
        elif w in ("q1", "q2", "q3", "q4"):
            q = int(w[1])
            start_month = (q - 1) * 3 + 1
            start = now.replace(month=start_month, day=1, hour=0, minute=0,
                                  second=0, microsecond=0)
            end_month = start_month + 3
            if end_month > 12:
                end = start.replace(year=now.year + 1, month=1)
            else:
                end = start.replace(month=end_month)
        elif w.endswith("d") and w[:-1].isdigit():
            n = int(w[:-1])
            start, end = now - timedelta(days=n), now
        else:
            return _ok({"error": f"unknown 'when' value: {when}",
                         "hint": "use today|yesterday|this_week|last_week|this_month|last_month|Q1..Q4|7d"})

        c = client()
        jobs_data = c.list_print_jobs(page=0, size=200)
        items = (jobs_data.get("jobs") if isinstance(jobs_data, dict) else None) or \
                (jobs_data.get("content") if isinstance(jobs_data, dict) else None) or []
        out: list[dict] = []
        for j in items:
            if user_email and (j.get("ownerEmail") or "").lower() != user_email.lower():
                continue
            ts = j.get("createdAt") or j.get("submittedAt") or ""
            try:
                jt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except Exception:
                continue
            if start <= jt <= end:
                out.append({
                    "job_id":     j.get("id") or j.get("jobId"),
                    "title":      j.get("title") or j.get("documentName", ""),
                    "printer":    j.get("printerName", ""),
                    "pages":      j.get("pages", 0),
                    "color_mode": j.get("colorMode", ""),
                    "submitted":  ts,
                })
            if len(out) >= limit:
                break
        return _ok({
            "user_email": user_email,
            "when": when,
            "interpreted_as": {"start": start.isoformat(), "end": end.isoformat()},
            "count": len(out),
            "jobs": out,
        })
    except Exception as e:
        logger.exception("print_history_natural failed")
        return _ok({"error": str(e)})


# ─── Dual Transport Router ────────────────────────────────────────────────────

class DualTransportApp:
    """
    ASGI-Router: leitet Anfragen an den passenden MCP-Transport weiter.

      POST /mcp                    → Streamable HTTP Transport (claude.ai)
      GET  /sse                    → SSE Transport (ChatGPT)
      POST /capture/webhook/{id}   → Capture Webhook (shared handler)

    Capture Webhooks werden in BearerAuthMiddleware von der Bearer-Prüfung
    ausgenommen — sie nutzen HMAC-Verifizierung.

    v4.6.7: Wenn CAPTURE_ENABLED=true, laeuft ein separater Capture-Server
    auf Port 8775. Der MCP-Server akzeptiert Capture-Requests weiterhin
    fuer Rueckwaertskompatibilitaet, loggt aber einen Hinweis.
    """

    def __init__(self, sse_app, http_app):
        self.sse_app = sse_app
        self.http_app = http_app
        # v4.6.7: Pruefe ob separater Capture-Server aktiv (bool statt Port)
        self._capture_separate = os.environ.get("CAPTURE_ENABLED", "false").lower() == "true"

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            await self.http_app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "?")
        if path == "/mcp" or path.startswith("/mcp/"):
            await self.http_app(scope, receive, send)
        elif path.startswith("/capture/webhook/") or path.startswith("/capture/debug"):
            if self._capture_separate:
                logger.info("▶ CAPTURE REQUEST [mcp-compat]: %s %s "
                            "(Hinweis: Capture-Server laeuft auf eigenem Port)", method, path)
            else:
                logger.info("▶ CAPTURE REQUEST [mcp]: %s %s", method, path)
            await self._handle_capture(scope, receive, send)
        else:
            await self.sse_app(scope, receive, send)

    # ── Capture Webhook — delegiert an shared handler (v4.4.6) ─────────────

    async def _read_body(self, receive) -> bytes:
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body", False):
                break
        return body

    async def _json_response(self, send, status: int, data: dict):
        import json as _j
        body = _j.dumps(data, ensure_ascii=False).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [
                [b"content-type", b"application/json; charset=utf-8"],
            ],
        })
        await send({"type": "http.response.body", "body": body})

    async def _handle_capture(self, scope, receive, send):
        """Delegiert an den shared capture webhook handler (v4.4.6)."""
        from capture.webhook_handler import handle_webhook

        path = scope.get("path", "")
        method = scope.get("method", "GET")
        raw_headers = dict(scope.get("headers", []))
        headers_str = {
            k.decode("utf-8", errors="replace"): v.decode("utf-8", errors="replace")
            for k, v in raw_headers.items()
        }
        body_bytes = await self._read_body(receive)

        # Profile-ID aus Pfad extrahieren
        if path.startswith("/capture/webhook/"):
            profile_id = path[len("/capture/webhook/"):].strip("/")
        elif path.startswith("/capture/debug"):
            profile_id = "00000000-0000-0000-0000-000000000000"
        else:
            profile_id = ""

        try:
            status, data = await handle_webhook(
                profile_id=profile_id,
                method=method,
                headers=headers_str,
                body_bytes=body_bytes,
                source="mcp",
            )
        except Exception as e:
            logger.error("Capture handler error: %s", e, exc_info=True)
            status, data = 500, {"error": str(e)}

        await self._json_response(send, status, data)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8765"))
    base = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/") or f"http://{host}:{port}"

    # Beide MCP-Transport-Apps
    sse_transport  = mcp.sse_app()
    http_transport = mcp.streamable_http_app()
    app = DualTransportApp(sse_transport, http_transport)

    # Layer 1: Multi-Tenant Bearer Auth (schlägt Token pro Request in der DB nach)
    app = BearerAuthMiddleware(app)

    # Layer 2: Multi-Tenant OAuth 2.0 (client_id/secret werden pro Request aus DB gelesen)
    app = OAuthMiddleware(app)

    # Gespeicherte Report-Schedules laden
    if _REPORTING_AVAILABLE:
        try:
            n = rep_scheduler.init_scheduler_from_templates()
            if n:
                logger.info("  %d Report-Schedule(s) aus Templates geladen", n)
        except Exception as _sched_err:
            logger.warning("Scheduler-Init fehlgeschlagen: %s", _sched_err)

    # Guest-Print Meta-Tick (v7.1.0) — polled alle aktiven Mailboxes
    try:
        from guestprint.scheduler import start_guestprint_scheduler
        if start_guestprint_scheduler():
            logger.info("  Guest-Print Meta-Tick aktiv")
    except Exception as _gp_err:
        logger.warning("Guest-Print Scheduler konnte nicht gestartet werden: %s", _gp_err)

    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║        PRINTIX MCP SERVER v%s — MULTI-TENANT            ║", APP_VERSION)
    logger.info("╠══════════════════════════════════════════════════════════════╣")
    logger.info("║  MCP (claude.ai):  %s/mcp", base)
    logger.info("║  SSE (ChatGPT):    %s/sse", base)
    logger.info("║  OAuth Authorize:  %s/oauth/authorize", base)
    logger.info("║  OAuth Token:      %s/oauth/token", base)
    logger.info("║  Health-Check:     %s/health", base)
    logger.info("╠══════════════════════════════════════════════════════════════╣")
    _web_port = int(os.environ.get("WEB_PORT", "8080"))
    _web_base = base if base else f"http://<host>:{_web_port}"
    logger.info("║  Benutzer registrieren:  %s", _web_base)
    # v4.6.7/v7.0.0: Capture-Status — Base-URL aus DB > MCP_PUBLIC_URL
    _capture_enabled = os.environ.get("CAPTURE_ENABLED", "false").strip().lower() == "true"
    if _capture_enabled:
        logger.info("║  Capture (separat, Port 8775): %s/capture/webhook/<id>",
                    base if base else "http://<host>:8775")
    else:
        logger.info("║  Capture (via MCP): %s/capture/webhook/<id>", base)
    logger.info("╚══════════════════════════════════════════════════════════════╝")

    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())
