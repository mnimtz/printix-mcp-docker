"""
Capture Store Routes — Web-UI für Capture-Profil-Verwaltung (v4.4.13)
====================================================================
Registriert alle /capture-Routen in der FastAPI-App.

Aufruf aus app.py:
    from web.capture_routes import register_capture_routes
    register_capture_routes(app, templates, t_ctx, require_login)

Routen:
  GET  /capture                       → Capture Store (Übersicht)
  GET  /capture/new                   → Neues Profil anlegen
  POST /capture/new                   → Profil speichern (neu)
  GET  /capture/{id}/edit             → Profil bearbeiten
  POST /capture/{id}/edit             → Profil speichern (Update)
  POST /capture/{id}/delete           → Profil löschen
  POST /capture/{id}/toggle           → Profil aktivieren/deaktivieren
  POST /capture/{id}/test             → Verbindungstest
  POST /capture/webhook/{profile_id}  → Printix Capture Webhook Endpoint
"""

import json
import logging
from typing import Callable, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

logger = logging.getLogger("printix.capture")


def register_capture_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    t_ctx: Callable,
    require_login: Callable,
) -> None:
    """Registriert alle Capture-Store-Routen."""

    # ── Import plugins (triggers @register_plugin) ──────────────────────────
    from capture.base_plugin import get_all_plugins, create_plugin_instance
    import capture.plugins  # noqa: F401 — auto-discovers all plugins via pkgutil

    # ── Helper: get tenant for current user ─────────────────────────────────
    def _get_tenant(user: dict) -> Optional[dict]:
        from db import get_tenant_by_user_id
        return get_tenant_by_user_id(user["id"])

    def _get_webhook_base(request: Request) -> tuple:
        """
        Webhook Base-URL Prioritaet (v4.5.0):
          1. CAPTURE_PUBLIC_URL (env) — eigene Capture-Domain
          2. capture_public_url (DB Setting)
          3. MCP_PUBLIC_URL (env) — Fallback auf MCP-Domain
          4. public_url (DB Setting)
          5. Fallback: request URL (wahrscheinlich FALSCH)
        Returns (base_url, is_configured, is_separate_capture)
        """
        import os
        # v4.5.0: Eigene Capture-URL hat hoechste Prioritaet
        capture_url = os.environ.get("CAPTURE_PUBLIC_URL", "").strip().rstrip("/")
        if capture_url:
            return capture_url, True, True
        try:
            from db import get_setting
            capture_url = get_setting("capture_public_url", "").strip().rstrip("/")
        except Exception:
            pass
        if capture_url:
            return capture_url, True, True
        # Kein separater Capture-Endpunkt — Fallback auf MCP-URL
        wb = os.environ.get("MCP_PUBLIC_URL", "").strip().rstrip("/")
        if wb:
            return wb, True, False
        try:
            from db import get_setting
            wb = get_setting("public_url", "").strip().rstrip("/")
        except Exception:
            pass
        if wb:
            return wb, True, False
        # Fallback: request URL — wahrscheinlich FALSCH fuer Webhooks
        return f"{request.url.scheme}://{request.url.netloc}", False, False

    def _is_capture_separate() -> bool:
        """Prueft ob separater Capture-Server aktiv (v4.6.0: bool statt Port)."""
        import os
        if os.environ.get("CAPTURE_ENABLED", "false").lower() == "true":
            return True
        if os.environ.get("CAPTURE_PUBLIC_URL", "").strip():
            return True
        try:
            from db import get_setting
            if get_setting("capture_public_url", "").strip():
                return True
        except Exception:
            pass
        return False

    # ── GET /capture — Store Overview ───────────────────────────────────────

    @app.get("/capture", response_class=HTMLResponse)
    async def capture_store(request: Request):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        from db import get_capture_profiles_by_tenant
        import asyncio
        profiles = await asyncio.to_thread(get_capture_profiles_by_tenant, tenant["id"])

        # Parse config for display
        for p in profiles:
            try:
                p["_config"] = json.loads(p.get("config_json", "{}"))
            except (json.JSONDecodeError, TypeError):
                p["_config"] = {}

        plugins = get_all_plugins()
        plugin_info = []
        for pid, cls in plugins.items():
            plugin_info.append({
                "id": cls.plugin_id,
                "name": cls.plugin_name,
                "icon": cls.plugin_icon,
                "description": cls.plugin_description,
                "color": cls.plugin_color,
            })

        ctx = t_ctx(request)
        ctx.update({
            "request": request,
            "user": user,
            "tenant": tenant,
            "profiles": profiles,
            "plugins": plugin_info,
            "profiles_count": len(profiles),
            "active_count": sum(1 for p in profiles if p["is_active"]),
            "webhook_base": _get_webhook_base(request)[0],
            "webhook_base_configured": _get_webhook_base(request)[1],
            "capture_separate": _get_webhook_base(request)[2],
        })
        return templates.TemplateResponse("capture_store.html", ctx)

    # ── GET /capture/new — New Profile Form ─────────────────────────────────

    @app.get("/capture/new", response_class=HTMLResponse)
    async def capture_new_form(request: Request):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        plugin_type = request.query_params.get("plugin", "paperless_ngx")
        plugin_instance = create_plugin_instance(plugin_type)
        if not plugin_instance:
            return RedirectResponse("/capture", status_code=303)

        ctx = t_ctx(request)
        ctx.update({
            "request": request,
            "user": user,
            "tenant": tenant,
            "mode": "create",
            "profile": None,
            "plugin": {
                "id": plugin_instance.plugin_id,
                "name": plugin_instance.plugin_name,
                "icon": plugin_instance.plugin_icon,
                "color": plugin_instance.plugin_color,
            },
            "config_fields": plugin_instance.config_schema(),
            "config_values": {},
            "error": "",
            "webhook_base": _get_webhook_base(request)[0],
        })
        return templates.TemplateResponse("capture_form.html", ctx)

    # ── POST /capture/new — Create Profile ──────────────────────────────────

    @app.post("/capture/new", response_class=HTMLResponse)
    async def capture_new_save(request: Request):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        form = await request.form()
        name = form.get("name", "").strip()
        plugin_type = form.get("plugin_type", "paperless_ngx")
        secret_key = form.get("secret_key", "").strip()
        connector_token = form.get("connector_token", "").strip()
        require_signature = form.get("require_signature") == "1"
        metadata_format = form.get("metadata_format", "flat").strip()
        index_fields_json = form.get("index_fields_json", "[]").strip() or "[]"

        plugin_instance = create_plugin_instance(plugin_type)
        if not plugin_instance:
            return RedirectResponse("/capture", status_code=303)

        # Build config from form fields
        config = {}
        for field in plugin_instance.config_schema():
            val = form.get(f"cfg_{field['key']}", "").strip()
            config[field["key"]] = val

        # Validate
        if not name:
            ctx = t_ctx(request)
            ctx.update({
                "request": request, "user": user, "tenant": tenant,
                "mode": "create", "profile": None,
                "plugin": {
                    "id": plugin_instance.plugin_id,
                    "name": plugin_instance.plugin_name,
                    "icon": plugin_instance.plugin_icon,
                    "color": plugin_instance.plugin_color,
                },
                "config_fields": plugin_instance.config_schema(),
                "config_values": config,
                "error": "Name is required",
                "webhook_base": _get_webhook_base(request)[0],
            })
            return templates.TemplateResponse("capture_form.html", ctx)

        from db import create_capture_profile
        import asyncio
        config_json = json.dumps(config)
        profile = await asyncio.to_thread(
            create_capture_profile,
            tenant_id=tenant["id"],
            name=name,
            plugin_type=plugin_type,
            secret_key=secret_key,
            connector_token=connector_token,
            config_json=config_json,
            require_signature=require_signature,
            metadata_format=metadata_format,
            index_fields_json=index_fields_json,
        )

        if profile:
            from db import add_tenant_log
            await asyncio.to_thread(
                add_tenant_log, tenant["id"], "INFO", "CAPTURE",
                f"Profile created: {name} ({plugin_type})"
            )

        return RedirectResponse("/capture", status_code=303)

    # ── GET /capture/{id}/edit — Edit Profile Form ──────────────────────────

    @app.get("/capture/{profile_id}/edit", response_class=HTMLResponse)
    async def capture_edit_form(request: Request, profile_id: str):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        from db import get_capture_profile
        import asyncio
        profile = await asyncio.to_thread(get_capture_profile, profile_id)
        if not profile or profile["tenant_id"] != tenant["id"]:
            return RedirectResponse("/capture", status_code=303)

        plugin_instance = create_plugin_instance(profile["plugin_type"], profile.get("config_json", "{}"))
        if not plugin_instance:
            return RedirectResponse("/capture", status_code=303)

        try:
            config_values = json.loads(profile.get("config_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            config_values = {}

        ctx = t_ctx(request)
        ctx.update({
            "request": request, "user": user, "tenant": tenant,
            "mode": "edit", "profile": profile,
            "plugin": {
                "id": plugin_instance.plugin_id,
                "name": plugin_instance.plugin_name,
                "icon": plugin_instance.plugin_icon,
                "color": plugin_instance.plugin_color,
            },
            "config_fields": plugin_instance.config_schema(),
            "config_values": config_values,
            "error": "",
            "webhook_base": _get_webhook_base(request)[0],
        })
        return templates.TemplateResponse("capture_form.html", ctx)

    # ── POST /capture/{id}/edit — Update Profile ────────────────────────────

    @app.post("/capture/{profile_id}/edit", response_class=HTMLResponse)
    async def capture_edit_save(request: Request, profile_id: str):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        from db import get_capture_profile, update_capture_profile
        import asyncio
        profile = await asyncio.to_thread(get_capture_profile, profile_id)
        if not profile or profile["tenant_id"] != tenant["id"]:
            return RedirectResponse("/capture", status_code=303)

        form = await request.form()
        name = form.get("name", "").strip()
        secret_key = form.get("secret_key", "").strip()
        connector_token = form.get("connector_token", "").strip()
        require_signature = form.get("require_signature") == "1"
        metadata_format = form.get("metadata_format", "flat").strip()
        index_fields_json = form.get("index_fields_json", "[]").strip() or "[]"

        plugin_instance = create_plugin_instance(profile["plugin_type"])
        if not plugin_instance:
            return RedirectResponse("/capture", status_code=303)

        config = {}
        for field in plugin_instance.config_schema():
            val = form.get(f"cfg_{field['key']}", "").strip()
            config[field["key"]] = val

        config_json = json.dumps(config)

        await asyncio.to_thread(
            update_capture_profile,
            profile_id=profile_id,
            name=name or None,
            secret_key=secret_key if secret_key else None,
            connector_token=connector_token if connector_token else None,
            config_json=config_json,
            require_signature=require_signature,
            metadata_format=metadata_format,
            index_fields_json=index_fields_json,
        )

        from db import add_tenant_log
        await asyncio.to_thread(
            add_tenant_log, tenant["id"], "INFO", "CAPTURE",
            f"Profile updated: {name or profile['name']}"
        )

        return RedirectResponse("/capture", status_code=303)

    # ── POST /capture/{id}/delete — Delete Profile ──────────────────────────

    @app.post("/capture/{profile_id}/delete")
    async def capture_delete(request: Request, profile_id: str):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        from db import get_capture_profile, delete_capture_profile, add_tenant_log
        import asyncio
        profile = await asyncio.to_thread(get_capture_profile, profile_id)
        if not profile or profile["tenant_id"] != tenant["id"]:
            return RedirectResponse("/capture", status_code=303)

        await asyncio.to_thread(delete_capture_profile, profile_id)
        await asyncio.to_thread(
            add_tenant_log, tenant["id"], "WARNING", "CAPTURE",
            f"Profile deleted: {profile['name']}"
        )

        return RedirectResponse("/capture", status_code=303)

    # ── POST /capture/{id}/toggle — Activate/Deactivate ────────────────────

    @app.post("/capture/{profile_id}/toggle")
    async def capture_toggle(request: Request, profile_id: str):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=303)

        tenant = _get_tenant(user)
        if not tenant:
            return RedirectResponse("/settings", status_code=303)

        from db import get_capture_profile, update_capture_profile
        import asyncio
        profile = await asyncio.to_thread(get_capture_profile, profile_id)
        if not profile or profile["tenant_id"] != tenant["id"]:
            return RedirectResponse("/capture", status_code=303)

        new_state = not profile["is_active"]
        await asyncio.to_thread(update_capture_profile, profile_id, is_active=new_state)

        return RedirectResponse("/capture", status_code=303)

    # ── POST /capture/{id}/test — Connection Test ───────────────────────────

    @app.post("/capture/{profile_id}/test")
    async def capture_test(request: Request, profile_id: str):
        user = require_login(request)
        if not user:
            return JSONResponse({"ok": False, "message": "Not logged in"}, status_code=401)

        tenant = _get_tenant(user)
        if not tenant:
            return JSONResponse({"ok": False, "message": "No tenant"}, status_code=400)

        from db import get_capture_profile, add_tenant_log
        import asyncio
        profile = await asyncio.to_thread(get_capture_profile, profile_id)
        if not profile or profile["tenant_id"] != tenant["id"]:
            return JSONResponse({"ok": False, "message": "Profile not found"}, status_code=404)

        plugin = create_plugin_instance(profile["plugin_type"], profile.get("config_json", "{}"))
        if not plugin:
            return JSONResponse({"ok": False, "message": "Unknown plugin type"})

        try:
            ok, msg = await plugin.test_connection()
        except Exception as e:
            logger.exception("Capture test_connection error: %s", e)
            ok, msg = False, f"Server error: {e}"

        # Log result
        await asyncio.to_thread(
            add_tenant_log, tenant["id"],
            "INFO" if ok else "WARNING", "CAPTURE",
            f"Connection test for '{profile['name']}': {'OK' if ok else 'FAILED'} — {msg}"
        )

        return JSONResponse({"ok": ok, "message": msg})

    # ── POST /capture/webhook/{profile_id} — Printix Capture Webhook ───────

    @app.post("/capture/webhook/{profile_id}")
    async def capture_webhook_handler(request: Request, profile_id: str):
        """
        Empfängt Printix Capture Notifications — delegiert an shared handler.
        URL-Format: /capture/webhook/{profile_id}
        """
        from capture.webhook_handler import handle_webhook

        logger.info("▶ CAPTURE REQUEST [web]: %s /capture/webhook/%s", request.method, profile_id)

        body_bytes = await request.body()
        headers_dict = {k.lower(): v for k, v in request.headers.items()}

        status, data = await handle_webhook(
            profile_id=profile_id,
            method=request.method,
            headers=headers_dict,
            body_bytes=body_bytes,
            source="web",
        )
        return JSONResponse(data, status_code=status)

    # ── GET /capture/webhook/{profile_id} — Health Check ────────────────────

    @app.get("/capture/webhook/{profile_id}")
    async def capture_webhook_health(request: Request, profile_id: str):
        """Health-Check / Debug: GET an shared handler."""
        from capture.webhook_handler import handle_webhook

        logger.info("▶ CAPTURE REQUEST [web]: GET /capture/webhook/%s", profile_id)

        headers_dict = {k.lower(): v for k, v in request.headers.items()}
        status, data = await handle_webhook(
            profile_id=profile_id,
            method="GET",
            headers=headers_dict,
            body_bytes=b"",
            source="web",
        )
        return JSONResponse(data, status_code=status)

    # ── Debug Endpoint — für direkte Browser-Aufrufe ─────────────────────────

    @app.api_route("/capture/debug", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def capture_debug(request: Request):
        """Debug-Endpoint: delegiert an shared handler mit Debug-UUID."""
        from capture.webhook_handler import handle_webhook

        logger.info("▶ CAPTURE REQUEST [web]: %s /capture/debug", request.method)

        body_bytes = await request.body()
        headers_dict = {k.lower(): v for k, v in request.headers.items()}
        status, data = await handle_webhook(
            profile_id="00000000-0000-0000-0000-000000000000",
            method=request.method,
            headers=headers_dict,
            body_bytes=body_bytes,
            source="web",
        )
        return JSONResponse(data, status_code=status)

    @app.api_route("/capture/debug/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def capture_debug_subpath(request: Request, path: str):
        """Catch-all für Debug mit Sub-Pfad."""
        return await capture_debug(request)
