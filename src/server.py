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
from typing import Optional
from collections import OrderedDict

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

@mcp.tool()
def printix_status() -> str:
    """
    Zeigt, welche Credential-Bereiche konfiguriert sind und die Tenant-ID.
    Gut zum Testen ob der MCP-Server korrekt konfiguriert ist.
    """
    try:
        result = client().get_credential_status()
        logger.info("Status abgefragt: %s", result)
        return _ok(result)
    except Exception as e:
        logger.error("Status-Fehler: %s", e)
        return _ok({"error": str(e)})


# ─── Drucker / Print Queues ───────────────────────────────────────────────────

@mcp.tool()
def printix_list_printers(search: str = "", page: int = 0, size: int = 50) -> str:
    """
    Listet alle Drucker-Queues (Print Queues) des Tenants.

    WICHTIG – Datenstruktur der Antwort:
    Jedes Item in 'printers' ist ein Printer-Queue-Paar.
    Ein physischer Drucker kann mehrere Queues haben.

    - Physische Drucker ermitteln: Nach printer_id deduplizieren.
      Die printer_id steht in _links.self.href als:
      /printers/{printer_id}/queues/{queue_id}
      Felder pro Drucker: name (Modell), vendor, location, connectionStatus,
      printerSignId (Kurzcode), serialNo.

    - Print Queues anzeigen: Jedes Item direkt als Queue verwenden.
      Queue-Name = name (z.B. "HP-M577 (Printix)", "Guestprint").
      Drucker-Modell = model + vendor.

    Beispiel: Bei 10 Druckern mit 19 Queues liefert die API 19 Items.
    Frage: "Zeige meine Drucker" → deduplizieren auf 10 eindeutige printer_ids.
    Frage: "Zeige meine Queues"  → alle 19 Items direkt ausgeben.

    Args:
        search: Optionaler Suchbegriff (Queue-/Druckername).
        page:   Seitennummer (0-basiert).
        size:   Einträge pro Seite (max. 100).
    """
    try:
        logger.debug("list_printers(search=%s, page=%d, size=%d)", search, page, size)
        return _ok(client().list_printers(search=search or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_get_printer(printer_id: str, queue_id: str) -> str:
    """
    Gibt Details und Fähigkeiten einer bestimmten Drucker-Queue zurück.
    Beide IDs findest du im _links.self.href der printix_list_printers-Ausgabe.

    Args:
        printer_id: ID des Druckers (aus _links.self.href in list_printers).
        queue_id:   ID der Drucker-Queue (aus _links.self.href in list_printers).
    """
    try:
        logger.debug("get_printer(printer_id=%s, queue_id=%s)", printer_id, queue_id)
        return _ok(client().get_printer(printer_id, queue_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Print Jobs ───────────────────────────────────────────────────────────────

@mcp.tool()
def printix_list_jobs(queue_id: str = "", page: int = 0, size: int = 50) -> str:
    """
    Listet Druckaufträge. Optionaler Filter nach Drucker-Queue.

    Args:
        queue_id: Optionale Printer Queue ID zum Filtern.
        page:     Seitennummer (0-basiert).
        size:     Einträge pro Seite.
    """
    try:
        logger.debug("list_jobs(queue_id=%s, page=%d, size=%d)", queue_id, page, size)
        return _ok(client().list_print_jobs(queue_id=queue_id or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_get_job(job_id: str) -> str:
    """
    Gibt Status und Details eines bestimmten Druckauftrags zurück.

    Args:
        job_id: ID des Druckauftrags.
    """
    try:
        logger.debug("get_job(job_id=%s)", job_id)
        return _ok(client().get_print_job(job_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
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
    Erstellt einen neuen Druckauftrag (API v1.1) in einer bestimmten Drucker-Queue.
    Gibt Upload-URL und Job-ID zurück — danach Datei hochladen und printix_complete_upload aufrufen.
    Beide IDs findest du im _links.self.href der printix_list_printers-Ausgabe.

    Args:
        printer_id:  Drucker-ID (aus _links.self.href in list_printers).
        queue_id:    Queue-ID (aus _links.self.href in list_printers).
        title:       Name des Druckauftrags (Pflicht).
        user:        Optionale E-Mail des Benutzers, dem der Auftrag zugeordnet wird.
        pdl:         Optionales Seitenformat: PCL5 | PCLXL | POSTSCRIPT | UFRII | TEXT | XPS.
        color:       True = Farbe, False = Monochrom (leer = Drucker-Standard).
        duplex:      NONE | SHORT_EDGE | LONG_EDGE.
        copies:      Anzahl Kopien (0 = Drucker-Standard).
        paper_size:  A4 | A3 | A0–A5 | B4–B5 | LETTER | LEGAL etc.
        orientation: PORTRAIT | LANDSCAPE | AUTO.
        scaling:     NOSCALE | SHRINK | FIT.
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


@mcp.tool()
def printix_complete_upload(job_id: str) -> str:
    """
    Signalisiert, dass der Datei-Upload abgeschlossen ist und löst den Druckvorgang aus.

    WICHTIG – Voraussetzung: Vor diesem Aufruf MUSS die Datei bereits per HTTP PUT
    zur uploadUrl hochgeladen worden sein (die uploadUrl kommt aus printix_submit_job).
    Reihenfolge: submit_job → Datei hochladen → complete_upload.

    Wird complete_upload ohne echten Datei-Upload aufgerufen, meldet der Backend-Server
    formal Erfolg, entfernt den Job aber sofort danach (leere Datei). Ein anschließender
    get_job liefert dann 404 — das ist korrektes Backend-Verhalten, kein Skill-Fehler.

    Args:
        job_id: ID des Druckauftrags (aus printix_submit_job).
    """
    try:
        logger.info("complete_upload(job_id=%s)", job_id)
        return _ok(client().complete_upload(job_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_delete_job(job_id: str) -> str:
    """
    Löscht einen Druckauftrag (eingereicht oder fehlgeschlagen).

    Args:
        job_id: ID des Druckauftrags.
    """
    try:
        logger.info("delete_job(job_id=%s)", job_id)
        return _ok(client().delete_print_job(job_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_change_job_owner(job_id: str, new_owner_email: str) -> str:
    """
    Überträgt einen Druckauftrag an einen anderen Benutzer (per E-Mail).

    Args:
        job_id:          ID des Druckauftrags.
        new_owner_email: E-Mail-Adresse des neuen Eigentümers.
    """
    try:
        logger.info("change_job_owner(job_id=%s, new_owner=%s)", job_id, new_owner_email)
        return _ok(client().change_job_owner(job_id, new_owner_email))
    except PrintixAPIError as e:
        return _err(e)


# ─── Card Management ──────────────────────────────────────────────────────────

@mcp.tool()
def printix_list_cards(user_id: str) -> str:
    """
    Listet alle Karten eines bestimmten Benutzers.
    Hinweis: Es gibt kein tenant-weites "alle Karten"-Endpoint in der Printix API.
    Karten müssen immer über einen Benutzer abgefragt werden.

    Args:
        user_id: Benutzer-ID in Printix (aus printix_list_users).
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


@mcp.tool()
def printix_search_card(card_id: str = "", card_number: str = "") -> str:
    """
    Ruft eine einzelne Karte per ID oder Kartennummer ab.
    Genau eines der beiden Argumente muss angegeben werden.

    Args:
        card_id:     Karten-ID in Printix.
        card_number: Physische Kartennummer (wird automatisch base64-encodiert).
    """
    try:
        logger.debug("search_card(card_id=%s, card_number=%s)", card_id, card_number or "***")
        api_card = client().search_card(card_id=card_id or None,
                                        card_number=card_number or None)
        enriched = _enrich_card_with_local_data(api_card, _get_card_tenant_id(), requested_card_number=card_number)
        return _ok(enriched)
    except (PrintixAPIError, ValueError) as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_register_card(user_id: str, card_number: str) -> str:
    """
    Registriert (verknüpft) eine Karte mit einem Benutzer.

    Args:
        user_id:     Benutzer-ID in Printix.
        card_number: Physische Kartennummer (wird automatisch base64-encodiert).
    """
    try:
        logger.info("register_card(user_id=%s)", user_id)
        return _ok(client().register_card(user_id, card_number))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_delete_card(card_id: str) -> str:
    """
    Entfernt eine Kartenzuordnung.

    Args:
        card_id: ID der Karte in Printix.
    """
    try:
        logger.info("delete_card(card_id=%s)", card_id)
        return _ok(client().delete_card(card_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── User Management ──────────────────────────────────────────────────────────

@mcp.tool()
def printix_list_users(
    role: str = "USER",
    query: str = "",
    page: int = 0,
    page_size: int = 50,
) -> str:
    """
    Listet Benutzer im Tenant.
    WICHTIG: Die API liefert standardmäßig nur GUEST_USER. Daher ist der Default hier USER.
    Für alle Nutzer: einmal mit role='USER', einmal mit role='GUEST_USER' aufrufen.
    Voraussetzung: Printix Premium + Cloud Print API guest user feature aktiviert.

    Args:
        role:      'USER' (normale Nutzer) oder 'GUEST_USER' (Gastnutzer). Default: 'USER'.
        query:     Optionaler Suchbegriff (Name oder E-Mail-Adresse).
        page:      Seitennummer (0-basiert).
        page_size: Einträge pro Seite (max. 50).
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


@mcp.tool()
def printix_get_user(user_id: str) -> str:
    """
    Gibt Details eines bestimmten Benutzers zurück.

    Args:
        user_id: Benutzer-ID in Printix.
    """
    try:
        logger.debug("get_user(user_id=%s)", user_id)
        return _ok(client().get_user(user_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_create_user(
    email: str,
    display_name: str,
    pin: str = "",
    password: str = "",
) -> str:
    """
    Erstellt einen Gast-Benutzerkonto.

    Args:
        email:        E-Mail-Adresse des neuen Benutzers.
        display_name: Anzeigename.
        pin:          Optionale PIN — muss GENAU 4 Ziffern sein (z.B. "4242"). Andere Längen führen zu VALIDATION_FAILED.
        password:     Optionales Passwort.
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


@mcp.tool()
def printix_delete_user(user_id: str) -> str:
    """
    Löscht einen Gast-Benutzer.

    Args:
        user_id: Benutzer-ID in Printix.
    """
    try:
        logger.info("delete_user(user_id=%s)", user_id)
        return _ok(client().delete_user(user_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_generate_id_code(user_id: str) -> str:
    """
    Generiert einen neuen 6-stelligen Identifikationscode für einen Benutzer.

    Args:
        user_id: Benutzer-ID in Printix.
    """
    try:
        logger.info("generate_id_code(user_id=%s)", user_id)
        return _ok(client().generate_id_code(user_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Groups ───────────────────────────────────────────────────────────────────

@mcp.tool()
def printix_list_groups(search: str = "", page: int = 0, size: int = 50) -> str:
    """
    Listet alle Gruppen im Tenant.

    Args:
        search: Optionaler Suchbegriff.
        page:   Seitennummer.
        size:   Einträge pro Seite.
    """
    try:
        logger.debug("list_groups(search=%s, page=%d, size=%d)", search, page, size)
        return _ok(client().list_groups(search=search or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_get_group(group_id: str) -> str:
    """
    Gibt Details einer bestimmten Gruppe zurück.

    Args:
        group_id: Gruppen-ID in Printix.
    """
    try:
        logger.debug("get_group(group_id=%s)", group_id)
        return _ok(client().get_group(group_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_create_group(name: str, external_id: str) -> str:
    """
    Erstellt eine neue Gruppe.
    VORAUSSETZUNG: Der Tenant muss eine konfigurierte Directory-Anbindung haben (z.B. Azure AD,
    Google Workspace). Ohne Directory schlägt der Call fehl mit:
    "Directory ID cannot be null when no directories are configured for tenant".

    Args:
        name:        Gruppenname.
        external_id: Pflicht: ID der Gruppe im externen Verzeichnis (z.B. Azure AD GUID).
    """
    try:
        logger.info("create_group(name=%s, external_id=%s)", name, external_id)
        return _ok(client().create_group(
            name=name,
            external_id=external_id,
        ))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_delete_group(group_id: str) -> str:
    """
    Löscht eine Gruppe.

    Args:
        group_id: Gruppen-ID in Printix.
    """
    try:
        logger.info("delete_group(group_id=%s)", group_id)
        return _ok(client().delete_group(group_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Workstation Monitoring ───────────────────────────────────────────────────

@mcp.tool()
def printix_list_workstations(
    search: str = "",
    site_id: str = "",
    page: int = 0,
    size: int = 50,
) -> str:
    """
    Listet Workstations (Computer mit Printix Client). Optional nach Standort oder Name filtern.

    Args:
        search:  Optionaler Suchbegriff (Hostname / Name).
        site_id: Optionale Standort-ID zum Filtern.
        page:    Seitennummer.
        size:    Einträge pro Seite.
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


@mcp.tool()
def printix_get_workstation(workstation_id: str) -> str:
    """
    Gibt Details einer bestimmten Workstation zurück.

    Args:
        workstation_id: Workstation-ID in Printix.
    """
    try:
        logger.debug("get_workstation(workstation_id=%s)", workstation_id)
        return _ok(client().get_workstation(workstation_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Sites ────────────────────────────────────────────────────────────────────

@mcp.tool()
def printix_list_sites(search: str = "", page: int = 0, size: int = 50) -> str:
    """
    Listet alle Standorte (Sites) im Tenant.

    Args:
        search: Optionaler Suchbegriff.
        page:   Seitennummer.
        size:   Einträge pro Seite.
    """
    try:
        logger.debug("list_sites(search=%s, page=%d, size=%d)", search, page, size)
        return _ok(client().list_sites(search=search or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_get_site(site_id: str) -> str:
    """
    Gibt Details eines bestimmten Standorts zurück.

    Args:
        site_id: Standort-ID in Printix.
    """
    try:
        logger.debug("get_site(site_id=%s)", site_id)
        return _ok(client().get_site(site_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_create_site(
    name: str,
    path: str,
    admin_group_ids: str = "",
    network_ids: str = "",
) -> str:
    """
    Erstellt einen neuen Standort.
    Hinweis: path ist Pflichtfeld laut API.

    Args:
        name:            Standortname.
        path:            Pflicht: Pfad des Standorts, z.B. '/Europe/Germany/Munich'.
        admin_group_ids: Optionale kommagetrennte Liste von Admin-Gruppen-IDs.
        network_ids:     Optionale kommagetrennte Liste von Netzwerk-IDs.
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


@mcp.tool()
def printix_update_site(
    site_id: str,
    name: str = "",
    path: str = "",
    admin_group_ids: str = "",
    network_ids: str = "",
) -> str:
    """
    Aktualisiert einen Standort.
    Hinweis: path sollte angegeben werden, da die API sonst VALIDATION_FAILED zurückgibt.
    Aktuellen path findest du mit printix_get_site.

    Args:
        site_id:         Standort-ID.
        name:            Neuer Name (leer = unverändert).
        path:            Standort-Pfad, z.B. '/Europe/Germany/Munich' (empfohlen).
        admin_group_ids: Kommagetrennte Liste von Admin-Gruppen-IDs (leer = unverändert).
        network_ids:     Kommagetrennte Liste von Netzwerk-IDs (leer = unverändert).
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


@mcp.tool()
def printix_delete_site(site_id: str) -> str:
    """
    Löscht einen Standort.

    Args:
        site_id: Standort-ID in Printix.
    """
    try:
        logger.info("delete_site(site_id=%s)", site_id)
        return _ok(client().delete_site(site_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── Networks ─────────────────────────────────────────────────────────────────

@mcp.tool()
def printix_list_networks(site_id: str = "", page: int = 0, size: int = 50) -> str:
    """
    Listet Netzwerke, optional gefiltert nach Standort.

    Args:
        site_id: Optionale Standort-ID.
        page:    Seitennummer.
        size:    Einträge pro Seite.
    """
    try:
        logger.debug("list_networks(site_id=%s, page=%d, size=%d)", site_id, page, size)
        return _ok(client().list_networks(site_id=site_id or None, page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_get_network(network_id: str) -> str:
    """
    Gibt Details eines bestimmten Netzwerks zurück.

    Args:
        network_id: Netzwerk-ID in Printix.
    """
    try:
        logger.debug("get_network(network_id=%s)", network_id)
        return _ok(client().get_network(network_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
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
    Erstellt ein neues Netzwerk.
    Hinweis: home_office, client_migrate_print_queues und air_print sind laut API Pflichtfelder.

    Args:
        name:                        Netzwerkname.
        home_office:                 True wenn Home-Office-Netzwerk (Standard: False).
        client_migrate_print_queues: 'GLOBAL_SETTING', 'YES' oder 'NO' (Standard: GLOBAL_SETTING).
        air_print:                   True um AirPrint zu aktivieren (Standard: False).
        site_id:                     Optionale Standort-ID.
        gateway_mac:                 Optionale Gateway MAC-Adresse.
        gateway_ip:                  Optionale Gateway IP-Adresse.
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


@mcp.tool()
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
    Aktualisiert ein Netzwerk.
    Liest zuerst den aktuellen Stand aus der API und schreibt dann alle Pflichtfelder
    (homeOffice, clientMigratePrintQueues, airPrint) zusammen mit den Änderungen zurück.

    Hinweis zur Antwort: Der Update-Endpoint liefert eine schlankere Antwortstruktur als GET —
    der site-Link fehlt in der direkten Rückgabe. Die Site-Zuordnung ist korrekt gespeichert.
    Für die vollständige Ansicht danach printix_get_network aufrufen.

    Args:
        network_id:                  Netzwerk-ID.
        name:                        Neuer Name (leer = unverändert).
        subnet:                      Neues Subnetz, z.B. '192.168.1.0/24' (leer = unverändert).
        home_office:                 True/False oder leer = unverändert.
        client_migrate_print_queues: 'GLOBAL_SETTING', 'YES' oder 'NO' (leer = unverändert).
        air_print:                   True/False oder leer = unverändert.
        site_id:                     Standort-ID (leer = unverändert).
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


@mcp.tool()
def printix_delete_network(network_id: str) -> str:
    """
    Löscht ein Netzwerk.

    Args:
        network_id: Netzwerk-ID in Printix.
    """
    try:
        logger.info("delete_network(network_id=%s)", network_id)
        return _ok(client().delete_network(network_id))
    except PrintixAPIError as e:
        return _err(e)


# ─── SNMP Configurations ──────────────────────────────────────────────────────

@mcp.tool()
def printix_list_snmp_configs(page: int = 0, size: int = 50) -> str:
    """
    Listet alle SNMP-Konfigurationen für Druckerüberwachung.

    Args:
        page: Seitennummer.
        size: Einträge pro Seite.
    """
    try:
        logger.debug("list_snmp_configs(page=%d, size=%d)", page, size)
        return _ok(client().list_snmp_configs(page=page, size=size))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
def printix_get_snmp_config(config_id: str) -> str:
    """
    Gibt Details einer SNMP-Konfiguration zurück.

    Args:
        config_id: SNMP-Konfigurations-ID.
    """
    try:
        logger.debug("get_snmp_config(config_id=%s)", config_id)
        return _ok(client().get_snmp_config(config_id))
    except PrintixAPIError as e:
        return _err(e)


@mcp.tool()
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
    Erstellt eine neue SNMP-Konfiguration für Druckermonitoring.
    Endpunkt: POST /snmp

    Args:
        name:               Name der Konfiguration (Pflicht).
        get_community_name: SNMP Get Community Name.
        set_community_name: SNMP Set Community Name.
        tenant_default:     True wenn dies die Standard-Konfiguration des Tenants ist.
        security_level:     NO_AUTH_NO_PRIVACY | AUTH_NO_PRIVACY | AUTH_PRIVACY.
        version:            SNMP Version: V1 | V2C | V3 (Großbuchstaben).
        username:           SNMPv3 Benutzername.
        context_name:       SNMPv3 Context Name.
        authentication:     NONE | MD5 | SHA | SHA256 | SHA384 | SHA512.
        authentication_key: SNMPv3 Authentication Key.
        privacy:            NONE | DES | AES | AES192 | ASE256.
        privacy_key:        SNMPv3 Privacy Key.
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


@mcp.tool()
def printix_delete_snmp_config(config_id: str) -> str:
    """
    Löscht eine SNMP-Konfiguration.

    Args:
        config_id: SNMP-Konfigurations-ID.
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

@mcp.tool()
def printix_reporting_status() -> str:
    """
    Prüft den Status des Reporting-Moduls: ODBC-Treiber, SQL-Konfiguration und Mail.

    Nützlich zur Diagnose wenn SQL-Abfragen fehlschlagen.
    Zeigt alle erkannten ODBC-Treiber, den gewählten Treiber und ob SQL + Mail konfiguriert sind.
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


@mcp.tool()
def printix_query_print_stats(
    start_date: str,
    end_date: str,
    group_by: str = "month",
    site_id: str = "",
    user_email: str = "",
    printer_id: str = "",
) -> str:
    """
    Druckvolumen-Statistik aus der Printix BI-Datenbank.

    Liefert Aufträge, Seiten, Farbanteil und Duplex-Quote für den gewählten Zeitraum.
    Ermöglicht Analyse nach Zeitraum, Standort, Benutzer oder Drucker.

    Args:
        start_date:  Startdatum (YYYY-MM-DD), z.B. "2025-01-01"
        end_date:    Enddatum   (YYYY-MM-DD), z.B. "2025-01-31"
        group_by:    Aggregation: day | week | month | user | printer | site (default: month)
        site_id:     Optional — Netzwerk-ID für Standort-Filter
        user_email:  Optional — E-Mail für Benutzer-Filter
        printer_id:  Optional — Drucker-ID für Drucker-Filter
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


@mcp.tool()
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
    Kostenaufstellung mit Papier-, Toner- und Gesamtkosten.

    Berechnet Kosten exakt nach der Printix PowerBI-Formel:
      Papierkosten = Blätter × cost_per_sheet (Duplex = halbe Blätter)
      Tonerkosten  = Seiten × cost_per_color/mono
      Gesamt       = Papier + Toner

    Args:
        start_date:     Startdatum (YYYY-MM-DD)
        end_date:       Enddatum   (YYYY-MM-DD)
        cost_per_sheet: Kosten pro Blatt Papier (default: 0.01 €)
        cost_per_mono:  Kosten pro S/W-Seite Toner (default: 0.02 €)
        cost_per_color: Kosten pro Farbseite Toner (default: 0.08 €)
        group_by:       day | week | month | site (default: month)
        site_id:        Optional — Netzwerk-ID für Standort-Filter
        currency:       Währungssymbol für Ausgabe (default: €)
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


@mcp.tool()
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
    Ranking der aktivsten Nutzer nach Druckvolumen oder Kosten.

    Args:
        start_date:     Startdatum (YYYY-MM-DD)
        end_date:       Enddatum   (YYYY-MM-DD)
        top_n:          Anzahl Nutzer im Ranking (default: 10)
        metric:         Sortierung: pages | cost | jobs | color_pages (default: pages)
        cost_per_sheet: Kosten pro Blatt (für Kostenkalkulation)
        cost_per_mono:  Kosten pro S/W-Seite
        cost_per_color: Kosten pro Farbseite
        site_id:        Optional — Netzwerk-ID für Standort-Filter
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


@mcp.tool()
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
    Ranking der meistgenutzten Drucker nach Volumen oder Kosten.

    Args:
        start_date:     Startdatum (YYYY-MM-DD)
        end_date:       Enddatum   (YYYY-MM-DD)
        top_n:          Anzahl Drucker im Ranking (default: 10)
        metric:         Sortierung: pages | cost | jobs | color_pages (default: pages)
        cost_per_sheet: Kosten pro Blatt
        cost_per_mono:  Kosten pro S/W-Seite
        cost_per_color: Kosten pro Farbseite
        site_id:        Optional — Netzwerk-ID für Standort-Filter
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


@mcp.tool()
def printix_query_anomalies(
    start_date: str,
    end_date: str,
    threshold_multiplier: float = 2.5,
) -> str:
    """
    Anomalie-Erkennung: Tage mit ungewöhnlich hohem oder niedrigem Druckvolumen.

    Berechnet Mittelwert und Standardabweichung des täglichen Druckvolumens
    und markiert Tage die mehr als threshold_multiplier × StdAbw abweichen.

    Args:
        start_date:           Startdatum (YYYY-MM-DD)
        end_date:             Enddatum   (YYYY-MM-DD)
        threshold_multiplier: Faktor für Ausreißer-Schwelle (default: 2.5 = 2,5 × StdAbw)
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


@mcp.tool()
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
    Vergleich zweier Zeiträume — z.B. aktueller Monat vs. Vormonat.

    Liefert für beide Perioden Gesamtwerte und berechnet prozentuale Veränderungen
    für Seiten, Kosten, aktive Nutzer und Auftragsvolumen.

    Args:
        period1_start: Startdatum Periode 1 (YYYY-MM-DD), z.B. letzter Monat
        period1_end:   Enddatum   Periode 1 (YYYY-MM-DD)
        period2_start: Startdatum Periode 2 (YYYY-MM-DD), z.B. aktueller Monat
        period2_end:   Enddatum   Periode 2 (YYYY-MM-DD)
        cost_per_sheet: Kosten pro Blatt
        cost_per_mono:  Kosten pro S/W-Seite
        cost_per_color: Kosten pro Farbseite
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

@mcp.tool()
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
    Speichert eine vollständige Report-Definition als wiederverwendbares Template.

    Das Template enthält alle Informationen für automatische Ausführung:
    Query-Parameter, Layout, Schedule und Empfänger.
    Bei Angabe einer report_id wird ein bestehendes Template überschrieben.

    TIPP: Nutze zuerst printix_list_design_options() um verfügbare Themes,
    Chart-Typen, Fonts etc. zu sehen. Mit printix_preview_report() kannst
    du das Design testen bevor du es als Template speicherst.

    Args:
        name:               Lesbarer Name, z.B. "Monatlicher Kostenreport Controlling"
        query_type:         print_stats | cost_report | top_users | top_printers | anomalies | trend | hour_dow_heatmap
                            sowie Stufe-2-Typen: printer_history | device_readings | job_history |
                            user_activity | sensitive_documents | dept_comparison | waste_analysis |
                            color_vs_bw | duplex_analysis | paper_size | service_desk |
                            fleet_utilization | sustainability | peak_hours | cost_allocation
        query_params:       JSON-String mit Query-Parametern, z.B. '{"start_date":"last_month_start","end_date":"last_month_end","group_by":"month"}'
        recipients:         Kommagetrennte E-Mail-Adressen, z.B. "controller@firma.de,cfo@firma.de"
        mail_subject:       Betreffzeile, z.B. "Druckkosten {month} {year}"
        output_formats:     Kommagetrennte Formate: html,csv,json,pdf,xlsx (default: html)
        schedule_frequency: Leer = kein Schedule | monthly | weekly | daily
        schedule_day:       Bei monthly: Tag 1-28. Bei weekly: 0=Mo...6=So (default: 1)
        schedule_time:      Uhrzeit der Ausführung HH:MM (default: 08:00)
        company_name:       Firmenname im Report-Header
        primary_color:      Primärfarbe im Report-Design (Hex, default: #0078D4)
        footer_text:        Optionaler Fußzeilentext
        created_prompt:     Ursprüngliche Nutzeranfrage (für spätere Regenerierung)
        report_id:          Optional — vorhandene ID zum Überschreiben
        logo_base64:        Optional — Base64-Kodierung (ohne data:-Prefix) eines Logo-Bildes
                            für den Report-Header. Max. 1 MB Rohgröße. Hat Vorrang vor logo_url.
        logo_mime:          MIME-Type des Base64-Logos, z.B. image/png, image/jpeg (default: image/png)
        logo_url:           Alternativ: externe URL zu einem Logo-Bild (nur wenn logo_base64 leer)
        theme_id:           Design-Theme: corporate_blue | modern_teal | executive_slate |
                            warm_sunset | forest_green | royal_purple | minimalist_gray
                            (leer = corporate_blue). Setzt automatisch passende Farben.
        chart_type:         Bevorzugter Chart-Typ: bar | line | donut | heatmap | sparkline
                            (leer = automatische Wahl je nach Report-Typ)
        header_variant:     Header-Stil: left | center | banner (default: left)
        density:            Tabellen-Dichte: compact | normal | comfortable (default: normal)
        font_family:        Schriftart: arial | helvetica | verdana | georgia | courier (default: arial)
        currency:           Währung: EUR | USD | GBP | CHF (default: EUR)
        show_env_impact:    Umwelt-Impact-Sektion anzeigen: true | false (default: false)
        logo_position:      Logo-Position im Header: left | right | center (default: right)
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


@mcp.tool()
def printix_list_report_templates() -> str:
    """
    Listet alle gespeicherten Report-Templates des aktuellen Benutzers.

    WICHTIG: Dieses Tool immer zuerst aufrufen wenn der Benutzer einen Report
    ausführen, versenden, löschen oder planen möchte und keine report_id bekannt ist.
    Die report_id aus der Liste dann an printix_run_report_now oder andere Tools übergeben.

    Gibt Name, Query-Typ, Empfänger, Schedule und nächste geplante Ausführung zurück.
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


@mcp.tool()
def printix_get_report_template(report_id: str) -> str:
    """
    Ruft ein einzelnes Report-Template vollständig ab.

    Args:
        report_id: Template-ID (aus printix_list_report_templates)
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


@mcp.tool()
def printix_delete_report_template(report_id: str) -> str:
    """
    Löscht ein Report-Template und entfernt einen eventuellen Schedule.

    Args:
        report_id: Template-ID (aus printix_list_report_templates)
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


@mcp.tool()
def printix_run_report_now(report_id: str = "", report_name: str = "") -> str:
    """
    Führt ein gespeichertes Report-Template sofort aus und versendet ihn per Mail.

    Workflow wenn der Benutzer "Report X senden" oder "schick mir den Bericht" sagt:
      1. Falls report_id unbekannt: printix_list_report_templates() aufrufen
      2. Passendes Template nach Name finden
      3. Dieses Tool mit der report_id aufrufen

    Alternativ: report_name direkt angeben — es wird automatisch nach Name gesucht
    (Groß-/Kleinschreibung egal, Teilstring reicht, z.B. "Monat" findet "Monatsbericht IT").

    Args:
        report_id:   Template-ID (aus printix_list_report_templates) — bevorzugt
        report_name: Name-Suche als Alternative wenn keine ID bekannt
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


@mcp.tool()
def printix_send_test_email(recipient: str) -> str:
    """
    Sendet eine Test-E-Mail über den konfigurierten Resend-API-Key des Tenants.
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


@mcp.tool()
def printix_schedule_report(
    report_id: str,
    frequency: str,
    day: int = 1,
    time: str = "08:00",
) -> str:
    """
    Legt einen Zeitplan für ein bestehendes Report-Template an oder aktualisiert ihn.

    Für monatliche Reports empfiehlt sich Tag 1-3 (Anfang des Folgemonats).
    Alle Zeiten in UTC.

    Args:
        report_id: Template-ID (aus printix_list_report_templates)
        frequency: monthly | weekly | daily
        day:       Bei monthly: 1-28 (Tag des Monats).
                   Bei weekly:  0=Montag … 6=Sonntag (default: 1)
        time:      Uhrzeit UTC HH:MM (default: 08:00)
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


@mcp.tool()
def printix_list_schedules() -> str:
    """
    Listet alle aktiven Report-Schedules mit nächstem Ausführungszeitpunkt.
    """
    if not _REPORTING_AVAILABLE:
        return _ok({"error": "Reporting-Modul nicht verfügbar."})
    try:
        jobs = rep_scheduler.list_scheduled_jobs()
        return _ok({"schedules": jobs, "count": len(jobs)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_delete_schedule(report_id: str) -> str:
    """
    Entfernt den Zeitplan eines Reports (Template bleibt erhalten).

    Args:
        report_id: Template-ID deren Schedule entfernt werden soll
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


@mcp.tool()
def printix_update_schedule(
    report_id: str,
    frequency: str = "",
    day: int = 0,
    time: str = "",
    recipients: str = "",
) -> str:
    """
    Ändert Timing oder Empfänger eines bestehenden Schedules.

    Nur angegebene Parameter werden geändert — alle anderen bleiben unverändert.

    Args:
        report_id:  Template-ID
        frequency:  Neu: monthly | weekly | daily (leer = unverändert)
        day:        Neu: Tag (0 = unverändert)
        time:       Neu: Uhrzeit UTC HH:MM (leer = unverändert)
        recipients: Neue kommagetrennte Empfängerliste (leer = unverändert)
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

@mcp.tool()
def printix_list_design_options() -> str:
    """
    Listet alle verfügbaren Design-Optionen für Report-Templates:
    Themes, Chart-Typen, Fonts, Header-Varianten, Dichte-Stufen, Währungen.

    Nutze diese Informationen beim Erstellen oder Bearbeiten von Report-Templates,
    um dem Benutzer passende Optionen vorzuschlagen.

    Beispiel-Workflow:
      1. printix_list_design_options() → zeigt alle Themes
      2. Benutzer wählt "executive_slate" als Theme
      3. printix_save_report_template(..., theme_id="executive_slate") → speichert
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


@mcp.tool()
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
    Erzeugt eine Report-Vorschau OHNE E-Mail-Versand — ideal zum iterativen
    Design im AI-Chat. Gibt den vollständigen Report als HTML (mit eingebetteten
    SVG-Charts) oder als JSON-Datenstruktur zurück.

    Zwei Modi:
      1. Ad-hoc: query_type + Datumsbereich angeben → Daten frisch abfragen
      2. Template: report_id angeben → gespeichertes Template als Basis

    Bei Ad-hoc wird ein kompletter Report gerendert inkl. KPIs, Charts und Tabellen.

    Workflow für AI-gesteuertes Report-Design:
      1. printix_list_design_options() → verfügbare Themes etc.
      2. printix_preview_report(query_type="print_stats", theme_id="executive_slate")
         → Vorschau ansehen
      3. "Kannst du die Farbe auf Grün ändern?" → printix_preview_report(..., primary_color="#1BA17D")
      4. Zufrieden? → printix_save_report_template(...) zum Speichern

    Args:
        query_type:        Report-Typ (print_stats, cost_report, top_users, etc.)
        start_date:        Start-Datum oder Preset (last_month_start, this_year_start, etc.)
        end_date:          End-Datum oder Preset
        query_params_json: Zusätzliche Query-Parameter als JSON-String
                           z.B. '{"group_by":"month","site_id":"123"}'
        theme_id:          Theme (corporate_blue, executive_slate, dark_mode, etc.)
        primary_color:     Überschreibt die Theme-Primärfarbe (Hex, z.B. #0078D4)
        chart_type:        Bevorzugter Chart-Typ: auto | bar | line | donut | heatmap
        company_name:      Firmenname im Header
        header_variant:    left | center | banner | minimal
        density:           compact | normal | airy
        font_family:       arial | georgia | roboto | fira_code | etc.
        logo_base64:       Base64-kodiertes Logo (ohne data:-Prefix)
        logo_mime:         MIME-Type des Logos (default: image/png)
        footer_text:       Fußzeile
        currency:          EUR | USD | GBP | CHF
        show_env_impact:   Umwelt-Auswirkung anzeigen (Papier, Bäume, CO₂)
        output_format:     html (Standard, mit Charts) | json (nur Rohdaten)
        report_id:         Optional — vorhandenes Template als Basis laden
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


@mcp.tool()
def printix_query_any(
    query_type: str,
    start_date: str = "last_month_start",
    end_date: str = "last_month_end",
    query_params_json: str = "",
) -> str:
    """
    Universelles Query-Tool für alle 22 Report-Typen (Stufe 1 + 2).

    Ersetzt die Notwendigkeit, für jeden Query-Typ ein eigenes MCP-Tool zu kennen.
    Gibt die Rohdaten als JSON zurück — ideal für AI-Analyse, Visualisierung
    oder als Basis für printix_preview_report.

    Verfügbare query_type-Werte:
      Stufe 1: print_stats, cost_report, top_users, top_printers, anomalies, trend
      Stufe 2: printer_history, device_readings, job_history, queue_stats,
               user_detail, user_copy_detail, user_scan_detail,
               workstation_overview, workstation_detail,
               tree_meter, service_desk, sensitive_documents,
               hour_dow_heatmap, off_hours_print, audit_log

    Args:
        query_type:        Einer der oben genannten Query-Typen
        start_date:        Start (Datum oder Preset: last_month_start, this_year_start, today, etc.)
        end_date:          Ende (Datum oder Preset)
        query_params_json: Weitere Parameter als JSON, z.B.:
                           '{"group_by":"user","site_id":"abc","top_n":20}'
                           '{"user_email":"max@firma.de"}'
                           '{"keyword_sets":"hr,finance","include_scans":true}'
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


@mcp.tool()
def printix_demo_setup_schema() -> str:
    """
    Initialisiert die lokale Demo-SQLite-Datenbank (idempotent).

    Legt folgende Demo-Tabellen an (nur wenn sie noch nicht existieren):
      demo_networks, demo_users, demo_printers, demo_jobs, demo_tracking_data,
      demo_jobs_scan, demo_jobs_copy, demo_jobs_copy_details, demo_sessions

    Idempotent — kann mehrfach ohne Schaden ausgeführt werden.
    Kein Azure SQL erforderlich — Demo-Daten liegen lokal auf SQLite.
    """
    err = _demo_check()
    if err:
        return _ok({"error": err})
    try:
        result = _demo_gen.setup_schema()
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
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
    Generiert ein vollständiges Demo-Dataset in der lokalen SQLite-Datenbank.

    Erstellt realistische Druck-, Scan- und Kopierjobs für den angegebenen Zeitraum
    — rückwirkend ab heute. Alle Reports (Volumen, Kosten, Top-User, Trends usw.)
    zeigen danach aussagekräftige Demo-Daten. Kein Azure SQL erforderlich.

    Args:
        user_count:        Anzahl Demo-User (1–200, default: 15)
        printer_count:     Anzahl Demo-Drucker (1–50, default: 6)
        months:            Anzahl Monate rückwirkend ab heute (1–36, default: 12)
        languages:         Kommagetrennte Sprachliste für Benutzernamen
                           Verfügbar: de, en, fr, it, es, nl, sv, no
                           Beispiel: "de,fr,en" → gemischte Herkunft
        sites:             Kommagetrennte Standortnamen
                           Beispiel: "Hauptsitz,München,Wien,Zürich"
        demo_tag:          Name für diese Demo-Session (für späteres Rollback)
                           Beispiel: "DEMO_ACME_2025" — leer = automatisch generiert
        jobs_per_user_day: Durchschnittliche Druckjobs pro User pro Werktag (default: 3.0)

    Beispiel-Aufruf:
        "Erstelle Demo-Daten: 20 User, 8 Drucker, 12 Monate, Sprachen DE/FR/EN,
         Standorte Berlin/Hamburg/München, Tag DEMO_KUNDE_2025"
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


@mcp.tool()
def printix_demo_rollback(demo_tag: str) -> str:
    """
    Löscht alle Demo-Daten einer bestimmten Session aus der lokalen SQLite-DB.

    Entfernt alle Zeilen aus demo_tracking_data, demo_jobs, demo_jobs_scan,
    demo_jobs_copy, demo_jobs_copy_details, demo_printers, demo_users,
    demo_networks und demo_sessions für den angegebenen demo_tag.

    Voraussetzung: printix_demo_status zeigt vorhandene Tags.

    Args:
        demo_tag: Name der Demo-Session, z.B. "DEMO_ACME_2025"
                  (sichtbar in printix_demo_status)
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


@mcp.tool()
def printix_demo_status() -> str:
    """
    Zeigt alle aktiven Demo-Sessions im aktuellen Tenant.

    Listet jede Session mit demo_tag, Erstellungsdatum, Anzahl User/Drucker/Jobs.
    Nützlich um Tags für printix_demo_rollback zu ermitteln.
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


@mcp.tool()
def printix_list_card_profiles() -> str:
    """
    Listet alle Karten-Transformationsprofile des Tenants.

    Profile definieren wie Kartenwerte umgewandelt werden (z.B. HEX→Decimal,
    Base64-Encoding, Byte-Reversal). Enthält sowohl Built-in-Profile
    (YSoft, Ricoh, Canon, etc.) als auch benutzerdefinierte.

    Felder: id, name, vendor, reader_model, mode, description, is_builtin, rules_json.
    """
    try:
        from cards.store import list_profiles
        tid = _get_card_tenant_id()
        profiles = list_profiles(tid)
        return _ok({"profiles": profiles, "count": len(profiles)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_get_card_profile(profile_id: str) -> str:
    """
    Zeigt Details eines Karten-Transformationsprofils.

    Enthält die vollständigen Regeln (rules_json) für die Transformation:
    strip_separators, input_mode, submit_mode, base64_source,
    remove_chars, replace_map, trim_prefix, append, prepend, etc.

    Args:
        profile_id: Profil-ID (z.B. 'builtin-plain-base64' oder eigene UUID).
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


@mcp.tool()
def printix_search_card_mappings(search: str = "", printix_user_id: str = "") -> str:
    """
    Durchsucht lokale Karten-Mappings.

    Jedes Mapping speichert die Zuordnung: Kartenwert → Printix-User,
    inklusive aller Transformations-Zwischenschritte (raw → normalized → final),
    das verwendete Profil und Notizen.

    Args:
        search:          Suchbegriff (sucht in raw-Wert, normalized, HEX, Base64).
        printix_user_id: Optional: nur Mappings für diesen Printix-User.
    """
    try:
        from cards.store import search_mappings
        tid = _get_card_tenant_id()
        q = search or printix_user_id or ""
        results = search_mappings(tid, q)
        return _ok({"mappings": results, "count": len(results)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_get_card_details(card_id: str = "", card_number: str = "") -> str:
    """
    Karte abfragen mit vollständigen Details — kombiniert Printix API + lokale DB.

    Liefert:
    - Printix Cloud: cardId, registeredAt, owner
    - Lokale DB: Transformation (raw → normalized → final), Profil-Name/Vendor,
      Reader-Model, Notizen, Vorschau aller Zwischenschritte

    Das ist die "enriched" Version von printix_search_card — nutze dieses Tool
    wenn Du möglichst viele Details zu einer Karte brauchst.

    Args:
        card_id:     Printix Card-ID.
        card_number: Kartennummer (wird automatisch Base64-codiert wenn nötig).
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


@mcp.tool()
def printix_decode_card_value(card_value: str) -> str:
    """
    Analysiert und decodiert einen Kartenwert — erkennt Format automatisch.

    Nützlich wenn ein Kartenwert aus einem Leser kommt und man verstehen will,
    welches Format vorliegt (ASCII, HEX, YSoft Decimal, Konica, etc.).

    Gibt zurück: erkanntes Format, decodierte Bytes, HEX-Darstellung,
    Decimal-Wert, reversed Bytes, mögliche Interpretationen.

    Args:
        card_value: Der Rohwert von der Karte (z.B. "MDQ1RkYwMDI=" oder "04:5F:F0:02").
    """
    try:
        from cards.transform import decode_printix_secret_value
        result = decode_printix_secret_value(card_value)
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_transform_card_value(
    card_value: str,
    profile_id: str = "",
    strip_separators: bool = False,
    submit_mode: str = "",
) -> str:
    """
    Transformiert einen Kartenwert mit einem Profil oder manuellen Regeln.

    Wendet Transformationsregeln an: Separatoren entfernen, HEX/Decimal-Konvertierung,
    Base64-Encoding, Byte-Reversal, Prefix/Suffix, etc.

    Gibt den transformierten Wert + Vorschau aller Zwischenschritte zurück.

    Args:
        card_value:       Rohwert der Karte.
        profile_id:       Profil-ID für vordefinierte Regeln (optional).
        strip_separators: Trennzeichen (:-.) entfernen (wenn kein Profil).
        submit_mode:      'base64_text', 'hex', 'decimal', 'raw' (wenn kein Profil).
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


@mcp.tool()
def printix_get_user_card_context(user_id: str) -> str:
    """
    Vollständiger Kartenkontext eines Benutzers: User-Details + alle Karten + lokale Mappings.

    Ideal wenn ein Agent im Benutzerkontext arbeiten soll und neben den Printix-Karten
    auch die lokal gespeicherten echten Kartenwerte, Profile und Transformationen sehen soll.

    Args:
        user_id: Benutzer-ID in Printix.
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

@mcp.tool()
def printix_query_audit_log(
    start_date: str = "",
    end_date: str = "",
    action_prefix: str = "",
    object_type: str = "",
    limit: int = 200,
) -> str:
    """
    Abfrage des lokalen Audit-Logs (SQLite, nicht SQL Server).

    Das Audit-Log erfasst alle Aktionen im MCP-Portal: User-Genehmigungen,
    Passwort-Resets, Credential-Änderungen, Feature-Requests, etc.

    Args:
        start_date:    Startdatum (YYYY-MM-DD), leer = letzte 30 Tage.
        end_date:      Enddatum (YYYY-MM-DD), leer = heute.
        action_prefix: Filter auf Action-Prefix (z.B. 'user.' für User-Aktionen).
        object_type:   Filter auf Object-Type (z.B. 'user', 'tenant', 'feature_request').
        limit:         Max. Einträge (Standard: 200).
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


@mcp.tool()
def printix_list_feature_requests(status: str = "", limit: int = 100) -> str:
    """
    Listet Feature-Requests / Feedback-Tickets.

    Zeigt alle Tickets oder filtert nach Status.
    Gültige Status: new, planned, in_progress, done, rejected, later.

    Args:
        status: Optional: nur Tickets mit diesem Status.
        limit:  Max. Einträge (Standard: 100).
    """
    try:
        import db
        rows = db.list_feature_requests(status=status, limit=limit)
        counts = db.count_feature_requests_by_status()
        return _ok({"feature_requests": rows, "count": len(rows),
                     "status_counts": counts})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_get_feature_request(ticket_id: int) -> str:
    """
    Zeigt Details eines Feature-Request-Tickets.

    Args:
        ticket_id: Die numerische Ticket-ID (nicht die Ticketnummer).
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

@mcp.tool()
def printix_list_backups() -> str:
    """
    Listet alle verfügbaren Backups des MCP-Servers.

    Backups enthalten: SQLite-Datenbanken (printix_multi.db, demo_data.db),
    Fernet-Schlüssel, Report-Templates, MCP-Secrets.

    Gibt Dateiname, Größe, Erstellungsdatum und Version zurück.
    """
    try:
        from backup_manager import list_backups
        backups = list_backups()
        return _ok({"backups": backups, "count": len(backups)})
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_create_backup() -> str:
    """
    Erstellt ein vollständiges Backup des MCP-Servers.

    Sichert: printix_multi.db, demo_data.db, fernet.key,
    report_templates.json, mcp_secrets.json.

    Gibt den Dateinamen und die Größe des erstellten Backups zurück.
    """
    try:
        from backup_manager import create_backup
        result = create_backup()
        return _ok(result)
    except Exception as e:
        return _ok({"error": str(e)})


# ─── Capture Profile Management ─────────────────────────────────────────────

@mcp.tool()
def printix_list_capture_profiles() -> str:
    """
    Listet alle Capture-Profile (Scan-Weiterleitungsregeln) des Tenants.

    Capture-Profile definieren Webhooks für die Printix Scan-Erfassung.
    Jedes Profil hat eine Plugin-Konfiguration (z.B. Paperless-NGX).

    Felder: id, name, plugin_type, webhook_url, config.
    """
    try:
        import db
        tid = _get_card_tenant_id()
        profiles = db.get_capture_profiles_by_tenant(tid)
        # Webhook-Base-URL ergänzen
        base_url = os.environ.get("CAPTURE_PUBLIC_URL", "").rstrip("/")
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


@mcp.tool()
def printix_capture_status() -> str:
    """
    Zeigt den Status des Capture-Systems.

    Enthält: ob der separate Capture-Server aktiv ist,
    welche Plugins verfügbar sind, Webhook-Base-URL,
    und Anzahl konfigurierter Profile.
    """
    try:
        capture_enabled = os.environ.get("CAPTURE_ENABLED", "false").lower() == "true"
        cap_url = os.environ.get("CAPTURE_PUBLIC_URL", "").rstrip("/")
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

@mcp.tool()
def printix_site_summary(site_id: str) -> str:
    """
    Vollständige Zusammenfassung einer Site: Site-Details + alle Networks + alle Drucker.

    Kombiniert mehrere API-Calls in einem Tool — spart Round-Trips.
    Ideal um einen schnellen Überblick über einen Standort zu bekommen.

    Args:
        site_id: Die Site-ID.
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


@mcp.tool()
def printix_network_printers(network_id: str = "", site_id: str = "") -> str:
    """
    Listet alle Drucker eines bestimmten Netzwerks oder einer Site.

    Filtert die Drucker-Liste nach Netzwerk- oder Site-Zugehörigkeit.
    Nützlich um zu sehen welche Geräte in welchem Netzwerk-Segment stehen.

    Args:
        network_id: Netzwerk-ID (optional).
        site_id:    Site-ID (optional, listet alle Drucker aller Networks der Site).
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


@mcp.tool()
def printix_get_queue_context(queue_id: str, printer_id: str = "") -> str:
    """
    Liefert den vollständigen Kontext einer Queue: Queue-/Printer-Objekt + letzte Jobs.

    Praktisch wenn ein Agent mit einer Queue arbeiten soll, aber aus der normalen
    list_printers-Antwort erst den passenden Printer/Queue-Eintrag herauslösen müsste.

    Args:
        queue_id: Queue-ID.
        printer_id: Optionale Printer-ID zur direkten Auflösung.
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


@mcp.tool()
def printix_get_network_context(network_id: str) -> str:
    """
    Liefert den vollständigen Kontext eines Netzwerks: Network + Site + Drucker + SNMP-Bezüge.

    Args:
        network_id: Netzwerk-ID.
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


@mcp.tool()
def printix_get_snmp_context(config_id: str) -> str:
    """
    Liefert den vollständigen Kontext einer SNMP-Konfiguration: SNMP + zugeordnete Networks/Sites/Drucker.

    Args:
        config_id: SNMP-Konfigurations-ID.
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

@mcp.tool()
def printix_find_user(query: str) -> str:
    """
    Fuzzy-Suche nach einem User ueber E-Mail, Name oder ID.

    Durchsucht USER und GUEST_USER im aktuellen Tenant. Liefert Kandidaten
    mit Score-freier Substring-Match. Ideal als Vorstufe fuer Tools, die
    eine user_id brauchen (printix_user_360, printix_get_user_card_context).

    Args:
        query: Suchbegriff (Teilstring in E-Mail / Name / ID).
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


@mcp.tool()
def printix_user_360(query: str) -> str:
    """
    Komplettes Profil eines Users auf einen Blick.

    Sucht den User via Fuzzy-Match und liefert: Profil, Karten (enriched),
    Workstations, Gruppen und lokale Card-Mappings. Perfekter Startpunkt
    fuer "was ist los mit User X?"-Fragen.

    Args:
        query: Suchbegriff (E-Mail, Name oder ID).
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


@mcp.tool()
def printix_printer_health_report() -> str:
    """
    Health-Report ueber alle Drucker des Tenants.

    Aggregiert Online/Offline/Status und gruppiert nach Site. Liefert
    ausserdem die Liste der offline-Drucker als "Problemfaelle" fuer
    schnelle Entscheidung.
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


@mcp.tool()
def printix_tenant_summary() -> str:
    """
    Executive-Dashboard in einem Call: Counts + Highlights.

    Liefert: Tenant-Info, User-Counts (USER/GUEST), aktive Drucker,
    Workstation-Anzahl, Kartenzaehler, Top-Gruppen. Fuer
    "gib mir einen Ueberblick ueber die Organisation"-Prompts.
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


@mcp.tool()
def printix_diagnose_user(email: str) -> str:
    """
    Troubleshooting-Tool: "Warum kann User X nicht drucken?"

    Prueft heuristisch: User existiert, Rolle passt, hat Karten registriert,
    hat Workstation, SSO-Status. Liefert eine Liste von Findings mit
    Severity + Loesungsvorschlag.

    Args:
        email: E-Mail-Adresse des Users.
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

@mcp.tool()
def printix_list_cards_by_tenant(status: str = "all") -> str:
    """
    Alle Karten tenant-weit ueber alle User hinweg.

    Sammelt Karten aller User (USER + GUEST_USER), reichert mit lokalem
    Mapping an und erlaubt Filter.

    Args:
        status: 'all' | 'unmapped' (nur Karten ohne lokales Mapping) |
                'mapped' (nur Karten mit Mapping).
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


@mcp.tool()
def printix_find_orphaned_mappings() -> str:
    """
    Lokale Card-Mappings ohne zugehoerige Printix-Karte ("Leichen").

    Laedt alle lokalen Mappings, dann die tenant-weiten Printix-Karten, und
    liefert die Differenz. Typische Ursache: Karte wurde ausserhalb unserer
    App in Printix geloescht — unser DB-Mapping blieb.
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


@mcp.tool()
def printix_bulk_import_cards(
    csv_data: str,
    profile_id: str = "",
    dry_run: bool = True,
) -> str:
    """
    Massenimport von Karten aus CSV. Header-Zeile erwartet:
    `email,card_uid[,notes]`

    Transformiert jede UID mit dem gewaehlten Profil und registriert sie
    in Printix. Mit dry_run=True wird nur simuliert (keine API-Calls,
    nur Preview).

    Args:
        csv_data:   CSV mit Header 'email,card_uid' (optional 'notes').
        profile_id: Transform-Profil (leer = Passthrough).
        dry_run:    True = nur validieren+preview, False = tatsaechlich registrieren.
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


@mcp.tool()
def printix_suggest_profile(sample_uid: str) -> str:
    """
    Schlaegt das passendste Transform-Profil fuer eine Sample-UID vor.

    Wendet alle Profile nacheinander an, scored nach (transform erfolgreich?
    final_value != raw? length-plausibel?) und liefert Ranking.

    Args:
        sample_uid: Beispiel-UID wie am Kartenleser gescannt.
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


@mcp.tool()
def printix_card_audit(user_email: str) -> str:
    """
    Audit-Trail fuer die Karten eines Users: was ist registriert, wann,
    welche Notizen, welches Profil.

    Args:
        user_email: E-Mail-Adresse.
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

@mcp.tool()
def printix_top_printers(days: int = 7, limit: int = 10, metric: str = "pages") -> str:
    """
    Meistgenutzte Drucker der letzten N Tage. Convenience-Wrapper um
    printix_query_top_printers mit auto-berechneten Datumsgrenzen.

    Args:
        days:   Zeitfenster in Tagen (default: 7).
        limit:  Top-N (default: 10).
        metric: pages | cost | jobs | color_pages.
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


@mcp.tool()
def printix_top_users(days: int = 7, limit: int = 10, metric: str = "pages") -> str:
    """
    Aktivste User der letzten N Tage. Convenience-Wrapper um
    printix_query_top_users.

    Args:
        days:   Zeitfenster in Tagen (default: 7).
        limit:  Top-N (default: 10).
        metric: pages | cost | jobs | color_pages.
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


@mcp.tool()
def printix_jobs_stuck(minutes: int = 15) -> str:
    """
    Jobs die laenger als `minutes` in der Queue haengen.

    Nutzt Printix list_print_jobs (aktueller Snapshot) und filtert nach
    Alter + Status. Basis fuer Alerting/Monitoring.

    Args:
        minutes: Schwellwert in Minuten (default: 15).
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


@mcp.tool()
def printix_print_trends(group_by: str = "day", days: int = 30) -> str:
    """
    Zeitreihe fuer Druckvolumen. Convenience-Wrapper um printix_query_trend.

    Args:
        group_by: day | week | month (default: day).
        days:     Zeitfenster in Tagen (default: 30).
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


@mcp.tool()
def printix_cost_by_department(
    department_field: str = "department",
    days: int = 30,
    cost_per_mono: float = 0.02,
    cost_per_color: float = 0.08,
) -> str:
    """
    Kosten aggregiert pro Kostenstelle/Abteilung.

    Liest `department_field` aus den User-Custom-Attributen, gruppiert
    Druckvolumen und rechnet Kosten. Erfordert, dass Printix das
    Attribut pro User liefert.

    Args:
        department_field: Name des User-Attributs (default: 'department').
        days:             Zeitfenster.
        cost_per_mono:    Kosten pro S/W-Seite.
        cost_per_color:   Kosten pro Farbseite.
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


@mcp.tool()
def printix_compare_periods(
    days_a: int = 30,
    days_b: int = 30,
    offset_b: int = 30,
) -> str:
    """
    Vergleicht zwei gleichgrosse Zeitraeume: "letzte N Tage" vs. "die N Tage davor".

    Nuetzlich fuer "Wie hat sich Druckvolumen seit letztem Monat entwickelt?".

    Args:
        days_a:   Laenge Zeitraum A in Tagen (jetzt).
        days_b:   Laenge Zeitraum B in Tagen.
        offset_b: Wie weit zurueck Zeitraum B startet (default: gleiche Laenge vorher).
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

@mcp.tool()
def printix_list_admins() -> str:
    """
    Alle Admin-/Manager-Rollen im Tenant.

    Filtert aus list_users auf SYSTEM_MANAGER, SITE_MANAGER, KIOSK_MANAGER.
    Hinweis: manche Manager-Rollen sind ueber die Standard-API nicht
    listbar — dann bleibt die Liste leer und die Existenz muss aus dem
    Portal kommen.
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


@mcp.tool()
def printix_permission_matrix() -> str:
    """
    Matrix: User x Gruppen. Wer ist in welchen Gruppen? Gruppen steuern
    in Printix typischerweise Drucker-Zuweisung.
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


@mcp.tool()
def printix_inactive_users(days: int = 90) -> str:
    """
    User, die laenger als N Tage nicht aktiv waren.

    Nutzt lastSignIn / lastActivity falls verfuegbar. Wenn das Feld
    fehlt, wird der User als "unknown" eingestuft.

    Args:
        days: Schwellwert in Tagen (default: 90).
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


@mcp.tool()
def printix_sso_status(email: str) -> str:
    """
    Entra/Azure-SSO-Status fuer einen User: ist er via SSO verknuepft,
    hat er sich schonmal angemeldet, welche Auth-Quelle?

    Best-effort: Printix-API-Felder variieren, wir liefern was da ist.

    Args:
        email: E-Mail-Adresse.
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


@mcp.tool()
def printix_explain_error(code_or_message: str) -> str:
    """
    Erklaert einen Printix-/MCP-Fehlercode oder Fehlertext in Klartext +
    liefert Loesungsvorschlaege aus der internen Wissensbasis.

    Args:
        code_or_message: Fehlercode (z.B. 'no_printix_user') oder Teil
                         der Fehlermeldung.
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


@mcp.tool()
def printix_suggest_next_action(context: str) -> str:
    """
    Heuristischer Advisor: gibt man ihm den "Zustand" (z.B. "User X hat 3
    fehlgeschlagene Jobs"), liefert er plausible Next-Steps.

    Args:
        context: Beschreibung des Problems/Zustands.
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


@mcp.tool()
def printix_send_to_user(
    user_email: str,
    file_url: str = "",
    file_content_b64: str = "",
    filename: str = "document.pdf",
    target_printer: str = "",
    copies: int = 1,
) -> str:
    """
    High-Level: druckt ein Dokument als User X.

    Fuehrt in einem Call durch: User-Lookup, Printer-Resolve (falls Name
    statt ID), submit_print_job, upload, complete, change_owner.
    Entweder file_url ODER file_content_b64 angeben.

    Args:
        user_email:       Empfaenger (Owner der Secure-Print-Karte).
        file_url:         HTTP(S)-URL aus der das Dokument geladen wird.
        file_content_b64: Base64-kodierter Dateiinhalt (Alternative zu URL).
        filename:         Dateiname (fuer Titel + MIME-Detection).
        target_printer:   Printer-Name oder printer_id/queue_id kombiniert ('pid:qid').
        copies:           Anzahl Kopien (default: 1).
    """
    import base64 as _b64
    import requests as _req
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

        # Job submit
        job = c.submit_print_job(printer_id=printer_id, queue_id=queue_id,
                                  title=filename, size_bytes=len(file_bytes), copies=copies)
        job_id = job.get("jobId") or job.get("id") or ""
        upload_url = (job.get("_links") or {}).get("upload", {}).get("href") or job.get("uploadUrl") or ""
        if not (job_id and upload_url):
            return _ok({"error": "submit_print_job missing job_id or upload_url", "raw": job})

        c.upload_file_to_url(upload_url, file_bytes, filename=filename)
        c.complete_upload(job_id)
        c.change_job_owner(job_id, user_email)
        return _ok({
            "ok": True, "job_id": job_id, "owner_email": user_email,
            "filename": filename, "size": len(file_bytes),
            "printer_id": printer_id, "queue_id": queue_id,
        })
    except PrintixAPIError as e:
        return _err(e)
    except Exception as e:
        return _ok({"error": str(e)})


@mcp.tool()
def printix_onboard_user(
    email: str,
    display_name: str,
    role: str = "USER",
    pin: str = "",
    password: str = "",
    groups: str = "",
) -> str:
    """
    Komplett-Onboarding eines neuen Users: anlegen, optional in Gruppen
    stecken. Fuer echte SSO-User geht Printix ueblicherweise ueber Entra —
    dieses Tool ist fuer manuelle / Guest-Accounts.

    Args:
        email:        E-Mail-Adresse.
        display_name: Anzeigename.
        role:         Ignoriert (API legt GUEST_USER an); fuer spaeter.
        pin:          Optionale 4-stellige PIN.
        password:     Optionales Passwort.
        groups:       Komma-getrennte Group-IDs (best-effort Zuweisung).
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


@mcp.tool()
def printix_offboard_user(email: str, force: bool = False) -> str:
    """
    Leaver-Flow: alle Karten loeschen, offene Jobs canceln, User-Account
    deaktivieren/loeschen.

    Args:
        email: E-Mail des scheidenden Users.
        force: Wenn True, ueberspringt Rueckfragen (ist eh non-interactive).
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

@mcp.tool()
def printix_whoami() -> str:
    """
    Debug-Hilfe: zeigt den aktuellen Tenant-Kontext — welcher Tenant
    antwortet, welche OAuth-Apps sind konfiguriert, welche Scopes
    stehen zur Verfuegung.
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


@mcp.tool()
def printix_quick_print(recipient_email: str, file_url: str, filename: str = "document.pdf") -> str:
    """
    One-Shot-Druck: nutzt den ersten verfuegbaren Drucker im Tenant und
    sendet die Datei als Secure-Print-Job fuer den Empfaenger. Fuer
    "schick das schnell an Marcus"-Flows.

    Args:
        recipient_email: Empfaenger (Secure-Print-Owner).
        file_url:        HTTP(S)-URL der Datei.
        filename:        Dateiname (fuer Titel).
    """
    return printix_send_to_user(user_email=recipient_email, file_url=file_url,
                                 filename=filename, target_printer="", copies=1)


@mcp.tool()
def printix_resolve_printer(name_or_location: str) -> str:
    """
    Findet den besten passenden Drucker ueber Fuzzy-Match auf Name,
    Location, Model. Liefert printer_id + queue_id fuer andere Tools.

    Args:
        name_or_location: Suchstring ("HP im 3. OG", "Finance", "MFP-24").
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


@mcp.tool()
def printix_natural_query(question: str) -> str:
    """
    Hinweis-Tool fuer natuerlichsprachige Fragen an die Reports-Engine.

    Macht keine echte NLP — liefert stattdessen Vorschlaege, welche
    konkreten Reports-Tools fuer die Frage relevant sind. Der Agent
    kann dann das konkrete Tool aufrufen.

    Args:
        question: Natuerliche Frage ("Wer hat letzten Monat am meisten gedruckt?").
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

    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║        PRINTIX MCP SERVER v%s — MULTI-TENANT            ║", APP_VERSION)
    logger.info("╠══════════════════════════════════════════════════════════════╣")
    logger.info("║  MCP (claude.ai):  %s/mcp", base)
    logger.info("║  SSE (ChatGPT):    %s/sse", base)
    logger.info("║  OAuth Authorize:  %s/oauth/authorize", base)
    logger.info("║  OAuth Token:      %s/oauth/token", base)
    logger.info("║  Health-Check:     %s/health", base)
    logger.info("╠══════════════════════════════════════════════════════════════╣")
    # Host-Port aus /data/options.json lesen (= externer Port wie in HA-Netzwerk-Tab konfiguriert)
    try:
        import json as _json
        with open("/data/options.json") as _f:
            _opts = _json.load(_f)
        _host_web_port = int(_opts.get("web_port", os.environ.get("WEB_PORT", "8080")))
    except Exception:
        _host_web_port = int(os.environ.get("WEB_PORT", "8080"))
    logger.info("║  Benutzer registrieren:  http://<HA-IP>:%d", _host_web_port)
    # v4.6.7: Capture-Status (bool statt Port)
    _capture_enabled = os.environ.get("CAPTURE_ENABLED", "false").lower() == "true"
    if _capture_enabled:
        _cap_url = os.environ.get("CAPTURE_PUBLIC_URL", "").rstrip("/") or "http://<HA-IP>:8775"
        logger.info("║  Capture (separat): %s/capture/webhook/<id>", _cap_url)
    else:
        logger.info("║  Capture (via MCP): %s/capture/webhook/<id>", base)
    logger.info("╚══════════════════════════════════════════════════════════════╝")

    uvicorn.run(app, host=host, port=port, log_level=LOG_LEVEL.lower())
