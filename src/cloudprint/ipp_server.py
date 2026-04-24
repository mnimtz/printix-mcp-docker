"""
IPP/IPPS-Server Endpoint (v6.5.0)
==================================
FastAPI-Handler der IPP-Print-Jobs empfängt.

Architektur:
  Endgerät → Printix Client → Printix Cloud
    → POST <ipps-url>/ipp/<tenant-id>  (Content-Type: application/ipp)
    → wir parsen, identifizieren User (aus IPP-Attributes!), speichern,
      leiten an Secure Print Queue weiter + Delegate-Forwarding.

Killer-Vorteil gegenüber LPR:
  Der User kommt als `requesting-user-name` IPP-Attribut rein — eine
  E-Mail-Adresse, keine UUID! Damit entfällt der ganze list_users-
  Lookup-Tanz den wir für LPR bauen mussten.

TLS:
  Wir laufen intern auf HTTP. Cloudflare Tunnel oder ein Reverse-Proxy
  macht die TLS-Termination zum Client. Damit erscheint die URL als
  ipps://ipps.printix.cloud/ipp/<tenant> nach außen.

Routing:
  Der Tenant wird aus dem Pfad /ipp/<tenant-id> extrahiert. Das ist
  das Äquivalent zum LPR-Queue-Namen.
"""

from __future__ import annotations

import logging
import os
import time
import uuid as _uuid
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response

from cloudprint import ipp_parser as ipp

logger = logging.getLogger("printix.cloudprint.ipp")

IPP_SPOOL_DIR = os.environ.get("IPP_SPOOL_DIR", "/data/ipp-spool")
MAX_JOB_SIZE = int(os.environ.get("IPP_MAX_JOB_SIZE", 50 * 1024 * 1024))


# ─── Registrierung in FastAPI ────────────────────────────────────────────────

def register_ipp_routes(app: FastAPI) -> None:
    """Mountet /ipp/<tenant_id>-POST als IPP-Empfang."""

    @app.post("/ipp/{tenant_id}")
    async def ipp_receive(tenant_id: str, request: Request):
        body = await request.body()
        return await _handle_ipp_request(tenant_id, body, request)

    @app.get("/ipp/{tenant_id}")
    async def ipp_info(tenant_id: str, request: Request):
        """Einfacher GET-Handler für Browser-Zugriff / Health-Check.

        v6.6.2: Loggt jede GET-Probe, damit z.B. `curl http://.../ipp/<tid>`
        im Server-Log sichtbar ist (wichtig für Erreichbarkeitschecks).
        """
        peer = request.client.host if request.client else "?"
        ua   = request.headers.get("user-agent", "-")
        host_hdr = request.headers.get("host", "-")
        logger.info(
            "IPP: GET-Probe von %s → tenant=%s host=%s UA=%s",
            peer, tenant_id, host_hdr, ua,
        )
        return Response(
            content=(
                b"This is an IPP endpoint. POST with Content-Type: "
                b"application/ipp to send print jobs."
            ),
            media_type="text/plain",
            status_code=200,
        )


# ─── Core Request-Handling ───────────────────────────────────────────────────

async def _handle_ipp_request(tenant_id: str, body: bytes,
                                request: Request) -> Response:
    """Parst den IPP-Request und dispatcht auf die passende Operation."""
    if len(body) == 0:
        raise HTTPException(status_code=400, detail="Empty body")

    if len(body) > MAX_JOB_SIZE + 65536:
        raise HTTPException(status_code=413, detail="Body too large")

    peer = request.client.host if request.client else "?"
    ua   = request.headers.get("user-agent", "-")
    has_auth = "yes" if request.headers.get("authorization") else "no"
    host_hdr = request.headers.get("host", "-")

    # v6.7.3: MAXIMAL-Logging-Modus — alles was Printix uns mitschickt
    # auf einer einzigen INFO-Zeile + DEBUG-Dump für Detail-Debugging.
    # Ziel: zu sehen ob es irgendwo einen Tenant-Hinweis gibt den wir
    # bisher übersehen.
    try:
        full_url = str(request.url)
    except Exception:
        full_url = request.url.path if hasattr(request, "url") else "?"

    # Alle HTTP-Header dumpen (auf INFO, weil wirklich relevant für Debugging)
    headers_dump = " | ".join(f"{k}={v}" for k, v in request.headers.items())
    logger.info("IPP-HTTP: peer=%s url=%s headers={%s}", peer, full_url, headers_dump)

    # Scope-Info (TLS, ASGI-Server, Client) als DEBUG
    if logger.isEnabledFor(10):  # logging.DEBUG
        try:
            scope = request.scope or {}
            scope_dump = {
                "type":          scope.get("type"),
                "scheme":        scope.get("scheme"),
                "http_version":  scope.get("http_version"),
                "method":        scope.get("method"),
                "path":          scope.get("path"),
                "raw_path":      scope.get("raw_path"),
                "query_string":  scope.get("query_string"),
                "root_path":     scope.get("root_path"),
                "server":        scope.get("server"),
                "client":        scope.get("client"),
                "extensions":    list((scope.get("extensions") or {}).keys()),
            }
            logger.debug("IPP-Scope: %s", scope_dump)
            # TLS-Info wenn verfügbar (Uvicorn legt das unter scope['extensions']['tls'] ab)
            tls_info = (scope.get("extensions") or {}).get("tls")
            if tls_info:
                logger.debug("IPP-TLS: %s", tls_info)
        except Exception as _se:
            logger.debug("IPP-Scope-Dump fehlgeschlagen: %s", _se)

    try:
        req = ipp.parse_request(body)
    except ipp.IppParseError as e:
        logger.warning("IPP: Parse-Fehler von %s (UA=%s): %s", peer, ua, e)
        return _ipp_response(
            ipp.build_response(request_id=0,
                               status_code=ipp.STATUS_CLIENT_ERROR_BAD),
        )

    op_name = _ipp_op_name(req.operation_id)
    logger.info(
        "IPP: Request von %s → tenant=%s op=%s (0x%04x) version=%d.%d "
        "request-id=%d body=%d Bytes auth=%s host=%s UA=%s",
        peer, tenant_id, op_name, req.operation_id,
        req.version[0], req.version[1], req.request_id,
        len(body), has_auth, host_hdr, ua,
    )

    # v6.7.3: ALLE IPP-Attribut-Gruppen + alle Werte auf INFO loggen
    # (vorher nur DEBUG, und nur operation+job Gruppen).
    try:
        for grp_name, attrs in req.all_groups().items():
            if not attrs:
                continue
            for attr_name, attr in attrs.items():
                logger.info(
                    "IPP-Attr [%s] %s (tag=0x%02x) = %r",
                    grp_name, attr_name, attr.value_tag, attr.values,
                )
    except Exception as _ae:
        logger.debug("IPP-Attr-Dump fehlgeschlagen: %s", _ae)

    # Operation-Dispatch
    if req.operation_id == ipp.OP_PRINT_JOB:
        return await _handle_print_job(tenant_id, req, request, peer=peer, ua=ua)
    if req.operation_id == ipp.OP_VALIDATE_JOB:
        logger.info("IPP: Validate-Job von %s (tenant=%s) → OK", peer, tenant_id)
        return _ipp_response(ipp.build_validate_job_response(req.request_id))
    if req.operation_id == ipp.OP_GET_PRINTER_ATTRIBUTES:
        logger.info("IPP: Get-Printer-Attributes (Capability-Probe) von %s → tenant=%s",
                    peer, tenant_id)
        base = _derive_printer_uri(request, tenant_id)
        return _ipp_response(
            ipp.build_get_printer_attributes_response(
                request_id=req.request_id,
                printer_uri=base,
                printer_name=f"Cloud Print Port — Tenant {tenant_id[:8]}…",
            ),
        )
    if req.operation_id == ipp.OP_GET_JOBS:
        logger.info("IPP: Get-Jobs von %s (tenant=%s) → leere Liste (Printix trackt Jobs selbst)",
                    peer, tenant_id)
        # Leere Liste — wir tracken Jobs nicht per IPP, das macht Printix selbst
        return _ipp_response(
            ipp.build_response(
                request_id=req.request_id,
                status_code=ipp.STATUS_SUCCESSFUL_OK,
            ),
        )

    # v6.7.3: Get-Job-Attributes (0x0009) → Dummy-OK
    # Printix fragt nach dem Submit den Job-Status ab. Wir tracken die Jobs
    # nicht per IPP-Job-ID, also antworten wir mit "completed" — Printix gibt
    # sich damit zufrieden und betrachtet den Job als abgeschlossen.
    if req.operation_id == 0x0009:  # Get-Job-Attributes
        logger.info("IPP: Get-Job-Attributes von %s (tenant=%s) → Dummy 'completed'",
                    peer, tenant_id)
        return _ipp_response(
            ipp.build_get_job_attributes_response(
                request_id=req.request_id,
                job_id=int(time.time() * 1000) & 0x7FFFFFFF,
                printer_uri=_derive_printer_uri(request, tenant_id),
            ),
        )

    logger.warning("IPP: Nicht unterstützte Operation 0x%04x von %s (tenant=%s)",
                   req.operation_id, peer, tenant_id)
    return _ipp_response(ipp.build_unsupported_op_response(req.request_id))


async def _handle_print_job(tenant_id: str, req: ipp.IppRequest,
                              request: Request,
                              peer: str = "?", ua: str = "-") -> Response:
    """Verarbeitet einen Print-Job. Daten speichern + an Printix weiterleiten."""
    import asyncio

    meta = ipp.extract_job_metadata(req)
    job_id_internal = _uuid.uuid4().hex[:8]
    base_uri = _derive_printer_uri(request, tenant_id)

    # Dokument auf Disk speichern
    Path(IPP_SPOOL_DIR).mkdir(parents=True, exist_ok=True)
    tenant_dir = Path(IPP_SPOOL_DIR) / tenant_id
    tenant_dir.mkdir(parents=True, exist_ok=True)
    file_path = tenant_dir / f"{job_id_internal}.prn"

    data = req.data or b""
    if len(data) > MAX_JOB_SIZE:
        logger.warning("IPP: Job zu groß (%d Bytes)", len(data))
        return _ipp_response(
            ipp.build_response(req.request_id,
                               status_code=ipp.STATUS_CLIENT_ERROR_BAD),
        )

    with open(file_path, "wb") as f:
        f.write(data)

    # v6.6.2: Ausführliches Logging — LPR-Parität wiederhergestellt.
    resolved_user = (
        meta.get("requesting_user_name")
        or meta.get("job_originating_user_name")
        or ""
    )
    identity_src = (
        "requesting-user-name"        if meta.get("requesting_user_name")        else
        "job-originating-user-name"   if meta.get("job_originating_user_name")   else
        "<keine>"
    )
    logger.info(
        "IPP: PRINT-JOB empfangen — id=%s tenant=%s peer=%s UA=%s "
        "user='%s' (src=%s) host='%s' job='%s' document='%s' "
        "format='%s' copies=%d size=%d Bytes spool=%s",
        job_id_internal, tenant_id, peer, ua,
        resolved_user or "-", identity_src,
        meta.get("job_originating_host_name", "") or "-",
        meta.get("job_name", "") or "-",
        meta.get("document_name", "") or "-",
        meta.get("document_format", "") or "-",
        meta.get("copies", 1) or 1,
        len(data),
        file_path,
    )
    if not resolved_user:
        logger.warning(
            "IPP: PRINT-JOB ohne User-Identität — weder `requesting-user-name` "
            "noch `job-originating-user-name` gesetzt. Delegate-Forwarding "
            "wird übersprungen. (tenant=%s peer=%s UA=%s)",
            tenant_id, peer, ua,
        )
    # v6.7.3: IPP-Attribute werden zentral in `_handle_ipp_request` auf INFO
    # geloggt (alle Gruppen, alle Werte). Der separate DEBUG-Dump entfällt.

    # Printix-konforme numeric job-id (32-bit int erwartet)
    numeric_job_id = int(time.time() * 1000) & 0x7FFFFFFF

    # Job-Weiterleitung asynchron, damit wir sofort antworten können
    asyncio.create_task(
        _forward_ipp_job(
            tenant_id=tenant_id,
            internal_job_id=job_id_internal,
            file_path=str(file_path),
            data_size=len(data),
            meta=meta,
        )
    )

    # Positive Response an Printix zurück — Job wurde angenommen
    return _ipp_response(
        ipp.build_print_job_response(
            request_id=req.request_id,
            job_id=numeric_job_id,
            printer_uri=base_uri,
            job_state=ipp.JOB_STATE_PROCESSING,
        ),
    )


async def _forward_ipp_job(tenant_id: str, internal_job_id: str,
                             file_path: str, data_size: int,
                             meta: dict) -> None:
    """Leitet einen empfangenen IPP-Job an Printix Secure Print weiter.

    Nutzt die gleiche Logik wie der LPR-Forwarder — inklusive
    Delegate-Forwarding. Der Haupt-Unterschied: wir haben hier die
    User-Identität bereits aus den IPP-Attributen.
    """
    from cloudprint.db_extensions import (
        get_tenant_by_printix_id, get_default_single_tenant,
        resolve_tenant_by_user_identity, resolve_user_email,
        create_cloudprint_job, update_cloudprint_job_status,
    )
    from db import get_tenant_full_by_user_id

    # User-Identität direkt aus IPP — kein API-Call nötig!
    raw_user_identity = (
        meta.get("requesting_user_name")
        or meta.get("job_originating_user_name")
        or ""
    ).strip()
    identity_source = (
        "ipp-requesting-user" if meta.get("requesting_user_name") else
        "ipp-originating-user" if meta.get("job_originating_user_name") else ""
    )

    # v6.7.4: Tenant-Resolution-Kette
    # ─────────────────────────────────────────────────────────────────────
    # Printix-Workstation-Client schickt IMMER auf `/ipp/printer` (fester
    # Pfad), die echte Tenant-ID kommt im URL nicht mit. Drei Fallbacks:
    #   1. URL-Pfad ist eine UUID → benutze die (für curl-Tests / manuelle
    #      Setups die direkt die Tenant-ID im Pfad mitschicken).
    #   2. Username aus `requesting-user-name` → DB-Lookup → Tenant.
    #   3. Single-Tenant-Setup → den einzigen aktiven Tenant nehmen.
    tenant_info = None
    resolution_method = ""
    looks_like_uuid = (
        len(tenant_id) == 36
        and tenant_id.count("-") == 4
        and tenant_id != "printer"
    )
    if looks_like_uuid:
        tenant_info = get_tenant_by_printix_id(tenant_id)
        if tenant_info:
            resolution_method = "url-path"
    if not tenant_info and raw_user_identity:
        tenant_info = resolve_tenant_by_user_identity(raw_user_identity)
        if tenant_info:
            resolution_method = "username-lookup"
    if not tenant_info:
        tenant_info = get_default_single_tenant()
        if tenant_info:
            resolution_method = "single-tenant-fallback"

    if not tenant_info:
        logger.warning(
            "IPP: KEINE Tenant-Auflösung möglich — pfad='%s' user='%s'. "
            "Job wird verworfen. (Setup: User mit dem genannten Username/Email "
            "in DB anlegen oder Single-Tenant-Setup verwenden.)",
            tenant_id, raw_user_identity or "-",
        )
        try:
            os.remove(file_path)
        except Exception:
            pass
        return

    local_tenant_id = tenant_info["tenant_id"]
    resolved_printix_tenant = tenant_info.get("printix_tenant_id", "")
    logger.info(
        "IPP: Tenant aufgelöst via %s — local=%s printix=%s",
        resolution_method, local_tenant_id, resolved_printix_tenant,
    )

    def _tenant_log(level: str, msg: str) -> None:
        try:
            from db import add_tenant_log
            add_tenant_log(local_tenant_id, level, "CLOUDPRINT", msg)
        except Exception:
            pass

    try:
        # v6.7.4: User-Identity → E-Mail (für Printix-Submit + Delegate-Forwarding)
        user_identity = resolve_user_email(raw_user_identity) if raw_user_identity else ""
        if user_identity != raw_user_identity:
            logger.info(
                "IPP: User-Resolution — '%s' → '%s' (via persistenter Printix-Cache)",
                raw_user_identity, user_identity,
            )

        # Datenformat normieren (IPP liefert MIME-Type)
        data_format = meta.get("document_format") or _detect_format(file_path)

        # Tracking-Eintrag anlegen
        create_cloudprint_job(
            job_id=internal_job_id,
            tenant_id=local_tenant_id,
            queue_name=tenant_id,
            username=user_identity,
            hostname=meta.get("job_originating_host_name", ""),
            job_name=meta.get("job_name") or meta.get("document_name", ""),
            data_size=data_size,
            data_format=data_format,
            detected_identity=user_identity,
            identity_source=identity_source,
            status="received",
        )
        _tenant_log("INFO",
            f"IPP-Job empfangen: '{meta.get('job_name', '-') }' "
            f"von {user_identity or '-'} ({data_size} Bytes, {data_format})")

        target_queue = tenant_info.get("lpr_target_queue", "")
        if not target_queue:
            err = "Keine Ziel-Queue konfiguriert — bitte unter Cloud Print setzen"
            _tenant_log("WARNING", f"IPP-Job {internal_job_id}: {err}")
            update_cloudprint_job_status(internal_job_id, "error", error_message=err)
            return

        full_tenant = get_tenant_full_by_user_id(tenant_info["user_id"])
        if not full_tenant:
            update_cloudprint_job_status(internal_job_id, "error",
                error_message="Tenant-Credentials nicht gefunden")
            return

        from printix_client import PrintixClient
        client = PrintixClient(
            tenant_id=full_tenant["printix_tenant_id"],
            print_client_id=full_tenant.get("print_client_id", ""),
            print_client_secret=full_tenant.get("print_client_secret", ""),
            shared_client_id=full_tenant.get("shared_client_id", ""),
            shared_client_secret=full_tenant.get("shared_client_secret", ""),
            um_client_id=full_tenant.get("um_client_id", ""),
            um_client_secret=full_tenant.get("um_client_secret", ""),
        )

        # Zielqueue → Printer-ID auflösen (wie im LPR-Code)
        import re as _re
        printers_data = client.list_printers(size=200)
        raw_list = printers_data.get("printers", []) if isinstance(printers_data, dict) else []
        if not raw_list and isinstance(printers_data, dict):
            raw_list = (printers_data.get("_embedded") or {}).get("printers", [])

        queue_match = None
        for p in raw_list:
            href = (p.get("_links") or {}).get("self", {}).get("href", "")
            m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
            if m and m.group(2) == target_queue:
                queue_match = {"printer_id": m.group(1), "queue_id": m.group(2)}
                break

        if not queue_match:
            err = f"Ziel-Queue '{target_queue}' in Printix nicht gefunden"
            _tenant_log("ERROR", f"IPP-Job {internal_job_id}: {err}")
            update_cloudprint_job_status(internal_job_id, "error",
                error_message=err, target_queue=target_queue)
            return

        # MIME → Printix-PDL normalisieren
        pdl = _normalize_pdl(data_format)

        # Submit (mit User-Identity als user=)
        submit_user = user_identity if "@" in user_identity else None
        update_cloudprint_job_status(internal_job_id, "forwarding",
            target_queue=target_queue)

        # v6.7.10: release_immediately=False → Job landet in der Release-Queue
        # des angegebenen Users, statt sofort gedruckt zu werden. Das ist
        # essentiell für Cloud Print Port / Delegate Print: Der User (oder
        # Delegate) soll den Job an einem Drucker seiner Wahl freigeben
        # können, nicht direkt drucken. `True` (Default) hätte den Job
        # gezwungen sofort an den angegebenen Queue-Drucker zu gehen.
        result = client.submit_print_job(
            printer_id=queue_match["printer_id"],
            queue_id=target_queue,
            title=meta.get("job_name") or f"IPP-Job {internal_job_id}",
            user=submit_user,
            pdl=pdl,
            release_immediately=False,
        )

        result_job = result.get("job", result) if isinstance(result, dict) else {}
        printix_job_id = result_job.get("id", "") if isinstance(result_job, dict) else ""
        upload_url = ""
        upload_headers = {}
        if isinstance(result, dict):
            upload_url = result.get("uploadUrl", "") or ""
            upload_links = result.get("uploadLinks") or []
            if not upload_url and upload_links and isinstance(upload_links[0], dict):
                upload_url = upload_links[0].get("url", "") or ""
                upload_headers = upload_links[0].get("headers") or {}

        if not printix_job_id:
            err = "Printix API gab keinen Job zurück"
            _tenant_log("ERROR", f"IPP-Job {internal_job_id}: {err}")
            update_cloudprint_job_status(internal_job_id, "error",
                error_message=err, target_queue=target_queue)
            return

        # Datei hochladen
        with open(file_path, "rb") as f:
            file_bytes = f.read()
        if upload_url:
            client.upload_file_to_url(upload_url, file_bytes, data_format, upload_headers)
            client.complete_upload(printix_job_id)

        # v6.7.15: Owner-Wechsel. Der `user=`-Parameter beim Submit ist von
        # Printix IGNORIERT — der Job bekommt automatisch den OAuth-App-Owner
        # (System-Manager). Mit dem separaten `/changeOwner`-Endpoint
        # übertragen wir die Ownership auf den echten User damit der Job in
        # dessen Release-Queue landet.
        if user_identity and "@" in user_identity:
            try:
                client.change_job_owner(printix_job_id, user_identity)
                logger.info(
                    "IPP: Owner-Wechsel OK → Job %s gehört jetzt %s",
                    printix_job_id, user_identity,
                )
            except Exception as _co_err:
                logger.warning(
                    "IPP: Owner-Wechsel fehlgeschlagen für %s (%s): %s",
                    printix_job_id, user_identity, _co_err,
                )

        update_cloudprint_job_status(
            internal_job_id, "forwarded",
            printix_job_id=printix_job_id, target_queue=target_queue,
            detected_identity=user_identity,
            identity_source=identity_source,
        )
        _tenant_log("INFO",
            f"IPP-Job '{meta.get('job_name')}' weitergeleitet → "
            f"Printix-Job {printix_job_id} (User: {user_identity})")
        logger.info(
            "IPP: Job forwarded → Printix-Job-ID=%s User=%s",
            printix_job_id, user_identity,
        )

        # ── Delegate-Forwarding ──────────────────────────────────────────
        # Für jeden aktiven Delegate des Owners eine zusätzliche
        # Job-Kopie an Printix senden (siehe cloudprint/forwarder.py).
        try:
            from cloudprint.forwarder import forward_to_delegates
            # Pseudo-Job-Objekt mit den vom Forwarder erwarteten Attributen.
            class _JobShim:
                pass
            job_shim = _JobShim()
            job_shim.job_id = internal_job_id
            job_shim.queue_name = tenant_id
            job_shim.hostname = meta.get("job_originating_host_name", "")
            job_shim.job_name = meta.get("job_name", "")
            job_shim.username = user_identity
            job_shim.data_path = file_path
            job_shim.data_size = data_size

            forward_to_delegates(
                client=client,
                tenant_id_for_log=local_tenant_id,
                printix_tenant_id=full_tenant.get("printix_tenant_id", ""),
                parent_job_id=internal_job_id,
                owner_identity=user_identity,
                original_printix_job_id=printix_job_id,
                target_queue=target_queue,
                printer_id=queue_match["printer_id"],
                job=job_shim,
                data_format=pdl,
                file_bytes=file_bytes,
                data_path=file_path,
            )
        except Exception as de:
            logger.warning("IPP: Delegate-Forwarding fehlgeschlagen: %s", de)

    except Exception as e:
        logger.error("IPP: Fehler bei Forwarding %s: %s", internal_job_id, e)
        try:
            update_cloudprint_job_status(internal_job_id, "error",
                error_message=str(e)[:300])
        except Exception:
            pass
    finally:
        # Temp-Datei aufräumen
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass


# ─── Helpers ─────────────────────────────────────────────────────────────────

# v6.7.2: IPP-Operations-IDs als lesbare Namen — für aussagekräftiges Logging.
_IPP_OP_NAMES = {
    0x0002: "Print-Job",
    0x0003: "Print-URI",
    0x0004: "Validate-Job",
    0x0005: "Create-Job",
    0x0006: "Send-Document",
    0x0007: "Send-URI",
    0x0008: "Cancel-Job",
    0x0009: "Get-Job-Attributes",
    0x000A: "Get-Jobs",
    0x000B: "Get-Printer-Attributes",
    0x000C: "Hold-Job",
    0x000D: "Release-Job",
    0x000E: "Restart-Job",
    0x0010: "Pause-Printer",
    0x0011: "Resume-Printer",
    0x0012: "Purge-Jobs",
}

def _ipp_op_name(op_id: int) -> str:
    """Mapped eine IPP-Operation-ID auf einen lesbaren Namen für Logs."""
    return _IPP_OP_NAMES.get(op_id, f"Unknown-Op-0x{op_id:04x}")


def _ipp_response(body: bytes, status: int = 200) -> Response:
    """Wrappt einen IPP-Response-Body in HTTP Content-Type application/ipp."""
    return Response(content=body, media_type="application/ipp", status_code=status)


def _derive_printer_uri(request: Request, tenant_id: str) -> str:
    """Baut die Printer-URI für Responses (ipps://host/ipp/<tid>).

    Nutzt den Host-Header vom Request damit die URI zum tatsächlichen
    Endpoint passt (Cloudflare Tunnel / Reverse-Proxy-kompatibel).
    """
    host = request.headers.get("host", "printix.cloud")
    # Annahme: TLS-Termination erfolgt extern (Cloudflare), daher ipps://
    return f"ipps://{host}/ipp/{tenant_id}"


def _detect_format(file_path: str) -> str:
    """Magic-Byte-Detection falls IPP kein document-format mitgibt."""
    try:
        with open(file_path, "rb") as f:
            header = f.read(16)
        if header.startswith(b"%PDF"):
            return "application/pdf"
        if header.startswith(b"%!PS"):
            return "application/postscript"
        if header.startswith(b"\x1b"):
            return "application/vnd.hp-pcl"
    except Exception:
        pass
    return "application/octet-stream"


def _normalize_pdl(mime: str) -> str:
    """Printix submit_print_job erwartet bestimmte PDL-Werte."""
    m = (mime or "").lower()
    if "pdf" in m:
        return "PDF"
    if "postscript" in m or "ps" == m:
        return "PS"
    if "pcl6" in m or "pcl-xl" in m or "pclxl" in m:
        return "PCLXL"
    if "pcl" in m or "hp-pcl" in m:
        return "PCL5"
    return "PDF"  # Fallback
