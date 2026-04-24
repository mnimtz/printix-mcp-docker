"""Guest-Print Admin-UI Routes (v7.1.0).

Registriert alle /guestprint-Routen. Aufruf aus app.py:

    from web.guestprint_routes import register_guestprint_routes
    register_guestprint_routes(app, templates, t_ctx, require_login)

Routen:
  GET  /guestprint                              → Redirect /guestprint/mailboxes

  GET  /guestprint/config                       → Entra-App-Config-Form
  POST /guestprint/config                       → Entra-App speichern

  GET  /guestprint/mailboxes                    → Postfach-Liste + New-Form
  POST /guestprint/mailboxes                    → Postfach anlegen
  POST /guestprint/mailboxes/{id}/test          → Graph-Connection testen
  POST /guestprint/mailboxes/{id}/poll          → Poll sofort ausfuehren
  POST /guestprint/mailboxes/{id}/delete        → Postfach loeschen

  GET  /guestprint/mailboxes/{id}               → Detail (Edit + Gaeste + Verlauf)
  POST /guestprint/mailboxes/{id}               → Postfach-Edit speichern

  POST /guestprint/mailboxes/{id}/guests        → Gast anlegen
  POST /guestprint/guests/{id}/edit             → Gast bearbeiten
  POST /guestprint/guests/{id}/delete           → Gast loeschen (+ optional Printix-User)
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import db
from guestprint import config as gp_config, graph as gp_graph
from guestprint import poller as gp_poller
from guestprint.printix import delete_guest as px_delete_guest

# Session-Keys fuer den Auto-Setup-Wizard. Der Device-Code-Admin-Token wird
# kurzzeitig im Session-Store gehalten, damit nach erfolgreichem App-Create
# die Postfachliste geladen werden kann — sobald der Admin ein Postfach
# gewaehlt hat (oder explizit abbricht), wird der Token geloescht.
_SESSION_DEVICE_CODE = "gp_entra_device_code"
_SESSION_ADMIN_TOKEN = "gp_entra_admin_token"
_SESSION_PROVISIONED = "gp_entra_provisioned_app"

logger = logging.getLogger("printix.guestprint")


def _make_printix_client(tenant: dict):
    """Replica of app._make_printix_client fuer die Route-Module."""
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from printix_client import PrintixClient
    return PrintixClient(
        tenant_id           = tenant.get("printix_tenant_id", ""),
        print_client_id     = tenant.get("print_client_id") or None,
        print_client_secret = tenant.get("print_client_secret") or None,
        card_client_id      = tenant.get("card_client_id") or None,
        card_client_secret  = tenant.get("card_client_secret") or None,
        ws_client_id        = tenant.get("ws_client_id") or None,
        ws_client_secret    = tenant.get("ws_client_secret") or None,
        um_client_id        = tenant.get("um_client_id") or None,
        um_client_secret    = tenant.get("um_client_secret") or None,
        shared_client_id    = tenant.get("shared_client_id") or None,
        shared_client_secret= tenant.get("shared_client_secret") or None,
    )


def _get_tenant(user: dict) -> dict:
    try:
        from db import get_tenant_full_by_user_id
        return get_tenant_full_by_user_id(user["id"]) or {}
    except Exception as e:
        logger.warning("Guest-Print: get_tenant fehlgeschlagen: %s", e)
        return {}


def _list_printer_queue_pairs(tenant: dict) -> list[dict]:
    """Holt (printer_id, queue_id, label) fuer die Dropdown-Auswahl in den
    Mailbox-/Gast-Formularen. Mirror von _extract_printer_queue_pairs aus
    app.py:3527 — wir duplizieren den Parser hier, damit die Route ohne
    Circular-Import auskommt.

    Bei fehlenden Credentials oder API-Fehlern: leere Liste (Formular
    degradiert dann still zum Freitext-Input).
    """
    if not tenant:
        return []
    has_print_api = bool(
        tenant.get("print_client_id") or tenant.get("shared_client_id")
    )
    if not has_print_api:
        return []

    try:
        client = _make_printix_client(tenant)
        data = client.list_printers(size=200)
    except Exception as e:
        logger.info("Guest-Print: list_printers fehlgeschlagen (%s) — Dropdown leer", e)
        return []

    raw_items: list[dict] = []
    if isinstance(data, dict):
        for key in ("printers", "content"):
            val = data.get(key)
            if isinstance(val, list):
                raw_items = val
                break

    import re
    pairs: list[dict] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        href = (item.get("_links") or {}).get("self", {}).get("href", "")
        m = re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
        printer_id = m.group(1) if m else (item.get("id", "") or "")
        queue_id   = m.group(2) if m else ""
        if not (printer_id and queue_id):
            continue
        vendor = item.get("vendor", "") or ""
        model  = item.get("model", "") or ""
        name   = item.get("name", "") or ""
        location = item.get("location", "") or ""
        printer_name = f"{vendor} {model}".strip() or name
        # Label: "HP LaserJet 4200 — Empfang-Queue @ Haus A"
        label = printer_name
        if name and name not in label:
            label = f"{label} — {name}" if label else name
        if location:
            label = f"{label} @ {location}"
        pairs.append({
            "printer_id": printer_id,
            "queue_id":   queue_id,
            "label":      label or f"{printer_id[:8]}…/{queue_id[:8]}…",
        })

    # Stabil sortieren — gleiche Drucker werden gruppiert
    pairs.sort(key=lambda p: p["label"].lower())
    return pairs


def register_guestprint_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    t_ctx: Callable,
    require_login: Callable,
) -> None:

    # ── Flash-Helpers ────────────────────────────────────────────────────────
    def _redirect_login() -> RedirectResponse:
        return RedirectResponse("/login", status_code=302)

    def _flash(request: Request, msg: str, kind: str = "success") -> None:
        request.session["flash_msg"]  = msg
        request.session["flash_kind"] = kind

    def _pop_flash(request: Request) -> tuple[str, str]:
        return (
            request.session.pop("flash_msg", ""),
            request.session.pop("flash_kind", "success"),
        )

    def _require_admin(request: Request) -> Optional[dict]:
        user = require_login(request)
        if not user:
            return None
        if not user.get("is_admin"):
            return None
        return user

    # ─────────────────────────────────────────────────────────────────────────
    # /guestprint — Redirect auf Mailbox-Liste
    # ─────────────────────────────────────────────────────────────────────────
    @app.get("/guestprint", response_class=RedirectResponse)
    async def guestprint_root(request: Request):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        return RedirectResponse("/guestprint/mailboxes", status_code=302)

    # ─────────────────────────────────────────────────────────────────────────
    # Entra-App-Config
    # ─────────────────────────────────────────────────────────────────────────
    @app.get("/guestprint/config", response_class=HTMLResponse)
    async def guestprint_config_get(request: Request):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)
        flash_msg, flash_kind = _pop_flash(request)
        cfg = gp_config.get_config()
        # Secret nie roh in HTML injizieren — wir zeigen nur "gesetzt/nicht gesetzt".
        return templates.TemplateResponse("guestprint_config.html", {
            "request":      request,
            "user":         user,
            "tenant":       _get_tenant(user),
            "tenant_id":    cfg.get("tenant_id", ""),
            "client_id":    cfg.get("client_id", ""),
            "has_secret":   bool(cfg.get("client_secret")),
            "is_configured": gp_config.is_configured(),
            "flash_msg":    flash_msg,
            "flash_kind":   flash_kind,
            **tc,
        })

    @app.post("/guestprint/config", response_class=HTMLResponse)
    async def guestprint_config_post(
        request: Request,
        tenant_id: str = Form(""),
        client_id: str = Form(""),
        client_secret: str = Form(""),
    ):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        try:
            gp_config.set_config(tenant_id, client_id, client_secret)
            _flash(request, "Guest-Print Entra-Konfiguration gespeichert.", "success")
        except Exception as e:
            logger.exception("Guest-Print Config-Save fehlgeschlagen")
            _flash(request, f"Speichern fehlgeschlagen: {e}", "error")
        return RedirectResponse("/guestprint/config", status_code=302)

    # ─────────────────────────────────────────────────────────────────────────
    # Auto-Setup-Wizard (Device Code Flow)
    # ─────────────────────────────────────────────────────────────────────────
    @app.post("/guestprint/config/device-code", response_class=JSONResponse)
    async def gp_device_code_start(request: Request):
        """Startet den Device Code Flow fuer den Guest-Print-Auto-Setup."""
        user = _require_admin(request)
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            from entra import start_device_code_flow_guestprint
        except ImportError:
            return JSONResponse({"error": "entra module not available"},
                                status_code=500)
        result = start_device_code_flow_guestprint()
        if not result or not result.get("device_code"):
            return JSONResponse({"error": "device_code_failed"}, status_code=502)
        request.session[_SESSION_DEVICE_CODE] = result["device_code"]
        return JSONResponse({
            "user_code":        result["user_code"],
            "verification_uri": result["verification_uri"],
            "expires_in":       result["expires_in"],
            "interval":         result.get("interval", 5),
            "message":          result.get("message", ""),
        })

    @app.get("/guestprint/config/device-poll", response_class=JSONResponse)
    async def gp_device_code_poll(request: Request):
        """Pollt den Token, erstellt bei Erfolg die Guest-Print-App + speichert."""
        user = _require_admin(request)
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        device_code = request.session.get(_SESSION_DEVICE_CODE, "")
        if not device_code:
            return JSONResponse({"status": "error", "error": "no_device_code"})

        try:
            from entra import (auto_register_guestprint_app,
                                list_tenant_mailboxes, poll_device_code_token)
        except ImportError:
            return JSONResponse({"status": "error",
                                  "error": "entra module not available"})

        poll = poll_device_code_token(device_code)
        status = poll.get("status")

        if status == "pending":
            return JSONResponse({"status": "pending"})
        if status == "expired":
            request.session.pop(_SESSION_DEVICE_CODE, None)
            return JSONResponse({"status": "expired"})
        if status == "error":
            request.session.pop(_SESSION_DEVICE_CODE, None)
            return JSONResponse({"status": "error",
                                  "error": poll.get("error", "")})

        # status == "success"
        access_token = poll.get("access_token", "")
        request.session.pop(_SESSION_DEVICE_CODE, None)
        if not access_token:
            return JSONResponse({"status": "error", "error": "no_access_token"})

        reg = auto_register_guestprint_app(access_token)
        if not reg or not reg.get("client_id"):
            return JSONResponse({"status": "error",
                                  "error": "app_creation_failed"})

        try:
            gp_config.set_config(
                tenant_id=reg.get("tenant_id", ""),
                client_id=reg.get("client_id", ""),
                client_secret=reg.get("client_secret", ""),
            )
        except Exception as e:
            logger.exception("Guest-Print Auto-Setup Speichern fehlgeschlagen")
            return JSONResponse({
                "status": "error",
                "error":  f"App erstellt, aber Speichern fehlgeschlagen: {e}",
            })

        # Postfachliste direkt nachladen, solange wir den Admin-Token haben.
        # Cache den Token kurz in der Session fuer ggf. Retry, sonst ist er
        # nach 1h eh tot.
        request.session[_SESSION_ADMIN_TOKEN] = access_token
        request.session[_SESSION_PROVISIONED] = {
            "client_id": reg.get("client_id", ""),
            "tenant_id": reg.get("tenant_id", ""),
            "consent_ok": bool(reg.get("consent_ok")),
        }

        mailboxes = list_tenant_mailboxes(access_token)

        try:
            from db import audit
            audit(user["id"], "guestprint_auto_setup",
                  f"Guest-Print-App erstellt (client_id={reg.get('client_id','')}, "
                  f"consent={'ok' if reg.get('consent_ok') else 'manuell'})")
        except Exception:
            pass

        return JSONResponse({
            "status":     "success",
            "client_id":  reg.get("client_id", ""),
            "tenant_id":  reg.get("tenant_id", ""),
            "consent_ok": bool(reg.get("consent_ok")),
            "mailboxes":  mailboxes,
        })

    @app.get("/guestprint/config/list-mailboxes", response_class=JSONResponse)
    async def gp_list_tenant_mailboxes(request: Request):
        """Laedt die Postfachliste erneut (wenn der Token noch in der
        Session liegt). Wird vom Wizard nach Postfach-Auswahl-Neuladen benutzt."""
        user = _require_admin(request)
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        token = request.session.get(_SESSION_ADMIN_TOKEN, "")
        if not token:
            return JSONResponse({"status": "expired", "mailboxes": []})
        try:
            from entra import list_tenant_mailboxes
        except ImportError:
            return JSONResponse({"status": "error",
                                  "error": "entra module not available"})
        mailboxes = list_tenant_mailboxes(token)
        return JSONResponse({"status": "ok", "mailboxes": mailboxes})

    @app.post("/guestprint/config/create-mailbox", response_class=HTMLResponse)
    async def gp_create_mailbox_from_wizard(
        request: Request,
        upn:         str = Form(""),
        name:        str = Form(""),
    ):
        """Schritt nach dem Auto-Setup: Admin waehlt ein Postfach, wir legen
        den guestprint_mailbox-Eintrag an und leiten auf die Detail-Seite."""
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        upn = (upn or "").strip().lower()
        if not upn:
            _flash(request, "Kein Postfach ausgewaehlt.", "error")
            return RedirectResponse("/guestprint/config", status_code=302)

        tenant = _get_tenant(user)
        tid = tenant.get("id", "")
        if not tid:
            _flash(request, "Kein Tenant gefunden.", "error")
            return RedirectResponse("/guestprint/config", status_code=302)

        try:
            mb = db.create_guestprint_mailbox(
                tenant_id=tid,
                name=name or upn,
                upn=upn,
                default_printer_id="",
                default_queue_id="",
                poll_interval_sec=60,
                folder_processed="GuestPrint/Processed",
                folder_skipped="GuestPrint/Skipped",
                max_attachment_bytes=26214400,
                enabled=True,
            )
        except Exception as e:
            logger.exception("Guest-Print Wizard: Mailbox-Create fehlgeschlagen")
            _flash(request, f"Postfach anlegen fehlgeschlagen: {e}", "error")
            return RedirectResponse("/guestprint/config", status_code=302)

        # Wizard-State aufraeumen — der Admin-Token bleibt nicht laenger
        # in der Session, als noetig.
        request.session.pop(_SESSION_ADMIN_TOKEN, None)
        request.session.pop(_SESSION_PROVISIONED, None)

        _flash(request, f"Postfach '{upn}' angelegt. Jetzt Drucker/Queue "
                        "waehlen und optional Gaeste hinzufuegen.", "success")
        return RedirectResponse(f"/guestprint/mailboxes/{mb['id']}",
                                 status_code=302)

    # ─────────────────────────────────────────────────────────────────────────
    # Mailbox-Liste
    # ─────────────────────────────────────────────────────────────────────────
    @app.get("/guestprint/mailboxes", response_class=HTMLResponse)
    async def mailboxes_list(request: Request):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)
        tenant = _get_tenant(user)
        tid = tenant.get("id", "")
        flash_msg, flash_kind = _pop_flash(request)
        mailboxes = []
        if tid:
            try:
                mailboxes = db.list_guestprint_mailboxes(tid)
            except Exception as e:
                logger.error("list_guestprint_mailboxes: %s", e)
                _flash(request, f"DB-Fehler: {e}", "error")

        return templates.TemplateResponse("guestprint_mailboxes.html", {
            "request":       request,
            "user":          user,
            "tenant":        tenant,
            "mailboxes":     mailboxes,
            "printer_queue_pairs": _list_printer_queue_pairs(tenant),
            "is_configured": gp_config.is_configured(),
            "flash_msg":     flash_msg,
            "flash_kind":    flash_kind,
            **tc,
        })

    @app.post("/guestprint/mailboxes", response_class=HTMLResponse)
    async def mailbox_create(
        request: Request,
        name:                  str = Form(""),
        upn:                   str = Form(""),
        default_printer_id:    str = Form(""),
        default_queue_id:      str = Form(""),
        poll_interval_sec:     int = Form(60),
        folder_processed:      str = Form("GuestPrint/Processed"),
        folder_skipped:        str = Form("GuestPrint/Skipped"),
        max_attachment_bytes:  int = Form(26214400),
        enabled:               str = Form(""),
    ):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        tenant = _get_tenant(user)
        tid = tenant.get("id", "")
        if not tid:
            _flash(request, "Kein Tenant gefunden", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)
        if not upn.strip():
            _flash(request, "UPN (Mailadresse des Postfachs) ist Pflicht.", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)

        try:
            mb = db.create_guestprint_mailbox(
                tenant_id=tid,
                name=name or upn,
                upn=upn,
                default_printer_id=default_printer_id,
                default_queue_id=default_queue_id,
                poll_interval_sec=int(poll_interval_sec or 60),
                folder_processed=folder_processed or "GuestPrint/Processed",
                folder_skipped=folder_skipped or "GuestPrint/Skipped",
                max_attachment_bytes=int(max_attachment_bytes or 26214400),
                enabled=bool(enabled),
            )
            _flash(request, f"Postfach '{mb['upn']}' angelegt.", "success")
            return RedirectResponse(f"/guestprint/mailboxes/{mb['id']}", status_code=302)
        except Exception as e:
            logger.exception("Mailbox-Create fehlgeschlagen")
            _flash(request, f"Anlegen fehlgeschlagen: {e}", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)

    @app.post("/guestprint/mailboxes/{mailbox_id}/test", response_class=JSONResponse)
    async def mailbox_test(mailbox_id: str, request: Request):
        user = _require_admin(request)
        if not user:
            return JSONResponse({"ok": False, "error": "auth"}, status_code=401)
        mb = db.get_guestprint_mailbox(mailbox_id)
        if not mb:
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
        try:
            result = gp_graph.test_connection(mb["upn"])
        except Exception as e:
            result = {"ok": False, "error": str(e)}
        return JSONResponse(result)

    @app.post("/guestprint/mailboxes/{mailbox_id}/poll", response_class=HTMLResponse)
    async def mailbox_poll_now(mailbox_id: str, request: Request):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        mb = db.get_guestprint_mailbox(mailbox_id)
        if not mb:
            _flash(request, "Postfach nicht gefunden", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)
        tenant = _get_tenant(user)
        try:
            client = _make_printix_client(tenant)
            result = gp_poller.process_mailbox(mb, client)
            msg = (
                f"Poll OK: {result['messages_seen']} Mails gesehen, "
                f"{result['messages_matched']} gematcht, "
                f"{result['attachments_ok']} gedruckt, "
                f"{result['attachments_failed']} fehlgeschlagen, "
                f"{result['attachments_skipped']} uebersprungen."
            )
            kind = "success" if not result["errors"] else "warning"
            if result["errors"]:
                msg += " Fehler: " + "; ".join(result["errors"][:3])
            _flash(request, msg, kind)
        except Exception as e:
            logger.exception("Mailbox-Poll fehlgeschlagen")
            _flash(request, f"Poll fehlgeschlagen: {e}", "error")
        return RedirectResponse(f"/guestprint/mailboxes/{mailbox_id}", status_code=302)

    @app.post("/guestprint/mailboxes/{mailbox_id}/delete", response_class=HTMLResponse)
    async def mailbox_delete(mailbox_id: str, request: Request):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        try:
            db.delete_guestprint_mailbox(mailbox_id)
            _flash(request, "Postfach geloescht (Gaeste-Eintraege ebenfalls).", "success")
        except Exception as e:
            logger.exception("Mailbox-Delete fehlgeschlagen")
            _flash(request, f"Loeschen fehlgeschlagen: {e}", "error")
        return RedirectResponse("/guestprint/mailboxes", status_code=302)

    # ─────────────────────────────────────────────────────────────────────────
    # Mailbox-Detail (Edit + Gaeste + Verlauf auf einer Seite)
    # ─────────────────────────────────────────────────────────────────────────
    @app.get("/guestprint/mailboxes/{mailbox_id}", response_class=HTMLResponse)
    async def mailbox_detail(mailbox_id: str, request: Request):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)
        tenant = _get_tenant(user)
        mb = db.get_guestprint_mailbox(mailbox_id)
        if not mb:
            _flash(request, "Postfach nicht gefunden", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)
        flash_msg, flash_kind = _pop_flash(request)

        try:
            guests = db.list_guestprint_guests(mailbox_id)
        except Exception as e:
            logger.error("list_guestprint_guests: %s", e)
            guests = []
        try:
            jobs = db.list_guestprint_jobs(mailbox_id=mailbox_id, limit=100)
        except Exception as e:
            logger.error("list_guestprint_jobs: %s", e)
            jobs = []

        return templates.TemplateResponse("guestprint_detail.html", {
            "request":    request,
            "user":       user,
            "tenant":     tenant,
            "mailbox":    mb,
            "guests":     guests,
            "jobs":       jobs,
            "printer_queue_pairs": _list_printer_queue_pairs(tenant),
            "flash_msg":  flash_msg,
            "flash_kind": flash_kind,
            **tc,
        })

    @app.post("/guestprint/mailboxes/{mailbox_id}", response_class=HTMLResponse)
    async def mailbox_edit_post(
        mailbox_id: str,
        request: Request,
        name:                  str = Form(""),
        upn:                   str = Form(""),
        default_printer_id:    str = Form(""),
        default_queue_id:      str = Form(""),
        poll_interval_sec:     int = Form(60),
        folder_processed:      str = Form("GuestPrint/Processed"),
        folder_skipped:        str = Form("GuestPrint/Skipped"),
        max_attachment_bytes:  int = Form(26214400),
        enabled:               str = Form(""),
    ):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        try:
            db.update_guestprint_mailbox(
                mailbox_id,
                name=name,
                upn=upn,
                default_printer_id=default_printer_id,
                default_queue_id=default_queue_id,
                poll_interval_sec=int(poll_interval_sec or 60),
                folder_processed=folder_processed,
                folder_skipped=folder_skipped,
                max_attachment_bytes=int(max_attachment_bytes or 26214400),
                enabled=bool(enabled),
            )
            _flash(request, "Postfach aktualisiert.", "success")
        except Exception as e:
            logger.exception("Mailbox-Edit fehlgeschlagen")
            _flash(request, f"Aktualisieren fehlgeschlagen: {e}", "error")
        return RedirectResponse(f"/guestprint/mailboxes/{mailbox_id}", status_code=302)

    # ─────────────────────────────────────────────────────────────────────────
    # Gaeste-CRUD
    # ─────────────────────────────────────────────────────────────────────────
    @app.post("/guestprint/mailboxes/{mailbox_id}/guests", response_class=HTMLResponse)
    async def guest_create(
        mailbox_id: str,
        request: Request,
        sender_email:    str = Form(""),
        full_name:       str = Form(""),
        expiration_days: int = Form(7),
        printer_id:      str = Form(""),
        queue_id:        str = Form(""),
        enabled:         str = Form("on"),
    ):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        if not sender_email.strip():
            _flash(request, "Email-Adresse ist Pflicht.", "error")
            return RedirectResponse(f"/guestprint/mailboxes/{mailbox_id}", status_code=302)
        try:
            db.create_guestprint_guest(
                mailbox_id=mailbox_id,
                sender_email=sender_email,
                full_name=full_name,
                expiration_days=int(expiration_days or 7),
                printer_id=printer_id,
                queue_id=queue_id,
                enabled=bool(enabled),
            )
            _flash(request, f"Gast '{sender_email}' angelegt.", "success")
        except Exception as e:
            logger.exception("Guest-Create fehlgeschlagen")
            _flash(request, f"Anlegen fehlgeschlagen: {e}", "error")
        return RedirectResponse(f"/guestprint/mailboxes/{mailbox_id}", status_code=302)

    @app.post("/guestprint/guests/{guest_id}/edit", response_class=HTMLResponse)
    async def guest_edit_post(
        guest_id: str,
        request: Request,
        sender_email:    str = Form(""),
        full_name:       str = Form(""),
        expiration_days: int = Form(7),
        printer_id:      str = Form(""),
        queue_id:        str = Form(""),
        enabled:         str = Form(""),
    ):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        g = db.get_guestprint_guest(guest_id)
        if not g:
            _flash(request, "Gast nicht gefunden", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)
        try:
            db.update_guestprint_guest(
                guest_id,
                sender_email=sender_email,
                full_name=full_name,
                expiration_days=int(expiration_days or 7),
                printer_id=printer_id,
                queue_id=queue_id,
                enabled=bool(enabled),
            )
            _flash(request, "Gast aktualisiert.", "success")
        except Exception as e:
            logger.exception("Guest-Edit fehlgeschlagen")
            _flash(request, f"Aktualisieren fehlgeschlagen: {e}", "error")
        return RedirectResponse(
            f"/guestprint/mailboxes/{g['mailbox_id']}", status_code=302,
        )

    @app.post("/guestprint/guests/{guest_id}/delete", response_class=HTMLResponse)
    async def guest_delete(
        guest_id: str,
        request: Request,
        delete_printix_user: str = Form(""),
    ):
        user = _require_admin(request)
        if not user:
            return _redirect_login()
        g = db.get_guestprint_guest(guest_id)
        if not g:
            _flash(request, "Gast nicht gefunden", "error")
            return RedirectResponse("/guestprint/mailboxes", status_code=302)
        mailbox_id = g["mailbox_id"]
        extra = ""
        # Optionaler Printix-User-Delete
        if delete_printix_user and g.get("printix_user_id"):
            try:
                tenant = _get_tenant(user)
                client = _make_printix_client(tenant)
                px_delete_guest(client, g["printix_user_id"])
                extra = " (Printix-User ebenfalls)"
            except Exception as e:
                logger.warning("Printix-Delete fehlgeschlagen: %s", e)
                extra = f" (Printix-Delete fehlgeschlagen: {e})"
        try:
            db.delete_guestprint_guest(guest_id)
            _flash(request, f"Gast '{g['sender_email']}' geloescht{extra}.", "success")
        except Exception as e:
            logger.exception("Guest-Delete fehlgeschlagen")
            _flash(request, f"Loeschen fehlgeschlagen: {e}", "error")
        return RedirectResponse(f"/guestprint/mailboxes/{mailbox_id}", status_code=302)


