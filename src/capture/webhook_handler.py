"""
Capture Webhook Handler — Printix/Tungsten Connector Model
===================================================================
Kanonischer Handler fuer Printix Capture Webhooks. Wird aufgerufen von:
  - capture_server.py  (Capture Port, source="capture")
  - server.py          (MCP Port,     source="mcp")
  - capture_routes.py  (Web-UI Port,  source="web")

Connector-Modell (v4.6.8):
  - Profil-Identifikation ueber URL: /capture/webhook/{profile_id}
  - Auth: HMAC-SHA256/512 (multi-secret) + Connector Token (multi-token)
  - Event-Typen: FileDeliveryJobReady, DocumentCaptured, ScanComplete, etc.
  - Metadaten: System-Metadaten + Custom Index Fields + metadataUrl Fetch
  - Payload: documentUrl direkt im Webhook-Body (Push-Modell)
  - Printix-kompatible Antwort: HTTP 200 + errorMessage
"""

import base64
import hashlib
import hmac as _hmac
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlparse
from app_version import APP_VERSION

logger = logging.getLogger("printix.capture")

DEBUG_PROFILE_ID = "00000000-0000-0000-0000-000000000000"


def _log_raw_request_for_sig_debug(source: str, headers: dict, body_bytes: bytes):
    """
    v4.6.7: Log complete raw request details for signature reverse-engineering.
    Called when signature verification fails but require_signature=False.
    """
    import hashlib as _hl
    import base64 as _b64

    logger.info("━━━ SIGNATURE DEBUG DUMP (require_signature=False) ━━━")
    logger.info("[%s] [sig-debug] body_len=%d body_sha256=%s",
                source, len(body_bytes),
                _hl.sha256(body_bytes).hexdigest()[:16])
    logger.info("[%s] [sig-debug] body_first_200=%s",
                source, body_bytes[:200].decode("utf-8", errors="replace"))

    # Log all x-printix-* headers
    for k, v in sorted(headers.items()):
        if k.startswith("x-printix-") or k in ("content-type", "content-length", "host"):
            logger.info("[%s] [sig-debug] header %s = %s", source, k, v)

    # Compute and log what the body hashes to with different algos
    sig = headers.get("x-printix-signature", "")
    ts = headers.get("x-printix-timestamp", "")
    path = headers.get("x-printix-request-path", "")
    rid = headers.get("x-printix-request-id", "")

    logger.info("[%s] [sig-debug] received_signature=%s", source, sig)
    logger.info("[%s] [sig-debug] received_sig_len=%d (raw bytes after b64decode=%d)",
                source, len(sig),
                len(_b64.b64decode(sig + "==")) if sig else 0)
    logger.info("[%s] [sig-debug] timestamp=%s path=%s request_id=%s",
                source, ts, path, rid)
    logger.info("━━━ END SIGNATURE DEBUG DUMP ━━━")


async def _fetch_printix_metadata(
    metadata_url: str,
    secret_key: str,
    source: str,
) -> dict[str, Any]:
    """
    v4.6.8: Fetch enriched metadata from Printix metadataUrl.

    Sends a signed GET request using the Printix Capture Connector HMAC scheme:
      StringToSign = "{RequestId}.{Timestamp}.get.{path}."
      (empty body for GET → trailing dot)

    Returns the metadata dict, or empty dict on failure.
    """
    if not metadata_url or not secret_key:
        return {}

    import aiohttp

    try:
        parsed = urlparse(metadata_url)
        request_path = parsed.path
        if parsed.query:
            request_path += f"?{parsed.query}"

        request_id = str(uuid.uuid4())
        timestamp = str(int(time.time()))
        method = "get"

        # StringToSign: same 5-component format, body is empty for GET
        string_to_sign = f"{request_id}.{timestamp}.{method}.{request_path}."
        key_bytes = base64.b64decode(secret_key)
        sig = _hmac.new(key_bytes, string_to_sign.encode("utf-8"), hashlib.sha256).digest()
        signature = base64.b64encode(sig).decode("utf-8")

        headers = {
            "X-Printix-Request-Id": request_id,
            "X-Printix-Timestamp": timestamp,
            "X-Printix-Signature": signature,
            "Accept": "application/json",
        }

        logger.info("[%s] [step:metadata] Fetching metadata from: %s", source, metadata_url[:80])

        async with aiohttp.ClientSession() as session:
            async with session.get(metadata_url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    logger.info("[%s] [step:metadata] OK — %d fields: %s",
                                source, len(data) if isinstance(data, dict) else 0,
                                list(data.keys()) if isinstance(data, dict) else type(data).__name__)

                    # Handle both "object" format and "nameValuePairs" format
                    if isinstance(data, dict):
                        return data
                    elif isinstance(data, list):
                        # nameValuePairs format: [{"name": "...", "value": "..."}, ...]
                        result: dict[str, Any] = {}
                        for item in data:
                            if isinstance(item, dict) and "name" in item:
                                result[item["name"]] = item.get("value", "")
                        logger.info("[%s] [step:metadata] Converted nameValuePairs → %d fields",
                                    source, len(result))
                        return result
                    return {}
                else:
                    body = await resp.text()
                    logger.warning("[%s] [step:metadata] HTTP %d — %s",
                                   source, resp.status, body[:200])
                    return {}

    except Exception as e:
        logger.warning("[%s] [step:metadata] Fetch failed: %s", source, e)
        return {}


# ── Known Capture Event Types ────────────────────────────────────────────────

KNOWN_EVENT_TYPES = {
    # Core Capture events (Printix/Tungsten)
    "FileDeliveryJobReady",
    "DocumentCaptured",
    "ScanComplete",
    "ScanJobCompleted",
    # Lifecycle events
    "JobCreated",
    "JobCompleted",
    "JobFailed",
    "JobCancelled",
    # Notification events
    "PrintCompleted",
    "CopyCompleted",
    "FaxReceived",
    # Test/debug
    "test",
    "ping",
}


# ── Capture Event Model ─────────────────────────────────────────────────────

@dataclass
class CaptureEvent:
    """Structured representation of a Printix Capture webhook event."""
    event_type: str = "unknown"
    document_url: str = ""
    filename: str = "scan.pdf"
    # System metadata (from Printix platform)
    system_metadata: dict = field(default_factory=dict)
    # Custom index fields (user-defined in Capture profile)
    index_fields: dict = field(default_factory=dict)
    # Full raw body for plugin access
    raw_body: dict = field(default_factory=dict)
    # Parsed payload info
    content_type: str = ""
    file_size: int = 0
    user_name: str = ""
    device_name: str = ""
    timestamp: str = ""


def _extract_event(body: dict) -> CaptureEvent:
    """Extract a structured CaptureEvent from the raw webhook body."""
    event = CaptureEvent()
    event.raw_body = body

    # ── Event Type ──────────────────────────────────────────────────────
    event.event_type = (
        body.get("eventType")
        or body.get("EventType")
        or body.get("event_type")
        or body.get("type")
        or "unknown"
    )

    # ── Document URL ────────────────────────────────────────────────────
    # Printix uses documentUrl; Azure Blob uses blobUrl; generic: fileUrl/url
    event.document_url = (
        body.get("documentUrl")
        or body.get("DocumentUrl")
        or body.get("documentURL")
        or body.get("blobUrl")
        or body.get("BlobUrl")
        or body.get("fileUrl")
        or body.get("FileUrl")
        or body.get("url")
        or ""
    )

    # ── Filename ────────────────────────────────────────────────────────
    event.filename = (
        body.get("fileName")
        or body.get("FileName")
        or body.get("filename")
        or body.get("name")
        or body.get("Name")
        or "scan.pdf"
    )

    # ── Content Type ────────────────────────────────────────────────────
    event.content_type = (
        body.get("contentType")
        or body.get("ContentType")
        or body.get("mimeType")
        or body.get("MimeType")
        or ""
    )

    # ── File Size ───────────────────────────────────────────────────────
    try:
        event.file_size = int(
            body.get("fileSize")
            or body.get("FileSize")
            or body.get("contentLength")
            or body.get("ContentLength")
            or 0
        )
    except (ValueError, TypeError):
        event.file_size = 0

    # ── System Metadata (Printix platform-level fields) ─────────────────
    sys_meta: dict[str, Any] = {}

    for key in ("tenantId", "TenantId", "tenant_id"):
        if key in body:
            sys_meta["tenant_id"] = body[key]
            break

    for key in ("userId", "UserId", "user_id", "userName", "UserName", "user_name"):
        if key in body:
            sys_meta["user"] = body[key]
            event.user_name = str(body[key])
            break

    for key in ("deviceName", "DeviceName", "device_name",
                "printerName", "PrinterName", "printer_name"):
        if key in body:
            sys_meta["device"] = body[key]
            event.device_name = str(body[key])
            break

    for key in ("timestamp", "Timestamp", "createdAt", "CreatedAt", "created_at"):
        if key in body:
            sys_meta["timestamp"] = body[key]
            event.timestamp = str(body[key])
            break

    for key in ("jobId", "JobId", "job_id"):
        if key in body:
            sys_meta["job_id"] = body[key]
            break

    for key in ("profileId", "ProfileId", "profile_id"):
        if key in body:
            sys_meta["profile_id"] = body[key]
            break

    event.system_metadata = sys_meta

    # ── Custom Index Fields / Metadata ──────────────────────────────────
    # Printix Capture supports both flat "metadata" and structured "indexFields"
    metadata_raw = body.get("metadata") or body.get("Metadata") or {}
    index_raw = (
        body.get("indexFields")
        or body.get("IndexFields")
        or body.get("index_fields")
        or {}
    )
    # Merge: index_fields take priority over flat metadata
    combined: dict[str, Any] = {}
    if isinstance(metadata_raw, dict):
        combined.update(metadata_raw)
    if isinstance(index_raw, dict):
        combined.update(index_raw)
    event.index_fields = combined

    return event


# ── Payload Validation ───────────────────────────────────────────────────────

def _validate_event(event: CaptureEvent) -> tuple[bool, list[str]]:
    """
    Validate a CaptureEvent against Capture Connector expectations.
    Returns (is_valid, list_of_warnings).
    Missing document_url is an error; other missing fields are warnings.
    """
    warnings: list[str] = []

    if not event.document_url:
        return False, ["No document URL found in payload"]

    if event.event_type == "unknown":
        warnings.append("No event type specified — defaulting to 'unknown'")
    elif event.event_type not in KNOWN_EVENT_TYPES:
        warnings.append(f"Unrecognized event type: '{event.event_type}'")

    if event.filename == "scan.pdf" and not any(
        k in event.raw_body for k in ("fileName", "FileName", "filename", "name", "Name")
    ):
        warnings.append("No filename in payload — using default 'scan.pdf'")

    return True, warnings


# ── Main Handler ─────────────────────────────────────────────────────────────

async def handle_webhook(
    profile_id: str,
    method: str,
    headers: dict[str, str],
    body_bytes: bytes,
    *,
    source: str = "unknown",
) -> tuple[int, dict[str, Any]]:
    """
    Kanonischer Capture-Webhook-Handler (v4.6.7).

    Processing steps:
      1. Profile lookup
      2. Authentication (multi-secret HMAC + connector token)
      3. JSON parse
      4. Event extraction (CaptureEvent model)
      5. Payload validation
      6. Plugin load + document processing
      7. Capture log
      8. Printix-compatible response

    Args:
        profile_id: Profil-UUID aus der URL
        method: HTTP-Methode (GET/POST)
        headers: Request-Headers (lowercase keys)
        body_bytes: Raw request body
        source: Aufrufquelle ("capture"/"web"/"mcp")

    Returns:
        (http_status_code, response_dict)
    """
    # ── Debug-UUID → enhanced debug handler ─────────────────────────────────
    if profile_id == DEBUG_PROFILE_ID:
        return _handle_debug(method, headers, body_bytes, source)

    # ── GET = Health-Check ───────────────────────────────────────────────────
    if method == "GET":
        return 200, {
            "status": "ok",
            "profile_id": profile_id,
            "endpoint": f"/capture/webhook/{profile_id}",
            "version": APP_VERSION,
        }

    # ── Nur POST akzeptieren ────────────────────────────────────────────────
    if method != "POST":
        return 405, {"errorMessage": "Method not allowed"}

    # ── Step 1: Profil laden ────────────────────────────────────────────────
    from db import get_capture_profile_for_webhook, add_capture_log

    logger.info("[%s] ━━━ Webhook empfangen: profile=%s ━━━", source, profile_id[:8])

    profile = get_capture_profile_for_webhook(profile_id)
    if not profile:
        logger.warning("[%s] [step:profile] Profil nicht gefunden: %s", source, profile_id)
        return 404, {"errorMessage": "Unknown profile"}

    tenant_id = profile["tenant_id"]
    profile_name = profile.get("name", "?")
    plugin_type = profile.get("plugin_type", "")

    logger.info("[%s] [step:profile] OK — name=%s plugin=%s",
                source, profile_name, plugin_type)

    # ── Step 2: Authentifizierung ───────────────────────────────────────────
    from capture.auth import verify_capture_auth

    require_sig = bool(profile.get("require_signature", False))
    auth_result = verify_capture_auth(body_bytes, headers, profile, method)
    logger.info("[%s] [step:auth] method=%s success=%s detail=%s",
                source, auth_result.method, auth_result.success, auth_result.detail)

    if not auth_result.success:
        if require_sig:
            add_capture_log(tenant_id, profile_id, profile_name,
                            "auth_failed", "error",
                            f"Auth failed: {auth_result.detail} (method={auth_result.method})")
            return 401, {"errorMessage": "Authentication failed"}
        else:
            # v4.6.7: Signature mismatch but require_signature=False → continue processing
            # Log full request details to help reverse-engineer the signature format
            logger.warning("[%s] [step:auth] SIGNATURE MISMATCH — require_signature=False, "
                           "continuing anyway (debug mode)", source)
            _log_raw_request_for_sig_debug(source, headers, body_bytes)

    # ── Step 3: Body parsen ─────────────────────────────────────────────────
    try:
        body = json.loads(body_bytes) if body_bytes else {}
    except (json.JSONDecodeError, ValueError) as e:
        logger.error("[%s] [step:parse] JSON parse error: %s (body=%s)",
                     source, e, body_bytes[:200].decode("utf-8", errors="replace"))
        add_capture_log(tenant_id, profile_id, profile_name,
                        "parse_error", "error",
                        f"Invalid JSON ({len(body_bytes)} bytes): {e}")
        return 400, {"errorMessage": "Invalid JSON"}

    logger.info("[%s] [step:parse] OK — %d bytes, keys=%s",
                source, len(body_bytes), list(body.keys()))

    # ── Step 4: Event extrahieren ───────────────────────────────────────────
    event = _extract_event(body)

    logger.info("[%s] [step:event] type=%s file=%s url=%s...",
                source, event.event_type, event.filename,
                event.document_url[:80] if event.document_url else "(none)")

    if event.user_name or event.device_name:
        logger.info("[%s] [step:event] user=%s device=%s",
                    source, event.user_name or "-", event.device_name or "-")

    if event.index_fields:
        logger.info("[%s] [step:event] index_fields=%s",
                    source, list(event.index_fields.keys()))

    if event.system_metadata:
        logger.debug("[%s] [step:event] system_metadata=%s",
                     source, list(event.system_metadata.keys()))

    # ── Step 5: Payload validieren ──────────────────────────────────────────
    is_valid, warnings = _validate_event(event)

    for w in warnings:
        logger.warning("[%s] [step:validate] %s", source, w)

    if not is_valid:
        detail = "; ".join(warnings)
        add_capture_log(tenant_id, profile_id, profile_name,
                        event.event_type, "error",
                        f"Invalid payload: {detail}. Keys: {list(body.keys())}")
        return 400, {"errorMessage": f"Invalid payload: {detail}"}

    logger.info("[%s] [step:validate] OK%s",
                source, f" (warnings: {len(warnings)})" if warnings else "")

    # ── Step 5b: Printix-Metadaten von metadataUrl laden (v4.6.8) ──────────
    printix_metadata: dict[str, Any] = {}
    metadata_url = body.get("metadataUrl") or body.get("MetadataUrl") or ""
    metadata_names = body.get("metadataNames") or body.get("MetadataNames") or []
    secrets = profile.get("secret_key", "").strip().split("\n")
    first_secret = secrets[0].strip() if secrets else ""

    if metadata_url and first_secret:
        printix_metadata = await _fetch_printix_metadata(
            metadata_url, first_secret, source,
        )
    elif metadata_url:
        logger.info("[%s] [step:metadata] metadataUrl present but no secret key — skipping fetch",
                    source)
    else:
        logger.debug("[%s] [step:metadata] No metadataUrl in payload", source)

    if metadata_names:
        logger.info("[%s] [step:metadata] metadataNames=%s", source, metadata_names)

    # ── Step 6: Plugin laden und Dokument verarbeiten ───────────────────────
    from capture.base_plugin import create_plugin_instance
    import capture.plugins  # noqa: F401 — auto-discovers all plugins via pkgutil

    plugin = create_plugin_instance(plugin_type, profile.get("config_json", "{}"))
    if not plugin:
        logger.error("[%s] [step:plugin] Plugin '%s' nicht gefunden", source, plugin_type)
        add_capture_log(tenant_id, profile_id, profile_name,
                        event.event_type, "error", f"Unknown plugin: {plugin_type}")
        return 500, {"errorMessage": f"Unknown plugin: {plugin_type}"}

    logger.info("[%s] [step:plugin] OK — %s (%s)", source, plugin.plugin_name, plugin_type)

    # Build combined metadata for plugin (v4.6.8: enriched with Printix metadata)
    # Priority: Printix metadata > index_fields > system_metadata > event context
    plugin_metadata: dict[str, Any] = {}
    # 1. System metadata (lowest priority)
    plugin_metadata.update(event.system_metadata)
    # 2. Index fields from webhook body
    plugin_metadata.update(event.index_fields)
    # 3. Printix metadata from metadataUrl (highest priority)
    if printix_metadata:
        plugin_metadata["_printix_metadata"] = printix_metadata
        # Flatten known fields into top level for easy access
        for k, v in printix_metadata.items():
            if k not in plugin_metadata:
                plugin_metadata[k] = v
    # 4. Add request context for fallback date/user info
    plugin_metadata["_event_type"] = event.event_type
    plugin_metadata["_filename"] = event.filename
    plugin_metadata["_user_name"] = event.user_name
    plugin_metadata["_device_name"] = event.device_name
    plugin_metadata["_scan_timestamp"] = headers.get("x-printix-timestamp", "")
    plugin_metadata["_job_id"] = body.get("jobId") or body.get("JobId") or ""
    plugin_metadata["_scan_id"] = body.get("scanId") or body.get("ScanId") or ""
    plugin_metadata["_callback_url"] = body.get("callbackUrl") or ""
    plugin_metadata["_metadata_names"] = metadata_names

    try:
        ok, msg = await plugin.process_document(
            event.document_url, event.filename, plugin_metadata, event.raw_body
        )
    except Exception as e:
        logger.exception("[%s] [step:process] Plugin-Fehler: %s", source, e)
        ok, msg = False, str(e)

    logger.info("[%s] [step:process] result=%s msg=%s",
                source, "OK" if ok else "FAIL", msg[:200] if msg else "")

    # ── Step 7: Capture-Log schreiben ───────────────────────────────────────
    details = f"auth={auth_result.method}"
    if event.user_name:
        details += f", user={event.user_name}"
    if event.device_name:
        details += f", device={event.device_name}"
    if event.file_size:
        details += f", size={event.file_size}"

    add_capture_log(tenant_id, profile_id, profile_name,
                    event.event_type, "ok" if ok else "error", msg or "",
                    details=details)

    # ── Step 8: Printix-kompatible Antwort ──────────────────────────────────
    # Printix Capture Connector Protokoll:
    #   HTTP 200 + errorMessage="" → Erfolg
    #   HTTP 200 + errorMessage="..." → Plugin-Fehler (Printix zeigt Meldung)
    # HTTP 4xx/5xx nur bei Infrastruktur-Fehlern (Profil/Auth/JSON).
    if ok:
        return 200, {"errorMessage": ""}
    else:
        return 200, {"errorMessage": msg}


# ── Enhanced Debug Handler ───────────────────────────────────────────────────

def _handle_debug(
    method: str,
    headers: dict[str, str],
    body_bytes: bytes,
    source: str,
) -> tuple[int, dict[str, Any]]:
    """
    Enhanced Debug-Endpoint (v4.6.7):
    - Shows detected auth method
    - Shows parsed event type and fields
    - Shows which required fields are present/missing
    - Shows whether payload matches Capture format
    """
    body_parsed = None
    body_text = ""
    try:
        body_parsed = json.loads(body_bytes) if body_bytes else None
    except Exception:
        body_text = body_bytes.decode("utf-8", errors="replace")[:2000] if body_bytes else ""

    # ── Auth detection ──────────────────────────────────────────────────────
    auth_info: dict[str, Any] = {"method": "none", "headers_found": []}
    for k in headers:
        if any(x in k for x in ("signature", "hmac", "printix",
                                 "authorization", "connector-token")):
            auth_info["headers_found"].append(k)

    if headers.get("x-printix-signature"):
        auth_info["method"] = "printix-native (x-printix-signature)"
        auth_info["timestamp"] = headers.get("x-printix-timestamp", "")
        auth_info["request_path"] = headers.get("x-printix-request-path", "")
        auth_info["request_id"] = headers.get("x-printix-request-id", "")
    elif headers.get("authorization", "").lower().startswith("bearer "):
        auth_info["method"] = "connector-token (Bearer)"
    elif headers.get("x-connector-token"):
        auth_info["method"] = "connector-token (x-connector-token)"
    elif headers.get("x-printix-signature-256"):
        auth_info["method"] = "hmac-sha256"
    elif headers.get("x-printix-signature-512"):
        auth_info["method"] = "hmac-sha512"
    elif headers.get("x-hub-signature-256"):
        auth_info["method"] = "hmac-sha256 (x-hub-signature-256)"

    # ── Event/field analysis ────────────────────────────────────────────────
    field_analysis: dict[str, Any] = {
        "is_json": body_parsed is not None,
        "event_type": None,
        "document_url": None,
        "filename": None,
        "content_type": None,
        "file_size": None,
        "metadata_present": False,
        "index_fields_present": False,
        "system_metadata": {},
        "fields_present": [],
        "expected_missing": [],
        "looks_like_capture": False,
    }

    if body_parsed and isinstance(body_parsed, dict):
        event = _extract_event(body_parsed)

        field_analysis["event_type"] = event.event_type
        field_analysis["document_url"] = event.document_url[:80] if event.document_url else None
        field_analysis["filename"] = event.filename
        field_analysis["content_type"] = event.content_type or None
        field_analysis["file_size"] = event.file_size or None
        field_analysis["metadata_present"] = bool(event.index_fields)
        field_analysis["index_fields_present"] = any(
            k in body_parsed for k in ("indexFields", "IndexFields", "index_fields")
        )
        field_analysis["system_metadata"] = event.system_metadata
        field_analysis["fields_present"] = list(body_parsed.keys())
        field_analysis["looks_like_capture"] = bool(
            event.document_url and event.event_type != "unknown"
        )

        if not event.document_url:
            field_analysis["expected_missing"].append("documentUrl")
        if event.event_type == "unknown":
            field_analysis["expected_missing"].append("eventType")

        # Validation result
        is_valid, warnings = _validate_event(event)
        field_analysis["valid"] = is_valid
        field_analysis["warnings"] = warnings

    debug_info = {
        "timestamp": datetime.now().isoformat(),
        "method": method,
        "source": source,
        "version": APP_VERSION,
        "auth": auth_info,
        "payload": field_analysis,
        "headers": headers,
        "body_size": len(body_bytes),
        "body_json": body_parsed,
        "body_text": body_text if not body_parsed else "",
    }

    # ── Structured logging ──────────────────────────────────────────────────
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    logger.info("  CAPTURE DEBUG (%s)", source)
    logger.info("  Method:     %s", method)
    logger.info("  Auth:       %s (headers: %s)", auth_info["method"],
                auth_info["headers_found"] or "none")
    if field_analysis["is_json"]:
        logger.info("  Event:      %s", field_analysis.get("event_type") or "?")
        logger.info("  Doc URL:    %s", field_analysis.get("document_url") or "(missing)")
        logger.info("  Filename:   %s", field_analysis.get("filename") or "(missing)")
        logger.info("  Metadata:   %s",
                    "yes" if field_analysis.get("metadata_present") else "no")
        logger.info("  Format OK:  %s",
                    "yes" if field_analysis.get("looks_like_capture") else "no")
        if field_analysis.get("expected_missing"):
            logger.info("  Missing:    %s", field_analysis["expected_missing"])
        if field_analysis.get("warnings"):
            for w in field_analysis["warnings"]:
                logger.info("  Warning:    %s", w)
    logger.info("  Headers:")
    for k, v in headers.items():
        logger.info("    %s: %s", k, v)
    logger.info("  Body (%d bytes):", len(body_bytes))
    if body_parsed:
        for k, v in body_parsed.items():
            logger.info("    %s: %s", k, str(v)[:200])
    elif body_text:
        logger.info("    (raw) %s", body_text[:500])
    else:
        logger.info("    (empty)")
    logger.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    return 200, {
        "status": "ok",
        "message": "Debug info logged — check add-on logs",
        "received": debug_info,
    }
