"""
Desktop-API-Routen (v6.7.31)
=============================
Endpunkte für den „Printix Send"-Windows-Client (und spätere Desktop-
Clients). Alle Routen sind Token-basiert authentifiziert via
`Authorization: Bearer <token>`.

Endpoints:
  POST /desktop/auth/login            — Credentials → Token
  POST /desktop/auth/logout           — Widerruft aktuellen Token
  GET  /desktop/me                    — Kurze User-Info für Token-Validation
  GET  /desktop/targets               — Zielliste für den aktuellen User
  POST /desktop/send                  — Datei-Upload + Dispatching
  GET  /desktop/client/latest-version — Update-Check (self-describing)

Response-Format: immer JSON. Fehler als `{"error": "…", "code": "…"}`
mit passendem HTTP-Status.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, FastAPI, File, Form, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from desktop_auth import (
    create_token, validate_token, revoke_token, list_tokens_for_user,
)

logger = logging.getLogger("printix.desktop")


# ─── Auth-Helper ─────────────────────────────────────────────────────────────

def _require_token(authorization: Optional[str]) -> Optional[dict]:
    """Extrahiert Token aus Auth-Header und validiert gegen DB."""
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return validate_token(parts[1].strip())


def _json_error(msg: str, code: str = "error", status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg, "code": code}, status_code=status)


# ─── Logging-Helpers ─────────────────────────────────────────────────────────

def _client_info(request: Request) -> dict:
    """Strukturierte Info über den Request-Absender für Log-Zeilen."""
    peer = request.client.host if request.client else "?"
    ua   = request.headers.get("user-agent", "-")[:120]
    host = request.headers.get("host", "-")
    return {"peer": peer, "ua": ua, "host": host}


def _log_req(request: Request, endpoint: str, extra: str = "") -> dict:
    """Einzeiler pro Request — Format analog zu IPP-HTTP.
    Returns ci-dict für späteren Gebrauch (z.B. in Fehler-Logs)."""
    ci = _client_info(request)
    logger.info(
        "Desktop: %s peer=%s host=%s UA=%s%s",
        endpoint, ci["peer"], ci["host"], ci["ua"],
        f" {extra}" if extra else "",
    )
    return ci


def _mask_token(token: Optional[str]) -> str:
    """Zeigt nur die letzten 8 Zeichen — nie den vollen Token im Log."""
    if not token:
        return "-"
    return f"…{token[-8:]}" if len(token) > 10 else "…"


# ─── Registrierung ───────────────────────────────────────────────────────────

async def _process_desktop_send_bg(
    user: dict,
    target_id: str,
    data: bytes,
    filename: str,
    copies: int,
    color: str,
    duplex: str,
    internal_id: str,
    t_start: float,
) -> None:
    """
    Background-Worker für /desktop/send (v6.7.43).

    Läuft als asyncio.Task, nachdem der HTTP-Handler bereits 202 Accepted
    zurückgegeben hat. Hintergrund: Cloudflare kappt jede HTTP-Verbindung
    nach 100 s (HTTP 524), aber die Printix-Pipeline (LibreOffice-Konvertierung
    + 5-Stage-Submit) braucht regelmäßig 90–180 s. Fire-and-forget umgeht
    diese Architektur-Grenze; Fehler landen im cloudprint_jobs-Eintrag
    und werden dort über die Web-UI sichtbar.
    """
    import time as _t
    try:
        import sys as _sys, os as _os
        src_dir = _os.path.dirname(_os.path.dirname(__file__))
        if src_dir not in _sys.path:
            _sys.path.insert(0, src_dir)
        from upload_converter import convert_to_pdf, ConversionError
        from db import get_tenant_full_by_user_id
        from cloudprint.db_extensions import (
            get_parent_user_id, get_cloudprint_config,
            get_delegations_for_owner, create_cloudprint_job,
            update_cloudprint_job_status,
        )
        from cloudprint.printix_cache_db import find_printix_user_by_identity

        def _fail(msg: str, code: str = "error") -> None:
            try:
                update_cloudprint_job_status(
                    internal_id, "error",
                    error_message=f"{code}: {msg}"[:500],
                )
            except Exception:
                pass
            logger.warning(
                "Desktop-Send BG-FAIL — user='%s' target=%s job_id=%s "
                "code=%s msg=%s",
                user.get("username"), target_id, internal_id, code, msg,
            )

        # === Stage 1: Format-Erkennung + Konvertierung =====================
        t_convert_start = _t.monotonic()
        try:
            pdf_data, conv_label = convert_to_pdf(data, filename)
            if pdf_data is not data:
                base = filename.rsplit(".", 1)[0] if "." in filename else filename
                display_filename = f"{base}.pdf"
                data = pdf_data
            else:
                display_filename = filename
            dt_conv = _t.monotonic() - t_convert_start
            logger.info(
                "Desktop-Send [1/5] convert OK — user='%s' conv='%s' "
                "out_size=%d dt=%.2fs job_id=%s",
                user.get("username"), conv_label, len(data), dt_conv, internal_id,
            )
        except ConversionError as ce:
            logger.warning(
                "Desktop-Send [1/5] convert FAIL — user='%s' file='%s' err=%s",
                user.get("username"), filename, ce,
            )
            _fail(str(ce), code="convert_failed")
            return
        except Exception as e:
            logger.error(
                "Desktop-Send [1/5] convert EXCEPTION — user='%s' file='%s' err=%s",
                user.get("username"), filename, e,
            )
            _fail(str(e)[:200], code="convert_error")
            return

        # === Stage 2: Tenant + Queue + Owner-Email =========================
        parent_id = get_parent_user_id(user["user_id"])
        tenant = get_tenant_full_by_user_id(parent_id)
        config = get_cloudprint_config(user["user_id"])

        if not tenant or not config or not config.get("lpr_target_queue"):
            fallback_source = ""
            try:
                from cloudprint.db_extensions import (
                    get_default_single_tenant, get_admin_tenant_with_queue,
                )
                fallback = get_default_single_tenant()
                if fallback and fallback.get("lpr_target_queue"):
                    fallback_source = "single-tenant"
                else:
                    fallback = get_admin_tenant_with_queue()
                    if fallback:
                        fallback_source = "admin-tenant"
                if fallback and fallback.get("lpr_target_queue"):
                    tenant = get_tenant_full_by_user_id(fallback["user_id"])
                    config = fallback
                    logger.info(
                        "Desktop-Send [2/5] fallback-tenant (%s) — user='%s' "
                        "→ tenant.user_id=%s queue=%s",
                        fallback_source, user.get("username"),
                        fallback.get("user_id"),
                        fallback.get("lpr_target_queue"),
                    )
            except Exception as _fb:
                logger.debug("Tenant-Fallback failed: %s", _fb)

        if not tenant or not config or not config.get("lpr_target_queue"):
            logger.warning(
                "Desktop-Send [2/5] no queue — user='%s' parent_id=%s "
                "tenant=%s queue=%s (auch Single-Tenant-Fallback leer)",
                user.get("username"), parent_id, bool(tenant),
                (config or {}).get("lpr_target_queue"),
            )
            _fail("no secure print queue configured", code="no_queue")
            return

        # Owner-Email ermitteln (für Default: den User selbst)
        user_email = (user.get("email") or "").strip()
        owner_email = user_email
        try:
            px_id = (user.get("printix_user_id") or "").strip()
            if px_id:
                from db import _conn as _dbconn
                with _dbconn() as _c:
                    row = _c.execute(
                        "SELECT email FROM cached_printix_users "
                        "WHERE printix_user_id=?", (px_id,),
                    ).fetchone()
                if row and row["email"]:
                    owner_email = row["email"]
            if not owner_email or "@" not in owner_email:
                pxu = find_printix_user_by_identity(user_email)
                if pxu and pxu.get("email"):
                    owner_email = pxu["email"]
        except Exception:
            pass

        target_id = (target_id or "").strip()
        target_type = ""
        if target_id == "print:self":
            submit_user_email = owner_email
            target_type = "print_secure"
        elif target_id.startswith("capture:"):
            profile_id = target_id.split(":", 1)[1].strip()
            from db import get_capture_profile, add_capture_log
            from capture.base_plugin import create_plugin_instance
            import capture.plugins  # noqa: F401

            profile = get_capture_profile(profile_id)
            if not profile:
                _fail("capture profile not found", code="target_not_found")
                return
            if not profile.get("is_active"):
                _fail("capture profile is disabled", code="target_disabled")
                return
            if tenant and profile.get("tenant_id") != tenant.get("id"):
                _fail("capture profile not accessible", code="target_forbidden")
                return

            plugin = create_plugin_instance(
                profile.get("plugin_type", ""),
                profile.get("config_json", "{}"),
            )
            if not plugin:
                _fail(
                    f"unknown capture plugin: {profile.get('plugin_type')}",
                    code="plugin_unknown",
                )
                return

            plugin_metadata = {
                "_source":       "desktop-send",
                "_user_name":    user.get("username", ""),
                "_user_email":   owner_email,
                "_device_name":  user.get("device_name", ""),
                "_filename":     display_filename,
                "title":         display_filename.rsplit(".", 1)[0]
                                 if "." in display_filename else display_filename,
            }

            t_up = _t.monotonic()
            try:
                ok, msg = await plugin.ingest_bytes(data, display_filename, plugin_metadata)
            except NotImplementedError as _ne:
                _fail(
                    f"Plugin '{profile.get('plugin_type')}' unterstützt keinen Direkt-Upload.",
                    code="plugin_no_ingest",
                )
                return
            except Exception as _pe:
                logger.exception(
                    "Desktop-Send [4/5] capture-plugin EXCEPTION — user='%s' "
                    "plugin=%s err=%s",
                    user.get("username"), profile.get("plugin_type"), _pe,
                )
                _fail(str(_pe)[:200], code="plugin_error")
                return
            dt_up = _t.monotonic() - t_up

            try:
                add_capture_log(
                    profile["tenant_id"], profile_id, profile.get("name", ""),
                    "DesktopSend", "ok" if ok else "error", msg or "",
                    details=f"user={user.get('username')}, "
                            f"file={display_filename}, size={len(data)}",
                )
            except Exception as _le:
                logger.debug("capture-log write failed: %s", _le)

            dt_total = _t.monotonic() - t_start
            if ok:
                try:
                    update_cloudprint_job_status(
                        internal_id, "forwarded",
                        target_queue=f"capture:{profile.get('plugin_type', '')}",
                    )
                except Exception:
                    pass
                logger.info(
                    "Desktop-Send COMPLETE (capture) — user='%s' target=%s "
                    "profile='%s' plugin=%s file='%s' size=%d dt_plugin=%.2fs "
                    "total_dt=%.2fs job_id=%s",
                    user["username"], target_id, profile.get("name", ""),
                    profile.get("plugin_type"), display_filename, len(data),
                    dt_up, dt_total, internal_id,
                )
            else:
                _fail(msg or "capture plugin returned failure", code="capture_failed")
            return
        elif target_id.startswith("print:delegate:"):
            deleg_id = target_id.split(":", 2)[2]
            try:
                delegs = get_delegations_for_owner(user["user_id"])
                delegate = next((d for d in delegs if str(d.get("id")) == str(deleg_id)), None)
            except Exception as _e:
                logger.warning(
                    "Desktop-Send [2/5] delegate-lookup err — user='%s' "
                    "deleg_id=%s: %s",
                    user.get("username"), deleg_id, _e,
                )
                delegate = None
            if not delegate or not delegate.get("delegate_email"):
                _fail("delegate target not found", code="target_not_found")
                return
            submit_user_email = delegate["delegate_email"]
            target_type = "print_delegate"
        else:
            _fail(f"unsupported target: {target_id}", code="target_unsupported")
            return

        logger.info(
            "Desktop-Send [2/5] resolved — user='%s' target=%s type=%s "
            "submit_to='%s' queue=%s job_id=%s",
            user.get("username"), target_id, target_type,
            submit_user_email, config["lpr_target_queue"], internal_id,
        )

        # === Stage 3-5: Printix Secure Print Submit ========================
        try:
            import re as _re
            from printix_client import PrintixClient
            client = PrintixClient(
                tenant_id=tenant["printix_tenant_id"],
                print_client_id=tenant.get("print_client_id", ""),
                print_client_secret=tenant.get("print_client_secret", ""),
                shared_client_id=tenant.get("shared_client_id", ""),
                shared_client_secret=tenant.get("shared_client_secret", ""),
                um_client_id=tenant.get("um_client_id", ""),
                um_client_secret=tenant.get("um_client_secret", ""),
            )
            printers_data = client.list_printers(size=200)
            raw_list = printers_data.get("printers", []) if isinstance(printers_data, dict) else []
            if not raw_list and isinstance(printers_data, dict):
                raw_list = (printers_data.get("_embedded") or {}).get("printers", [])
            printer_id = ""
            target_queue = config["lpr_target_queue"]
            for p in raw_list:
                href = (p.get("_links") or {}).get("self", {}).get("href", "")
                m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                if m and m.group(2) == target_queue:
                    printer_id = m.group(1)
                    break
            if not printer_id:
                logger.error(
                    "Desktop-Send [3/5] printer-id-lookup FAIL — user='%s' "
                    "queue=%s scanned_printers=%d",
                    user.get("username"), target_queue, len(raw_list),
                )
                _fail("target queue not found in Printix", code="queue_missing")
                return
            logger.info(
                "Desktop-Send [3/5] printer resolved — user='%s' printer_id=%s "
                "queue=%s job_id=%s",
                user.get("username"), printer_id, target_queue, internal_id,
            )

            # Jetzt (nachdem Tenant bekannt ist) den Tracking-Eintrag
            # auf "forwarding" updaten bzw. anlegen. create_cloudprint_job
            # ist idempotent genug: wir haben den Eintrag in desktop_send
            # bereits angelegt und aktualisieren hier nur das Ziel.
            try:
                update_cloudprint_job_status(
                    internal_id, "forwarding",
                    target_queue=target_queue,
                    detected_identity=submit_user_email,
                    identity_source="desktop-send",
                )
            except Exception:
                pass

            result = client.submit_print_job(
                printer_id=printer_id,
                queue_id=target_queue,
                title=display_filename,
                user=submit_user_email,
                pdl="PDF",
                release_immediately=False,
                color=bool(color),
                duplex=("LONG_EDGE" if duplex else "NONE"),
                copies=max(1, min(99, int(copies or 1))),
            )
            result_job = result.get("job", result) if isinstance(result, dict) else {}
            px_job_id = result_job.get("id", "") if isinstance(result_job, dict) else ""
            upload_url = ""
            upload_headers = {}
            if isinstance(result, dict):
                upload_url = result.get("uploadUrl", "") or ""
                links = result.get("uploadLinks") or []
                if not upload_url and links and isinstance(links[0], dict):
                    upload_url = links[0].get("url", "") or ""
                    upload_headers = links[0].get("headers") or {}

            if not px_job_id or not upload_url:
                logger.error(
                    "Desktop-Send [3/5] submit FAIL (no job-id/upload-url) — "
                    "user='%s' result_keys=%s",
                    user.get("username"),
                    list(result.keys()) if isinstance(result, dict) else "?",
                )
                _fail("Printix accepted no job", code="printix_no_job")
                return
            logger.info(
                "Desktop-Send [3/5] submit OK — user='%s' printix_job=%s job_id=%s",
                user.get("username"), px_job_id, internal_id,
            )

            t_upload = _t.monotonic()
            client.upload_file_to_url(upload_url, data, "application/pdf", upload_headers)
            dt_upload = _t.monotonic() - t_upload
            logger.info(
                "Desktop-Send [4a/5] blob-upload OK — user='%s' size=%d dt=%.2fs",
                user.get("username"), len(data), dt_upload,
            )
            client.complete_upload(px_job_id)
            logger.info(
                "Desktop-Send [4b/5] completeUpload OK — user='%s' printix_job=%s",
                user.get("username"), px_job_id,
            )

            if "@" in submit_user_email:
                try:
                    client.change_job_owner(px_job_id, submit_user_email)
                    logger.info(
                        "Desktop-Send [5/5] changeOwner OK — user='%s' "
                        "printix_job=%s owner='%s'",
                        user.get("username"), px_job_id, submit_user_email,
                    )

                    # Auto-Register printix_user_id: wenn der angemeldete User
                    # (user_id im lokalen Portal) noch keine echte Printix-UUID
                    # gespeichert hat — oder eine ungueltige mgr:-Manager-ID —
                    # dann holen wir sie jetzt aus dem Job, den wir gerade
                    # gesubmittet haben. Nach changeOwner ist ownerId die
                    # echte UUID des Ziel-Users. Kostet 1 zusaetzlichen
                    # list_print_jobs-Call (size=10) — laeuft nur wenn noetig.
                    try:
                        current_pxid = (user.get("printix_user_id") or "").strip()
                        needs_update = (
                            not current_pxid
                            or current_pxid.startswith("mgr:")
                            or ":" in current_pxid  # jede andere Prefix-Form auch
                        )
                        # Nur fuer den submittenden User selbst — nicht fuer
                        # Delegates (submit_user_email waere dann die Delegate-
                        # Email, wir wollen aber die UUID des Owners, also des
                        # eingeloggten Desktop-User).
                        own_email = (user.get("email") or "").strip().lower()
                        if (needs_update and own_email
                                and own_email == submit_user_email.lower()):
                            from db import _conn as _dbc
                            jobs_resp = client.list_print_jobs(size=10)
                            jobs = []
                            if isinstance(jobs_resp, dict):
                                jobs = (jobs_resp.get("jobs")
                                        or jobs_resp.get("content") or [])
                            elif isinstance(jobs_resp, list):
                                jobs = jobs_resp
                            new_uuid = ""
                            for j in jobs:
                                if j.get("id") == px_job_id:
                                    candidate = (j.get("ownerId") or "").strip()
                                    # Nur echte UUIDs akzeptieren — die haben
                                    # 36 Zeichen mit 4 Bindestrichen. mgr:-Praefix
                                    # ausschliessen (die Card-API lehnt die ab).
                                    if (candidate
                                            and not candidate.startswith("mgr:")
                                            and ":" not in candidate
                                            and len(candidate) >= 30):
                                        new_uuid = candidate
                                    break
                            if new_uuid and new_uuid != current_pxid:
                                with _dbc() as _c:
                                    _c.execute(
                                        "UPDATE users SET printix_user_id=? WHERE id=?",
                                        (new_uuid, user["user_id"]),
                                    )
                                logger.info(
                                    "Desktop-Send: auto-registered printix_user_id=%s "
                                    "fuer user='%s' (old='%s')",
                                    new_uuid, user.get("username"), current_pxid or "-",
                                )
                    except Exception as _ar:
                        logger.warning(
                            "Desktop-Send: auto-register printix_user_id "
                            "fehlgeschlagen fuer user='%s' err=%s",
                            user.get("username"), _ar,
                        )
                except Exception as _co:
                    logger.warning(
                        "Desktop-Send [5/5] changeOwner FAIL — user='%s' "
                        "printix_job=%s owner='%s' err=%s",
                        user.get("username"), px_job_id, submit_user_email, _co,
                    )
            else:
                logger.warning(
                    "Desktop-Send [5/5] changeOwner skip — submit_user_email "
                    "hat kein @: '%s'", submit_user_email,
                )

            update_cloudprint_job_status(
                internal_id, "forwarded",
                printix_job_id=px_job_id, target_queue=target_queue,
                detected_identity=submit_user_email,
                identity_source="desktop-send",
            )

            dt_total = _t.monotonic() - t_start
            logger.info(
                "Desktop-Send COMPLETE — user='%s' target=%s type=%s file='%s' "
                "size=%d printix_job=%s owner='%s' total_dt=%.2fs job_id=%s",
                user["username"], target_id, target_type, display_filename,
                len(data), px_job_id, submit_user_email, dt_total, internal_id,
            )
        except Exception as e:
            logger.exception(
                "Desktop-Send BG EXCEPTION — user='%s' target=%s file='%s' err=%s",
                user.get("username"), target_id, filename, e,
            )
            _fail(str(e)[:300], code="send_failed")
    except Exception as outer:
        # Letzter Fallback — damit die Task nicht stumm stirbt.
        logger.exception(
            "Desktop-Send BG OUTER EXCEPTION — user='%s' job_id=%s err=%s",
            user.get("username") if isinstance(user, dict) else "?",
            internal_id, outer,
        )


def register_desktop_routes(app: FastAPI, get_app_version) -> None:
    """Registriert alle /desktop/*-Routen in der FastAPI-App.

    `get_app_version` ist eine Callable die die Addon-Version als String
    zurückgibt (aus `app_version.APP_VERSION`).
    """

    # ── Auth ──────────────────────────────────────────────────────────────
    @app.post("/desktop/auth/login")
    async def desktop_login(request: Request):
        """Login-Endpoint — akzeptiert sowohl JSON als auch Form-Body.

        Der Windows-Client (PrintixSend) schickt JSON via PostAsJsonAsync.
        Ältere Aufrufe (z.B. curl, Postman-Form) funktionieren weiterhin
        als multipart/x-www-form-urlencoded.
        """
        ct_header = request.headers.get("content-type", "").lower()
        username = ""
        password = ""
        device_name = ""
        if "application/json" in ct_header:
            try:
                body = await request.json()
            except Exception:
                body = {}
            username = (body.get("username") or "").strip()
            password = body.get("password") or ""
            device_name = (body.get("device_name") or "").strip()
        else:
            form = await request.form()
            username = (form.get("username") or "").strip()
            password = form.get("password") or ""
            device_name = (form.get("device_name") or "").strip()

        if not username or not password:
            return _json_error("username and password required",
                               code="auth_missing_fields", status=422)

        ci = _log_req(request, "POST /auth/login",
                      f"username='{username}' device='{device_name or '-'}'")
        from db import authenticate_user
        user = authenticate_user(username.strip(), password)
        if not user:
            logger.warning(
                "Desktop-Login FAIL (invalid credentials) — user='%s' peer=%s",
                username, ci["peer"],
            )
            return _json_error("invalid credentials", code="auth_invalid", status=401)
        if user.get("status") and user.get("status") != "approved":
            logger.warning(
                "Desktop-Login FAIL (not approved) — user='%s' status=%s peer=%s",
                username, user.get("status"), ci["peer"],
            )
            return _json_error("account not approved", code="auth_pending", status=403)

        token = create_token(user["id"], device_name=device_name)
        logger.info(
            "Desktop-Login OK (local) — user='%s' uid=%s role=%s device='%s' "
            "token=%s peer=%s",
            user["username"], user["id"], user.get("role_type", "user"),
            device_name or "-", _mask_token(token), ci["peer"],
        )
        return JSONResponse({
            "token": token,
            "user": {
                "id": user["id"],
                "username": user["username"],
                "email": user.get("email", ""),
                "full_name": user.get("full_name", ""),
                "role_type": user.get("role_type", "user"),
            },
        })

    @app.post("/desktop/auth/logout")
    async def desktop_logout(request: Request,
                               authorization: str = Header(default="")):
        ci = _log_req(request, "POST /auth/logout")
        # Token aus Header extrahieren und widerrufen (auch bei ungültigem Token OK)
        token_value = ""
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token_value = parts[1].strip()
            revoked = revoke_token(token_value)
            logger.info("Desktop-Logout — token=%s revoked=%s peer=%s",
                        _mask_token(token_value), revoked, ci["peer"])
        else:
            logger.debug("Desktop-Logout — kein Token im Header (peer=%s)", ci["peer"])
        return JSONResponse({"ok": True})

    @app.get("/desktop/me")
    async def desktop_me(request: Request,
                           authorization: str = Header(default="")):
        ci = _log_req(request, "GET /me")
        user = _require_token(authorization)
        if not user:
            logger.warning("Desktop-Me FAIL (token invalid) — peer=%s", ci["peer"])
            return _json_error("token invalid", code="auth_required", status=401)
        logger.info(
            "Desktop-Me OK — user='%s' uid=%s device='%s' peer=%s",
            user.get("username"), user.get("user_id"),
            user.get("device_name", "-"), ci["peer"],
        )
        return JSONResponse({
            "user": {
                "id": user["user_id"],
                "username": user["username"],
                "email": user.get("email", ""),
                "full_name": user.get("full_name", ""),
                "role_type": user.get("role_type", "user"),
                "device_name": user.get("device_name", ""),
            },
        })

    # ── Targets ───────────────────────────────────────────────────────────
    @app.get("/desktop/targets")
    async def desktop_targets(request: Request,
                                authorization: str = Header(default="")):
        """Liefert eine Zielliste für den Desktop-Client.

        MVP-Zieltypen:
          - print_secure    → eigene Secure-Print-Queue
          - print_delegate  → Delegate-Print an eine konfigurierte Person
          - capture_profile → (Phase 4) Capture-Profile

        Aufbau pro Ziel: {id, type, label, icon, is_default, description}
        """
        ci = _log_req(request, "GET /targets")
        user = _require_token(authorization)
        if not user:
            logger.warning("Desktop-Targets FAIL (no token) — peer=%s", ci["peer"])
            return _json_error("token invalid", code="auth_required", status=401)

        from db import get_tenant_full_by_user_id
        from cloudprint.db_extensions import (
            get_parent_user_id, get_delegations_for_owner, get_cloudprint_config,
        )

        parent_id = get_parent_user_id(user["user_id"])
        tenant = get_tenant_full_by_user_id(parent_id)
        config = get_cloudprint_config(user["user_id"])

        # v6.7.35/36: Symmetrische Fallback-Kette zu /desktop/send
        # — Single-Tenant → Admin-Tenant.
        if not (config and config.get("lpr_target_queue")):
            try:
                from cloudprint.db_extensions import (
                    get_default_single_tenant, get_admin_tenant_with_queue,
                )
                fallback = get_default_single_tenant()
                if not (fallback and fallback.get("lpr_target_queue")):
                    fallback = get_admin_tenant_with_queue()
                if fallback and fallback.get("lpr_target_queue"):
                    config = fallback
                    if not tenant:
                        tenant = get_tenant_full_by_user_id(fallback["user_id"])
                    logger.debug(
                        "Desktop-Targets: Fallback-Tenant aktiv für user='%s' "
                        "→ queue=%s",
                        user.get("username"), fallback.get("lpr_target_queue"),
                    )
            except Exception as _fb:
                logger.debug("Desktop-Targets Fallback failed: %s", _fb)

        targets: list[dict] = []
        breakdown = {"self": 0, "delegates": 0, "capture": 0}

        # 1) Eigene Secure-Print-Queue
        if tenant and config and config.get("lpr_target_queue"):
            targets.append({
                "id": "print:self",
                "type": "print_secure",
                "label": "Mein Secure Print",
                "description": "Direkt in deine persönliche Release-Queue",
                "icon": "printer",
                "is_default": True,
            })
            breakdown["self"] = 1
        else:
            logger.debug(
                "Desktop-Targets: kein Secure-Print — tenant=%s queue=%s user='%s'",
                bool(tenant), (config or {}).get("lpr_target_queue"),
                user.get("username"),
            )

        # 2) Delegates (jede aktive Delegation = 1 Ziel)
        try:
            delegations = get_delegations_for_owner(user["user_id"])
            for d in delegations:
                if d.get("status") != "active":
                    continue
                email = d.get("delegate_email", "")
                name  = d.get("delegate_full_name") or d.get("delegate_username") or email
                if not email:
                    continue
                targets.append({
                    "id": f"print:delegate:{d['id']}",
                    "type": "print_delegate",
                    "label": f"Delegate: {name}",
                    "description": email,
                    "icon": "user",
                    "is_default": False,
                    "delegate_email": email,
                })
                breakdown["delegates"] += 1
        except Exception as _e:
            logger.warning(
                "Desktop-Targets: Delegate-Lookup failed — user='%s' err=%s",
                user.get("username"), _e,
            )

        # 3) Capture-Profile — alle aktiven Profile des Tenants als Send-To-Ziel.
        #    Client zeigt sie automatisch als eigenen "Senden an"-Eintrag
        #    (Send2Printix — Capture: <Name>).
        #
        #    Routing in /desktop/send ist noch Stub (liefert einen klaren
        #    Hinweis) — das eigentliche Capture-Dispatching folgt in einer
        #    späteren Version. Die Einträge erscheinen aber bereits im
        #    Explorer-"Senden an"-Menü.
        try:
            if tenant and tenant.get("id"):
                from db import get_capture_profiles_by_tenant
                profiles = get_capture_profiles_by_tenant(tenant["id"])
                for p in profiles:
                    if not p.get("is_active"):
                        continue
                    name = (p.get("name") or "").strip() or "Capture"
                    plugin_type = (p.get("plugin_type") or "").strip()
                    targets.append({
                        "id": f"capture:{p['id']}",
                        "type": "capture_profile",
                        "label": f"Capture: {name}",
                        "description": plugin_type or "Capture-Ziel",
                        "icon": "archive",
                        "is_default": False,
                    })
                    breakdown["capture"] += 1
        except Exception as _e:
            logger.warning(
                "Desktop-Targets: Capture-Lookup failed — user='%s' err=%s",
                user.get("username"), _e,
            )

        logger.info(
            "Desktop-Targets OK — user='%s' targets=%d (self=%d delegates=%d "
            "capture=%d) peer=%s",
            user.get("username"), len(targets),
            breakdown["self"], breakdown["delegates"], breakdown["capture"],
            ci["peer"],
        )
        return JSONResponse({"targets": targets})

    # ── Send (Datei-Upload + Dispatch) ────────────────────────────────────
    @app.post("/desktop/send")
    async def desktop_send(
        request: Request,
        authorization: str = Header(default=""),
        target_id: str = Form(...),
        file: UploadFile = File(...),
        copies: int = Form(1),
        color: str = Form(""),
        duplex: str = Form(""),
    ):
        import time as _t
        t_start = _t.monotonic()
        ci = _log_req(request, "POST /send",
                      f"target_id='{target_id}' filename='{file.filename if file else '-'}'")
        user = _require_token(authorization)
        if not user:
            logger.warning("Desktop-Send FAIL (no token) — peer=%s target=%s",
                           ci["peer"], target_id)
            return _json_error("token invalid", code="auth_required", status=401)

        if not file or not file.filename:
            logger.warning("Desktop-Send FAIL (no file) — user='%s' peer=%s",
                           user.get("username"), ci["peer"])
            return _json_error("no file", code="no_file", status=400)

        MAX = 50 * 1024 * 1024
        data = await file.read()
        if not data:
            logger.warning("Desktop-Send FAIL (empty file) — user='%s' peer=%s",
                           user.get("username"), ci["peer"])
            return _json_error("empty file", code="empty_file", status=400)
        if len(data) > MAX:
            logger.warning(
                "Desktop-Send FAIL (too large) — user='%s' size=%d peer=%s",
                user.get("username"), len(data), ci["peer"],
            )
            return _json_error("file too large (max 50 MB)",
                               code="too_large", status=413)
        logger.info(
            "Desktop-Send START — user='%s' device='%s' target=%s filename='%s' "
            "size=%d copies=%s color=%s duplex=%s peer=%s",
            user.get("username"), user.get("device_name", "-"), target_id,
            file.filename, len(data), copies,
            bool(color), bool(duplex), ci["peer"],
        )

        # v6.7.43: Fire-and-forget — Cloudflare kappt jede HTTP-Verbindung
        # nach 100 s (HTTP 524), aber unsere Pipeline (LibreOffice-Konvertierung
        # + 5-Stage-Printix-Submit) braucht regelmäßig 90–180 s. Daher:
        # jetzt nur noch validieren, Job-Tracking-Eintrag anlegen, 202 Accepted
        # zurück und die eigentliche Verarbeitung in asyncio.create_task().
        # Der Windows-Client sieht damit innerhalb weniger Sekunden "queued"
        # und der Server arbeitet in Ruhe weiter. Fehler landen im
        # cloudprint_jobs-Eintrag (Status=error) und sind in der Web-UI
        # unter „Meine Druckjobs" einsehbar.
        import asyncio
        import uuid as _uuid
        internal_id = _uuid.uuid4().hex[:10]

        # Tracking-Eintrag früh anlegen, damit ein sofortiger Fehler in der
        # Background-Task immer ein update_cloudprint_job_status() treffen
        # kann. tenant_id ist hier noch leer — die BG-Task trägt später das
        # echte Ziel (target_queue, identity) nach.
        try:
            import sys as _sys, os as _os
            src_dir = _os.path.dirname(_os.path.dirname(__file__))
            if src_dir not in _sys.path:
                _sys.path.insert(0, src_dir)
            from cloudprint.db_extensions import create_cloudprint_job
            create_cloudprint_job(
                job_id=internal_id,
                tenant_id="",
                queue_name="",
                username=(user.get("email") or user.get("username") or "")[:120],
                hostname=f"desktop:{user.get('device_name', '')}"[:80],
                job_name=file.filename,
                data_size=len(data),
                data_format="application/octet-stream",
                detected_identity=(user.get("email") or ""),
                identity_source="desktop-send",
                status="queued",
            )
        except Exception as _cj:
            # Wenn das Tracking-Insert fehlschlägt, trotzdem weiter —
            # der eigentliche Druckflow ist wichtiger als die UI-Anzeige.
            logger.debug("initial cloudprint_job insert failed: %s", _cj)

        asyncio.create_task(_process_desktop_send_bg(
            user=user,
            target_id=target_id,
            data=data,
            filename=file.filename,
            copies=copies,
            color=color,
            duplex=duplex,
            internal_id=internal_id,
            t_start=t_start,
        ))

        logger.info(
            "Desktop-Send QUEUED — user='%s' target=%s job_id=%s size=%d "
            "— 202 Accepted, Verarbeitung läuft asynchron",
            user.get("username"), target_id, internal_id, len(data),
        )
        return JSONResponse({
            "ok": True,
            "status": "queued",
            "job_id": internal_id,
            "target": target_id,
            "filename": file.filename,
            "size": len(data),
            "message": "Job angenommen — Verarbeitung läuft im Hintergrund.",
        }, status_code=202)

    # ── Entra SSO via Device Code Flow (v6.7.32) ──────────────────────────
    # Der Desktop-Client startet den Flow, zeigt dem User einen Code und die
    # Microsoft-URL an; User öffnet die URL im Browser, gibt den Code ein,
    # meldet sich mit Entra an. Der Client pollt derweil unseren poll-Endpoint
    # bis Microsoft den Access-Token zurückgibt — dann mappen wir den Entra-
    # User auf unseren MCP-User und geben einen Desktop-Token zurück.
    #
    # Im Gegensatz zum Web-Flow gibt's hier keine Session — der Device-Code
    # wird in einer Pending-Tabelle zwischengespeichert und nach Abschluss
    # gelöscht.

    @app.post("/desktop/auth/entra/start")
    async def desktop_entra_start(request: Request,
                                    device_name: str = Form("")):
        ci = _log_req(request, "POST /auth/entra/start",
                      f"device='{device_name or '-'}'")
        from db import get_setting
        if (get_setting("entra_enabled", "0") or "0") != "1":
            logger.warning("Desktop-Entra-Start FAIL (entra disabled) — peer=%s",
                           ci["peer"])
            return _json_error("Entra SSO not enabled on this server",
                               code="entra_disabled", status=400)
        try:
            from entra import start_device_code_flow
        except ImportError:
            logger.error("Desktop-Entra-Start EXC (entra module missing)")
            return _json_error("Entra module not available",
                               code="entra_unavailable", status=500)

        # v6.7.33-fix: Für Desktop-Login brauchen wir User.Read um nach dem
        # Auth-Flow via /me das Profil (oid + email) abzurufen — NICHT die
        # Application-Scopes aus dem Admin-Setup-Flow (der war für
        # App-Registration gedacht).
        _user_read_scope = (
            "https://graph.microsoft.com/User.Read "
            "offline_access openid email profile"
        )
        result = start_device_code_flow(scopes=_user_read_scope)
        if not result or not result.get("device_code"):
            logger.error(
                "Desktop-Entra-Start FAIL (Microsoft refused) — peer=%s result=%s",
                ci["peer"], result,
            )
            return _json_error("Microsoft refused device-code start",
                               code="entra_start_failed", status=502)

        # Device-Code in Pending-Tabelle cachen, keyed by session_id
        import secrets, json as _json
        from datetime import datetime, timezone
        from db import _conn
        session_id = secrets.token_urlsafe(24)
        now = datetime.now(timezone.utc).isoformat()
        with _conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS desktop_entra_pending (
                    session_id   TEXT PRIMARY KEY,
                    device_code  TEXT NOT NULL,
                    device_name  TEXT NOT NULL DEFAULT '',
                    created_at   TEXT NOT NULL,
                    expires_at   TEXT NOT NULL
                );
            """)
            from datetime import timedelta
            expires = (datetime.now(timezone.utc) +
                       timedelta(seconds=int(result.get("expires_in", 900)))).isoformat()
            conn.execute(
                "INSERT INTO desktop_entra_pending "
                "(session_id, device_code, device_name, created_at, expires_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, result["device_code"],
                 (device_name or "").strip(), now, expires),
            )
        logger.info(
            "Desktop-Entra-Start OK — session=%s… user_code=%s expires_in=%ss "
            "interval=%ss device='%s' peer=%s",
            session_id[:12], result.get("user_code", ""),
            result.get("expires_in", 900), result.get("interval", 5),
            device_name or "-", ci["peer"],
        )

        return JSONResponse({
            "session_id":        session_id,
            "user_code":         result.get("user_code", ""),
            "verification_uri":  result.get("verification_uri", "https://microsoft.com/devicelogin"),
            "expires_in":        result.get("expires_in", 900),
            "interval":          result.get("interval", 5),
            "message":           result.get("message", ""),
        })

    @app.post("/desktop/auth/entra/poll")
    async def desktop_entra_poll(request: Request,
                                   session_id: str = Form(...)):
        """Vom Desktop-Client im Interval aufgerufen. Status:
           - pending:      User hat noch nicht im Browser abgeschlossen
           - ok:           Anmeldung erfolgreich — Token zurück
           - expired:      Device-Code abgelaufen
           - error:        technischer Fehler
           - no_match:     Entra-User konnte keinem MCP-User zugeordnet werden
        """
        ci = _log_req(request, "POST /auth/entra/poll",
                      f"session={session_id[:12] if session_id else '-'}…")
        from db import _conn
        with _conn() as conn:
            row = conn.execute(
                "SELECT device_code, device_name FROM desktop_entra_pending "
                "WHERE session_id = ?", (session_id,),
            ).fetchone()
        if not row:
            logger.warning(
                "Desktop-Entra-Poll FAIL (session unknown) — session=%s… peer=%s",
                session_id[:12] if session_id else "-", ci["peer"],
            )
            return _json_error("unknown session", code="session_unknown", status=404)

        device_code = row["device_code"]
        device_name = row["device_name"]

        try:
            from entra import poll_device_code_token
            result = poll_device_code_token(device_code)
        except ImportError:
            logger.error("Desktop-Entra-Poll EXC — entra module missing")
            return _json_error("Entra module not available",
                               code="entra_unavailable", status=500)

        status = result.get("status", "pending")
        logger.debug(
            "Desktop-Entra-Poll — session=%s… status=%s device='%s'",
            session_id[:12], status, device_name or "-",
        )
        if status == "pending":
            return JSONResponse({"status": "pending"})
        if status == "expired":
            with _conn() as conn:
                conn.execute("DELETE FROM desktop_entra_pending WHERE session_id = ?",
                             (session_id,))
            logger.info(
                "Desktop-Entra-Poll EXPIRED — session=%s… (cleaned up)",
                session_id[:12],
            )
            return JSONResponse({"status": "expired"})
        if status == "error":
            logger.warning(
                "Desktop-Entra-Poll ERROR — session=%s… err=%s",
                session_id[:12], result.get("error", ""),
            )
            return JSONResponse({"status": "error",
                                 "error": result.get("error", "")})

        # status == "success" — Access-Token holen, Userprofil abrufen, mappen
        if status != "success" or not result.get("access_token"):
            logger.warning(
                "Desktop-Entra-Poll unexpected state — session=%s… status=%s",
                session_id[:12], status,
            )
            return JSONResponse({"status": "error", "error": "unexpected_state"})

        # Profil von Microsoft Graph holen (/me Endpoint).
        # Alternativ kann man `id_token` aus dem Response decoden — wir
        # holen aber direkt über das access_token um sicher zu sein die
        # richtige "oid" zu kriegen.
        import requests as _requests
        try:
            me = _requests.get(
                "https://graph.microsoft.com/v1.0/me",
                headers={"Authorization": f"Bearer {result['access_token']}"},
                timeout=15,
            )
            me.raise_for_status()
            me_data = me.json()
            profile = {
                "oid":   me_data.get("id", ""),
                "email": (me_data.get("mail") or
                          me_data.get("userPrincipalName") or ""),
                "name":  (me_data.get("displayName") or
                          me_data.get("givenName") or ""),
            }
            from db import get_or_create_entra_user
        except Exception as e:
            logger.error("Desktop-Entra: Profil-Abruf fehlgeschlagen: %s", e)
            return JSONResponse({"status": "error", "error": str(e)[:200]})

        if not profile or not profile.get("oid"):
            logger.warning(
                "Desktop-Entra-Poll NO_MATCH (no profile) — session=%s…",
                session_id[:12],
            )
            return JSONResponse({"status": "no_match",
                                 "error": "no user profile from Microsoft"})
        logger.info(
            "Desktop-Entra-Poll: Profil abgerufen — oid=%s… email='%s' name='%s'",
            profile["oid"][:10], profile.get("email", ""), profile.get("name", ""),
        )

        try:
            user = get_or_create_entra_user(
                entra_oid=profile["oid"],
                email=profile.get("email", ""),
                display_name=profile.get("name", ""),
            )
        except Exception as e:
            logger.error(
                "Desktop-Entra-Poll: get_or_create_entra_user FAIL — "
                "oid=%s… email='%s' err=%s",
                profile["oid"][:10], profile.get("email", ""), e,
            )
            return JSONResponse({"status": "error", "error": str(e)[:200]})

        if not user or user.get("status") in ("disabled", "suspended"):
            logger.warning(
                "Desktop-Entra-Poll NO_MATCH — user-lookup returned %s "
                "(status=%s) for email='%s'",
                "None" if not user else "user",
                (user or {}).get("status"),
                profile.get("email", ""),
            )
            return JSONResponse({"status": "no_match",
                                 "error": "user not approved"})

        # Desktop-Token anlegen + Pending-Eintrag löschen
        token = create_token(user["id"], device_name=device_name or "Entra-Desktop")
        with _conn() as conn:
            conn.execute("DELETE FROM desktop_entra_pending WHERE session_id = ?",
                         (session_id,))
        logger.info(
            "Desktop-Entra-Login OK — user='%s' uid=%s email='%s' oid=%s… "
            "token=%s device='%s'",
            user.get("username"), user.get("id"), user.get("email", ""),
            profile.get("oid", "")[:10], _mask_token(token),
            device_name or "Entra-Desktop",
        )
        return JSONResponse({
            "status": "ok",
            "token": token,
            "user": {
                "id": user["id"],
                "username": user.get("username", ""),
                "email": user.get("email", ""),
                "full_name": user.get("full_name", ""),
                "role_type": user.get("role_type", "user"),
            },
        })

    # ── Update-Check ──────────────────────────────────────────────────────
    @app.get("/desktop/client/latest-version")
    async def desktop_client_version(request: Request):
        """Self-describing Version-Endpoint. Der Client pingt das beim Start
        und zeigt ggf. einen Update-Hinweis an.

        Aktuell: die Addon-Version ist zugleich die minimale Server-Version.
        Der Client hat seine eigene Version — `required_client_version` kann
        der Admin später als Setting pflegen (global_min_client_version).
        """
        from db import get_setting
        required = (get_setting("min_client_version", "") or "").strip()
        download_url = (get_setting("client_download_url", "") or "").strip()
        return JSONResponse({
            "server_version": get_app_version(),
            "min_client_version": required or None,
            "download_url": download_url or None,
            # Endpoint-Versionen damit Client bei Breaking-Changes migrieren kann:
            "api_version": "1.0",
        })
