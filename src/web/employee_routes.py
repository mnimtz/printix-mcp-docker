"""
Employee Routes — Self-Service-Portal für Mitarbeiter (v1.0.0)
===============================================================
Registriert alle /my-Routen in der FastAPI-App.

Aufruf aus app.py:
    from web.employee_routes import register_employee_routes
    register_employee_routes(app, templates, t_ctx, require_login)

Routen (Mitarbeiter-Self-Service):
  GET  /my                           → Mitarbeiter-Dashboard
  GET  /my/jobs                      → Eigene Druckjobs
  POST /my/jobs/{id}/delete          → Druckjob löschen
  GET  /my/delegation                → Delegation verwalten
  POST /my/delegation/add            → Delegate vorschlagen
  POST /my/delegation/{id}/remove    → Delegation entfernen
  GET  /my/cloud-print               → Cloud Print Weiterleitung (Queue, IPPS-Info)
  POST /my/cloud-print/save          → Weiterleitungs-Konfiguration speichern
  GET  /my/reports                   → Reports Light (3 Basis-Reports)

Routen (Admin/User — Mitarbeiter-Verwaltung):
  GET  /employees                    → Mitarbeiterliste
  GET  /employees/new                → Mitarbeiter anlegen
  POST /employees/new                → Mitarbeiter speichern
  GET  /employees/{id}               → Mitarbeiter-Detail
  POST /employees/{id}/delete        → Mitarbeiter löschen
"""

import logging
import re
import secrets
import os
from typing import Callable, Optional

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("printix.employee")

# Empfohlene Printix-Send-Client-Version für den Download-Tab.
# Bei einem neuen Client-Release hier bumpen (oder künftig als Setting pflegen).
RECOMMENDED_CLIENT_VERSION = "6.7.50"

# i18n-Keys beim Import patchen
try:
    from cloudprint.i18n_employee import patch_translations
    patch_translations()
except Exception as _e:
    logger.warning("Employee i18n patch fehlgeschlagen: %s", _e)


def register_employee_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    t_ctx: Callable,
    require_login: Callable,
) -> None:
    """Registriert alle Mitarbeiter- und Employee-Self-Service-Routen."""

    # ── Helpers ────────────────────────────────────────────────────────────

    def _require_employee(request: Request) -> Optional[dict]:
        """Prüft ob der User ein Employee ist und gibt ihn zurück."""
        user = require_login(request)
        if not user:
            return None
        # Employees und normale User/Admins dürfen die /my-Routen nutzen
        return user

    def _require_manager(request: Request) -> Optional[dict]:
        """Prüft ob der User Admin oder normaler User (kein Employee) ist."""
        user = require_login(request)
        if not user:
            return None
        role = user.get("role_type", "user")
        if role == "employee":
            return None
        return user

    def _get_parent_id(user: dict) -> str:
        """Ermittelt den Parent-User-ID (sich selbst für Admin/User)."""
        from cloudprint.db_extensions import get_parent_user_id
        return get_parent_user_id(user["id"]) or user["id"]

    def _normalize_username(value: str) -> str:
        base = re.sub(r"[^a-z0-9._-]+", "-", (value or "").strip().lower()).strip("-._")
        return base or f"delegate-{secrets.token_hex(3)}"

    def _build_import_username(printix_user: dict) -> str:
        for key in ("userName", "username", "email", "upn", "name", "fullName"):
            value = (printix_user.get(key, "") or "").strip()
            if not value:
                continue
            if "@" in value:
                value = value.split("@", 1)[0]
            return _normalize_username(value)
        return f"delegate-{(printix_user.get('id', '') or secrets.token_hex(4))[:8]}"

    def _build_unique_username(base_username: str) -> str:
        from db import username_exists
        candidate = _normalize_username(base_username)
        if not username_exists(candidate):
            return candidate
        idx = 2
        while username_exists(f"{candidate}-{idx}"):
            idx += 1
        return f"{candidate}-{idx}"

    def _tenant_client_for_user(user: dict):
        from db import get_tenant_full_by_user_id
        from printix_client import PrintixClient

        parent_id = _get_parent_id(user)
        tenant = get_tenant_full_by_user_id(parent_id)
        if not tenant or not tenant.get("printix_tenant_id"):
            return None, None
        client = PrintixClient(
            tenant_id=tenant["printix_tenant_id"],
            print_client_id=tenant.get("print_client_id", ""),
            print_client_secret=tenant.get("print_client_secret", ""),
            card_client_id=tenant.get("card_client_id", ""),
            card_client_secret=tenant.get("card_client_secret", ""),
            ws_client_id=tenant.get("ws_client_id", ""),
            ws_client_secret=tenant.get("ws_client_secret", ""),
            um_client_id=tenant.get("um_client_id", ""),
            um_client_secret=tenant.get("um_client_secret", ""),
            shared_client_id=tenant.get("shared_client_id", ""),
            shared_client_secret=tenant.get("shared_client_secret", ""),
        )
        return tenant, client

    def _iter_user_items(payload):
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("users", "items", "data", "results"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return []

    def _load_importable_printix_users(manager_user: dict) -> tuple[list[dict], str]:
        from cloudprint.db_extensions import get_employees

        try:
            tenant, client = _tenant_client_for_user(manager_user)
            if not client or not tenant:
                return [], "tenant_missing"
            parent_id = _get_parent_id(manager_user)
            employees = get_employees(parent_id)
            existing_printix_ids = {e.get("printix_user_id", "") for e in employees if e.get("printix_user_id")}
            existing_emails = {e.get("email", "").strip().lower() for e in employees if e.get("email")}

            seen_ids: set[str] = set()
            importable: list[dict] = []
            for role in ("USER", "GUEST_USER"):
                try:
                    raw = client.list_users(role=role, page=0, page_size=200)
                except Exception as role_exc:
                    logger.warning("Printix-User-Liste (%s) fehlgeschlagen: %s", role, role_exc)
                    continue
                for item in _iter_user_items(raw):
                    user_id = (item.get("id", "") or "").strip()
                    email = (item.get("email", "") or item.get("userPrincipalName", "") or item.get("upn", "")).strip()
                    if not user_id or user_id in seen_ids or user_id in existing_printix_ids:
                        continue
                    if email and email.lower() in existing_emails:
                        continue
                    seen_ids.add(user_id)
                    display_name = (
                        item.get("name")
                        or item.get("fullName")
                        or item.get("displayName")
                        or item.get("username")
                        or item.get("userName")
                        or email
                        or user_id
                    )
                    importable.append({
                        "id": user_id,
                        "email": email,
                        "full_name": display_name,
                        "role": role,
                        "username_candidate": _build_import_username(item),
                    })
            importable.sort(key=lambda row: ((row.get("full_name") or "").lower(), (row.get("email") or "").lower()))
            return importable, ""
        except Exception as e:
            logger.warning("Printix-Importliste fehlgeschlagen: %s", e)
            return [], str(e)

    # ══════════════════════════════════════════════════════════════════════
    # MITARBEITER SELF-SERVICE (/my/*)
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/my", response_class=HTMLResponse)
    async def my_dashboard(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import (
            get_delegations_for_owner, get_delegations_for_delegate,
            get_tenant_for_user,
        )
        tenant = get_tenant_for_user(user["id"])
        delegations_out = get_delegations_for_owner(user["id"])
        delegations_in = get_delegations_for_delegate(user["id"])

        return templates.TemplateResponse("employee/my_dashboard.html", {
            "request": request, "user": user, "tenant": tenant,
            "delegations_out": delegations_out,
            "delegations_in": delegations_in,
            **t_ctx(request),
        })

    # ── Druckjobs ─────────────────────────────────────────────────────────

    @app.get("/my/jobs", response_class=HTMLResponse)
    async def my_jobs(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import (
            get_tenant_for_user, get_cloudprint_jobs_for_employee,
            get_recent_cloudprint_jobs_debug, get_parent_user_id,
        )
        tenant = get_tenant_for_user(user["id"])
        jobs = []
        cloud_jobs = []
        cloud_stats = {"total": 0, "forwarded": 0, "received": 0, "error": 0}
        unmatched_recent = []  # v6.4.2: Debug-Info wenn Match leer

        if tenant and tenant.get("printix_tenant_id"):
            try:
                from db import get_tenant_full_by_user_id
                parent_id = get_parent_user_id(user["id"])
                full_tenant = get_tenant_full_by_user_id(parent_id)
                if full_tenant:
                    from printix_client import PrintixClient
                    client = PrintixClient(
                        tenant_id=full_tenant["printix_tenant_id"],
                        print_client_id=full_tenant.get("print_client_id", ""),
                        print_client_secret=full_tenant.get("print_client_secret", ""),
                        shared_client_id=full_tenant.get("shared_client_id", ""),
                        shared_client_secret=full_tenant.get("shared_client_secret", ""),
                    )
                    all_jobs = client.list_print_jobs(size=100)
                    raw = all_jobs if isinstance(all_jobs, list) else (all_jobs or {}).get("jobs", []) if isinstance(all_jobs, dict) else []
                    user_email = user.get("email", "").lower()
                    jobs = [
                        j for j in raw
                        if j.get("ownerEmail", "").lower() == user_email
                        or j.get("ownerName", "").lower() == user.get("username", "").lower()
                    ]
            except Exception as e:
                logger.warning("Printix-Jobs-Abruf fehlgeschlagen: %s", e)

        # v5.20.0: Cloud-Print-Jobs über IPP-Tracking (bzw. LPR-Legacy), gematcht
        # via ALLE bekannten Identitäts-Felder des angemeldeten Employees.
        try:
            tenant_id = tenant.get("id", "") if tenant else ""
            if tenant_id:
                cloud_jobs = get_cloudprint_jobs_for_employee(
                    tenant_id=tenant_id,
                    employee=user,
                    limit=100,
                )
                for cj in cloud_jobs:
                    cloud_stats["total"] += 1
                    status_val = (cj.get("status") or "").lower()
                    if status_val in cloud_stats:
                        cloud_stats[status_val] += 1

                # v6.4.2: Wenn personal-match 0 ist aber generell Jobs im
                # Tenant empfangen wurden, zeige die letzten zum Debuggen.
                # So sieht der User welche Identitäten in den Jobs stehen
                # und kann seine printix_user_id entsprechend setzen.
                if not cloud_jobs:
                    unmatched_recent = get_recent_cloudprint_jobs_debug(
                        tenant_id=tenant_id, limit=5,
                    )
        except Exception as e:
            logger.warning("CloudPrint-Jobs-Abruf fehlgeschlagen: %s", e)

        return templates.TemplateResponse("employee/my_jobs.html", {
            "request": request, "user": user,
            "jobs": jobs, "cloud_jobs": cloud_jobs,
            "cloud_stats": cloud_stats,
            "unmatched_recent": unmatched_recent,
            **t_ctx(request),
        })

    @app.post("/my/jobs/{job_id}/delete")
    async def my_job_delete(request: Request, job_id: str):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        try:
            from db import get_tenant_full_by_user_id
            from cloudprint.db_extensions import get_parent_user_id
            parent_id = get_parent_user_id(user["id"])
            full_tenant = get_tenant_full_by_user_id(parent_id)
            if full_tenant:
                from printix_client import PrintixClient
                client = PrintixClient(
                    tenant_id=full_tenant["printix_tenant_id"],
                    print_client_id=full_tenant.get("print_client_id", ""),
                    print_client_secret=full_tenant.get("print_client_secret", ""),
                    shared_client_id=full_tenant.get("shared_client_id", ""),
                    shared_client_secret=full_tenant.get("shared_client_secret", ""),
                )
                client.delete_print_job(job_id)
        except Exception as e:
            logger.warning("Job-Löschung fehlgeschlagen: %s", e)

        return RedirectResponse("/my/jobs?flash=job_deleted", status_code=302)

    # ── Delegation ────────────────────────────────────────────────────────

    @app.get("/my/delegation", response_class=HTMLResponse)
    async def my_delegation(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import (
            get_delegations_for_owner, get_delegations_for_delegate,
            get_printix_delegate_candidates, get_parent_user_id,
            get_tenant_for_user,
        )
        parent_id = get_parent_user_id(user["id"])
        delegations_out = get_delegations_for_owner(user["id"])
        delegations_in = get_delegations_for_delegate(user["id"])

        # v6.7.14: Delegate-Kandidaten sind alle Printix-User des Tenants
        # (aus cached_printix_users). Der Owner selbst + bereits gewählte
        # Delegates werden ausgefiltert.
        tenant = get_tenant_for_user(user["id"])
        available = []
        if tenant and tenant.get("id"):
            available = get_printix_delegate_candidates(
                tenant_id=tenant["id"], owner_user_id=user["id"],
            )
            # Owner selbst ausfiltern (via printix_user_id oder email)
            owner_pxid = (user.get("printix_user_id") or "").lower()
            owner_email = (user.get("email") or "").lower()
            available = [
                a for a in available
                if (not owner_pxid or a["printix_user_id"].lower() != owner_pxid)
                and (not owner_email or a["email"].lower() != owner_email)
            ]

        flash = request.query_params.get("flash", "")
        return templates.TemplateResponse("employee/my_delegation.html", {
            "request": request, "user": user,
            "delegations_out": delegations_out,
            "delegations_in": delegations_in,
            "available_delegates": available,
            "flash": flash,
            **t_ctx(request),
        })

    @app.get("/my/delegation/search")
    async def my_delegation_search(request: Request, q: str = ""):
        """JSON-Typeahead-Endpoint: liefert max. 20 Mitarbeiter passend zum Query.

        Wird vom Delegation-Picker aufgerufen um bei großen Tenants die
        Liste schnell einzugrenzen, ohne alle Kandidaten ins Dropdown zu laden.
        """
        from fastapi.responses import JSONResponse
        user = _require_employee(request)
        if not user:
            return JSONResponse({"results": []}, status_code=401)

        from cloudprint.db_extensions import (
            get_delegations_for_owner, get_parent_user_id,
            search_available_delegates,
        )
        parent_id = get_parent_user_id(user["id"])
        existing = {d["delegate_user_id"] for d in get_delegations_for_owner(user["id"])}

        hits = search_available_delegates(
            parent_user_id=parent_id,
            query=q or "",
            exclude_user_id=user["id"],
            exclude_ids=list(existing),
            limit=20,
        )
        return JSONResponse({
            "query": q,
            "results": [
                {
                    "id":        h["id"],
                    "username":  h.get("username", ""),
                    "email":     h.get("email", ""),
                    "full_name": h.get("full_name", ""),
                    "label":     (h.get("full_name") or h.get("username") or ""),
                    "sub":       h.get("email", ""),
                }
                for h in hits
            ],
        })

    @app.post("/my/delegation/add")
    async def my_delegation_add(
        request: Request,
        printix_user_id: str = Form(...),
    ):
        """v6.7.14: Delegate-Target ist ein Printix-User aus dem Cache.
        Gespeichert wird die Printix-Identität direkt in der Delegations-Zeile
        (ohne MCP-Employee-Spiegel-Zwang).
        """
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import (
            add_printix_delegate, get_tenant_for_user,
        )
        from cloudprint.printix_cache_db import find_printix_user_by_identity

        # Details des gewählten Printix-Users aus dem Cache holen
        tenant = get_tenant_for_user(user["id"])
        if not tenant or not tenant.get("id"):
            return RedirectResponse("/my/delegation?flash=no_tenant", status_code=302)

        pxuser = find_printix_user_by_identity(printix_user_id.strip())
        # Fallback: direkt per Printix-ID in cached_printix_users suchen
        if not pxuser:
            from db import _conn
            with _conn() as conn:
                row = conn.execute(
                    """SELECT printix_user_id, email, full_name, username
                       FROM cached_printix_users
                       WHERE tenant_id = ? AND printix_user_id = ?""",
                    (tenant["id"], printix_user_id.strip()),
                ).fetchone()
            pxuser = dict(row) if row else None

        if not pxuser or not pxuser.get("email"):
            logger.warning("Delegation-Add: Printix-User %s nicht im Cache",
                           printix_user_id)
            return RedirectResponse(
                "/my/delegation?flash=user_not_found", status_code=302,
            )

        add_printix_delegate(
            owner_user_id=user["id"],
            printix_user_id=pxuser["printix_user_id"],
            email=pxuser["email"],
            full_name=pxuser.get("full_name", ""),
            created_by=user["id"],
        )
        return RedirectResponse("/my/delegation?flash=delegation_added", status_code=302)

    @app.post("/my/delegation/{delegation_id}/remove")
    async def my_delegation_remove(request: Request, delegation_id: int):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import get_delegation_by_id, delete_delegation
        deleg = get_delegation_by_id(delegation_id)
        if deleg and (deleg["owner_user_id"] == user["id"] or deleg["delegate_user_id"] == user["id"]):
            delete_delegation(delegation_id)

        return RedirectResponse("/my/delegation?flash=delegation_removed", status_code=302)

    # ── Cloud Print Weiterleitung ────────────────────────────────────────

    # v6.7.21: Setup-Guide für Admin/User — beschreibt wie der Delegation-
    # Drucker in Printix manuell angelegt wird. Technische Details dynamisch
    # aus der Tenant-Config gezogen.
    @app.get("/my/setup-guide", response_class=HTMLResponse)
    async def my_setup_guide(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        # Nur Admin/User — Mitarbeiter haben keinen Zugang
        if user.get("role_type") == "employee":
            return RedirectResponse("/my", status_code=302)

        from db import get_setting
        ipps_public_url = get_setting("ipps_public_url", "") or get_setting("public_url", "")
        ipps_public_host = ""
        if ipps_public_url:
            ipps_public_host = ipps_public_url.replace("https://", "") \
                .replace("http://", "").replace("ipps://", "").strip("/")
            # Port vom Host trennen für die Einzel-Anzeige
            if ":" in ipps_public_host:
                ipps_public_host = ipps_public_host.rsplit(":", 1)[0]
        import os as _os
        ipps_port = get_setting("ipps_port", "") or _os.environ.get("IPP_PORT", "621")

        return templates.TemplateResponse("employee/setup_guide.html", {
            "request": request, "user": user,
            "ipps_public_url": ipps_public_url,
            "ipps_public_host": ipps_public_host,
            "ipps_port": ipps_port,
            **t_ctx(request),
        })

    # v6.7.27: Web-Upload → Direkt-Push in die Secure Print Queue.
    # Zielgruppe: jeder eingeloggte User (Admin, User, Employee). Ohne
    # Printix-Workstation-Client direkt vom Browser aus drucken.
    @app.get("/my/upload", response_class=HTMLResponse)
    async def my_upload_get(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from db import get_tenant_full_by_user_id
        from cloudprint.db_extensions import get_parent_user_id, get_cloudprint_config
        parent_id = get_parent_user_id(user["id"])
        tenant = get_tenant_full_by_user_id(parent_id)
        config = get_cloudprint_config(user["id"])

        # Queue-Anzeigename ermitteln (für den „Ziel"-Hinweis)
        target_queue_name = ""
        if tenant and config and config.get("lpr_target_queue"):
            try:
                import sys as _sys, os as _os, re as _re
                src_dir = _os.path.dirname(_os.path.dirname(__file__))
                if src_dir not in _sys.path:
                    _sys.path.insert(0, src_dir)
                from printix_client import PrintixClient
                client = PrintixClient(
                    tenant_id=tenant["printix_tenant_id"],
                    print_client_id=tenant.get("print_client_id", ""),
                    print_client_secret=tenant.get("print_client_secret", ""),
                    shared_client_id=tenant.get("shared_client_id", ""),
                    shared_client_secret=tenant.get("shared_client_secret", ""),
                )
                data = client.list_printers(size=200)
                raw = data.get("printers", []) if isinstance(data, dict) else []
                if not raw:
                    raw = (data.get("_embedded") or {}).get("printers", []) if isinstance(data, dict) else []
                for item in raw:
                    href = (item.get("_links") or {}).get("self", {}).get("href", "")
                    m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                    if m and m.group(2) == config["lpr_target_queue"]:
                        target_queue_name = item.get("name", "")
                        break
            except Exception as e:
                logger.debug("Queue-Name-Lookup fehlgeschlagen: %s", e)

        flash = request.query_params.get("flash", "")
        return templates.TemplateResponse("employee/my_upload.html", {
            "request": request, "user": user,
            "target_queue_name": target_queue_name,
            "flash": flash,
            "upload_error": request.query_params.get("err", ""),
            "uploaded_filename": request.query_params.get("file", ""),
            **t_ctx(request),
        })

    @app.post("/my/upload")
    async def my_upload_post(
        request: Request,
        document: UploadFile = File(...),
        color: str = Form(""),
        duplex: str = Form(""),
        copies: int = Form(1),
    ):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        if not document or not document.filename:
            return RedirectResponse("/my/upload?flash=upload_no_file", status_code=302)

        # Datei einlesen (max. 50 MB)
        MAX_BYTES = 50 * 1024 * 1024
        data = await document.read()
        if not data:
            return RedirectResponse("/my/upload?flash=upload_no_file", status_code=302)
        if len(data) > MAX_BYTES:
            return RedirectResponse("/my/upload?flash=upload_too_big", status_code=302)

        # v6.7.28: Upload-Konverter — wenn nicht PDF, versuchen wir zu
        # konvertieren (LibreOffice für Office, Pillow für Bilder, Text-
        # Renderer für plain). Bei Erfolg landen wir mit PDF-Bytes weiter.
        original_filename = document.filename
        try:
            import sys as _sys, os as _os
            src_dir = _os.path.dirname(_os.path.dirname(__file__))
            if src_dir not in _sys.path:
                _sys.path.insert(0, src_dir)
            from upload_converter import convert_to_pdf, ConversionError
            pdf_data, conv_label = convert_to_pdf(data, original_filename)
            if pdf_data is not data:
                logger.info(
                    "Web-Upload: Konvertierung OK (%s) — file=%s in=%d out=%d bytes",
                    conv_label, original_filename, len(data), len(pdf_data),
                )
                # Filename auf .pdf umstellen damit der Titel in Printix passt
                base = original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
                display_filename = f"{base}.pdf"
                data = pdf_data
            else:
                display_filename = original_filename
        except ConversionError as _ce:
            from urllib.parse import quote
            logger.warning("Web-Upload: Konvertierung fehlgeschlagen: %s", _ce)
            return RedirectResponse(
                f"/my/upload?flash=upload_wrong_type&err={quote(str(_ce)[:180])}",
                status_code=302,
            )
        except Exception as _ce:
            from urllib.parse import quote
            logger.error("Web-Upload: Konvertierung-Exception: %s", _ce)
            return RedirectResponse(
                f"/my/upload?flash=upload_error&err={quote(str(_ce)[:180])}",
                status_code=302,
            )

        # Tenant + Config laden
        from db import get_tenant_full_by_user_id
        from cloudprint.db_extensions import (
            get_parent_user_id, get_cloudprint_config,
            create_cloudprint_job, update_cloudprint_job_status,
        )
        from cloudprint.printix_cache_db import find_printix_user_by_identity
        parent_id = get_parent_user_id(user["id"])
        tenant = get_tenant_full_by_user_id(parent_id)
        config = get_cloudprint_config(user["id"])
        if not tenant or not config or not config.get("lpr_target_queue"):
            return RedirectResponse("/my/upload?flash=upload_no_config", status_code=302)

        target_queue = config["lpr_target_queue"]

        # Print-User-Email ermitteln
        # - Mitarbeiter mit printix_user_id → direkt lookup
        # - sonst: via Email-Match im Printix-Cache
        user_email = (user.get("email") or "").strip()
        user_printix_email = ""
        try:
            # Strategie 1: User.printix_user_id direkt
            px_id = (user.get("printix_user_id") or "").strip()
            if px_id:
                from db import _conn as _dbconn
                with _dbconn() as _c:
                    row = _c.execute(
                        "SELECT email FROM cached_printix_users WHERE printix_user_id=?",
                        (px_id,),
                    ).fetchone()
                if row and row["email"]:
                    user_printix_email = row["email"]
            # Strategie 2: per Email finden
            if not user_printix_email and user_email:
                px = find_printix_user_by_identity(user_email)
                if px and px.get("email"):
                    user_printix_email = px["email"]
        except Exception as _e:
            logger.debug("Printix-Email-Lookup fehlgeschlagen: %s", _e)
        # Fallback: MCP-email als letzter Halm
        user_printix_email = user_printix_email or user_email

        # Submit an Printix
        try:
            import sys as _sys, os as _os
            src_dir = _os.path.dirname(_os.path.dirname(__file__))
            if src_dir not in _sys.path:
                _sys.path.insert(0, src_dir)
            from printix_client import PrintixClient
            import re as _re
            client = PrintixClient(
                tenant_id=tenant["printix_tenant_id"],
                print_client_id=tenant.get("print_client_id", ""),
                print_client_secret=tenant.get("print_client_secret", ""),
                shared_client_id=tenant.get("shared_client_id", ""),
                shared_client_secret=tenant.get("shared_client_secret", ""),
                um_client_id=tenant.get("um_client_id", ""),
                um_client_secret=tenant.get("um_client_secret", ""),
            )

            # Printer-ID zur Queue finden
            printers_data = client.list_printers(size=200)
            raw_list = printers_data.get("printers", []) if isinstance(printers_data, dict) else []
            if not raw_list and isinstance(printers_data, dict):
                raw_list = (printers_data.get("_embedded") or {}).get("printers", [])
            printer_id = ""
            for p in raw_list:
                href = (p.get("_links") or {}).get("self", {}).get("href", "")
                m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                if m and m.group(2) == target_queue:
                    printer_id = m.group(1)
                    break
            if not printer_id:
                logger.warning("Web-Upload: Ziel-Queue %s nicht in Printix gefunden", target_queue)
                return RedirectResponse(
                    f"/my/upload?flash=upload_error&err=queue-not-found",
                    status_code=302,
                )

            # Tracking-Eintrag
            import uuid as _uuid
            internal_id = _uuid.uuid4().hex[:10]
            create_cloudprint_job(
                job_id=internal_id,
                tenant_id=tenant.get("id", ""),
                queue_name=tenant.get("printix_tenant_id", ""),
                username=user_printix_email,
                hostname="web-upload",
                job_name=display_filename,
                data_size=len(data),
                data_format="application/pdf",
                detected_identity=user_printix_email,
                identity_source="web-upload",
                status="forwarding",
            )

            # Submit
            result = client.submit_print_job(
                printer_id=printer_id,
                queue_id=target_queue,
                title=display_filename,
                user=user_printix_email,
                pdl="PDF",
                release_immediately=False,
                color=bool(color),
                duplex=("LONG_EDGE" if duplex else "NONE"),
                copies=max(1, min(99, int(copies or 1))),
            )
            result_job = result.get("job", result) if isinstance(result, dict) else {}
            printix_job_id = result_job.get("id", "") if isinstance(result_job, dict) else ""
            upload_url = ""
            upload_headers = {}
            if isinstance(result, dict):
                upload_url = result.get("uploadUrl", "") or ""
                links = result.get("uploadLinks") or []
                if not upload_url and links and isinstance(links[0], dict):
                    upload_url = links[0].get("url", "") or ""
                    upload_headers = links[0].get("headers") or {}

            if not printix_job_id or not upload_url:
                update_cloudprint_job_status(internal_id, "error",
                    error_message="Printix API returned no job-id / upload-url")
                return RedirectResponse(
                    "/my/upload?flash=upload_error&err=printix-no-job",
                    status_code=302,
                )

            # Upload + complete
            client.upload_file_to_url(upload_url, data, "application/pdf", upload_headers)
            client.complete_upload(printix_job_id)

            # Owner-Wechsel (v6.7.15 gelernt)
            if user_printix_email and "@" in user_printix_email:
                try:
                    client.change_job_owner(printix_job_id, user_printix_email)
                except Exception as _co:
                    logger.warning("Web-Upload Owner-Wechsel fehlgeschlagen: %s", _co)

            update_cloudprint_job_status(
                internal_id, "forwarded",
                printix_job_id=printix_job_id, target_queue=target_queue,
                detected_identity=user_printix_email,
                identity_source="web-upload",
            )

            logger.info(
                "Web-Upload OK — user=%s file=%s size=%d printix_job=%s",
                user_printix_email, display_filename, len(data), printix_job_id,
            )
            from urllib.parse import quote
            return RedirectResponse(
                f"/my/upload?flash=upload_ok&file={quote(display_filename or '')}",
                status_code=302,
            )

        except Exception as e:
            logger.error("Web-Upload fehlgeschlagen: %s", e)
            from urllib.parse import quote
            return RedirectResponse(
                f"/my/upload?flash=upload_error&err={quote(str(e)[:150])}",
                status_code=302,
            )

    @app.get("/my/cloud-print", response_class=HTMLResponse)
    async def my_cloud_print(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import get_cloudprint_config
        from db import get_setting
        config = get_cloudprint_config(user["id"])
        flash = request.query_params.get("flash", "")
        # v6.5.0: IPPS-URL aus Settings (z.B. https://ipps.printix.cloud)
        # v6.6.0: LPR komplett entfernt — IPPS ist der einzige Cloud-Print-Eingang.
        ipps_public_url = get_setting("ipps_public_url", "") or get_setting("public_url", "")
        # Host-Teil extrahieren für Printix-Drucker-Anlage (ohne Schema)
        ipps_public_host = ""
        if ipps_public_url:
            ipps_public_host = ipps_public_url.replace("https://", "").replace("http://", "").replace("ipps://", "").strip("/")

        # Printix Queues laden für Dropdown (nur für Admin/User)
        queues = []
        role = user.get("role_type", "user")
        if role != "employee":
            try:
                from db import get_tenant_full_by_user_id
                from cloudprint.db_extensions import get_parent_user_id
                parent_id = get_parent_user_id(user["id"])
                full_tenant = get_tenant_full_by_user_id(parent_id)
                if full_tenant and (full_tenant.get("print_client_id") or full_tenant.get("shared_client_id")):
                    import sys as _sys, os as _os, re as _re
                    src_dir = _os.path.dirname(_os.path.dirname(__file__))
                    if src_dir not in _sys.path:
                        _sys.path.insert(0, src_dir)
                    from printix_client import PrintixClient
                    client = PrintixClient(
                        tenant_id=full_tenant["printix_tenant_id"],
                        print_client_id=full_tenant.get("print_client_id", ""),
                        print_client_secret=full_tenant.get("print_client_secret", ""),
                        shared_client_id=full_tenant.get("shared_client_id", ""),
                        shared_client_secret=full_tenant.get("shared_client_secret", ""),
                    )
                    # Gleiche Logik wie /tenant/queues — Drucker laden, Queue-Paare extrahieren
                    data = client.list_printers(size=200)
                    raw = data.get("printers", []) if isinstance(data, dict) else []
                    if not raw:
                        # Fallback: _embedded.printers
                        raw = (data.get("_embedded") or {}).get("printers", []) if isinstance(data, dict) else []
                    for item in raw:
                        href = (item.get("_links") or {}).get("self", {}).get("href", "")
                        m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                        if m:
                            queues.append({
                                "queue_id": m.group(2),
                                "queue_name": item.get("name", m.group(2)),
                                "printer_name": item.get("name", ""),
                                "printer_id": m.group(1),
                            })
            except Exception as e:
                logger.warning("Queues-Abruf fehlgeschlagen: %s", e)

        # v6.7.24: Anzeigenamen zur aktuell konfigurierten Ziel-Queue finden.
        # Das UI-Feld zeigt sonst nur die UUID was nicht sprechend ist.
        target_queue_name = ""
        if config and config.get("lpr_target_queue"):
            tq = config["lpr_target_queue"]
            for q in queues:
                if q.get("queue_id") == tq:
                    target_queue_name = (f"{q.get('queue_name', '')} "
                                          f"({q.get('printer_name', '')})").strip()
                    break

        return templates.TemplateResponse("employee/my_cloud_print.html", {
            "request": request, "user": user,
            "config": config, "queues": queues,
            "target_queue_name": target_queue_name,
            "ipps_public_url": ipps_public_url,
            "ipps_public_host": ipps_public_host,
            "flash": flash,
            **t_ctx(request),
        })

    @app.get("/my/send-to", response_class=HTMLResponse)
    async def my_send_to(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        return templates.TemplateResponse("employee/my_send_to.html", {
            "request": request, "user": user,
            "client_version": RECOMMENDED_CLIENT_VERSION,
            **t_ctx(request),
        })

    # ── Mobile App (iOS) — QR-Onboarding ──────────────────────────────────
    #
    # Die iOS-App „Mobile Print" braucht zum Einrichten nur die
    # Server-URL; User-Login läuft danach in der App selbst. Statt URLs
    # abzutippen, scannt der Benutzer hier einen QR-Code mit genau der
    # Basis-URL (optional plus Hinweis-Payload für zukünftige Keys).
    #
    # Kein personalisiertes Token — die Verbindung ist rein öffentlich,
    # echte Anmeldung passiert via /desktop/auth/login gegen den gleichen
    # Tenant-Kontext wie am Desktop.

    def _public_base_url(request: Request) -> str:
        """Basis-URL für die Mobile-App.

        **Wichtig**: Die iOS-App spricht /desktop/auth/login und /desktop/*
        an — diese Endpunkte leben auf der **Web-App** (WEB_PORT, frei
        konfigurierbar in HA, z. B. 8010), NICHT auf dem MCP-Server
        (MCP_PORT, Default 8765), wo die BearerAuthMiddleware jeden
        unauthenticated Request mit 401 abfängt.

        Deshalb benutzen wir ausschließlich die URL, unter der der User
        gerade /my/mobile-app aufgerufen hat (request.base_url). Die
        enthält automatisch Schema, Host UND den echten WEB_PORT
        (oder die Reverse-Proxy-URL, wenn Uvicorn mit
        --proxy-headers / ProxyHeadersMiddleware läuft) — also genau
        das, was die iOS-App braucht, um /desktop/* zu erreichen.

        Hinweis: die config.yaml-Setting `public_url` ist bewusst NICHT
        der Fallback, weil sie typischerweise auf den MCP-Server zeigt.
        """
        return str(request.base_url).rstrip("/")

    @app.get("/my/mobile-app", response_class=HTMLResponse)
    async def my_mobile_app(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        base_url = _public_base_url(request)
        # TestFlight-Einladungs-URL. Hart verdrahtet, weil wir derzeit
        # nur einen Beta-Kanal fahren; spaeter ggf. via Config. Wichtig:
        # der Link oeffnet in der TestFlight-App (muss der User vorher
        # einmal aus dem App Store installieren — Hinweis im Template).
        testflight_url = "https://testflight.apple.com/join/skx3gZnk"
        return templates.TemplateResponse("employee/my_mobile_app.html", {
            "request": request, "user": user,
            "mobile_base_url": base_url,
            "testflight_url": testflight_url,
            # Der Payload, der im QR steckt — identisch mit dem, was der
            # iOS-Scanner erwartet. JSON hält die Tür offen für künftige
            # Felder (z. B. default_target oder brand).
            "mobile_qr_payload_hint": (
                '{"v":1,"server":"' + base_url + '"}'
            ),
            **t_ctx(request),
        })

    @app.get("/my/mobile-app/qr.png")
    async def my_mobile_app_qr(request: Request):
        """PNG-QR mit einem schlanken JSON-Payload:
        `{"v":1,"server":"https://..."}`.
        Der iOS-Scanner erkennt `server` und springt direkt in den
        Login-Screen — ohne URL-Tippen.
        """
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        import json, io
        try:
            import segno
        except ImportError:
            # Wenn das Paket fehlt, nicht hart crashen — der User bekommt
            # stattdessen die Server-URL im Text auch ohne QR angezeigt.
            from fastapi.responses import Response
            return Response(status_code=503, content="segno not installed")

        base_url = _public_base_url(request)
        payload = json.dumps({"v": 1, "server": base_url}, separators=(",", ":"))
        qr = segno.make(payload, error="m")
        buf = io.BytesIO()
        # scale=10 → ~370 px Kante bei typischem Payload; kein Border,
        # damit die Kachel im Template sauber eingebettet ist.
        qr.save(buf, kind="png", scale=10, border=2, dark="#0f172a", light="#ffffff")
        from fastapi.responses import Response
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "private, max-age=60"},
        )

    @app.get("/my/mobile-app/testflight-qr.png")
    async def my_mobile_app_testflight_qr(request: Request):
        """PNG-QR mit der TestFlight-Einladungs-URL. Gleiche Rendering-
        Pipeline wie der Server-QR — nur anderer Payload. So vermeiden
        wir einen externen QR-API-Aufruf im Template (Datenschutz +
        Offline-Tauglichkeit).
        """
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        import io
        try:
            import segno
        except ImportError:
            from fastapi.responses import Response
            return Response(status_code=503, content="segno not installed")

        testflight_url = "https://testflight.apple.com/join/skx3gZnk"
        qr = segno.make(testflight_url, error="m")
        buf = io.BytesIO()
        qr.save(buf, kind="png", scale=10, border=2, dark="#0f172a", light="#ffffff")
        from fastapi.responses import Response
        return Response(
            content=buf.getvalue(),
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    @app.post("/my/cloud-print/save")
    async def my_cloud_print_save(
        request: Request,
        target_queue: str = Form(""),
    ):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        # Nur Admin/User darf konfigurieren
        role = user.get("role_type", "user")
        if role == "employee":
            return RedirectResponse("/my/cloud-print", status_code=302)

        from cloudprint.db_extensions import update_cloudprint_config
        update_cloudprint_config(user["id"], target_queue, None)
        return RedirectResponse("/my/cloud-print?flash=config_saved", status_code=302)

    # ── Reports Light ─────────────────────────────────────────────────────

    @app.get("/my/reports", response_class=HTMLResponse)
    async def my_reports(request: Request):
        user = _require_employee(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        return templates.TemplateResponse("employee/my_reports.html", {
            "request": request, "user": user,
            **t_ctx(request),
        })

    # ══════════════════════════════════════════════════════════════════════
    # MITARBEITER-VERWALTUNG (/employees/*) — nur für Admin/User
    # ══════════════════════════════════════════════════════════════════════

    @app.get("/my/employees", response_class=HTMLResponse)
    @app.get("/employees", response_class=HTMLResponse)
    async def employees_list(request: Request):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import get_employees
        employees = get_employees(user["id"])
        importable_printix_users, import_error = _load_importable_printix_users(user)
        flash = request.query_params.get("flash", "")

        return templates.TemplateResponse("employee/employees_list.html", {
            "request": request, "user": user,
            "employees": employees, "flash": flash,
            "importable_printix_users": importable_printix_users,
            "import_error": import_error,
            "imported_result": None,
            **t_ctx(request),
        })

    @app.get("/my/employees/new", response_class=HTMLResponse)
    @app.get("/employees/new", response_class=HTMLResponse)
    async def employees_new(request: Request):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        return templates.TemplateResponse("employee/employees_new.html", {
            "request": request, "user": user, "error": "",
            **t_ctx(request),
        })

    @app.post("/my/employees/new")
    @app.post("/employees/new")
    async def employees_create(
        request: Request,
        username: str = Form(...),
        email: str = Form(""),
        full_name: str = Form(""),
        password: str = Form(""),
    ):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from db import username_exists
        if username_exists(username):
            return templates.TemplateResponse("employee/employees_new.html", {
                "request": request, "user": user,
                "error": "username_exists",
                **t_ctx(request),
            })

        if not password:
            password = secrets.token_urlsafe(12)

        from cloudprint.db_extensions import create_employee
        create_employee(
            parent_user_id=user["id"],
            username=username,
            password=password,
            email=email,
            full_name=full_name,
        )
        return RedirectResponse("/my/employees?flash=employee_created", status_code=302)

    @app.post("/my/employees/import")
    @app.post("/employees/import")
    async def employees_import(request: Request, printix_user_id: str = Form(...)):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import create_employee, get_employees, get_employee_by_printix_user_id

        parent_id = _get_parent_id(user)
        existing = get_employee_by_printix_user_id(printix_user_id, parent_id)
        if existing:
            return RedirectResponse(f"/my/employees/{existing['id']}", status_code=302)

        importable_printix_users, import_error = _load_importable_printix_users(user)
        selected = next((row for row in importable_printix_users if row.get("id") == printix_user_id), None)
        if not selected:
            employees = get_employees(parent_id)
            return templates.TemplateResponse("employee/employees_list.html", {
                "request": request, "user": user,
                "employees": employees, "flash": "employee_import_failed",
                "importable_printix_users": importable_printix_users,
                "import_error": import_error or "not_found",
                "imported_result": None,
                **t_ctx(request),
            })

        generated_password = secrets.token_urlsafe(12)
        username = _build_unique_username(selected.get("username_candidate") or selected.get("email") or selected.get("full_name") or "delegate-user")
        created = create_employee(
            parent_user_id=parent_id,
            username=username,
            password=generated_password,
            email=selected.get("email", ""),
            full_name=selected.get("full_name", ""),
            printix_user_id=printix_user_id,
            must_change_password=True,
        )
        employees = get_employees(parent_id)
        importable_printix_users, import_error = _load_importable_printix_users(user)
        return templates.TemplateResponse("employee/employees_list.html", {
            "request": request, "user": user,
            "employees": employees, "flash": "employee_imported",
            "importable_printix_users": importable_printix_users,
            "import_error": import_error,
            "imported_result": {
                "employee_id": created["id"],
                "username": created["username"],
                "password": generated_password,
                "full_name": created.get("full_name", ""),
                "email": created.get("email", ""),
            },
            **t_ctx(request),
        })

    # v6.7.24: Bulk-Import aller gecachten Printix-User auf einen Schlag.
    # Legt für jeden noch nicht vorhandenen MCP-Employee-Spiegel an und
    # verschickt (falls Mail-Setup vorhanden) eine Willkommens-Mail.
    # Erwartet Zwei-Stufen-Bestätigung im UI.
    @app.post("/my/employees/bulk-import")
    @app.post("/employees/bulk-import")
    async def employees_bulk_import(request: Request):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import (
            create_employee, get_employees, get_employee_by_printix_user_id,
        )
        from db import get_tenant_full_by_user_id, get_setting

        parent_id = _get_parent_id(user)
        importable, import_error = _load_importable_printix_users(user)
        tenant = get_tenant_full_by_user_id(parent_id)

        # Login-URL für die Einladungsmail
        login_url = (get_setting("public_url", "")
                     or tenant.get("tenant_url", "")
                     or "http://<HA-IP>:8080").rstrip("/")
        if not login_url.endswith("/login"):
            login_url = f"{login_url}/login"

        try:
            from reporting.notify_helper import send_employee_invitation
        except Exception:
            send_employee_invitation = None

        stats = {"created": 0, "mailed": 0, "skipped": 0, "errors": 0}
        for row in importable or []:
            try:
                px_id = row.get("id") or ""
                email = (row.get("email") or "").strip()
                if not px_id or not email:
                    stats["skipped"] += 1
                    continue
                if get_employee_by_printix_user_id(px_id, parent_id):
                    stats["skipped"] += 1
                    continue

                password = secrets.token_urlsafe(12)
                username = _build_unique_username(
                    row.get("username_candidate") or email.split("@")[0]
                    or row.get("full_name") or "delegate-user"
                )
                full_name = row.get("full_name") or email
                create_employee(
                    parent_user_id=parent_id,
                    username=username,
                    password=password,
                    email=email,
                    full_name=full_name,
                    printix_user_id=px_id,
                    must_change_password=True,
                )
                stats["created"] += 1

                # Willkommens-Mail (wenn Mail konfiguriert)
                if send_employee_invitation:
                    try:
                        sent = send_employee_invitation(
                            tenant=tenant,
                            recipient_email=email,
                            full_name=full_name,
                            username=username,
                            password=password,
                            login_url=login_url,
                            admin_name=user.get("full_name") or user.get("username", ""),
                        )
                        if sent:
                            stats["mailed"] += 1
                    except Exception as _m:
                        logger.warning("Bulk-Import Mail an %s fehlgeschlagen: %s",
                                       email, _m)
            except Exception as _e:
                logger.warning("Bulk-Import Eintrag übersprungen: %s", _e)
                stats["errors"] += 1

        logger.info(
            "Bulk-Import abgeschlossen: created=%d mailed=%d skipped=%d errors=%d",
            stats["created"], stats["mailed"], stats["skipped"], stats["errors"],
        )

        # Aktualisierte Listen
        employees = get_employees(parent_id)
        importable_after, import_error = _load_importable_printix_users(user)
        return templates.TemplateResponse("employee/employees_list.html", {
            "request": request, "user": user,
            "employees": employees, "flash": "bulk_import_done",
            "bulk_stats": stats,
            "importable_printix_users": importable_after,
            "import_error": import_error,
            "imported_result": None,
            **t_ctx(request),
        })

    @app.get("/my/employees/{employee_id}", response_class=HTMLResponse)
    @app.get("/employees/{employee_id}", response_class=HTMLResponse)
    async def employees_detail(request: Request, employee_id: str):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import (
            get_employee_by_id, get_delegations_for_owner,
            get_delegations_for_delegate,
        )
        employee = get_employee_by_id(employee_id, user["id"])
        if not employee:
            return RedirectResponse("/my/employees", status_code=302)

        delegations_out = get_delegations_for_owner(employee_id)
        delegations_in = get_delegations_for_delegate(employee_id)

        return templates.TemplateResponse("employee/employees_detail.html", {
            "request": request, "user": user,
            "employee": employee,
            "delegations_out": delegations_out,
            "delegations_in": delegations_in,
            **t_ctx(request),
        })

    @app.post("/my/employees/{employee_id}/delete")
    @app.post("/employees/{employee_id}/delete")
    async def employees_delete(request: Request, employee_id: str):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import delete_employee
        delete_employee(employee_id, user["id"])
        return RedirectResponse("/my/employees?flash=employee_deleted", status_code=302)

    # ── Admin: Delegationen genehmigen ────────────────────────────────────

    @app.post("/my/employees/delegation/{delegation_id}/approve")
    @app.post("/employees/delegation/{delegation_id}/approve")
    async def delegation_approve(request: Request, delegation_id: int):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import update_delegation_status
        update_delegation_status(delegation_id, "active")
        return RedirectResponse(request.headers.get("referer", "/my/employees"), status_code=302)

    @app.post("/my/employees/delegation/{delegation_id}/reject")
    @app.post("/employees/delegation/{delegation_id}/reject")
    async def delegation_reject(request: Request, delegation_id: int):
        user = _require_manager(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        from cloudprint.db_extensions import delete_delegation
        delete_delegation(delegation_id)
        return RedirectResponse(request.headers.get("referer", "/my/employees"), status_code=302)
