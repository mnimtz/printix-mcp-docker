"""
Printix MCP — Web-Verwaltungsoberfläche (FastAPI)
==================================================

Routen:
  GET  /                          → Redirect je nach Zustand
  GET  /register                  → Schritt 1: Account
  POST /register                  → Account speichern
  GET  /register/api              → Schritt 2: Printix API-Credentials
  POST /register/api              → API speichern
  GET  /register/optional         → Schritt 3: SQL + Mail (optional)
  POST /register/optional         → Optional speichern
  GET  /register/summary          → Schritt 4: Zusammenfassung
  POST /register/summary          → User + Tenant anlegen

  GET  /login                     → Login-Seite
  POST /login                     → Login prüfen → Redirect
  GET  /logout                    → Session löschen → Redirect /login
  GET  /pending                   → Warteseite für noch nicht genehmigte User

  GET  /dashboard                 → Benutzer-Dashboard
  GET  /settings                  → Credentials / API-Daten bearbeiten (Selbstverwaltung)
  POST /settings                  → Credentials speichern
  POST /settings/regenerate-oauth → OAuth-Secret neu generieren
  GET  /settings/password         → Passwort ändern
  POST /settings/password         → Passwort ändern speichern
  GET  /help                      → Verbindungsanleitung (personalisiert)

  GET  /admin                     → Admin-Dashboard
  GET  /admin/users               → Benutzerliste
  POST /admin/users/{id}/approve  → User genehmigen
  POST /admin/users/{id}/disable  → User deaktivieren / sperren
  POST /admin/users/{id}/delete   → User löschen (inkl. Tenant)
  GET  /admin/users/{id}/edit     → User bearbeiten
  POST /admin/users/{id}/edit     → User-Edit speichern
  GET  /admin/users/create        → Admin: neuen User anlegen
  POST /admin/users/create        → Admin: neuen User speichern
  GET  /admin/audit               → Audit-Log
  GET  /admin/settings            → Server-Einstellungen (Base URL etc.)
  POST /admin/settings            → Server-Einstellungen speichern

  GET  /lang/{code}               → Sprache wechseln (EFIGS)
"""

import os
import json
import logging
import secrets
import tempfile
from typing import Optional

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

logger = logging.getLogger("printix.web")

# Templates-Verzeichnis (relativ zu diesem File)
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


# v6.7.5: Helper für persistenten Printix-User-Sync.
# Wird nach Credentials-Save und auf Refresh-Button-Klick getriggert.
async def _trigger_printix_user_sync(tenant: dict) -> dict:
    """Pullt alle Printix-User dieses Tenants in `cached_printix_users`.

    Läuft in einem Thread (synchroner Printix-Client + DB-Writes), die
    asyncio-Wrapper ist nur Convenience für `asyncio.create_task`.
    """
    import asyncio as _asyncio
    from printix_client import PrintixClient
    from cloudprint.printix_cache_db import sync_users_for_tenant

    def _do_sync():
        client = PrintixClient(
            tenant_id=tenant["printix_tenant_id"],
            print_client_id=tenant.get("print_client_id", ""),
            print_client_secret=tenant.get("print_client_secret", ""),
            ws_client_id=tenant.get("ws_client_id", ""),
            ws_client_secret=tenant.get("ws_client_secret", ""),
            um_client_id=tenant.get("um_client_id", ""),
            um_client_secret=tenant.get("um_client_secret", ""),
            shared_client_id=tenant.get("shared_client_id", ""),
            shared_client_secret=tenant.get("shared_client_secret", ""),
        )
        return sync_users_for_tenant(
            tenant_id=tenant["id"],
            printix_tenant_id=tenant["printix_tenant_id"],
            client=client,
        )

    return await _asyncio.to_thread(_do_sync)


def create_app(session_secret: str) -> FastAPI:
    app = FastAPI(title="Printix Management Console", docs_url=None, redoc_url=None)

    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    def current_app_version() -> str:
        version_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "VERSION")
        try:
            with open(version_path, "r", encoding="utf-8") as fh:
                return fh.read().strip() or "?"
        except Exception:
            return "?"

    # ── Package Builder (singleton, lebt für die Laufzeit der App) ────────────
    from package_builder import PackageBuilderCore as _PBC
    _pkg_builder = _PBC()

    # ── i18n ──────────────────────────────────────────────────────────────────

    from i18n import (
        detect_language, make_translator,
        SUPPORTED_LANGUAGES, LANGUAGE_NAMES, DEFAULT_LANGUAGE,
    )

    def get_lang(request: Request) -> str:
        """Gibt den aktiven Sprachcode zurück (Session → Accept-Language → Default)."""
        lang = request.session.get("lang")
        if lang in SUPPORTED_LANGUAGES:
            return lang
        return detect_language(request.headers.get("accept-language"))

    def t_ctx(request: Request) -> dict:
        """Gibt den i18n-Kontext für Templates zurück."""
        lang = get_lang(request)
        ctx = {
            "_":             make_translator(lang),
            "lang":          lang,
            "lang_names":    LANGUAGE_NAMES,
            "supported_langs": SUPPORTED_LANGUAGES,
        }
        # v3.9.0 — Badge "offene Tickets" im Nav (nur für Admins relevant)
        try:
            from db import count_feature_requests_by_status
            counts = count_feature_requests_by_status()
            ctx["feedback_new_count"] = counts.get("new", 0)
        except Exception:
            ctx["feedback_new_count"] = 0
        return ctx

    # ── Helpers ────────────────────────────────────────────────────────────────

    def get_session_user(request: Request) -> Optional[dict]:
        user_id = request.session.get("user_id")
        if not user_id:
            return None
        try:
            from db import get_user_by_id
            return get_user_by_id(user_id)
        except Exception:
            return None

    def _generate_temp_password(length: int = 14) -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789!@$%?"
        return "".join(secrets.choice(alphabet) for _ in range(length))

    def require_login(request: Request) -> Optional[dict]:
        user = get_session_user(request)
        if not user:
            return None
        if user.get("status") != "approved":
            return None
        return user

    def _user_home_target(user: Optional[dict]) -> str:
        if not user:
            return "/login"
        if user.get("is_admin"):
            return "/admin"
        if user.get("role_type") == "employee":
            return "/my"
        if user.get("status") == "pending":
            return "/pending"
        return "/dashboard"

    @app.middleware("http")
    async def invitation_activation_guard(request: Request, call_next):
        allowed_paths = {
            "/login",
            "/logout",
            "/pending",
            "/account/activate",
        }
        path = request.url.path or "/"
        if not path.startswith("/auth/entra") and not path.startswith("/lang/") and path not in allowed_paths:
            session = request.scope.get("session") or {}
            user_id = session.get("user_id")
            if user_id:
                try:
                    from db import get_user_by_id
                    active_user = get_user_by_id(user_id)
                except Exception:
                    active_user = None
                if active_user and active_user.get("must_change_password"):
                    return RedirectResponse("/account/activate", status_code=302)
                if active_user and active_user.get("role_type") == "employee":
                    employee_allowed_prefixes = (
                        "/my",
                        "/logout",
                        "/account/activate",
                        "/lang/",
                        "/auth/entra",
                    )
                    employee_allowed_paths = {
                        "/",
                        "/login",
                        "/pending",
                    }
                    if path not in employee_allowed_paths and not any(path.startswith(prefix) for prefix in employee_allowed_prefixes):
                        return RedirectResponse("/my", status_code=302)
        return await call_next(request)

    app.add_middleware(SessionMiddleware, secret_key=session_secret, max_age=3600 * 8)

    def mcp_base_url() -> str:
        """Gibt die öffentliche MCP-Basis-URL zurück (DB-Setting > ENV > Fallback)."""
        try:
            from db import get_setting
            db_url = get_setting("public_url", "")
            if db_url:
                return db_url.rstrip("/")
        except Exception:
            pass
        env_url = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/")
        if env_url:
            return env_url
        return "http://<HA-IP>:8765"

    # ── Sprach-Route ──────────────────────────────────────────────────────────

    @app.get("/lang/{code}", response_class=RedirectResponse)
    async def switch_language(code: str, request: Request):
        if code in SUPPORTED_LANGUAGES:
            request.session["lang"] = code
        # Open-Redirect-Schutz: Referer-Header darf nur zurückführen, wenn er
        # same-origin ist. Andernfalls fallen wir auf "/" zurück.
        referer = request.headers.get("referer", "")
        safe_target = "/"
        if referer:
            try:
                from urllib.parse import urlparse
                ref = urlparse(referer)
                if not ref.netloc or ref.netloc == request.url.netloc:
                    # Relative Pfade oder gleiche Origin akzeptieren
                    safe_target = referer
            except Exception:
                safe_target = "/"
        return RedirectResponse(safe_target, status_code=302)

    # ── Root ──────────────────────────────────────────────────────────────────

    @app.get("/", response_class=RedirectResponse)
    async def root(request: Request):
        try:
            from db import has_users
            if not has_users():
                return RedirectResponse("/register", status_code=302)
        except Exception:
            return RedirectResponse("/register", status_code=302)
        user = get_session_user(request)
        if user:
            return RedirectResponse(_user_home_target(user), status_code=302)
        return RedirectResponse("/login", status_code=302)

    # ── Registrierung ─────────────────────────────────────────────────────────

    @app.get("/register", response_class=HTMLResponse)
    async def register_step1_get(request: Request):
        return templates.TemplateResponse("register_step1.html", {
            "request": request, "step": 1, "error": None, **t_ctx(request)
        })

    @app.post("/register", response_class=HTMLResponse)
    async def register_step1_post(
        request: Request,
        username:  str = Form(...),
        password:  str = Form(...),
        password2: str = Form(...),
        email:     str = Form(default=""),
        full_name: str = Form(default=""),
        company:   str = Form(default=""),
    ):
        tc = t_ctx(request)
        _  = tc["_"]
        error = None
        if len(username) < 3:
            error = "Benutzername muss mindestens 3 Zeichen lang sein."
        elif len(password) < 8:
            error = "Passwort muss mindestens 8 Zeichen lang sein."
        elif password != password2:
            error = _("reg_pw_mismatch")
        else:
            try:
                from db import username_exists
                if username_exists(username):
                    error = _("reg_user_exists")
            except Exception as e:
                error = f"Datenbankfehler: {e}"

        if error:
            return templates.TemplateResponse("register_step1.html", {
                "request": request, "step": 1, "error": error,
                "username": username, "email": email,
                "full_name": full_name, "company": company, **tc,
            })

        request.session["reg_username"]  = username
        request.session["reg_password"]  = password
        request.session["reg_email"]     = email
        request.session["reg_full_name"] = full_name
        request.session["reg_company"]   = company
        return RedirectResponse("/register/api", status_code=302)

    @app.get("/register/api", response_class=HTMLResponse)
    async def register_step2_get(request: Request):
        if "reg_username" not in request.session:
            return RedirectResponse("/register", status_code=302)
        return templates.TemplateResponse("register_step2.html", {
            "request": request, "step": 2, "error": None, **t_ctx(request)
        })

    @app.post("/register/api", response_class=HTMLResponse)
    async def register_step2_post(
        request: Request,
        printix_tenant_id:     str = Form(...),
        print_client_id:       str = Form(default=""),
        print_client_secret:   str = Form(default=""),
        card_client_id:        str = Form(default=""),
        card_client_secret:    str = Form(default=""),
        ws_client_id:          str = Form(default=""),
        ws_client_secret:      str = Form(default=""),
        um_client_id:          str = Form(default=""),
        um_client_secret:      str = Form(default=""),
        shared_client_id:      str = Form(default=""),
        shared_client_secret:  str = Form(default=""),
        tenant_name:           str = Form(default=""),
    ):
        if "reg_username" not in request.session:
            return RedirectResponse("/register", status_code=302)
        tc = t_ctx(request)

        if not printix_tenant_id.strip():
            return templates.TemplateResponse("register_step2.html", {
                "request": request, "step": 2,
                "error": "Printix Tenant-ID ist Pflichtfeld.", **tc,
            })

        has_creds = any([
            print_client_id and print_client_secret,
            card_client_id and card_client_secret,
            ws_client_id and ws_client_secret,
            um_client_id and um_client_secret,
            shared_client_id and shared_client_secret,
        ])
        if not has_creds:
            return templates.TemplateResponse("register_step2.html", {
                "request": request, "step": 2,
                "error": "Mindestens ein vollständiges API-Credentials-Paar wird benötigt.",
                "printix_tenant_id": printix_tenant_id, "tenant_name": tenant_name, **tc,
            })

        request.session["reg_tenant_id"]           = printix_tenant_id.strip()
        request.session["reg_tenant_name"]          = tenant_name.strip() or printix_tenant_id.strip()
        request.session["reg_print_client_id"]      = print_client_id.strip()
        request.session["reg_print_client_secret"]  = print_client_secret.strip()
        request.session["reg_card_client_id"]       = card_client_id.strip()
        request.session["reg_card_client_secret"]   = card_client_secret.strip()
        request.session["reg_ws_client_id"]         = ws_client_id.strip()
        request.session["reg_ws_client_secret"]     = ws_client_secret.strip()
        request.session["reg_um_client_id"]         = um_client_id.strip()
        request.session["reg_um_client_secret"]     = um_client_secret.strip()
        request.session["reg_shared_client_id"]     = shared_client_id.strip()
        request.session["reg_shared_client_secret"] = shared_client_secret.strip()
        return RedirectResponse("/register/optional", status_code=302)

    @app.get("/register/optional", response_class=HTMLResponse)
    async def register_step3_get(request: Request):
        if "reg_tenant_id" not in request.session:
            return RedirectResponse("/register", status_code=302)
        return templates.TemplateResponse("register_step3.html", {
            "request": request, "step": 3, "error": None, **t_ctx(request)
        })

    @app.post("/register/optional", response_class=HTMLResponse)
    async def register_step3_post(
        request: Request,
        sql_server:   str = Form(default=""),
        sql_database: str = Form(default="printix_bi_data_2_1"),
        sql_username: str = Form(default=""),
        sql_password: str = Form(default=""),
        mail_api_key: str = Form(default=""),
        mail_from:    str = Form(default=""),
    ):
        if "reg_tenant_id" not in request.session:
            return RedirectResponse("/register", status_code=302)

        request.session["reg_sql_server"]   = sql_server.strip()
        request.session["reg_sql_database"] = sql_database.strip()
        request.session["reg_sql_username"] = sql_username.strip()
        request.session["reg_sql_password"] = sql_password.strip()
        request.session["reg_mail_api_key"] = mail_api_key.strip()
        request.session["reg_mail_from"]    = mail_from.strip()
        return RedirectResponse("/register/summary", status_code=302)

    @app.get("/register/summary", response_class=HTMLResponse)
    async def register_step4_get(request: Request):
        if "reg_tenant_id" not in request.session:
            return RedirectResponse("/register", status_code=302)

        base = mcp_base_url()
        return templates.TemplateResponse("register_step4.html", {
            "request": request, "step": 4,
            "username":       request.session.get("reg_username", ""),
            "email":          request.session.get("reg_email", ""),
            "tenant_id":      request.session.get("reg_tenant_id", ""),
            "tenant_name":    request.session.get("reg_tenant_name", ""),
            "sql_configured": bool(request.session.get("reg_sql_server")),
            "mail_configured":bool(request.session.get("reg_mail_api_key")),
            "base_url": base,
            "error": None, **t_ctx(request),
        })

    @app.post("/register/summary", response_class=HTMLResponse)
    async def register_step4_post(request: Request):
        if "reg_tenant_id" not in request.session:
            return RedirectResponse("/register", status_code=302)

        base = mcp_base_url()
        tc   = t_ctx(request)

        try:
            from db import create_user, create_tenant, has_users, audit

            is_first = not has_users()

            user = create_user(
                username=request.session["reg_username"],
                password=request.session["reg_password"],
                email=request.session.get("reg_email", ""),
                is_first=is_first,
                full_name=request.session.get("reg_full_name", ""),
                company=request.session.get("reg_company", ""),
            )

            tenant = create_tenant(
                user_id=user["id"],
                printix_tenant_id=request.session["reg_tenant_id"],
                name=request.session.get("reg_tenant_name", ""),
                print_client_id=request.session.get("reg_print_client_id", ""),
                print_client_secret=request.session.get("reg_print_client_secret", ""),
                card_client_id=request.session.get("reg_card_client_id", ""),
                card_client_secret=request.session.get("reg_card_client_secret", ""),
                ws_client_id=request.session.get("reg_ws_client_id", ""),
                ws_client_secret=request.session.get("reg_ws_client_secret", ""),
                um_client_id=request.session.get("reg_um_client_id", ""),
                um_client_secret=request.session.get("reg_um_client_secret", ""),
                shared_client_id=request.session.get("reg_shared_client_id", ""),
                shared_client_secret=request.session.get("reg_shared_client_secret", ""),
                sql_server=request.session.get("reg_sql_server", ""),
                sql_database=request.session.get("reg_sql_database", ""),
                sql_username=request.session.get("reg_sql_username", ""),
                sql_password=request.session.get("reg_sql_password", ""),
                mail_api_key=request.session.get("reg_mail_api_key", ""),
                mail_from=request.session.get("reg_mail_from", ""),
            )

            audit(user["id"], "register", f"Tenant '{tenant['name']}' registriert")

            for key in list(request.session.keys()):
                if key.startswith("reg_"):
                    del request.session[key]

        except Exception as e:
            logger.error("Registrierung fehlgeschlagen: %s", e)
            return templates.TemplateResponse("register_step4.html", {
                "request": request, "step": 4, "error": str(e),
                "username":    request.session.get("reg_username", ""),
                "tenant_id":   request.session.get("reg_tenant_id", ""),
                "tenant_name": request.session.get("reg_tenant_name", ""),
                "base_url": base, "sql_configured": False, "mail_configured": False, **tc,
            })

        return templates.TemplateResponse("register_success.html", {
            "request": request,
            "username":           user["username"],
            "is_admin":           user.get("is_admin", False),
            "bearer_token":       tenant["bearer_token"],
            "oauth_client_id":    tenant["oauth_client_id"],
            "oauth_client_secret":tenant["oauth_client_secret"],
            "base_url":           base,
            "mcp_url":            f"{base}/mcp",
            "sse_url":            f"{base}/sse",
            "oauth_authorize_url":f"{base}/oauth/authorize",
            "oauth_token_url":    f"{base}/oauth/token",
            **tc,
        })

    # ── Login ──────────────────────────────────────────────────────────────────

    def _entra_login_enabled() -> bool:
        """Prüft ob Entra-Login für die Login-Seite angezeigt werden soll."""
        try:
            from entra import is_enabled
            return is_enabled()
        except Exception:
            return False

    @app.get("/login", response_class=HTMLResponse)
    async def login_get(request: Request):
        if get_session_user(request):
            return RedirectResponse("/", status_code=302)
        return templates.TemplateResponse("login.html", {
            "request": request, "error": None,
            "entra_enabled": _entra_login_enabled(),
            **t_ctx(request),
        })

    @app.post("/login", response_class=HTMLResponse)
    async def login_post(
        request: Request,
        username: str = Form(...),
        password: str = Form(...),
    ):
        tc = t_ctx(request)
        _  = tc["_"]
        entra_on = _entra_login_enabled()
        try:
            from db import authenticate_user, audit
            user = authenticate_user(username, password)
        except Exception as e:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": f"Datenbankfehler: {e}",
                "username": username, "entra_enabled": entra_on, **tc,
            })

        if not user:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": _("login_error"),
                "username": username, "entra_enabled": entra_on, **tc,
            })

        status = user.get("status", "")
        if status == "disabled" or status == "suspended":
            return templates.TemplateResponse("login.html", {
                "request": request, "error": _("login_suspended"),
                "username": username, "entra_enabled": entra_on, **tc,
            })

        request.session["user_id"] = user["id"]
        try:
            audit(user["id"], "login", "Eingeloggt")
        except Exception:
            pass

        # v6.2.0: Background-Prefetch — Tenant-Daten werden parallel
        # geladen, damit die ersten Seiten nach dem Login sofort da sind.
        # Fehlschläge werden nur geloggt, beeinflussen den Login nicht.
        try:
            from db import get_tenant_full_by_user_id as _gt
            t = _gt(user["id"])
            if t:
                from cache import schedule_prefetch
                schedule_prefetch(t, lambda tt=t: _make_printix_client(tt))
        except Exception as _pe:
            logger.debug("Login-Prefetch skip: %s", _pe)

        if user.get("must_change_password"):
            return RedirectResponse("/account/activate", status_code=302)
        return RedirectResponse(_user_home_target(user), status_code=302)

    @app.get("/logout", response_class=RedirectResponse)
    async def logout(request: Request):
        lang = request.session.get("lang")
        # v6.1.0: Beim Logout den Tenant-Cache dieses Users invalidieren,
        # damit beim nächsten Login frische Daten geladen werden.
        try:
            user_id = request.session.get("user_id")
            if user_id:
                from db import get_tenant_by_user_id as _gt
                t = _gt(user_id)
                if t:
                    import sys as _s, os as _o
                    _s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
                    from cache import tenant_cache as _tc
                    _tc.clear_tenant(t.get("id", ""))
        except Exception:
            pass
        request.session.clear()
        if lang:
            request.session["lang"] = lang
        return RedirectResponse("/login", status_code=302)

    # ── Entra ID (Azure AD) SSO ────────────────────────────────────────────────

    @app.get("/auth/entra/login")
    async def entra_login(request: Request):
        """Leitet den Benutzer zur Microsoft-Anmeldeseite weiter."""
        try:
            from entra import is_enabled, build_authorize_url, generate_state
        except ImportError:
            return RedirectResponse("/login", status_code=302)

        if not is_enabled():
            return RedirectResponse("/login", status_code=302)

        state = generate_state()
        request.session["entra_state"] = state
        # Gespeicherte Redirect URI verwenden (konsistent mit App-Registrierung)
        try:
            from db import get_setting
            saved_uri = get_setting("entra_redirect_uri", "")
        except Exception:
            saved_uri = ""
        if not saved_uri:
            base = _get_base_url(request)
            saved_uri = f"{base}/auth/entra/callback"
        redirect_uri = saved_uri
        url = build_authorize_url(redirect_uri, state)
        return RedirectResponse(url, status_code=302)

    @app.get("/auth/entra/callback")
    async def entra_callback(request: Request):
        """Callback von Microsoft nach erfolgreicher Anmeldung."""
        tc = t_ctx(request)
        _ = tc["_"]
        _e = {"entra_enabled": True}  # Entra ist aktiv (wir sind im Callback)

        code = request.query_params.get("code", "")
        state = request.query_params.get("state", "")
        error = request.query_params.get("error", "")
        error_desc = request.query_params.get("error_description", "")

        if error:
            logger.warning("Entra callback error: %s — %s", error, error_desc)
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": f"Microsoft-Anmeldung fehlgeschlagen: {error_desc or error}",
                **_e, **tc,
            })

        # CSRF-State prüfen
        expected_state = request.session.pop("entra_state", "")
        if not state or state != expected_state:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Ungültiger State-Parameter — bitte erneut versuchen.",
                **_e, **tc,
            })

        if not code:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Kein Authorization Code erhalten.",
                **_e, **tc,
            })

        # Code gegen Token tauschen
        try:
            from entra import exchange_code_for_user
        except ImportError:
            return templates.TemplateResponse("login.html", {
                "request": request, "error": "Entra-Modul nicht verfügbar.",
                **_e, **tc,
            })

        # Gespeicherte Redirect URI verwenden (muss mit Login-Request übereinstimmen)
        try:
            from db import get_setting as _gs
            saved_uri = _gs("entra_redirect_uri", "")
        except Exception:
            saved_uri = ""
        if not saved_uri:
            base = _get_base_url(request)
            saved_uri = f"{base}/auth/entra/callback"
        redirect_uri = saved_uri
        user_info = exchange_code_for_user(code, redirect_uri)

        if not user_info or not user_info.get("oid"):
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Microsoft-Anmeldung fehlgeschlagen — kein Benutzerprofil erhalten.",
                **_e, **tc,
            })

        # User finden oder erstellen
        try:
            from db import get_or_create_entra_user, audit
            user = get_or_create_entra_user(
                entra_oid=user_info["oid"],
                email=user_info.get("email", ""),
                display_name=user_info.get("name", ""),
            )
        except Exception as e:
            logger.error("Entra user lookup/create Fehler: %s", e)
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": f"Datenbankfehler: {e}",
                **_e, **tc,
            })

        if not user:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Benutzer konnte nicht angelegt werden.",
                **_e, **tc,
            })

        # Status prüfen
        status = user.get("status", "")
        if status in ("disabled", "suspended"):
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": _("login_suspended"),
                **_e, **tc,
            })

        # Session setzen
        request.session["user_id"] = user["id"]
        try:
            audit(user["id"], "login", f"Entra-Login ({user_info.get('email', '')})")
        except Exception:
            pass

        # v6.2.0: Background-Prefetch auch beim Entra-SSO-Login
        try:
            from db import get_tenant_full_by_user_id as _gt
            t = _gt(user["id"])
            if t:
                from cache import schedule_prefetch
                schedule_prefetch(t, lambda tt=t: _make_printix_client(tt))
        except Exception as _pe:
            logger.debug("Entra-Login-Prefetch skip: %s", _pe)

        if user.get("must_change_password"):
            return RedirectResponse("/account/activate", status_code=302)
        return RedirectResponse(_user_home_target(user), status_code=302)

    @app.get("/account/activate", response_class=HTMLResponse)
    async def account_activate_get(request: Request):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not user.get("must_change_password"):
            return RedirectResponse(_user_home_target(user), status_code=302)
        return templates.TemplateResponse("account_activate.html", {
            "request": request,
            "user": user,
            "saved": False,
            "error": None,
            **t_ctx(request),
        })

    @app.post("/account/activate", response_class=HTMLResponse)
    async def account_activate_post(
        request: Request,
        new_password: str = Form(...),
        new_password2: str = Form(...),
    ):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not user.get("must_change_password"):
            return RedirectResponse(_user_home_target(user), status_code=302)
        tc = t_ctx(request)
        _ = tc["_"]
        error = None
        if new_password != new_password2:
            error = _("reg_pw_mismatch")
        elif len(new_password) < 8:
            error = _("invite_pw_length_error")
        else:
            try:
                from db import complete_invitation_password_change, audit
                complete_invitation_password_change(user["id"], new_password)
                audit(user["id"], "accept_invitation", "Einladung angenommen und Passwort gesetzt", object_type="user", object_id=user["id"])
            except Exception as e:
                error = str(e)
        if error:
            return templates.TemplateResponse("account_activate.html", {
                "request": request,
                "user": user,
                "saved": False,
                "error": error,
                **tc,
            })
        refreshed = get_session_user(request)
        target = _user_home_target(refreshed or user)
        return templates.TemplateResponse("account_activate.html", {
            "request": request,
            "user": refreshed or user,
            "saved": True,
            "error": None,
            "redirect_target": target,
            **tc,
        })

    # ── Entra Auto-Setup (Ein-Klick via Bootstrap-App) ─────────────────────
    #
    # ─── Device Code Flow: Admin klickt Button → Code anzeigen → automatische
    # App-Registration via Graph API. Keine Bootstrap-App nötig.

    @app.post("/admin/entra/device-code")
    async def entra_device_code_start(request: Request):
        """Startet den Device Code Flow fuer Entra Auto-Setup."""
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            from entra import start_device_code_flow
        except ImportError:
            return JSONResponse({"error": "entra module not available"}, status_code=500)

        result = start_device_code_flow()
        if not result or not result.get("device_code"):
            return JSONResponse({"error": "device_code_failed"}, status_code=502)

        # Device code in Session speichern (fuer Polling)
        request.session["entra_device_code"] = result["device_code"]
        request.session["entra_device_interval"] = result.get("interval", 5)

        return JSONResponse({
            "user_code":        result["user_code"],
            "verification_uri": result["verification_uri"],
            "expires_in":       result["expires_in"],
            "interval":         result.get("interval", 5),
            "message":          result.get("message", ""),
        })

    @app.get("/admin/entra/device-poll")
    async def entra_device_code_poll(request: Request):
        """Pollt den Token-Status des laufenden Device Code Flows."""
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        device_code = request.session.get("entra_device_code", "")
        if not device_code:
            return JSONResponse({"status": "error", "error": "no_device_code"})

        try:
            from entra import poll_device_code_token, auto_register_app
        except ImportError:
            return JSONResponse({"status": "error", "error": "entra module not available"})

        poll_result = poll_device_code_token(device_code)

        if poll_result["status"] == "pending":
            return JSONResponse({"status": "pending"})

        if poll_result["status"] == "expired":
            request.session.pop("entra_device_code", None)
            return JSONResponse({"status": "expired"})

        if poll_result["status"] == "error":
            request.session.pop("entra_device_code", None)
            return JSONResponse({"status": "error", "error": poll_result.get("error", "")})

        # status == "success" — Token erhalten, App erstellen
        access_token = poll_result["access_token"]
        request.session.pop("entra_device_code", None)

        base = _get_base_url(request)
        sso_redirect_uri = f"{base}/auth/entra/callback"
        result = auto_register_app(access_token, sso_redirect_uri)

        if not result or not result.get("client_id"):
            return JSONResponse({
                "status": "error",
                "error": "app_creation_failed",
            })

        # Credentials in Settings speichern
        try:
            from db import set_setting, _enc, audit
            set_setting("entra_enabled", "1")
            set_setting("entra_client_id", result["client_id"])
            if result.get("client_secret"):
                set_setting("entra_client_secret", _enc(result["client_secret"]))
            if result.get("tenant_id"):
                set_setting("entra_tenant_id", result["tenant_id"])
            set_setting("entra_auto_approve", "0")

            set_setting("entra_redirect_uri", sso_redirect_uri)

            audit(user["id"], "entra_auto_setup",
                  f"SSO-App via Device Code Flow erstellt (client_id={result['client_id']}, redirect_uri={sso_redirect_uri})")
            logger.info("Entra Auto-Setup erfolgreich: client_id=%s, redirect_uri=%s",
                        result["client_id"], sso_redirect_uri)
        except Exception as e:
            logger.error("Entra Auto-Setup DB-Fehler: %s", e)
            return JSONResponse({
                "status": "error",
                "error": f"App erstellt, aber Speichern fehlgeschlagen: {e}",
            })

        return JSONResponse({
            "status": "success",
            "client_id": result["client_id"],
            "tenant_id": result.get("tenant_id", ""),
        })

    def _get_base_url(request: Request) -> str:
        """Ermittelt die Base-URL der Web-UI aus dem eingehenden Request.

        Wichtig: Verwendet NICHT public_url (das ist fuer den MCP-Server).
        Stattdessen wird die URL aus dem Request abgeleitet (Host-Header,
        x-forwarded-* bei Reverse-Proxy).
        """
        scheme = request.headers.get("x-forwarded-proto", request.url.scheme)
        host = (
            request.headers.get("x-forwarded-host")
            or request.headers.get("host", "")
        )
        if host:
            # Host-Header enthaelt bereits den externen Port (z.B. "192.168.1.100:8010")
            return f"{scheme}://{host}".rstrip("/")
        # Fallback: aus request.url
        hostname = request.url.hostname
        port = request.url.port
        if port and port not in (80, 443):
            return f"{scheme}://{hostname}:{port}"
        return f"{scheme}://{hostname}"

    # ── Warteseite ────────────────────────────────────────────────────────────

    @app.get("/pending", response_class=HTMLResponse)
    async def pending(request: Request):
        user = get_session_user(request)
        return templates.TemplateResponse("pending.html", {
            "request": request, "user": user, **t_ctx(request)
        })

    # ── Dashboard ─────────────────────────────────────────────────────────────

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        """Landing page — rendert SOFORT ohne teure API/SQL-Calls.

        v5.20.0: Alle zahlenbasierten Panels (KPIs, Umwelt, Drucker-Zählung,
        Sparkline, Forecast) werden via JavaScript nachträglich über
        `/dashboard/data` geladen. Damit bleibt das initiale Rendering auch
        bei schlafender Azure-SQL-DB oder langsamer Printix-API unter 100 ms.
        """
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if user.get("status") == "pending":
            return RedirectResponse("/pending", status_code=302)

        base   = mcp_base_url()
        tenant = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
        except Exception:
            pass

        has_api = bool(tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")))
        has_sql = bool(tenant and tenant.get("sql_server"))

        return templates.TemplateResponse("dashboard.html", {
            "request": request, "user": user, "tenant": tenant,
            "base_url": base,
            "mcp_url":  f"{base}/mcp",
            "sse_url":  f"{base}/sse",
            "app_version": current_app_version(),
            "has_api": has_api,
            "has_sql": has_sql,
            # Initial leer — wird per /dashboard/data nachgeladen
            "kpis": {},
            "env_summary": {},
            "sparkline_data": [],
            "forecast": {},
            **t_ctx(request),
        })

    # v6.7.117: In-Memory-Cache fuer /dashboard/data.
    # Azure SQL pausiert bei Nicht-Nutzung → erster Connect dauert 30-60s.
    # Mit 60s-TTL-Cache ist nur der erste Aufruf langsam, alle weiteren instant.
    _dashboard_cache: dict = {}  # user_id -> {"ts": epoch, "data": dict}
    _DASHBOARD_CACHE_TTL = 60.0  # Sekunden
    _dashboard_locks: dict = {}  # user_id -> asyncio.Lock (verhindert Thundering Herd)

    @app.get("/dashboard/data")
    async def dashboard_data(request: Request):
        """JSON-Endpunkt für Lazy-Loading + Auto-Refresh des Dashboards.

        Liefert Printix-Drucker-Zählung, SQL-KPIs, Umwelt-Summary, Sparkline
        und Forecast in einem Aufruf. Printix-API und SQL laufen parallel
        via asyncio.gather — damit summiert sich die Latenz nicht.

        v6.7.117: 60s-Cache + per-User-Lock gegen Azure-SQL-Cold-Start.
        """
        import asyncio as _aio
        import time as _time
        user = get_session_user(request)
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        _uid = str(user["id"])
        _now = _time.time()

        # Cache-Hit: < 60s alt → sofort zurueck
        _cached = _dashboard_cache.get(_uid)
        if _cached and (_now - _cached["ts"]) < _DASHBOARD_CACHE_TTL:
            payload = dict(_cached["data"])
            payload["_cache_age_s"] = round(_now - _cached["ts"], 1)
            return JSONResponse(payload)

        # Lock pro User: wenn parallel 5 Requests reinkommen, rechnet nur einer,
        # die anderen warten auf das Ergebnis.
        lock = _dashboard_locks.setdefault(_uid, _aio.Lock())
        async with lock:
            # Re-check nach Lock-Erwerb — evtl. hat ein paralleler Request
            # inzwischen den Cache gefuellt.
            _cached = _dashboard_cache.get(_uid)
            if _cached and (_time.time() - _cached["ts"]) < _DASHBOARD_CACHE_TTL:
                payload = dict(_cached["data"])
                payload["_cache_age_s"] = round(_time.time() - _cached["ts"], 1)
                return JSONResponse(payload)
            return await _compute_dashboard_data(request, user, _uid, _now)

    async def _compute_dashboard_data(request, user, _uid: str, _started_at: float):
        """Eigentliche Berechnung — nur hinter dem Lock aufrufen."""
        import asyncio as _aio
        import time as _time
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
        except Exception:
            tenant = None

        has_api = bool(tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")))
        has_sql = bool(tenant and tenant.get("sql_server"))

        result = {"has_api": has_api, "has_sql": has_sql}

        # ── Printer-Count via Printix API ─────────────────────────────────
        async def _load_printers():
            if not has_api or not tenant:
                return {"total_printers": 0, "active_printers": 0}
            try:
                import re as _re
                client = _make_printix_client(tenant)
                raw_data = await _aio.to_thread(lambda: client.list_printers(size=200))
                if isinstance(raw_data, dict):
                    raw_list = raw_data.get("printers", raw_data.get("content", []))
                elif isinstance(raw_data, list):
                    raw_list = raw_data
                else:
                    raw_list = []
                seen = set()
                active = 0
                for p in raw_list:
                    href = (p.get("_links") or {}).get("self", {}).get("href", "")
                    m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                    pid = m.group(1) if m else p.get("id", str(id(p)))
                    if pid not in seen:
                        seen.add(pid)
                        cs = (p.get("connectionStatus") or "").lower()
                        if cs in ("connected", "online"):
                            active += 1
                return {"total_printers": len(seen), "active_printers": active}
            except Exception as e:
                logger.warning("Dashboard-Printer-Count: %s", e)
                return {"total_printers": 0, "active_printers": 0,
                        "error_printers": str(e)[:200]}

        # ── SQL-KPIs + Umwelt + Sparkline + Forecast ─────────────────────
        async def _load_sql():
            if not has_sql:
                return {}
            try:
                from reporting.sql_client import set_config_from_tenant
                set_config_from_tenant(tenant)

                from datetime import date as _date, timedelta as _td
                today = _date.today()
                week_start  = today - _td(days=today.weekday())
                month_start = today.replace(day=1)
                spark_start = today - _td(days=6)

                def _load():
                    from reporting.query_tools import (
                        query_print_stats, query_tree_meter, query_forecast, _fmt_date,
                    )
                    day_data   = query_print_stats(str(today),       str(today), group_by="day")
                    week_data  = query_print_stats(str(week_start),  str(today), group_by="day")
                    month_data = query_print_stats(str(month_start), str(today), group_by="day")

                    def _s(rows, f):
                        return sum(int(r.get(f) or 0) for r in rows)

                    mp  = _s(month_data, "total_pages")
                    mpc = _s(month_data, "color_pages")
                    mpd = _s(month_data, "duplex_pages")

                    _kpis = {
                        "today_pages": _s(day_data, "total_pages"),
                        "today_jobs":  _s(day_data, "total_jobs"),
                        "week_pages":  _s(week_data, "total_pages"),
                        "week_jobs":   _s(week_data, "total_jobs"),
                        "month_pages": mp,
                        "month_jobs":  _s(month_data, "total_jobs"),
                        "color_ratio": round(mpc / mp * 100, 1) if mp else 0,
                        "duplex_rate": round(mpd / mp * 100, 1) if mp else 0,
                    }

                    # Sparkline 7 Tage
                    spark_data = query_print_stats(str(spark_start), str(today), group_by="day")
                    _spark = [0] * 7
                    for r in spark_data:
                        try:
                            d = _fmt_date(r["period"])
                            idx = (d - spark_start).days
                            if 0 <= idx < 7:
                                _spark[idx] = int(r.get("total_pages") or 0)
                        except Exception:
                            pass

                    # Umwelt
                    tree = query_tree_meter(str(month_start), str(today))
                    from reporting.report_engine import compute_env_impact
                    _env = compute_env_impact(
                        tree.get("total_pages", 0),
                        tree.get("duplex_pages", 0),
                        tree.get("saved_sheets_duplex", 0),
                    )

                    # Forecast
                    from dateutil.relativedelta import relativedelta
                    fc_start = (today - relativedelta(months=6)).replace(day=1)
                    try:
                        _fc = query_forecast(str(fc_start), str(today), group_by="month")
                    except Exception:
                        _fc = {}

                    return _kpis, _spark, _env, _fc

                sql_kpis, sparkline, env, forecast = await _aio.to_thread(_load)
                return {
                    "kpis": sql_kpis,
                    "env_summary": env,
                    "sparkline_data": sparkline,
                    "forecast": forecast,
                }
            except Exception as e:
                logger.warning("Dashboard-SQL: %s", e)
                return {"error_sql": str(e)[:200]}

        # v6.7.117: Hard timeout 75s — reicht fuer Azure-SQL-Cold-Start
        # (erste Connection ~30s, dann 4 Queries ~5-10s each = max ~70s).
        # Ergebnis wird 60s gecached, damit Folge-Requests instant sind.
        try:
            printers, sql_bundle = await _aio.wait_for(
                _aio.gather(_load_printers(), _load_sql()),
                timeout=75.0,
            )
        except _aio.TimeoutError:
            logger.warning("Dashboard-Data: timeout nach 75s — Teilergebnisse")
            # Fallback auf letzten Cache-Eintrag auch wenn expired
            stale = _dashboard_cache.get(_uid)
            if stale:
                payload = dict(stale["data"])
                payload["_cache_age_s"] = round(_time.time() - stale["ts"], 1)
                payload["_stale_after_timeout"] = True
                return JSONResponse(payload)
            printers = {"total_printers": 0, "active_printers": 0,
                        "error_printers": "timeout"}
            sql_bundle = {"error_sql": "timeout"}

        kpis = dict(sql_bundle.get("kpis") or {})
        kpis.update(printers)  # total_printers + active_printers

        result.update({
            "kpis": kpis,
            "env_summary":    sql_bundle.get("env_summary") or {},
            "sparkline_data": sql_bundle.get("sparkline_data") or [],
            "forecast":       sql_bundle.get("forecast") or {},
        })
        if "error_printers" in printers:
            result["error_printers"] = printers["error_printers"]
        if "error_sql" in sql_bundle:
            result["error_sql"] = sql_bundle["error_sql"]

        # v6.0.1: SQL liefert date/datetime/Decimal-Objekte die Starlette's
        # JSONResponse nicht direkt serialisieren kann. Vor dem Return alles
        # in primitive Typen umwandeln.
        safe = _json_safe(result)

        # v6.7.117: Cache nur erfolgreiche Ergebnisse (keine Timeouts)
        if "error_printers" not in safe and "error_sql" not in safe:
            _dashboard_cache[_uid] = {"ts": _time.time(), "data": safe}
        elif safe.get("error_printers") != "timeout" and safe.get("error_sql") != "timeout":
            # Teilerfolge (z.B. SQL down, Printer OK) trotzdem cachen mit kurzer TTL
            _dashboard_cache[_uid] = {"ts": _time.time(), "data": safe}

        return JSONResponse(safe)

    def _json_safe(obj):
        """Rekursiver Konverter: macht dict/list JSON-serialisierbar."""
        from datetime import date as _date, datetime as _datetime
        from decimal import Decimal as _Decimal
        if isinstance(obj, dict):
            return {str(k): _json_safe(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [_json_safe(v) for v in obj]
        if isinstance(obj, (_datetime, _date)):
            return obj.isoformat()
        if isinstance(obj, _Decimal):
            return float(obj)
        if isinstance(obj, bytes):
            try:
                return obj.decode("utf-8", errors="replace")
            except Exception:
                return str(obj)
        return obj

    # ── Fleet Health Monitor (v4.3.3) ─────────────────────────────────────────

    # ── Fleet-Daten laden (shared zwischen /fleet und /fleet/data) ──────

    async def _load_fleet_data(user: dict) -> dict:
        """
        Laedt Fleet-Daten (v4.5.1): Printix API + SQL Enrichment + Error Rate.
        Returns dict mit fleet_kpis, printers, alerts, has_printers, has_sql.
        """
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
        except Exception as e:
            logger.warning("Fleet: Tenant-Lookup fehlgeschlagen: %s", e)
            tenant = None

        has_api = bool(tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")))
        has_sql = bool(tenant and tenant.get("sql_server"))
        printers_list: list[dict] = []
        fleet_kpis = {"total": 0, "active_today": 0, "inactive_7d": 0, "avg_utilization": 0}
        alerts: list[dict] = []

        if not (has_api and tenant):
            return {"fleet_kpis": fleet_kpis, "printers": printers_list,
                    "alerts": alerts, "has_printers": has_api, "has_sql": has_sql}

        # 1. Drucker von Printix API laden (primaere Datenquelle)
        api_printers: list[dict] = []
        try:
            import asyncio as _aio
            import re as _re
            client = _make_printix_client(tenant)
            raw_data = await _aio.to_thread(lambda: client.list_printers(size=200))
            if isinstance(raw_data, dict):
                raw_list = raw_data.get("printers", raw_data.get("content", []))
            elif isinstance(raw_data, list):
                raw_list = raw_data
            else:
                raw_list = []

            # Deduplizieren nach printer_id
            printer_map: dict[str, dict] = {}
            for p in raw_list:
                href = (p.get("_links") or {}).get("self", {}).get("href", "")
                m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                pid = m.group(1) if m else p.get("id", str(id(p)))
                if pid not in printer_map:
                    vendor = p.get("vendor", "")
                    model = p.get("model", "")
                    api_status = (p.get("connectionStatus") or "").lower()
                    printer_map[pid] = {
                        "printer_id": pid,
                        "name": f"{vendor} {model}".strip() if (vendor or model) else p.get("name", "Unknown"),
                        "model": model,
                        "vendor": vendor,
                        "location": p.get("location", ""),
                        "api_status": api_status,
                        "printerSignId": p.get("printerSignId", ""),
                    }
            api_printers = list(printer_map.values())
        except Exception as e:
            logger.warning("Fleet: Printix API Fehler: %s", e)

        # 2. SQL-Daten laden (optional — Enrichment + Error Rate)
        sql_by_id: dict[str, dict] = {}
        sql_by_name: dict[str, dict] = {}
        error_counts: dict[str, dict] = {}  # printer_id -> {failed, total_all}

        if has_sql:
            try:
                import asyncio as _aio
                from reporting.sql_client import set_config_from_tenant, query_fetchall
                set_config_from_tenant(tenant)

                from datetime import date as _date, timedelta as _td
                today = _date.today()
                start_90d = today - _td(days=90)

                def _load_fleet_sql():
                    from reporting.query_tools import query_device_readings, _V, _fmt_date
                    readings = query_device_readings(str(start_90d), str(today))
                    # Index by printer_id and printer_name for flexible matching
                    by_id = {}
                    by_name = {}
                    for r in readings:
                        pid = str(r.get("printer_id", ""))
                        pname = r.get("printer_name", "")
                        if pid:
                            by_id[pid] = r
                        if pname:
                            by_name[pname] = r

                    # v4.5.1: Error Rate — zaehle failed Jobs pro Drucker
                    errors = {}
                    try:
                        from reporting.sql_client import get_tenant_id
                        tid = get_tenant_id()
                        error_sql = f"""
                            SELECT
                                td.printer_id,
                                COUNT(DISTINCT CASE WHEN td.print_job_status <> 'PRINT_OK'
                                      THEN td.job_id END) AS failed_jobs,
                                COUNT(DISTINCT td.job_id) AS total_jobs_all
                            FROM {_V('tracking_data')} td
                            WHERE td.tenant_id = ?
                              AND td.print_time >= ?
                              AND td.print_time < DATEADD(day, 1, CAST(? AS DATE))
                            GROUP BY td.printer_id
                        """
                        err_rows = query_fetchall(error_sql,
                                                  (tid, _fmt_date(start_90d), _fmt_date(today)))
                        for r in err_rows:
                            pid = str(r.get("printer_id", ""))
                            if pid:
                                errors[pid] = {
                                    "failed": int(r.get("failed_jobs") or 0),
                                    "total_all": int(r.get("total_jobs_all") or 0),
                                }
                    except Exception as e:
                        logger.debug("Fleet: Error-Rate SQL fehlgeschlagen: %s", e)

                    return by_id, by_name, errors

                sql_by_id, sql_by_name, error_counts = await _aio.to_thread(_load_fleet_sql)
            except Exception as e:
                logger.warning("Fleet: SQL-Daten Fehler: %s", e)

        # 3. API + SQL mergen — primaer ueber printer_id, Fallback ueber name
        from datetime import date as _date, timedelta as _td, datetime as _dt
        today = _date.today()
        total_util = 0.0
        util_count = 0

        for p in api_printers:
            pid = p.get("printer_id", "")
            name = p["name"]

            # v4.5.1: Merge primaer ueber printer_id, Fallback ueber name
            sql_data = sql_by_id.get(pid) or sql_by_name.get(name) or {}

            # API connectionStatus -> primaerer Status
            api_status = p.get("api_status", "")
            if api_status in ("connected", "online"):
                status = "active"
            elif api_status in ("disconnected", "offline"):
                status = "critical"
            else:
                status = "unknown"

            # SQL-Daten als Enrichment (optional)
            last_act = sql_data.get("last_activity")
            total_jobs = int(sql_data.get("total_jobs") or 0)
            total_pages = int(sql_data.get("total_pages") or 0)
            active_days = int(sql_data.get("active_days") or 0)
            days_ago = None

            if last_act:
                try:
                    last_date = _dt.fromisoformat(str(last_act)).date() if not isinstance(last_act, _date) else last_act
                    days_ago = (today - last_date).days
                    # Verfeinere Status mit SQL-Aktivitaetsdaten
                    if status == "unknown":
                        if days_ago == 0:
                            status = "active"
                        elif days_ago <= 7:
                            status = "warning"
                        else:
                            status = "critical"
                except Exception:
                    pass

            utilization = round(active_days / 90 * 100, 1) if active_days else 0.0
            total_util += utilization
            util_count += 1

            # v4.5.1: Error Rate berechnen
            # Verwende SQL printer_id aus sql_data fuer den Error-Lookup
            sql_pid = str(sql_data.get("printer_id", "")) or pid
            err_info = error_counts.get(sql_pid, {})
            failed_jobs = err_info.get("failed", 0)
            total_all = err_info.get("total_all", 0)
            error_rate = round(failed_jobs * 100.0 / total_all, 1) if total_all > 0 else 0.0

            printers_list.append({
                "name": name,
                "model": sql_data.get("model_name") or p.get("model", ""),
                "location": sql_data.get("location") or p.get("location", ""),
                "site": sql_data.get("site_name", ""),
                "status": status,
                "api_status": api_status,
                "last_activity": str(last_act) if last_act else None,
                "days_ago": days_ago,
                "total_jobs": total_jobs,
                "total_pages": total_pages,
                "utilization": utilization,
                "error_rate": error_rate,
            })

        # Sortieren: Critical zuerst, dann Warning, dann Active
        status_order = {"critical": 0, "warning": 1, "unknown": 2, "active": 3}
        printers_list.sort(key=lambda x: status_order.get(x["status"], 9))

        # KPIs
        fleet_kpis = {
            "total": len(printers_list),
            "active_today": sum(1 for x in printers_list if x["status"] == "active"),
            "inactive_7d": sum(1 for x in printers_list if x["status"] == "critical"),
            "avg_utilization": round(total_util / util_count, 1) if util_count else 0,
        }

        # Alerts — inactive + high error rate
        for p in printers_list:
            if p["status"] == "critical":
                alerts.append({
                    "type": "inactive",
                    "printer_name": p["name"],
                    "detail": p.get("api_status") or (f"{p['days_ago']}d" if p.get("days_ago") else "?"),
                })
            elif p.get("error_rate", 0) > 10:
                alerts.append({
                    "type": "high_errors",
                    "printer_name": p["name"],
                    "detail": f"Error Rate: {p['error_rate']}%",
                })

        return {"fleet_kpis": fleet_kpis, "printers": printers_list,
                "alerts": alerts, "has_printers": has_api, "has_sql": has_sql}

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet_health(request: Request):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if user.get("status") == "pending":
            return RedirectResponse("/pending", status_code=302)

        tc = t_ctx(request)
        try:
            fleet = await _load_fleet_data(user)
        except Exception as e:
            logger.error("Fleet: Render fehlgeschlagen: %s", e, exc_info=True)
            fleet = {"fleet_kpis": {"total": 0, "active_today": 0, "inactive_7d": 0, "avg_utilization": 0},
                     "printers": [], "alerts": [], "has_printers": False, "has_sql": False}

        return templates.TemplateResponse("fleet.html", {
            "request": request, "user": user,
            "has_printers": fleet["has_printers"], "has_sql": fleet["has_sql"],
            "fleet_kpis": fleet["fleet_kpis"],
            "printers": fleet["printers"],
            "alerts": fleet["alerts"],
            **tc,
        })

    @app.get("/fleet/data")
    async def fleet_data(request: Request):
        """v4.5.1: JSON-Endpunkt fuer Fleet Auto-Refresh mit echten Daten."""
        user = get_session_user(request)
        if not user:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            fleet = await _load_fleet_data(user)
            return JSONResponse({
                "fleet_kpis": fleet["fleet_kpis"],
                "printers": fleet["printers"],
                "alerts": fleet["alerts"],
            })
        except Exception as e:
            logger.error("Fleet /fleet/data Fehler: %s", e, exc_info=True)
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    # ── Package Builder — Clientless / Zero Trust ─────────────────────────────

    @app.get("/fleet/package-builder", response_class=HTMLResponse)
    async def pkg_builder_page(request: Request):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        vendors = _pkg_builder.get_vendors_list()
        return templates.TemplateResponse("fleet_package_builder.html", {
            "request": request, "user": user,
            "vendors": vendors, **tc,
        })

    @app.post("/fleet/package-builder/upload")
    async def pkg_builder_upload(
        request: Request,
        vendor_id: str = Form(""),
        file: UploadFile = File(...),
    ):
        tc = t_ctx(request)
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "Nicht angemeldet."}, status_code=401)

        file_bytes = await file.read()
        session_id, error = _pkg_builder.receive_upload(
            file_bytes,
            filename=file.filename or "package.zip",
            vendor_id=vendor_id or None,
        )
        if not session_id:
            return JSONResponse({"ok": False, "error": error})

        # Tenant-Kontext für Vorbelegung
        tenant = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
        except Exception:
            pass

        result = _pkg_builder.analyze_localized(session_id, tenant=tenant, tr=tc["_"])
        if not result.ok:
            _pkg_builder.cleanup_session(session_id)
            return JSONResponse({"ok": False, "error": result.error})

        resp = result.to_dict()
        resp["session_id"] = session_id
        resp["original_filename"] = file.filename or "package.zip"
        return JSONResponse(resp)

    @app.post("/fleet/package-builder/patch")
    async def pkg_builder_patch(request: Request):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "Nicht angemeldet."}, status_code=401)

        body = await request.json()
        session_id = body.get("session_id", "")
        field_values: dict = body.get("fields", {})
        original_filename: str = body.get("original_filename", "package.zip")

        if not session_id:
            return JSONResponse({"ok": False, "error": "Keine Session-ID."})

        result = _pkg_builder.patch(session_id, field_values, original_filename)
        resp = result.to_dict()
        if result.ok:
            notes = _pkg_builder.get_install_notes_localized(session_id, field_values, tr=t_ctx(request)["_"])
            resp["install_notes"] = notes
        return JSONResponse(resp)

    @app.get("/fleet/package-builder/download/{session_id}")
    async def pkg_builder_download(request: Request, session_id: str):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)

        path, filename = _pkg_builder.get_download_path(session_id)
        if not path:
            return HTMLResponse("<h2>Download nicht verfügbar oder abgelaufen.</h2>", status_code=404)

        import asyncio as _aio
        # Nach dem Senden bereinigen (verzögert, damit FileResponse fertig ist)
        async def _cleanup():
            await _aio.sleep(5)
            _pkg_builder.cleanup_session(session_id)

        _aio.create_task(_cleanup())
        return FileResponse(
            path=path,
            filename=filename,
            media_type="application/zip",
        )

    # ── Sustainability Report (v4.3.3) ────────────────────────────────────────

    @app.get("/reports/sustainability", response_class=HTMLResponse)
    async def sustainability_report(request: Request):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if user.get("status") == "pending":
            return RedirectResponse("/pending", status_code=302)

        tc = t_ctx(request)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
        except Exception:
            tenant = None

        has_sql = bool(tenant and tenant.get("sql_server"))

        # Default-Zeitraum: aktuelles Jahr
        from datetime import date as _date
        today = _date.today()
        start_date = request.query_params.get("start_date", str(today.replace(month=1, day=1)))
        end_date = request.query_params.get("end_date", str(today))

        env = {}
        tree_data = {}
        equivalents = {}
        monthly_trend = []

        if has_sql:
            try:
                import asyncio as _aio
                from reporting.sql_client import set_config_from_tenant
                set_config_from_tenant(tenant)

                def _load_sustainability():
                    from reporting.query_tools import query_tree_meter, query_print_stats
                    from reporting.report_engine import compute_env_impact

                    tree = query_tree_meter(start_date, end_date)
                    _env = compute_env_impact(
                        tree.get("total_pages", 0),
                        tree.get("duplex_pages", 0),
                        tree.get("saved_sheets_duplex", 0),
                    )

                    # Äquivalenzen berechnen
                    _eq = {
                        "car_km": round(_env.get("co2_kg", 0) / 0.21, 1),     # 210g CO2/km
                        "bathtubs": round(_env.get("water_l", 0) / 150, 1),    # 150L/Wanne
                        "phone_charges": round(_env.get("energy_kwh", 0) * 1000 / 12, 0),  # 12Wh/Ladung
                    }

                    # Monatlicher Trend
                    monthly = query_print_stats(start_date, end_date, group_by="month")
                    _trend = []
                    for m in monthly:
                        m_tree = query_tree_meter(str(m["period"]), str(m["period"]))
                        m_env = compute_env_impact(
                            m_tree.get("total_pages", 0),
                            m_tree.get("duplex_pages", 0),
                            m_tree.get("saved_sheets_duplex", 0),
                        )
                        _trend.append({
                            "period": str(m["period"]),
                            "co2_kg": m_env.get("co2_kg", 0),
                            "trees": m_env.get("trees", 0),
                            "water_l": m_env.get("water_l", 0),
                            "saved_sheets": m_tree.get("saved_sheets_duplex", 0),
                        })

                    return tree, _env, _eq, _trend

                tree_data, env, equivalents, monthly_trend = await _aio.to_thread(
                    _load_sustainability
                )
            except Exception as e:
                logger.warning("Sustainability-Laden fehlgeschlagen: %s", e)

        return templates.TemplateResponse("sustainability.html", {
            "request": request, "user": user,
            "has_sql": has_sql,
            "env": env,
            "tree_data": tree_data,
            "equivalents": equivalents,
            "monthly_trend": monthly_trend,
            "start_date": start_date,
            "end_date": end_date,
            **tc,
        })

    # ── Settings (Selbstverwaltung) ────────────────────────────────────────────

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_get(request: Request):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
        except Exception:
            tenant = {}
        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "tenant": tenant,
            "saved": False, "error": None, **t_ctx(request),
        })

    @app.post("/settings", response_class=HTMLResponse)
    async def settings_post(
        request: Request,
        printix_tenant_id:    str = Form(default=""),
        tenant_url:           str = Form(default=""),
        print_client_id:      str = Form(default=""),
        print_client_secret:  str = Form(default=""),
        card_client_id:       str = Form(default=""),
        card_client_secret:   str = Form(default=""),
        ws_client_id:         str = Form(default=""),
        ws_client_secret:     str = Form(default=""),
        um_client_id:         str = Form(default=""),
        um_client_secret:     str = Form(default=""),
        shared_client_id:     str = Form(default=""),
        shared_client_secret: str = Form(default=""),
        sql_server:           str = Form(default=""),
        sql_database:         str = Form(default=""),
        sql_username:         str = Form(default=""),
        sql_password:         str = Form(default=""),
        mail_api_key:         str = Form(default=""),
        mail_from:            str = Form(default=""),
    ):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)

        try:
            from db import update_tenant_credentials, get_tenant_full_by_user_id, audit
            update_tenant_credentials(
                user_id=user["id"],
                printix_tenant_id=printix_tenant_id.strip() or None,
                tenant_url=tenant_url.strip().rstrip("/") or None,
                print_client_id=print_client_id.strip() or None,
                print_client_secret=print_client_secret.strip() or None,
                card_client_id=card_client_id.strip() or None,
                card_client_secret=card_client_secret.strip() or None,
                ws_client_id=ws_client_id.strip() or None,
                ws_client_secret=ws_client_secret.strip() or None,
                um_client_id=um_client_id.strip() or None,
                um_client_secret=um_client_secret.strip() or None,
                shared_client_id=shared_client_id.strip() or None,
                shared_client_secret=shared_client_secret.strip() or None,
                sql_server=sql_server.strip() or None,
                sql_database=sql_database.strip() or None,
                sql_username=sql_username.strip() or None,
                sql_password=sql_password.strip() or None,
                mail_api_key=mail_api_key.strip() or None,
                mail_from=mail_from.strip() or None,
            )
            audit(user["id"], "update_settings", "Credentials aktualisiert")
            tenant = get_tenant_full_by_user_id(user["id"])

            # v6.7.5: Bei gesetzten User-Management-Credentials → Background-Sync
            # der Printix-User in den persistenten Cache. Notwendig damit das
            # IPP-Routing den Username → Tenant auflösen kann.
            if tenant and tenant.get("um_client_id") and tenant.get("um_client_secret") \
                    and tenant.get("printix_tenant_id"):
                try:
                    import asyncio as _asyncio
                    _asyncio.create_task(_trigger_printix_user_sync(tenant))
                    logger.info("Printix-User-Sync für Tenant %s im Hintergrund gestartet",
                                tenant.get("printix_tenant_id"))
                except Exception as _se:
                    logger.warning("Sync-Trigger fehlgeschlagen: %s", _se)
        except Exception as e:
            logger.error("Settings-Fehler: %s", e)
            return templates.TemplateResponse("settings.html", {
                "request": request, "user": user, "tenant": {},
                "saved": False, "error": str(e), **tc,
            })

        return templates.TemplateResponse("settings.html", {
            "request": request, "user": user, "tenant": tenant,
            "saved": True, "error": None, **tc,
        })

    # v6.7.5: Manueller Refresh-Endpoint für den Printix-User-Cache
    # Aufruf z.B. per Button auf der Settings-Seite oder via curl/Postman.
    @app.post("/tenant/cache/refresh-users", response_class=JSONResponse)
    async def tenant_cache_refresh_users(request: Request):
        user = require_login(request)
        if not user:
            return JSONResponse({"error": "not authenticated"}, status_code=401)
        from db import get_tenant_full_by_user_id
        tenant = get_tenant_full_by_user_id(user["id"])
        if not tenant or not tenant.get("printix_tenant_id"):
            return JSONResponse({"error": "kein Tenant konfiguriert"}, status_code=400)
        try:
            result = await _trigger_printix_user_sync(tenant)
            return JSONResponse({
                "ok": True,
                "tenant_id": tenant["id"],
                "printix_tenant_id": tenant["printix_tenant_id"],
                **result,
            })
        except Exception as e:
            logger.error("Manueller User-Sync fehlgeschlagen: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/settings/regenerate-oauth", response_class=JSONResponse)
    async def settings_regenerate_oauth(request: Request):
        user = require_login(request)
        if not user:
            return JSONResponse({"error": "Nicht authentifiziert"}, status_code=401)
        try:
            from db import regenerate_oauth_secret, audit
            new_secret = regenerate_oauth_secret(user["id"])
            audit(user["id"], "regen_oauth_secret", "OAuth-Secret neu generiert")
            return JSONResponse({"oauth_client_secret": new_secret})
        except Exception as e:
            logger.error("Regen-OAuth-Fehler: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/settings/password", response_class=HTMLResponse)
    async def settings_password_get(request: Request):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse("settings_password.html", {
            "request": request, "user": user,
            "saved": False, "error": None, **t_ctx(request),
        })

    @app.post("/settings/password", response_class=HTMLResponse)
    async def settings_password_post(
        request: Request,
        old_password:  str = Form(...),
        new_password:  str = Form(...),
        new_password2: str = Form(...),
    ):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        _ = tc["_"]
        error = None

        if new_password != new_password2:
            error = _("reg_pw_mismatch")
        elif len(new_password) < 8:
            error = "Passwort muss mindestens 8 Zeichen lang sein."
        else:
            try:
                from db import authenticate_user, reset_user_password, audit
                if not authenticate_user(user["username"], old_password):
                    error = _("settings_pw_wrong")
                else:
                    reset_user_password(user["id"], new_password)
                    audit(user["id"], "change_password", "Passwort geändert")
            except Exception as e:
                error = str(e)

        if error:
            return templates.TemplateResponse("settings_password.html", {
                "request": request, "user": user,
                "saved": False, "error": error, **tc,
            })
        return templates.TemplateResponse("settings_password.html", {
            "request": request, "user": user,
            "saved": True, "error": None, **tc,
        })

    # ── Hilfe / Verbindungsanleitung ──────────────────────────────────────────

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request):
        user = require_login(request)
        if not user:
            return RedirectResponse("/login", status_code=302)

        base   = mcp_base_url()
        tenant = None
        try:
            from db import get_tenant_by_user_id
            tenant = get_tenant_by_user_id(user["id"])
        except Exception:
            pass

        return templates.TemplateResponse("help.html", {
            "request": request, "user": user, "tenant": tenant,
            "base_url":           base,
            "mcp_url":            f"{base}/mcp",
            "sse_url":            f"{base}/sse",
            "oauth_authorize_url":f"{base}/oauth/authorize",
            "oauth_token_url":    f"{base}/oauth/token",
            **t_ctx(request),
        })

    # ── Admin ──────────────────────────────────────────────────────────────────

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_dashboard(request: Request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_all_users, count_tenants
            users        = get_all_users()
            tenant_count = count_tenants()
        except Exception:
            users = []; tenant_count = 0
        return templates.TemplateResponse("admin_dashboard.html", {
            "request": request, "user": user,
            "users": users, "tenant_count": tenant_count,
            "base_url": mcp_base_url(), **t_ctx(request),
        })

    @app.get("/admin/users", response_class=HTMLResponse)
    async def admin_users(request: Request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_all_users
            users = get_all_users()
            user_map = {u.get("id", ""): u for u in users}
            for entry in users:
                parent_id = (entry.get("parent_user_id") or "").strip()
                if entry.get("is_admin"):
                    entry["relationship_kind"] = "global_admin"
                    entry["relationship_name"] = ""
                    entry["relationship_email"] = ""
                elif entry.get("role_type") == "employee":
                    parent = user_map.get(parent_id, {})
                    entry["relationship_kind"] = "employee_of"
                    entry["relationship_name"] = parent.get("full_name") or parent.get("username") or ""
                    entry["relationship_email"] = parent.get("email") or ""
                else:
                    entry["relationship_kind"] = "tenant_admin"
                    entry["relationship_name"] = entry.get("full_name") or entry.get("username") or ""
                    entry["relationship_email"] = entry.get("email") or ""
        except Exception:
            users = []
        return templates.TemplateResponse("admin_users.html", {
            "request": request, "user": user, "users": users, **t_ctx(request)
        })

    @app.post("/admin/users/{user_id}/approve")
    async def admin_approve(user_id: str, request: Request):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import set_user_status, audit
            set_user_status(user_id, "approved")
            audit(admin["id"], "approve_user", f"User {user_id} genehmigt", object_type="user", object_id=user_id)
        except Exception as e:
            logger.error("Approve-Fehler: %s", e)
        return RedirectResponse("/admin/users", status_code=302)

    @app.get("/admin/users/invite", response_class=HTMLResponse)
    async def admin_invite_user_get(request: Request):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse("admin_user_invite.html", {
            "request": request,
            "user": admin,
            "saved": False,
            "error": None,
            **t_ctx(request),
        })

    @app.post("/admin/users/invite", response_class=HTMLResponse)
    async def admin_invite_user_post(
        request: Request,
        username: str = Form(...),
        email: str = Form(...),
        full_name: str = Form(default=""),
        company: str = Form(default=""),
        invite_lang: str = Form(default="de"),
        role_type: str = Form(default="user"),
    ):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        _ = tc["_"]
        error = None
        if len(username.strip()) < 3:
            error = _("invite_username_length_error")
        elif "@" not in email or "." not in email:
            error = _("invite_email_required_error")
        else:
            try:
                from db import username_exists
                if username_exists(username.strip()):
                    error = _("reg_user_exists")
            except Exception as e:
                error = str(e)
        if error:
            return templates.TemplateResponse("admin_user_invite.html", {
                "request": request,
                "user": admin,
                "saved": False,
                "error": error,
                "f_username": username,
                "f_email": email,
                "f_full_name": full_name,
                "f_company": company,
                "f_invite_lang": invite_lang,
                "f_role_type": role_type,
                **tc,
            })

        temp_password = _generate_temp_password()
        created_user = None
        try:
            from db import create_invited_user, get_tenant_full_by_user_id, delete_user, audit
            tenant = get_tenant_full_by_user_id(admin["id"]) or {}
            if not tenant.get("mail_api_key") or not tenant.get("mail_from"):
                raise RuntimeError(_("invite_mail_not_configured"))

            created_user = create_invited_user(
                username=username.strip(),
                password=temp_password,
                email=email.strip(),
                full_name=full_name.strip(),
                company=company.strip(),
                invited_by_user_id=admin["id"],
                invitation_language=invite_lang.strip(),
                role_type=role_type.strip(),
            )

            from invite_mail import render_invitation_email
            from reporting.mail_client import send_report
            login_url = f"{_get_base_url(request)}/login"
            subject, html_body = render_invitation_email(
                lang=invite_lang.strip(),
                full_name=full_name.strip(),
                username=username.strip(),
                password=temp_password,
                login_url=login_url,
            )
            send_report(
                recipients=[email.strip()],
                subject=subject,
                html_body=html_body,
                api_key=tenant.get("mail_api_key", ""),
                mail_from=tenant.get("mail_from", ""),
                mail_from_name=tenant.get("mail_from_name", "") or "Printix Management Console",
            )
            audit(
                admin["id"],
                "invite_user",
                f"Benutzer '{username.strip()}' eingeladen ({email.strip()}, lang={invite_lang.strip()})",
                object_type="user",
                object_id=created_user["id"],
            )
        except Exception as e:
            logger.error("Invite-User-Fehler: %s", e)
            if created_user:
                try:
                    from db import delete_user
                    delete_user(created_user["id"])
                except Exception:
                    pass
            return templates.TemplateResponse("admin_user_invite.html", {
                "request": request,
                "user": admin,
                "saved": False,
                "error": str(e),
                "f_username": username,
                "f_email": email,
                "f_full_name": full_name,
                "f_company": company,
                "f_invite_lang": invite_lang,
                "f_role_type": role_type,
                **tc,
            })

        return templates.TemplateResponse("admin_user_invite.html", {
            "request": request,
            "user": admin,
            "saved": True,
            "error": None,
            "created_username": username.strip(),
            "created_email": email.strip(),
            **tc,
        })

    @app.post("/admin/users/{user_id}/disable")
    async def admin_disable(user_id: str, request: Request):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        if user_id == admin["id"]:
            return RedirectResponse("/admin/users", status_code=302)
        try:
            from db import set_user_status, audit
            set_user_status(user_id, "disabled")
            audit(admin["id"], "disable_user", f"User {user_id} deaktiviert", object_type="user", object_id=user_id)
        except Exception as e:
            logger.error("Disable-Fehler: %s", e)
        return RedirectResponse("/admin/users", status_code=302)

    @app.post("/admin/users/{user_id}/delete")
    async def admin_delete_user(user_id: str, request: Request):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        if user_id == admin["id"]:
            return RedirectResponse("/admin/users", status_code=302)
        try:
            from db import delete_user, audit
            delete_user(user_id)
            audit(admin["id"], "delete_user", f"User {user_id} gelöscht", object_type="user", object_id=user_id)
        except Exception as e:
            logger.error("Delete-Fehler: %s", e)
        return RedirectResponse("/admin/users", status_code=302)

    @app.get("/admin/users/create", response_class=HTMLResponse)
    async def admin_create_user_get(request: Request):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse("admin_user_create.html", {
            "request": request, "user": admin,
            "saved": False, "error": None, **t_ctx(request),
        })

    @app.post("/admin/users/create", response_class=HTMLResponse)
    async def admin_create_user_post(
        request:   Request,
        username:  str = Form(...),
        password:  str = Form(...),
        password2: str = Form(...),
        email:     str = Form(default=""),
        full_name: str = Form(default=""),
        company:   str = Form(default=""),
        role_type: str = Form(default="user"),
        status:    str = Form(default="approved"),
    ):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        _  = tc["_"]
        error = None

        if len(username) < 3:
            error = "Benutzername muss mindestens 3 Zeichen lang sein."
        elif len(password) < 8:
            error = "Passwort muss mindestens 8 Zeichen lang sein."
        elif password != password2:
            error = _("reg_pw_mismatch")
        else:
            try:
                from db import username_exists
                if username_exists(username):
                    error = _("reg_user_exists")
            except Exception as e:
                error = str(e)

        if error:
            return templates.TemplateResponse("admin_user_create.html", {
                "request": request, "user": admin,
                "saved": False, "error": error,
                "f_username": username, "f_email": email,
                "f_full_name": full_name, "f_company": company,
                "f_role_type": role_type, "f_status": status, **tc,
            })

        try:
            from db import create_user_admin, audit
            new_user = create_user_admin(
                username=username.strip(),
                password=password,
                email=email.strip(),
                role_type=role_type.strip(),
                status=status,
                full_name=full_name.strip(),
                company=company.strip(),
            )
            audit(admin["id"], "create_user", f"User '{username}' direkt angelegt (Status: {status})")
        except Exception as e:
            logger.error("Create-User-Fehler: %s", e)
            return templates.TemplateResponse("admin_user_create.html", {
                "request": request, "user": admin,
                "saved": False, "error": str(e), **tc,
            })

        return templates.TemplateResponse("admin_user_create.html", {
            "request": request, "user": admin,
            "saved": True, "error": None,
            "created_username": new_user["username"], **tc,
        })

    @app.get("/admin/users/{user_id}/edit", response_class=HTMLResponse)
    async def admin_edit_user_get(user_id: str, request: Request):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_user_by_id
            target = get_user_by_id(user_id)
        except Exception:
            target = None
        if not target:
            return RedirectResponse("/admin/users", status_code=302)
        return templates.TemplateResponse("admin_user_edit.html", {
            "request": request, "user": admin, "target": target,
            "saved": False, "error": None, **t_ctx(request),
        })

    @app.post("/admin/users/{user_id}/edit", response_class=HTMLResponse)
    async def admin_edit_user_post(
        user_id:         str,
        request:         Request,
        username:        str = Form(...),
        email:           str = Form(default=""),
        full_name:       str = Form(default=""),
        company:         str = Form(default=""),
        role_type:       str = Form(default="user"),
        status:          str = Form(default="approved"),
        printix_user_id: str = Form(default=""),
    ):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)

        try:
            from db import update_user, username_exists, get_user_by_id, audit
            if username_exists(username, exclude_id=user_id):
                target = get_user_by_id(user_id)
                return templates.TemplateResponse("admin_user_edit.html", {
                    "request": request, "user": admin, "target": target,
                    "saved": False, "error": tc["_"]("reg_user_exists"), **tc,
                })
            update_user(
                user_id=user_id,
                username=username.strip(),
                email=email.strip(),
                full_name=full_name.strip(),
                company=company.strip(),
                role_type=role_type.strip(),
                status=status,
                printix_user_id=printix_user_id.strip(),
            )
            audit(admin["id"], "edit_user", f"User {user_id} bearbeitet", object_type="user", object_id=user_id)
            target = get_user_by_id(user_id)
        except Exception as e:
            logger.error("Edit-Fehler: %s", e)
            return templates.TemplateResponse("admin_user_edit.html", {
                "request": request, "user": admin,
                "target": {"id": user_id, "username": username, "email": email},
                "saved": False, "error": str(e), **tc,
            })

        return templates.TemplateResponse("admin_user_edit.html", {
            "request": request, "user": admin, "target": target,
            "saved": True, "error": None, **tc,
        })

    @app.post("/admin/users/{user_id}/resolve-printix-id", response_class=JSONResponse)
    async def admin_resolve_printix_id(user_id: str, request: Request):
        """Versucht die Printix-User-ID für einen lokalen User automatisch zu
        ermitteln — durchsucht die jüngsten Printix-Jobs nach einem Match auf
        die E-Mail-Adresse. Nützlich wenn der User ein System-Manager ist,
        der über list_users?role=USER,GUEST_USER nicht sichtbar ist.
        """
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return JSONResponse({"ok": False, "error": "not_authorized"}, status_code=401)
        try:
            from db import get_user_by_id, get_tenant_full_by_user_id, update_user, audit
            target = get_user_by_id(user_id)
            if not target:
                return JSONResponse({"ok": False, "error": "user_not_found"}, status_code=404)
            target_email = (target.get("email") or "").strip().lower()
            if not target_email:
                return JSONResponse({"ok": False, "error": "no_email",
                    "message": "Lokaler User hat keine E-Mail-Adresse."})

            # Tenant-Credentials des Admin-Users nehmen (derzeit eingeloggt)
            tenant = get_tenant_full_by_user_id(admin["id"])
            if not tenant or not (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                return JSONResponse({"ok": False, "error": "no_print_creds",
                    "message": "Print-API-Credentials nicht konfiguriert."})

            client = _make_printix_client(tenant)
            # Strategie 1: list_users?query=<email> — erfasst normale USER/GUEST.
            # Printix liefert die Liste mal unter "users", mal unter "content"
            # (je nach API-Version/Endpoint-Variante) — beide Keys pruefen,
            # sonst uebersieht der Match alle Treffer in neueren Instanzen.
            try:
                search_result = client.list_users(role="USER,GUEST_USER",
                                                   query=target_email, page_size=20)
                users = []
                if isinstance(search_result, dict):
                    users = (search_result.get("users")
                             or search_result.get("content")
                             or [])
                elif isinstance(search_result, list):
                    users = search_result
                logger.info("Auto-resolve: list_users query=%s → %d Treffer",
                            target_email, len(users))
                for u in users:
                    if (u.get("email") or "").lower() == target_email:
                        found_id = u.get("id", "") or u.get("userId", "")
                        if not found_id:
                            # Fallback: ID aus _links.self.href ziehen
                            href = (((u.get("_links") or {}).get("self") or {})
                                    .get("href", ""))
                            if href:
                                found_id = href.rstrip("/").split("/")[-1]
                        if found_id:
                            update_user(user_id=user_id, printix_user_id=found_id)
                            audit(admin["id"], "resolve_printix_id",
                                  f"Printix-User-ID {found_id} für {target.get('username')}",
                                  object_type="user", object_id=user_id)
                            return JSONResponse({"ok": True, "printix_user_id": found_id,
                                "source": "list_users", "email": target_email})
            except Exception as e1:
                logger.warning("Auto-resolve list_users fehlgeschlagen für %s: %s",
                               target_email, e1)

            # Strategie 1b: Full-Scan via list_all_users — kein Query, aber
            # garantiert alle Seiten. Deckt den Fall ab, dass Printix den
            # query-Parameter ignoriert (manche API-Versionen) oder nur
            # Name-Substrings matcht und nicht E-Mail.
            try:
                all_users = client.list_all_users()
                logger.info("Auto-resolve: list_all_users → %d Eintraege", len(all_users))
                for u in all_users:
                    if (u.get("email") or "").lower() == target_email:
                        found_id = u.get("id", "") or u.get("userId", "")
                        if not found_id:
                            href = (((u.get("_links") or {}).get("self") or {})
                                    .get("href", ""))
                            if href:
                                found_id = href.rstrip("/").split("/")[-1]
                        if found_id:
                            update_user(user_id=user_id, printix_user_id=found_id)
                            audit(admin["id"], "resolve_printix_id",
                                  f"Printix-User-ID {found_id} für {target.get('username')} "
                                  f"(via full scan)",
                                  object_type="user", object_id=user_id)
                            return JSONResponse({"ok": True, "printix_user_id": found_id,
                                "source": "list_all_users", "email": target_email})
            except Exception as e1b:
                logger.warning("Auto-resolve list_all_users fehlgeschlagen für %s: %s",
                               target_email, e1b)

            # Strategie 2: jüngste Print-Jobs durchsuchen nach ownerEmail match
            try:
                jobs_data = client.list_print_jobs(size=100)
                jobs = []
                if isinstance(jobs_data, dict):
                    jobs = jobs_data.get("jobs") or jobs_data.get("content") or []
                elif isinstance(jobs_data, list):
                    jobs = jobs_data
                for j in jobs:
                    oe = (j.get("ownerEmail") or "").lower()
                    if oe == target_email:
                        oid = j.get("ownerId") or ""
                        if oid:
                            update_user(user_id=user_id, printix_user_id=oid)
                            audit(admin["id"], "resolve_printix_id",
                                  f"Printix-User-ID {oid} für {target.get('username')} "
                                  f"aus Print-Job ermittelt",
                                  object_type="user", object_id=user_id)
                            return JSONResponse({"ok": True, "printix_user_id": oid,
                                "source": "print_jobs", "email": target_email})
            except Exception as e2:
                logger.debug("print_jobs-Strategie: %s", e2)

            # Strategie 3: cached_printix_users — deckt Manager-Rollen ab,
            # die die list_users-API nicht zurueckgibt (System/Site/Kiosk
            # Manager). Tenant-Owner werden beim Setup synthetisch mit ihrer
            # echten Printix-UUID eingetragen.
            try:
                from cloudprint.printix_cache_db import find_printix_user_by_identity
                px_user = find_printix_user_by_identity(target_email)
                if not px_user:
                    # Fallback: nur der Local-Part (vor dem @)
                    local_part = target_email.split("@")[0] if "@" in target_email else ""
                    if local_part:
                        px_user = find_printix_user_by_identity(local_part)
                if px_user and px_user.get("printix_user_id"):
                    found_id = px_user["printix_user_id"]
                    # mgr:-Praefix = synthetischer SYSTEM_MANAGER-Eintrag.
                    # Solche IDs werden von der Printix Card-API abgelehnt
                    # ("Failed to convert 'user' with value: 'mgr:...'").
                    # Fuer Cards-Funktionalitaet braucht der User eine echte
                    # User-UUID — Manager koennen aktuell keine Karten anlegen.
                    if found_id.startswith("mgr:") or ":" in found_id:
                        logger.warning(
                            "Auto-resolve: cache hit für %s liefert Manager-ID %s — "
                            "nicht fuer Cards geeignet, ueberspringe",
                            target_email, found_id,
                        )
                        # Nicht speichern, nicht returnen — zu den finalen
                        # not_found-Fehlermeldung durchfallen lassen.
                    else:
                        logger.info("Auto-resolve: cache hit für %s → %s (role=%s)",
                                    target_email, found_id, px_user.get("role"))
                        update_user(user_id=user_id, printix_user_id=found_id)
                        audit(admin["id"], "resolve_printix_id",
                              f"Printix-User-ID {found_id} für {target.get('username')} "
                              f"(aus cached_printix_users)",
                              object_type="user", object_id=user_id)
                        return JSONResponse({"ok": True, "printix_user_id": found_id,
                            "source": "cached_printix_users", "email": target_email})
            except Exception as e3:
                logger.warning("Auto-resolve cache-Lookup fehlgeschlagen für %s: %s",
                               target_email, e3)

            return JSONResponse({"ok": False, "error": "not_found",
                "message": (
                    "UUID konnte nicht automatisch ermittelt werden. "
                    f"Suche mit E-Mail '{target_email}' fand keine Treffer in "
                    "list_users(), den letzten 100 Print-Jobs oder im lokalen "
                    "Printix-User-Cache. Manager-Rollen (System/Site/Kiosk) "
                    "sind über die Printix-API nicht abrufbar — bitte UUID "
                    "manuell aus der Printix-Admin-URL kopieren "
                    "(https://manager.printix.net/users/<UUID>)."
                )})
        except Exception as e:
            logger.error("resolve_printix_id error: %s", e)
            return JSONResponse({"ok": False, "error": "internal", "message": str(e)[:200]},
                                status_code=500)

    @app.post("/admin/users/{user_id}/reset-password")
    async def admin_reset_password(
        user_id:      str,
        request:      Request,
        new_password: str = Form(...),
    ):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import reset_user_password, audit
            reset_user_password(user_id, new_password)
            audit(admin["id"], "reset_password", f"Passwort für User {user_id} zurückgesetzt", object_type="user", object_id=user_id)
        except Exception as e:
            logger.error("Reset-PW-Fehler: %s", e)
        return RedirectResponse(f"/admin/users/{user_id}/edit?pw_saved=1", status_code=302)

    @app.get("/admin/audit", response_class=HTMLResponse)
    async def admin_audit(request: Request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_audit_log
            entries = get_audit_log(limit=200)
        except Exception:
            entries = []
        return templates.TemplateResponse("admin_audit.html", {
            "request": request, "user": user, "entries": entries, **t_ctx(request)
        })

    # ─── Feedback / Feature-Request-Ticketsystem (v3.9.0) ─────────────────────
    @app.get("/feedback", response_class=HTMLResponse)
    async def feedback_list(request: Request):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import list_feature_requests
            if user.get("is_admin"):
                tickets = list_feature_requests(limit=500)
            else:
                tickets = list_feature_requests(user_id=user["id"], limit=500)
        except Exception as e:
            logger.error("feedback_list-Fehler: %s", e)
            tickets = []
        flash = request.query_params.get("flash", "")
        return templates.TemplateResponse("feedback.html", {
            "request": request, "user": user,
            "tickets": tickets, "flash": flash,
            **t_ctx(request),
        })

    @app.post("/feedback/new", response_class=HTMLResponse)
    async def feedback_new(
        request: Request,
        title: str = Form(...),
        description: str = Form(default=""),
        category: str = Form(default="feature"),
    ):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        if not title.strip():
            return RedirectResponse("/feedback", status_code=302)
        try:
            from db import create_feature_request, audit, get_tenant_by_user_id
            t = get_tenant_by_user_id(user["id"]) or {}
            ticket = create_feature_request(
                user_id=user["id"],
                user_email=user.get("email") or user.get("username", ""),
                title=title.strip()[:200],
                description=(description or "").strip()[:4000],
                category=(category or "feature").strip(),
                tenant_id=t.get("id", ""),
            )
            audit(user["id"], "feedback_create",
                  f"Ticket {ticket.get('ticket_no', '?')}: {title[:80]}",
                  object_type="feature_request", object_id=str(ticket.get("id", "")))
        except Exception as e:
            logger.error("feedback_new-Fehler: %s", e)
            return RedirectResponse("/feedback?flash=Fehler", status_code=302)
        tc = t_ctx(request)
        flash = tc["_"]("feedback_flash_created") + f" {ticket.get('ticket_no','')}"
        from urllib.parse import quote
        return RedirectResponse(f"/feedback?flash={quote(flash)}", status_code=302)

    @app.get("/feedback/{ticket_id}", response_class=HTMLResponse)
    async def feedback_detail(ticket_id: int, request: Request):
        user = get_session_user(request)
        if not user:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_feature_request
            ticket = get_feature_request(ticket_id)
        except Exception:
            ticket = None
        if not ticket:
            return RedirectResponse("/feedback", status_code=302)
        # Nicht-Admins dürfen nur ihre eigenen Tickets sehen
        if not user.get("is_admin") and ticket.get("user_id") != user["id"]:
            return RedirectResponse("/feedback", status_code=302)
        return templates.TemplateResponse("feedback_detail.html", {
            "request": request, "user": user, "ticket": ticket,
            **t_ctx(request),
        })

    @app.post("/feedback/{ticket_id}/update", response_class=HTMLResponse)
    async def feedback_update(
        ticket_id: int,
        request: Request,
        status: str = Form(...),
        priority: str = Form(default="normal"),
        admin_note: str = Form(default=""),
    ):
        admin = get_session_user(request)
        if not admin or not admin.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from db import update_feature_request_status, audit, get_feature_request
            ok = update_feature_request_status(
                ticket_id, status=status, admin_note=admin_note, priority=priority,
            )
            if ok:
                t = get_feature_request(ticket_id) or {}
                audit(admin["id"], "feedback_update",
                      f"Ticket {t.get('ticket_no','?')} → {status}",
                      object_type="feature_request", object_id=str(ticket_id))
        except Exception as e:
            logger.error("feedback_update-Fehler: %s", e)
        return RedirectResponse(f"/feedback/{ticket_id}", status_code=302)

    def _admin_settings_ctx(
        request,
        user,
        saved=False,
        error=None,
        auto_setup_success=False,
        backup_success=None,
        backup_error=None,
        restore_success=None,
    ):
        """Baut den Template-Kontext für admin_settings.html."""
        from urllib.parse import urlparse
        try:
            from backup_manager import list_backups
            backups = list_backups()
        except Exception:
            backups = []
        try:
            from db import get_setting
            public_url = get_setting("public_url", "")
        except Exception:
            public_url = os.environ.get("MCP_PUBLIC_URL", "")
        # v4.5.0: Capture-spezifische URL
        try:
            from db import get_setting as _gs
            capture_public_url = _gs("capture_public_url", "")
        except Exception:
            capture_public_url = os.environ.get("CAPTURE_PUBLIC_URL", "")
        # v6.5.0: IPPS (Cloud Print über HTTPS/IPP-Protokoll)
        # v6.6.0: LPR komplett entfernt — IPPS ist der einzige Cloud-Print-Eingang.
        try:
            from db import get_setting as _gs2
            ipps_public_url = _gs2("ipps_public_url", "")
            ipps_port       = _gs2("ipps_port", "") or os.environ.get("IPP_PORT", "8080")
        except Exception:
            ipps_public_url = ""
            ipps_port       = os.environ.get("IPP_PORT", "8080")
        parsed_ipps = urlparse(ipps_public_url) if ipps_public_url else None
        ipps_public_host = (parsed_ipps.hostname if parsed_ipps else "") or ""

        # v6.7.25: Globales Mail-Fallback. API-Key wird verschlüsselt
        # gespeichert — wir geben nur ein has_key-Flag ans Template, nie den
        # Klartext (gleicher Schutz wie beim entra_client_secret).
        try:
            has_global_mail_key   = bool(_gs2("global_mail_api_key", ""))
            global_mail_from      = _gs2("global_mail_from", "")
            global_mail_from_name = _gs2("global_mail_from_name", "")
        except Exception:
            has_global_mail_key = False
            global_mail_from = global_mail_from_name = ""
        # Entra-Konfiguration
        try:
            from db import get_setting as gs
            entra_cfg = {
                "enabled":      gs("entra_enabled", "0") == "1",
                "tenant_id":    gs("entra_tenant_id", ""),
                "client_id":    gs("entra_client_id", ""),
                "has_secret":   bool(gs("entra_client_secret", "")),
                "auto_approve": gs("entra_auto_approve", "0") == "1",
            }
        except Exception:
            entra_cfg = {"enabled": False, "tenant_id": "", "client_id": "",
                         "has_secret": False, "auto_approve": False}
        # Gespeicherte Redirect URI (aus Auto-Setup oder manuell gesetzt)
        try:
            saved_redirect = gs("entra_redirect_uri", "")
        except Exception:
            saved_redirect = ""
        if not saved_redirect:
            base = _get_base_url(request)
            saved_redirect = f"{base}/auth/entra/callback"
        return {
            "request": request, "user": user,
            "public_url": public_url,
            "capture_public_url": capture_public_url,
            "ipps_public_url": ipps_public_url,
            "ipps_public_host": ipps_public_host,
            "ipps_port": str(ipps_port),
            "has_global_mail_key": has_global_mail_key,
            "global_mail_from": global_mail_from,
            "global_mail_from_name": global_mail_from_name,
            "entra": entra_cfg,
            "entra_redirect_uri": saved_redirect,
            "auto_setup_success": auto_setup_success,
            "backups": backups,
            "backup_success": backup_success,
            "backup_error": backup_error,
            "restore_success": restore_success,
            "saved": saved, "error": error,
            **t_ctx(request),
        }

    @app.get("/admin/settings", response_class=HTMLResponse)
    async def admin_settings_get(request: Request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        return templates.TemplateResponse("admin_settings.html",
            _admin_settings_ctx(request, user))

    @app.post("/admin/settings", response_class=HTMLResponse)
    async def admin_settings_post(
        request:              Request,
        public_url:           str = Form(default=""),
        capture_public_url:   str = Form(default=""),
        entra_enabled:        str = Form(default=""),
        entra_tenant_id:      str = Form(default=""),
        entra_client_id:      str = Form(default=""),
        entra_client_secret:  str = Form(default=""),
        entra_auto_approve:   str = Form(default=""),
        entra_redirect_uri:   str = Form(default=""),
        ipps_public_url:      str = Form(default=""),
        ipps_port:            str = Form(default=""),
        global_mail_api_key:   str = Form(default=""),
        global_mail_from:      str = Form(default=""),
        global_mail_from_name: str = Form(default=""),
    ):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)

        url = public_url.strip().rstrip("/")
        capture_url = capture_public_url.strip().rstrip("/")
        try:
            from db import set_setting, _enc, audit
            set_setting("public_url", url)
            set_setting("capture_public_url", capture_url)

            # v6.5.0: Cloud Print / IPPS (v6.6.0: LPR entfernt)
            set_setting("ipps_public_url", ipps_public_url.strip().rstrip("/"))
            set_setting("ipps_port", ipps_port.strip() or "")

            # v6.7.25: Globales Mail-Fallback. API-Key nur überschreiben wenn
            # ein neuer Wert eingegeben wurde (wie bei entra_client_secret).
            if global_mail_api_key.strip():
                set_setting("global_mail_api_key", _enc(global_mail_api_key.strip()))
            set_setting("global_mail_from",      global_mail_from.strip())
            set_setting("global_mail_from_name", global_mail_from_name.strip())

            # Entra-Settings speichern
            set_setting("entra_enabled", "1" if entra_enabled else "0")
            set_setting("entra_tenant_id", entra_tenant_id.strip())
            set_setting("entra_client_id", entra_client_id.strip())
            set_setting("entra_auto_approve", "1" if entra_auto_approve else "0")
            # Secret nur überschreiben wenn neuer Wert eingegeben wurde
            if entra_client_secret.strip():
                set_setting("entra_client_secret", _enc(entra_client_secret.strip()))
            # Redirect URI speichern (muss mit Azure App Registration übereinstimmen)
            if entra_redirect_uri.strip():
                set_setting("entra_redirect_uri", entra_redirect_uri.strip().rstrip("/"))

            changes = [f"public_url={url}"]
            if capture_url:
                changes.append(f"capture_public_url={capture_url}")
            if entra_enabled:
                changes.append("entra=aktiviert")
            audit(user["id"], "admin_settings", ", ".join(changes))
        except Exception as e:
            logger.error("Admin-Settings-Fehler: %s", e)
            return templates.TemplateResponse("admin_settings.html",
                _admin_settings_ctx(request, user, error=str(e)))

        return templates.TemplateResponse("admin_settings.html",
            _admin_settings_ctx(request, user, saved=True))

    @app.post("/admin/settings/backup/create", response_class=HTMLResponse)
    async def admin_backup_create(request: Request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from backup_manager import create_backup
            from db import audit
            result = create_backup()
            audit(user["id"], "backup_create", f"Backup erstellt: {result['filename']}")
            return templates.TemplateResponse(
                "admin_settings.html",
                _admin_settings_ctx(request, user, backup_success=result),
            )
        except Exception as e:
            logger.error("Backup-Erstellung fehlgeschlagen: %s", e, exc_info=True)
            return templates.TemplateResponse(
                "admin_settings.html",
                _admin_settings_ctx(request, user, backup_error=str(e)),
            )

    @app.get("/admin/settings/backups/{filename}")
    async def admin_backup_download(filename: str, request: Request):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        try:
            from backup_manager import resolve_backup_path
            path = resolve_backup_path(filename)
        except Exception:
            return RedirectResponse("/admin/settings", status_code=302)
        return FileResponse(path, filename=path.name, media_type="application/zip")

    @app.post("/admin/settings/backup/restore", response_class=HTMLResponse)
    async def admin_backup_restore(
        request: Request,
        backup_zip: UploadFile = File(...),
    ):
        user = get_session_user(request)
        if not user or not user.get("is_admin"):
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        _ = tc["_"]
        if not backup_zip.filename or not backup_zip.filename.lower().endswith(".zip"):
            return templates.TemplateResponse(
                "admin_settings.html",
                _admin_settings_ctx(request, user, backup_error=_("backup_restore_invalid_file")),
            )
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(prefix="printix-restore-", suffix=".zip", delete=False) as tmp:
                tmp_path = tmp.name
                tmp.write(await backup_zip.read())
            from backup_manager import restore_backup
            result = restore_backup(tmp_path)
            return templates.TemplateResponse(
                "admin_settings.html",
                _admin_settings_ctx(request, user, restore_success=result),
            )
        except Exception as e:
            logger.error("Backup-Restore fehlgeschlagen: %s", e, exc_info=True)
            return templates.TemplateResponse(
                "admin_settings.html",
                _admin_settings_ctx(request, user, backup_error=str(e)),
            )
        finally:
            try:
                if tmp_path and os.path.exists(tmp_path):
                    os.unlink(tmp_path)
            except Exception:
                pass


    # ─── Logs ────────────────────────────────────────────────────────────────────

    @app.get("/logs", response_class=HTMLResponse)
    async def logs_get(
        request:   Request,
        min_level: str = "DEBUG",
        category:  str = "",
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        try:
            from db import get_tenant_by_user_id, get_tenant_logs
            tenant = get_tenant_by_user_id(user["id"])
            tid    = tenant["id"] if tenant else None
            entries = get_tenant_logs(tid, min_level=min_level, category=category) if tid else []
        except Exception as e:
            logger.error("Logs-Fehler: %s", e)
            entries = []
            tid = None
        # UTC → Lokale Zeit konvertieren (TZ-Umgebungsvariable, Fallback Europe/Berlin)
        try:
            import datetime as _dt
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI(os.environ.get("TZ", "Europe/Berlin"))
            for _e in entries:
                try:
                    _ts = _dt.datetime.fromisoformat(_e["timestamp"])
                    if _ts.tzinfo:
                        _e["timestamp"] = _ts.astimezone(_tz).isoformat()
                except Exception:
                    pass
        except Exception:
            pass
        return templates.TemplateResponse("logs.html", {
            "request": request, "user": user,
            "entries": entries,
            "min_level": min_level.upper(),
            "levels": ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
            "category": category.upper(),
            "categories": ["", "PRINTIX_API", "SQL", "AUTH", "CAPTURE", "SYSTEM"],
            **tc,
        })

    @app.post("/logs/clear", response_class=JSONResponse)
    async def logs_clear(request: Request):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
        try:
            from db import get_tenant_by_user_id, clear_tenant_logs
            tenant = get_tenant_by_user_id(user["id"])
            if not tenant:
                return JSONResponse({"ok": False, "error": "no tenant"})
            n = clear_tenant_logs(tenant["id"])
            return JSONResponse({"ok": True, "deleted": n})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)})


    # ─── Tenant: Printers / Queues / Users+Cards ─────────────────────────────────

    def _make_printix_client(tenant: dict):
        """Erstellt einen PrintixClient aus Tenant-Credentials (Full-Record mit Secrets)."""
        import sys, os
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from printix_client import PrintixClient
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

    # ─── Tenant-Cache (v6.1.0) ──────────────────────────────────────────
    # Nutzt das zentrale cache-Modul. Siehe src/cache.py für Details.
    import sys as _sys_cache, os as _os_cache
    _sys_cache.path.insert(0, _os_cache.path.dirname(_os_cache.path.dirname(_os_cache.path.abspath(__file__))))
    from cache import tenant_cache as _tcache, format_age as _format_age

    def _cached_users(tenant: dict, client, query: str = "") -> list[dict]:
        """Liefert list_all_users() über den zentralen Cache.

        Query ist Teil des Cache-Keys weil der `query=`-Parameter an
        Printix geht und das Ergebnis dort bereits filtert. Ohne Query
        haben wir die Vollliste die wir lokal weiterfiltern können.
        """
        topic = "users" if not query else f"users:q={query.strip().lower()}"
        return _tcache.get(
            tenant.get("id", ""), topic,
            loader=lambda: client.list_all_users(
                query=query or None, page_size=200,
            ),
        )

    async def _load_card_counts_parallel(client, tenant_id: str,
                                          user_ids: list[str]) -> dict[str, int]:
        """Lädt list_user_cards() für viele User parallel via TenantCache.

        Cache-Hits auf "cards_per_user"-Sub-Keys werden sofort zurückgegeben,
        Misses laufen via asyncio.gather gleichzeitig.
        """
        import asyncio as _aio
        results: dict[str, int] = {}
        to_fetch: list[str] = []

        for uid in user_ids:
            if not uid:
                continue
            # Sub-Key Cache-Hit? (TTL aus DEFAULT_TTLS["cards_per_user"])
            cached = None
            ts = None
            try:
                # Direkter Zugriff über das interne _sub Dict wäre schneller,
                # aber um die API sauber zu halten nutzen wir is_fresh via age.
                from cache import DEFAULT_TTLS as _TTLS
                age_key = (tenant_id, "cards_per_user", uid)
                hit = _tcache._sub.get(age_key)
                if hit:
                    import time as _time
                    if (_time.time() - hit[0]) < _TTLS.get("cards_per_user", 900):
                        results[uid] = hit[1]
                        continue
            except Exception:
                pass
            to_fetch.append(uid)

        if not to_fetch:
            return results

        async def _count(uid: str) -> tuple[str, int]:
            try:
                data = await _aio.to_thread(lambda u=uid: client.list_user_cards(u))
                raw = data.get("cards", data.get("content", [])) if isinstance(data, dict) else []
                n = len(raw) if isinstance(raw, list) else 0
            except Exception:
                n = 0
            return uid, n

        pairs = await _aio.gather(*(_count(uid) for uid in to_fetch))
        import time as _time
        now = _time.time()
        for uid, n in pairs:
            results[uid] = n
            _tcache._sub[(tenant_id, "cards_per_user", uid)] = (now, n)
        return results

    def _invalidate_tenant_user_cache(tenant_id: str) -> None:
        """Entfernt User-Cache + alle Sub-Einträge (card counts).

        Alias für Backwards-Compat während der Umstellung — neue Routen
        sollten direkt _tcache.invalidate() verwenden.
        """
        if not tenant_id:
            return
        # Alle "users" und "users:q=..." Einträge dieses Tenants entfernen
        for k in list(_tcache._data.keys()):
            if k[0] == tenant_id and (k[1] == "users" or k[1].startswith("users:")):
                _tcache._data.pop(k, None)
        # Sub-Einträge cards_per_user mitinvalidieren weil Karten-Zähler
        # zusammen mit User-Liste angezeigt werden
        _tcache.invalidate(tenant_id, "cards_per_user")

    def _cache_meta(tenant_id: str, topic: str) -> dict:
        """Kleiner Helper für Templates: liefert Alter + formatiertes Alter."""
        age = _tcache.age_seconds(tenant_id, topic)
        return {
            "age_s": age,
            "age_str": _format_age(age) if age is not None else "",
            "loaded_at": _tcache.last_refreshed(tenant_id, topic),
        }

    # Generische Cache-Wrapper — werden in den Tenant-Routen genutzt ────
    def _cached_printers(tenant: dict, client, search: str = ""):
        """list_printers(size=200) via Cache. Search-Param nicht im Key —
        lokale Filterung ist schneller als erneuter API-Call."""
        topic = "printers"
        data = _tcache.get(
            tenant.get("id", ""), topic,
            loader=lambda: client.list_printers(size=200),
        )
        return data

    def _cached_workstations(tenant: dict, client):
        return _tcache.get(
            tenant.get("id", ""), "workstations",
            loader=lambda: client.list_workstations(size=200),
        )

    def _cached_networks(tenant: dict, client):
        return _tcache.get(
            tenant.get("id", ""), "networks",
            loader=lambda: client.list_networks(size=200),
        )

    def _cached_sites(tenant: dict, client):
        return _tcache.get(
            tenant.get("id", ""), "sites",
            loader=lambda: client.list_sites(size=200),
        )

    def _cached_snmp_configs(tenant: dict, client):
        return _tcache.get(
            tenant.get("id", ""), "snmp",
            loader=lambda: client.list_snmp_configs(size=200),
        )

    def _cached_groups(tenant: dict, client):
        return _tcache.get(
            tenant.get("id", ""), "groups",
            loader=lambda: client.list_groups(size=200),
        )

    def _paged_items(data, *keys: str) -> list[dict]:
        if not isinstance(data, dict):
            return []
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
        content = data.get("content")
        return content if isinstance(content, list) else []

    def _extract_resource_id(item: dict) -> str:
        if not isinstance(item, dict):
            return ""
        rid = item.get("id")
        if rid:
            return str(rid)
        href = ((item.get("_links") or {}).get("self") or {}).get("href", "")
        return href.rstrip("/").split("/")[-1] if href else ""

    def _clean_optional(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value or None

    def _split_csv(value: Optional[str]) -> list[str]:
        if not value:
            return []
        return [part.strip() for part in value.split(",") if part.strip()]

    def _extract_printer_queue_pairs(raw_items: list[dict]) -> list[dict]:
        import re as _re
        pairs: list[dict] = []
        for item in raw_items if isinstance(raw_items, list) else []:
            href = (item.get("_links") or {}).get("self", {}).get("href", "")
            match = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
            printer_id = match.group(1) if match else item.get("id", "")
            queue_id = match.group(2) if match else ""
            vendor = item.get("vendor", "")
            model = item.get("model", "")
            printer_name = f"{vendor} {model}".strip() if (vendor or model) else item.get("name", "")
            pairs.append({
                "raw": item,
                "printer_id": printer_id,
                "queue_id": queue_id,
                "queue_name": item.get("name", ""),
                "printer_name": printer_name or item.get("name", ""),
                "vendor": vendor,
                "model": model,
                "location": item.get("location", ""),
                "status": item.get("connectionStatus", ""),
                "printerSignId": item.get("printerSignId", ""),
                "type": item.get("type", item.get("queueType", "")),
            })
        return pairs

    @app.get("/tenant", response_class=HTMLResponse)
    async def tenant_overview(request: Request):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        stats = {
            "printer_count": 0,
            "queue_count": 0,
            "user_count": 0,
            "guest_count": 0,
            "workstation_count": 0,
            "active_workstation_count": 0,
        }
        status = {
            "print_api": {"enabled": False, "state": "missing"},
            "card_api": {"enabled": False, "state": "missing"},
            "workstation_api": {"enabled": False, "state": "missing"},
            "sql": {"enabled": False, "state": "missing"},
        }
        warnings: list[str] = []
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if not tenant:
                warnings.append("common_no_tenant")
            else:
                status["print_api"]["enabled"] = bool(tenant.get("print_client_id") or tenant.get("shared_client_id"))
                status["card_api"]["enabled"] = bool(tenant.get("card_client_id") or tenant.get("shared_client_id"))
                status["workstation_api"]["enabled"] = bool(tenant.get("ws_client_id") or tenant.get("shared_client_id"))
                status["sql"]["enabled"] = bool(tenant.get("sql_server"))
                status["print_api"]["state"] = "configured" if status["print_api"]["enabled"] else "missing"
                status["card_api"]["state"] = "configured" if status["card_api"]["enabled"] else "missing"
                status["workstation_api"]["state"] = "configured" if status["workstation_api"]["enabled"] else "missing"
                status["sql"]["state"] = "configured" if status["sql"]["enabled"] else "missing"
                if status["print_api"]["enabled"] or status["card_api"]["enabled"] or status["workstation_api"]["enabled"]:
                    client = _make_printix_client(tenant)
                    if status["print_api"]["enabled"]:
                        try:
                            printers_data = client.list_printers(size=100)
                            raw_printers = printers_data.get("printers", printers_data.get("content", []))
                            import re as _re
                            printer_ids = set()
                            queue_ids = set()
                            for p in raw_printers if isinstance(raw_printers, list) else []:
                                href = (p.get("_links") or {}).get("self", {}).get("href", "")
                                m = _re.search(r"/printers/([^/]+)/queues/([^/?]+)", href)
                                pid = m.group(1) if m else p.get("id", "")
                                qid = m.group(2) if m else ""
                                if pid:
                                    printer_ids.add(pid)
                                if qid:
                                    queue_ids.add(qid)
                            stats["printer_count"] = len(printer_ids)
                            stats["queue_count"] = len(queue_ids)
                        except Exception as e:
                            logger.warning("tenant_overview printers unavailable: %s", e)
                            warnings.append("tenant_overview_warn_print")
                    if status["card_api"]["enabled"]:
                        try:
                            regular = client.list_users(role="USER", page_size=200)
                            guests = client.list_users(role="GUEST_USER", page_size=200)
                            reg_users = regular.get("users", regular.get("content", []))
                            guest_users = guests.get("users", guests.get("content", []))
                            stats["user_count"] = len(reg_users) if isinstance(reg_users, list) else 0
                            stats["guest_count"] = len(guest_users) if isinstance(guest_users, list) else 0
                        except Exception as e:
                            logger.warning("tenant_overview users unavailable: %s", e)
                            warnings.append("tenant_overview_warn_users")
                    if status["workstation_api"]["enabled"]:
                        try:
                            workstations_data = client.list_workstations(size=200)
                            raw_workstations = workstations_data.get("workstations", workstations_data.get("content", []))
                            if isinstance(raw_workstations, list):
                                stats["workstation_count"] = len(raw_workstations)
                                stats["active_workstation_count"] = sum(1 for ws in raw_workstations if ws.get("active"))
                        except Exception as e:
                            logger.warning("tenant_overview workstations unavailable: %s", e)
                            warnings.append("tenant_overview_warn_workstations")
                if status["sql"]["enabled"]:
                    try:
                        from reporting.sql_client import set_config_from_tenant, query_fetchone
                        set_config_from_tenant(tenant)
                        probe = query_fetchone("SELECT 1 AS ok")
                        status["sql"]["state"] = "connected" if probe else "configured"
                    except Exception as e:
                        msg = str(e).lower()
                        if "40615" in msg or "not allowed to access the server" in msg or "firewall" in msg:
                            status["sql"]["state"] = "blocked"
                            warnings.append("tenant_overview_warn_sql_firewall")
                        else:
                            status["sql"]["state"] = "issue"
                            warnings.append("tenant_overview_warn_sql_issue")
        except Exception as e:
            logger.error("tenant_overview error: %s", e)
            warnings.append(str(e))
        return templates.TemplateResponse("tenant_overview.html", {
            "request": request,
            "user": user,
            "stats": stats,
            "status": status,
            "warnings": warnings,
            "active_tab": "overview",
            **tc,
        })

    # ─── Cache-Refresh (v6.1.0) ──────────────────────────────────────────
    @app.post("/tenant/refresh")
    async def tenant_refresh(request: Request,
                              topic: str = Form("all"),
                              back: str = Form("/tenant")):
        """Invalidiert Cache-Einträge und leitet zurück zur aufrufenden Seite.

        topic:
          "all"            → alle Topics dieses Tenants (globaler Refresh-Button)
          "users"/"printers"/"queues"/"workstations"/... → nur dieses Topic

        back: Pfad der zurück-URL (muss mit / beginnen und nicht // — simple
              Anti-Open-Redirect-Sicherung).
        """
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_by_user_id
            t = get_tenant_by_user_id(user["id"])
            if t:
                target_topic = None if (topic or "all").lower() == "all" else topic.strip()
                _tcache.invalidate(t.get("id", ""), target_topic)
        except Exception as _re:
            logger.warning("tenant_refresh error: %s", _re)
        # Anti-Open-Redirect: nur relative Pfade zulassen
        safe = back if (back.startswith("/") and not back.startswith("//")) else "/tenant"
        # Ensure fragment is stripped and only go to same app paths
        if "://" in safe:
            safe = "/tenant"
        return RedirectResponse(safe, status_code=303)

    @app.get("/tenant/printers", response_class=HTMLResponse)
    async def tenant_printers(request: Request, search: str = ""):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        printers = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                data = _cached_printers(tenant, client, search)
                raw = _paged_items(data, "printers")
                # v6.1.0: Search lokal nach dem Cache-Hit — so erspart man sich
                # den teuren API-Call bei jeder Filter-Änderung.
                if search:
                    s = search.strip().lower()
                    raw = [p for p in raw if s in (p.get("name") or "").lower()
                           or s in (p.get("location") or "").lower()
                           or s in (p.get("vendor") or "").lower()
                           or s in (p.get("model") or "").lower()]
                # Deduplizieren nach printer_id – jedes Item im API-Response ist ein
                # Printer-Queue-Paar. Queues desselben Druckers werden gruppiert.
                printer_map = {}
                for p in _extract_printer_queue_pairs(raw):
                    pid = p.get("printer_id", "")
                    qid = p.get("queue_id", "")
                    if pid not in printer_map:
                        printer_map[pid] = {
                            "printer_id":    pid,
                            "name":          p.get("printer_name", ""),
                            "vendor":        p.get("vendor", ""),
                            "location":      p.get("location", ""),
                            "status":        p.get("status", ""),
                            "printerSignId": p.get("printerSignId", ""),
                            "queues":        [],
                        }
                    printer_map[pid]["queues"].append({
                        "name":     p.get("queue_name", ""),
                        "queue_id": qid,
                        "detail_url": f"/tenant/queues/{pid}/{qid}" if pid and qid else "",
                    })
                printers = list(printer_map.values())
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_printers error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_printers.html", {
            "request": request, "user": user,
            "printers": printers, "search": search, "error": error,
            "active_tab": "printers", **tc,
        })

    @app.get("/tenant/queues", response_class=HTMLResponse)
    async def tenant_queues(request: Request, search: str = ""):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        queues = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                # v6.1.0: Gemeinsamer Cache mit /tenant/printers
                data = _cached_printers(tenant, client, search)
                raw = _paged_items(data, "printers")
                if search:
                    s = search.strip().lower()
                    raw = [p for p in raw if s in (p.get("name") or "").lower()
                           or s in (p.get("location") or "").lower()]
                for p in _extract_printer_queue_pairs(raw):
                    queues.append({
                        "queue_name":   p.get("queue_name", ""),
                        "queue_id":     p.get("queue_id", ""),
                        "printer_name": p.get("printer_name", ""),
                        "printer_id":   p.get("printer_id", ""),
                        "location":     p.get("location", ""),
                        "status":       p.get("status", ""),
                        "queue_type":   p.get("type", ""),
                    })
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_queues error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_queues.html", {
            "request": request, "user": user,
            "queues": queues, "search": search, "error": error,
            "active_tab": "queues", **tc,
        })

    @app.get("/tenant/printers/{printer_id}", response_class=HTMLResponse)
    async def tenant_printer_detail(request: Request, printer_id: str, queue_id: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        printer = None
        detail = None
        recent_jobs = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                raw_pairs = _extract_printer_queue_pairs(_paged_items(client.list_printers(size=200), "printers"))
                printer_pairs = [pair for pair in raw_pairs if pair.get("printer_id") == printer_id]
                if not printer_pairs:
                    error = "tenant_printer_not_found"
                else:
                    selected_queue_id = queue_id or printer_pairs[0].get("queue_id", "")
                    if selected_queue_id:
                        detail_raw = client.get_printer(printer_id, selected_queue_id)
                        detail_items = _paged_items(detail_raw, "printers")
                        detail = detail_items[0] if detail_items else detail_raw
                        try:
                            jobs_data = client.list_print_jobs(queue_id=selected_queue_id, size=8)
                            recent_jobs = _paged_items(jobs_data, "jobs")
                        except Exception as jobs_error:
                            logger.warning("tenant_printer_detail jobs unavailable: %s", jobs_error)
                    primary = printer_pairs[0]
                    printer = {
                        "printer_id": printer_id,
                        "name": primary.get("printer_name", ""),
                        "vendor": primary.get("vendor", ""),
                        "model": primary.get("model", ""),
                        "location": primary.get("location", ""),
                        "status": primary.get("status", ""),
                        "printerSignId": primary.get("printerSignId", ""),
                        "selected_queue_id": selected_queue_id,
                        "queues": [
                            {
                                "queue_id": pair.get("queue_id", ""),
                                "name": pair.get("queue_name", ""),
                                "status": pair.get("status", ""),
                                "detail_url": f"/tenant/queues/{printer_id}/{pair.get('queue_id', '')}",
                            }
                            for pair in printer_pairs
                        ],
                    }
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_printer_detail error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_printer_detail.html", {
            "request": request,
            "user": user,
            "printer": printer,
            "detail": detail,
            "detail_json": json.dumps(detail or {}, indent=2, ensure_ascii=False),
            "recent_jobs": recent_jobs,
            "error": error,
            "active_tab": "printers",
            **tc,
        })

    @app.get("/tenant/queues/{printer_id}/{queue_id}", response_class=HTMLResponse)
    async def tenant_queue_detail(request: Request, printer_id: str, queue_id: str):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        queue = None
        detail = None
        recent_jobs = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                raw_pairs = _extract_printer_queue_pairs(_paged_items(client.list_printers(size=200), "printers"))
                queue_pair = next((pair for pair in raw_pairs if pair.get("printer_id") == printer_id and pair.get("queue_id") == queue_id), None)
                if not queue_pair:
                    error = "tenant_queue_not_found"
                else:
                    detail_raw = client.get_printer(printer_id, queue_id)
                    detail_items = _paged_items(detail_raw, "printers")
                    detail = detail_items[0] if detail_items else detail_raw
                    queue = {
                        "printer_id": printer_id,
                        "queue_id": queue_id,
                        "name": queue_pair.get("queue_name", ""),
                        "printer_name": queue_pair.get("printer_name", ""),
                        "vendor": queue_pair.get("vendor", ""),
                        "model": queue_pair.get("model", ""),
                        "location": queue_pair.get("location", ""),
                        "status": queue_pair.get("status", ""),
                        "queue_type": queue_pair.get("type", ""),
                        "printer_detail_url": f"/tenant/printers/{printer_id}?queue_id={queue_id}",
                    }
                    try:
                        jobs_data = client.list_print_jobs(queue_id=queue_id, size=8)
                        recent_jobs = _paged_items(jobs_data, "jobs")
                    except Exception as jobs_error:
                        logger.warning("tenant_queue_detail jobs unavailable: %s", jobs_error)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_queue_detail error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_queue_detail.html", {
            "request": request,
            "user": user,
            "queue": queue,
            "detail": detail,
            "detail_json": json.dumps(detail or {}, indent=2, ensure_ascii=False),
            "recent_jobs": recent_jobs,
            "error": error,
            "active_tab": "queues",
            **tc,
        })

    @app.get("/tenant/sites", response_class=HTMLResponse)
    async def tenant_sites(request: Request, search: str = "", flash: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        sites = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                data = client.list_sites(search=search or None, size=200)
                for site in _paged_items(data, "sites"):
                    site["site_id"] = _extract_resource_id(site)
                    site["network_count"] = len(site.get("networkIds", []) or [])
                    site["admin_group_count"] = len(site.get("adminGroupIds", []) or [])
                    sites.append(site)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_sites error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_sites.html", {
            "request": request, "user": user,
            "sites": sites, "search": search, "flash": flash, "error": error,
            "active_tab": "sites", **tc,
        })

    @app.get("/tenant/sites/create", response_class=HTMLResponse)
    async def tenant_site_create_get(request: Request):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        groups = []
        networks = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                groups = _paged_items(client.list_groups(size=200), "groups")
                networks = _paged_items(client.list_networks(size=200), "networks")
                for item in groups + networks:
                    item["id"] = _extract_resource_id(item)
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_site_create_get error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_site_detail.html", {
            "request": request, "user": user, "site": None,
            "groups": groups, "networks": networks,
            "selected_admin_group_ids": [], "selected_network_ids": [],
            "flash": "", "error": error, "active_tab": "sites", **tc,
        })

    @app.post("/tenant/sites/create")
    async def tenant_site_create_post(
        request: Request,
        name: str = Form(...),
        path: str = Form(...),
        admin_group_ids: list[str] = Form([]),
        network_ids: list[str] = Form([]),
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        from urllib.parse import quote_plus as _qp
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            created = client.create_site(
                name=name.strip(),
                path=path.strip(),
                admin_group_ids=[x for x in admin_group_ids if x],
                network_ids=[x for x in network_ids if x],
            )
            site_id = _extract_resource_id(created)
            target = f"/tenant/sites/{site_id}?flash=created" if site_id else "/tenant/sites?flash=created"
            return RedirectResponse(target, status_code=302)
        except Exception as e:
            return RedirectResponse(f"/tenant/sites?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.get("/tenant/sites/{site_id}", response_class=HTMLResponse)
    async def tenant_site_detail(request: Request, site_id: str, flash: str = "", errmsg: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        site = None
        groups = []
        networks = []
        error = errmsg or None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                site = client.get_site(site_id)
                if isinstance(site, dict):
                    site["site_id"] = _extract_resource_id(site)
                groups = _paged_items(client.list_groups(size=200), "groups")
                networks = _paged_items(client.list_networks(size=200), "networks")
                for item in groups + networks:
                    item["id"] = _extract_resource_id(item)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_site_detail error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_site_detail.html", {
            "request": request, "user": user,
            "site": site, "groups": groups, "networks": networks,
            "selected_admin_group_ids": (site or {}).get("adminGroupIds", []) or [],
            "selected_network_ids": (site or {}).get("networkIds", []) or [],
            "selected_admin_group_names": [
                item.get("name") or item.get("id")
                for item in groups
                if item.get("id") in ((site or {}).get("adminGroupIds", []) or [])
            ],
            "selected_network_names": [
                item.get("name") or item.get("id")
                for item in networks
                if item.get("id") in ((site or {}).get("networkIds", []) or [])
            ],
            "detail_json": json.dumps(site or {}, indent=2, ensure_ascii=False),
            "flash": flash, "error": error, "active_tab": "sites", **tc,
        })

    @app.post("/tenant/sites/{site_id}")
    async def tenant_site_update_post(
        request: Request,
        site_id: str,
        name: str = Form(...),
        path: str = Form(...),
        admin_group_ids: list[str] = Form([]),
        network_ids: list[str] = Form([]),
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        from urllib.parse import quote_plus as _qp
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            client.update_site(
                site_id,
                name=name.strip(),
                path=path.strip(),
                admin_group_ids=[x for x in admin_group_ids if x],
                network_ids=[x for x in network_ids if x],
            )
            return RedirectResponse(f"/tenant/sites/{site_id}?flash=updated", status_code=302)
        except Exception as e:
            return RedirectResponse(f"/tenant/sites/{site_id}?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.post("/tenant/sites/{site_id}/delete")
    async def tenant_site_delete_post(request: Request, site_id: str):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            client.delete_site(site_id)
            return RedirectResponse("/tenant/sites?flash=deleted", status_code=302)
        except Exception as e:
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(f"/tenant/sites?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.get("/tenant/networks", response_class=HTMLResponse)
    async def tenant_networks(request: Request, site_id: str = "", flash: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        networks = []
        sites = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                sites = _paged_items(client.list_sites(size=200), "sites")
                for site in sites:
                    site["site_id"] = _extract_resource_id(site)
                data = client.list_networks(site_id=site_id or None, size=200)
                for network in _paged_items(data, "networks"):
                    network["network_id"] = _extract_resource_id(network)
                    networks.append(network)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_networks error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_networks.html", {
            "request": request, "user": user,
            "networks": networks, "sites": sites, "site_filter": site_id, "flash": flash, "error": error,
            "active_tab": "networks", **tc,
        })

    @app.get("/tenant/networks/create", response_class=HTMLResponse)
    async def tenant_network_create_get(request: Request):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        sites = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                sites = _paged_items(client.list_sites(size=200), "sites")
                for site in sites:
                    site["site_id"] = _extract_resource_id(site)
            else:
                error = "no_print_creds"
        except Exception as e:
            error = str(e)
        return templates.TemplateResponse("tenant_network_detail.html", {
            "request": request, "user": user, "network": None,
            "sites": sites, "flash": "", "error": error,
            "detail_json": json.dumps({}, indent=2, ensure_ascii=False),
            "active_tab": "networks", **tc,
        })

    @app.post("/tenant/networks/create")
    async def tenant_network_create_post(
        request: Request,
        name: str = Form(...),
        site_id: str = Form(""),
        gateway_mac: str = Form(""),
        gateway_ip: str = Form(""),
        client_migrate_print_queues: str = Form("GLOBAL_SETTING"),
        home_office: Optional[str] = Form(None),
        air_print: Optional[str] = Form(None),
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        from urllib.parse import quote_plus as _qp
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            created = client.create_network(
                name=name.strip(),
                home_office=bool(home_office),
                client_migrate_print_queues=(client_migrate_print_queues or "GLOBAL_SETTING").strip().upper(),
                air_print=bool(air_print),
                site_id=_clean_optional(site_id),
                gateway_mac=_clean_optional(gateway_mac),
                gateway_ip=_clean_optional(gateway_ip),
            )
            network_id = _extract_resource_id(created)
            target = f"/tenant/networks/{network_id}?flash=created" if network_id else "/tenant/networks?flash=created"
            return RedirectResponse(target, status_code=302)
        except Exception as e:
            return RedirectResponse(f"/tenant/networks?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.get("/tenant/networks/{network_id}", response_class=HTMLResponse)
    async def tenant_network_detail(request: Request, network_id: str, flash: str = "", errmsg: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        network = None
        sites = []
        error = errmsg or None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                network = client.get_network(network_id)
                if isinstance(network, dict):
                    network["network_id"] = _extract_resource_id(network)
                sites = _paged_items(client.list_sites(size=200), "sites")
                for site in sites:
                    site["site_id"] = _extract_resource_id(site)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_network_detail error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_network_detail.html", {
            "request": request, "user": user,
            "network": network, "sites": sites,
            "network_site_name": next(
                (
                    site.get("name") or site.get("site_id")
                    for site in sites
                    if site.get("site_id") == ((network or {}).get("siteId") or "")
                ),
                "",
            ),
            "detail_json": json.dumps(network or {}, indent=2, ensure_ascii=False),
            "flash": flash, "error": error, "active_tab": "networks", **tc,
        })

    @app.post("/tenant/networks/{network_id}")
    async def tenant_network_update_post(
        request: Request,
        network_id: str,
        name: str = Form(...),
        subnet: str = Form(""),
        site_id: str = Form(""),
        client_migrate_print_queues: str = Form("GLOBAL_SETTING"),
        home_office: Optional[str] = Form(None),
        air_print: Optional[str] = Form(None),
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        from urllib.parse import quote_plus as _qp
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            client.update_network(
                network_id,
                name=name.strip(),
                subnet=_clean_optional(subnet),
                home_office=bool(home_office),
                client_migrate_print_queues=(client_migrate_print_queues or "GLOBAL_SETTING").strip().upper(),
                air_print=bool(air_print),
                site_id=_clean_optional(site_id),
            )
            return RedirectResponse(f"/tenant/networks/{network_id}?flash=updated", status_code=302)
        except Exception as e:
            return RedirectResponse(f"/tenant/networks/{network_id}?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.post("/tenant/networks/{network_id}/delete")
    async def tenant_network_delete_post(request: Request, network_id: str):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            client.delete_network(network_id)
            return RedirectResponse("/tenant/networks?flash=deleted", status_code=302)
        except Exception as e:
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(f"/tenant/networks?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.get("/tenant/snmp", response_class=HTMLResponse)
    async def tenant_snmp_list(request: Request, flash: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        snmp_configs = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                data = client.list_snmp_configs(size=200)
                for config in _paged_items(data, "snmp", "snmpConfigurations"):
                    config["snmp_id"] = _extract_resource_id(config)
                    snmp_configs.append(config)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_snmp_list error: %s", e)
            error = str(e)
        return templates.TemplateResponse("tenant_snmp.html", {
            "request": request, "user": user,
            "snmp_configs": snmp_configs, "flash": flash, "error": error,
            "active_tab": "snmp", **tc,
        })

    @app.get("/tenant/snmp/create", response_class=HTMLResponse)
    async def tenant_snmp_create_get(request: Request):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        networks = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                networks = _paged_items(client.list_networks(size=200), "networks")
                for network in networks:
                    network["network_id"] = _extract_resource_id(network)
            else:
                error = "no_print_creds"
        except Exception as e:
            error = str(e)
        return templates.TemplateResponse("tenant_snmp_detail.html", {
            "request": request, "user": user, "snmp_config": None,
            "networks": networks, "selected_network_ids": [],
            "detail_json": json.dumps({}, indent=2, ensure_ascii=False),
            "flash": "", "error": error, "active_tab": "snmp", **tc,
        })

    @app.post("/tenant/snmp/create")
    async def tenant_snmp_create_post(
        request: Request,
        name: str = Form(...),
        version: str = Form("V2C"),
        get_community_name: str = Form(""),
        set_community_name: str = Form(""),
        tenant_default: Optional[str] = Form(None),
        security_level: str = Form(""),
        username: str = Form(""),
        context_name: str = Form(""),
        authentication: str = Form(""),
        authentication_key: str = Form(""),
        privacy: str = Form(""),
        privacy_key: str = Form(""),
        network_ids: list[str] = Form([]),
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        from urllib.parse import quote_plus as _qp
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            created = client.create_snmp_config(
                name=name.strip(),
                get_community_name=_clean_optional(get_community_name),
                set_community_name=_clean_optional(set_community_name),
                tenant_default=bool(tenant_default),
                security_level=_clean_optional(security_level.upper() if security_level else security_level),
                version=(version or "V2C").strip().upper(),
                username=_clean_optional(username),
                context_name=_clean_optional(context_name),
                authentication=_clean_optional(authentication.upper() if authentication else authentication),
                authentication_key=_clean_optional(authentication_key),
                privacy=_clean_optional(privacy.upper() if privacy else privacy),
                privacy_key=_clean_optional(privacy_key),
                network_ids=[x for x in network_ids if x],
            )
            snmp_id = _extract_resource_id(created)
            target = f"/tenant/snmp/{snmp_id}?flash=created" if snmp_id else "/tenant/snmp?flash=created"
            return RedirectResponse(target, status_code=302)
        except Exception as e:
            return RedirectResponse(f"/tenant/snmp?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.get("/tenant/snmp/{config_id}", response_class=HTMLResponse)
    async def tenant_snmp_detail(request: Request, config_id: str, flash: str = "", errmsg: str = ""):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        snmp_config = None
        networks = []
        error = errmsg or None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("print_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                snmp_config = client.get_snmp_config(config_id)
                if isinstance(snmp_config, dict):
                    snmp_config["snmp_id"] = _extract_resource_id(snmp_config)
                networks = _paged_items(client.list_networks(size=200), "networks")
                for network in networks:
                    network["network_id"] = _extract_resource_id(network)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_print_creds"
        except Exception as e:
            logger.error("tenant_snmp_detail error: %s", e)
            error = str(e)
        selected_network_ids = []
        if snmp_config:
            selected_network_ids = list(snmp_config.get("networkIds", []) or [])
            if not selected_network_ids:
                for link in ((snmp_config.get("_links") or {}).get("networks") or []):
                    href = link.get("href", "")
                    if href:
                        selected_network_ids.append(href.rstrip("/").split("/")[-1])
        return templates.TemplateResponse("tenant_snmp_detail.html", {
            "request": request, "user": user,
            "snmp_config": snmp_config, "networks": networks,
            "selected_network_ids": selected_network_ids,
            "selected_network_names": [
                network.get("name") or network.get("network_id")
                for network in networks
                if network.get("network_id") in selected_network_ids
            ],
            "detail_json": json.dumps(snmp_config or {}, indent=2, ensure_ascii=False),
            "flash": flash, "error": error, "active_tab": "snmp", **tc,
        })

    @app.post("/tenant/snmp/{config_id}")
    async def tenant_snmp_update_post(
        request: Request,
        config_id: str,
        name: str = Form(...),
        version: str = Form("V2C"),
        get_community_name: str = Form(""),
        set_community_name: str = Form(""),
        tenant_default: Optional[str] = Form(None),
        security_level: str = Form(""),
        username: str = Form(""),
        context_name: str = Form(""),
        authentication: str = Form(""),
        authentication_key: str = Form(""),
        privacy: str = Form(""),
        privacy_key: str = Form(""),
        network_ids: list[str] = Form([]),
    ):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        from urllib.parse import quote_plus as _qp
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            client.update_snmp_config(
                config_id,
                name=name.strip(),
                get_community_name=_clean_optional(get_community_name),
                set_community_name=_clean_optional(set_community_name),
                tenant_default=bool(tenant_default),
                security_level=_clean_optional(security_level.upper() if security_level else security_level),
                version=(version or "V2C").strip().upper(),
                username=_clean_optional(username),
                context_name=_clean_optional(context_name),
                authentication=_clean_optional(authentication.upper() if authentication else authentication),
                authentication_key=_clean_optional(authentication_key),
                privacy=_clean_optional(privacy.upper() if privacy else privacy),
                privacy_key=_clean_optional(privacy_key),
                network_ids=[x for x in network_ids if x],
            )
            return RedirectResponse(f"/tenant/snmp/{config_id}?flash=updated", status_code=302)
        except Exception as e:
            return RedirectResponse(f"/tenant/snmp/{config_id}?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.post("/tenant/snmp/{config_id}/delete")
    async def tenant_snmp_delete_post(request: Request, config_id: str):
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            client.delete_snmp_config(config_id)
            return RedirectResponse("/tenant/snmp?flash=deleted", status_code=302)
        except Exception as e:
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(f"/tenant/snmp?flash=error&errmsg={_qp(str(e)[:160])}", status_code=302)

    @app.get("/tenant/users", response_class=HTMLResponse)
    async def tenant_users(request: Request, search: str = "", page: int = 0,
                           role: str = "all"):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        PAGE_SIZE = 10
        all_users = []
        error = None
        total_count = 0
        card_match_ids: set[str] = set()
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            has_user_creds = bool(
                tenant and (
                    tenant.get("um_client_id")
                    or tenant.get("card_client_id")
                    or tenant.get("shared_client_id")
                )
            )
            if tenant and has_user_creds:
                client = _make_printix_client(tenant)

                # v6.1.0: Zentraler TenantCache — bei Filter-Clicks oder
                # Pagination wird der Printix-API-Roundtrip eingespart.
                # TTL 10min, invalidiert beim Create/Delete oder manuell
                # über POST /tenant/refresh.
                all_users = _cached_users(tenant, client, search or "")

                # v5.20.0: Zusätzliche Karten-Suche in der lokalen card_mappings-DB.
                # Die Printix-`query`-Filter matcht nur auf Name/Email — wer nach
                # einer Kartennummer sucht, bekam bisher 0 Treffer.
                if search.strip():
                    try:
                        import sys as _sys, os as _os
                        src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                        if src_dir not in _sys.path:
                            _sys.path.insert(0, src_dir)
                        from cards.store import search_mappings
                        hits = search_mappings(tenant.get("printix_tenant_id", ""), search)
                        card_user_ids = {
                            h["printix_user_id"] for h in (hits or [])
                            if h.get("printix_user_id")
                        }
                        if card_user_ids:
                            known_ids = {u.get("id", "") for u in all_users}
                            missing = card_user_ids - known_ids
                            for uid in missing:
                                try:
                                    u = client.get_user(uid)
                                    if isinstance(u, dict) and u.get("id"):
                                        all_users.append(u)
                                        card_match_ids.add(uid)
                                except Exception as _ue:
                                    logger.debug(
                                        "tenant_users card-hit user lookup failed: %s", _ue
                                    )
                            # Auch bestehende Treffer markieren falls Karte matcht
                            for u in all_users:
                                if u.get("id") in card_user_ids:
                                    card_match_ids.add(u.get("id", ""))
                    except Exception as _ce:
                        logger.warning("tenant_users card search failed: %s", _ce)

                # v5.20.0: Role-Filter (all | user | guest) für die UI-Chips
                role_key = (role or "all").lower()
                if role_key == "user":
                    all_users = [u for u in all_users if u.get("role") == "USER"]
                elif role_key == "guest":
                    all_users = [u for u in all_users if u.get("role") == "GUEST_USER"]
                # Stabile Sortierung: USER vor GUEST_USER, dann nach Name
                all_users.sort(key=lambda u: (
                    0 if u.get("role") == "USER" else 1,
                    (u.get("fullName") or u.get("email") or "").lower(),
                ))

                total_count = len(all_users)

                # v4.6.11: Paginierung — nur aktuelle Seite anzeigen
                page = max(0, page)
                start = page * PAGE_SIZE
                page_users = all_users[start:start + PAGE_SIZE]

                # v6.0.0: Karten-Zähler parallel via asyncio.gather + 2min-Cache.
                # Statt 10× seriell list_user_cards() (~2s) laufen die Calls
                # gleichzeitig, Cache-Hits komplett ohne HTTP.
                page_uids = [u.get("id", "") for u in page_users if u.get("id")]
                card_counts = await _load_card_counts_parallel(
                    client, tenant.get("id", ""), page_uids
                )
                for u in page_users:
                    uid = u.get("id", "")
                    u["_card_count"] = card_counts.get(uid, 0)
                    # v5.20.0: Markierung für Treffer via Karten-Match
                    u["_card_match"] = uid in card_match_ids

                all_users = page_users
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_card_creds"
        except Exception as e:
            logger.error("tenant_users error: %s", e)
            error = str(e)

        total_pages = max(1, (total_count + PAGE_SIZE - 1) // PAGE_SIZE)
        # v6.1.0: Cache-Metadaten für das Refresh-Widget im Template
        _meta_topic = "users" if not search else f"users:q={search.strip().lower()}"
        return templates.TemplateResponse("tenant_users.html", {
            "request": request, "user": user,
            "users": all_users, "search": search, "error": error,
            "active_tab": "users",
            "page": page, "total_pages": total_pages, "total_count": total_count,
            "page_size": PAGE_SIZE,
            "card_matches": len(card_match_ids),
            "cache_meta_users": _cache_meta(tenant.get("id", "") if tenant else "", _meta_topic),
            **tc,
        })

    # ─── Printix Tenant: Workstations ─────────────────────────────────────────

    @app.get("/tenant/workstations", response_class=HTMLResponse)
    async def tenant_workstations(request: Request, search: str = "", status: str = ""):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        workstations = []
        error = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant and (tenant.get("ws_client_id") or tenant.get("shared_client_id")):
                client = _make_printix_client(tenant)
                # v6.1.0: Cache (TTL 2 min — Online-Status ändert sich häufiger)
                data = _cached_workstations(tenant, client)
                raw = data.get("workstations", data.get("content", [])) if isinstance(data, dict) else []
                # Search lokal anwenden statt erneuter API-Call
                if search:
                    s = search.strip().lower()
                    raw = [ws for ws in raw
                           if s in (ws.get("name") or "").lower()
                           or s in (ws.get("hostname") or "").lower()
                           or s in (ws.get("ipAddress") or "").lower()
                           or s in (ws.get("userName") or "").lower()]
                if isinstance(raw, list):
                    for ws in raw:
                        workstations.append(ws)
            elif not tenant:
                error = "no_tenant"
            else:
                error = "no_ws_creds"
        except Exception as e:
            logger.error("tenant_workstations error: %s", e)
            error = str(e)
        active_count = sum(1 for ws in workstations if ws.get("active"))
        total_count = len(workstations)
        # Filter by status toggle
        if status == "online":
            workstations = [ws for ws in workstations if ws.get("active")]
        elif status == "offline":
            workstations = [ws for ws in workstations if not ws.get("active")]
        return templates.TemplateResponse("tenant_workstations.html", {
            "request": request, "user": user,
            "workstations": workstations, "search": search, "error": error,
            "active_tab": "workstations", "active_count": active_count,
            "total_count": total_count, "status_filter": status, **tc,
        })

    # ─── Printix Tenant: Users/Cards (create must be before {user_id}) ──────────

    @app.get("/tenant/users/create", response_class=HTMLResponse)
    async def tenant_user_create_get(request: Request):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)

        # v5.20.0: Kartenprofile aus Store laden für optionale Karte-sofort-anlegen
        profiles = []
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if tenant:
                import sys as _sys, os as _os
                src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                if src_dir not in _sys.path:
                    _sys.path.insert(0, src_dir)
                from cards.store import list_profiles, init_cards_tables
                from cards.profiles import get_builtin_profiles
                init_cards_tables()
                profiles = get_builtin_profiles() + list_profiles(tenant.get("id", ""))
        except Exception as _pe:
            logger.debug("tenant_user_create_get: profile load failed: %s", _pe)

        return templates.TemplateResponse("tenant_user_create.html", {
            "request": request, "user": user, "error": None,
            "active_tab": "users", "profiles": profiles, **tc,
        })

    @app.post("/tenant/users/create", response_class=HTMLResponse)
    async def tenant_user_create_post(
        request: Request,
        email:                  str = Form(...),
        display_name:           str = Form(...),
        role:                   str = Form("GUEST_USER"),
        pin:                    str = Form(""),
        password:               str = Form(""),
        expiration_timestamp:   str = Form(""),
        send_welcome_email:     str = Form(""),
        send_expiration_email:  str = Form(""),
        card_number:            str = Form(""),
        card_profile_id:        str = Form(""),
    ):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            has_user_creds = bool(
                tenant and (
                    tenant.get("um_client_id")
                    or tenant.get("card_client_id")
                    or tenant.get("shared_client_id")
                )
            )
            if not has_user_creds:
                return templates.TemplateResponse("tenant_user_create.html", {
                    "request": request, "user": user, "error": "no_card_creds", "active_tab": "users", **tc,
                })
            client = _make_printix_client(tenant)
            role_clean = (role or "GUEST_USER").strip().upper()
            if role_clean not in ("USER", "GUEST_USER"):
                role_clean = "GUEST_USER"
            # v5.20.0: role=USER braucht die neue User Management API —
            # ohne um_* Credentials gibt Printix 403 zurück.
            if role_clean == "USER" and not (
                tenant.get("um_client_id") or tenant.get("shared_client_id")
            ):
                return templates.TemplateResponse("tenant_user_create.html", {
                    "request": request, "user": user,
                    "error": "no_um_creds_for_regular_user",
                    "active_tab": "users", **tc,
                })

            raw_result = client.create_user(
                email=email.strip(),
                display_name=display_name.strip(),
                role=role_clean,
                pin=pin.strip() or None,
                password=password.strip() or None,
                expiration_timestamp=expiration_timestamp.strip() or None,
                send_welcome_email=bool(send_welcome_email),
                send_expiration_email=bool(send_expiration_email),
            )
            created = client.extract_created_user(raw_result)
            new_id = created.get("id", "")
            # v6.0.0: Cache invalidieren, damit der neue User sofort sichtbar wird
            _invalidate_tenant_user_cache(tenant.get("id", ""))

            # v6.7.11: Printix-User automatisch als MCP-Employee spiegeln
            # ────────────────────────────────────────────────────────────
            # Damit der frisch angelegte Printix-User sofort als Delegate
            # verfügbar ist (der Delegate-Picker zieht aus MCP-Employees),
            # legen wir automatisch einen lokalen Mitarbeiter-Account an,
            # verknüpft mit der Printix-User-ID. Parent = der aktuell
            # eingeloggte Admin = MCP-Tenant-Owner.
            # v6.7.13: Willkommens-Mail mit Login-URL + Zugangsdaten.
            mcp_employee_created: Optional[dict] = None
            if new_id and (created.get("email") or email.strip()):
                try:
                    from cloudprint.db_extensions import (
                        create_employee, get_employee_by_printix_user_id,
                    )
                    from db import username_exists
                    parent_id = user["id"]
                    if not get_employee_by_printix_user_id(new_id, parent_id):
                        final_email = (created.get("email") or email.strip()).strip()
                        base_username = (final_email.split("@")[0]
                                         or f"printix-{new_id[:8]}")
                        local_username = base_username
                        counter = 1
                        while username_exists(local_username):
                            counter += 1
                            local_username = f"{base_username}{counter}"
                        mcp_password = secrets.token_urlsafe(12)  # für MCP-Login
                        final_full_name = (created.get("displayName")
                                           or display_name.strip()
                                           or final_email)
                        create_employee(
                            parent_user_id=parent_id,
                            username=local_username,
                            password=mcp_password,
                            email=final_email,
                            full_name=final_full_name,
                            printix_user_id=new_id,
                            must_change_password=True,
                        )
                        mcp_employee_created = {
                            "username": local_username,
                            "email": final_email,
                            "full_name": final_full_name,
                            "password": mcp_password,
                        }
                        logger.info(
                            "Printix-User angelegt: MCP-Employee automatisch "
                            "gespiegelt — username=%s email=%s "
                            "printix_user_id=%s parent=%s",
                            local_username, final_email, new_id, parent_id,
                        )
                except Exception as _emp_err:
                    logger.warning(
                        "Printix-User OK, aber MCP-Employee-Mirror "
                        "fehlgeschlagen: %s", _emp_err,
                    )

            # v6.7.13: Willkommens-Mail an den neu angelegten Printix-User
            # (wenn MCP-Mirror erfolgreich + Mail-Credentials konfiguriert).
            # Bei Mail-Fehlschlag oder fehlender Config: Passwort wird im
            # Flash-Screen nach Redirect angezeigt, damit der Admin es
            # manuell weitergeben kann.
            invitation_sent = False
            if mcp_employee_created:
                try:
                    from reporting.notify_helper import send_employee_invitation
                    from db import get_setting
                    # Login-URL: admin public_url (aus /admin/settings) bevorzugt,
                    # sonst der tenant-spezifische public_url, sonst Fallback.
                    login_url = (get_setting("public_url", "")
                                 or tenant.get("tenant_url", "")
                                 or "http://<HA-IP>:8080").rstrip("/")
                    if not login_url.endswith("/login"):
                        login_url = f"{login_url}/login"
                    invitation_sent = send_employee_invitation(
                        tenant=tenant,
                        recipient_email=mcp_employee_created["email"],
                        full_name=mcp_employee_created["full_name"],
                        username=mcp_employee_created["username"],
                        password=mcp_employee_created["password"],
                        login_url=login_url,
                        admin_name=user.get("full_name") or user.get("username", ""),
                    )
                except Exception as _invite_err:
                    logger.warning(
                        "Employee-Invite fehlgeschlagen: %s", _invite_err,
                    )

                # v6.7.13: Wenn die Mail NICHT verschickt wurde, MCP-Credentials
                # in die Flash-Session packen — damit der Admin sie manuell an
                # den User weitergeben kann. Pfad: gleicher Flash-Screen wie
                # für Pin/IdCode.
                if not invitation_sent:
                    existing_flash = request.session.get(
                        "_created_user_secrets", {"user_id": new_id, "values": {}},
                    )
                    existing_flash["user_id"] = new_id
                    vals = existing_flash.get("values", {}) or {}
                    vals["mcp_username"] = mcp_employee_created["username"]
                    vals["mcp_password"] = mcp_employee_created["password"]
                    existing_flash["values"] = vals
                    request.session["_created_user_secrets"] = existing_flash
                    logger.info(
                        "Employee-Invite nicht versendet — MCP-Credentials "
                        "im Flash-Screen hinterlegt für '%s'",
                        mcp_employee_created["email"],
                    )

            # v6.7.11: Persistenten Printix-Cache synchronisieren, damit der
            # neue User sofort in `cached_printix_users` steht (für
            # IPP-Routing und Delegate-Validation).
            if new_id:
                try:
                    from cloudprint.printix_cache_db import sync_users_for_tenant
                    sync_users_for_tenant(
                        tenant_id=tenant["id"],
                        printix_tenant_id=tenant.get("printix_tenant_id", ""),
                        client=client,
                    )
                except Exception as _se:
                    logger.warning("Printix-Cache-Sync fehlgeschlagen: %s", _se)

            # v5.20.0: Auto-generated pin/idCode/password einmalig im
            # Flash-Context mitgeben, damit der Admin sie abschreiben kann.
            if new_id and created:
                generated = {
                    k: created.get(k)
                    for k in ("pin", "idCode", "password")
                    if created.get(k)
                }
                if generated:
                    request.session["_created_user_secrets"] = {
                        "user_id": new_id,
                        "values": generated,
                    }

            # v5.20.0: Optional: Karte direkt hinzufügen wenn angegeben.
            # Nutzt vorhandenes Profil für Transformation (bytes-reverse,
            # hex/dec-Konvertierung, Prefix-Stripping etc.) oder eine simple
            # Default-Transform wenn kein Profil gewählt wurde.
            card_raw = (card_number or "").strip()
            if new_id and card_raw:
                try:
                    import sys as _sys, os as _os
                    src_dir = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
                    if src_dir not in _sys.path:
                        _sys.path.insert(0, src_dir)
                    from cards.transform import transform_card_value
                    from cards.store import get_profile, save_mapping, init_cards_tables
                    init_cards_tables()

                    rules = {}
                    if card_profile_id:
                        prof = get_profile(card_profile_id, tenant.get("id", ""))
                        raw_rules = (prof or {}).get("rules_json", {}) if prof else {}
                        if isinstance(raw_rules, str):
                            import json as _json
                            try:
                                rules = _json.loads(raw_rules or "{}")
                            except Exception:
                                rules = {}
                        elif isinstance(raw_rules, dict):
                            rules = raw_rules

                    preview = transform_card_value(card_raw, **rules) if rules else transform_card_value(card_raw)
                    final_value = preview.get("final_submit_value") or card_raw
                    normalized  = preview.get("normalized") or card_raw

                    client.register_card(new_id, final_value)

                    # Lokales Mapping persistieren → wird für Karten-Suche genutzt
                    tenant_id_local = tenant.get("id", "")
                    printix_tid     = tenant.get("printix_tenant_id", "")
                    # Karten-ID über list_user_cards auflösen
                    new_card_id = ""
                    try:
                        after = client.list_user_cards(new_id)
                        raw_cards = after.get("cards", after.get("content", []))
                        if isinstance(raw_cards, list) and raw_cards:
                            # Nur 1 Karte vorhanden (gerade angelegt) → nehmen wir die
                            first_card = raw_cards[-1]
                            href = ((first_card.get("_links") or {}).get("self") or {}).get("href", "")
                            new_card_id = href.split("/")[-1] if href else first_card.get("id", "")
                    except Exception:
                        new_card_id = ""

                    save_mapping(
                        tenant_id=tenant_id_local,
                        printix_user_id=new_id,
                        printix_card_id=new_card_id,
                        local_value=card_raw,
                        final_value=final_value,
                        normalized_value=normalized,
                        source="created_with_user",
                        notes="",
                        profile_id=card_profile_id or "",
                        preview=preview,
                        printix_secret_value=final_value,
                    )
                except Exception as _card_err:
                    logger.warning("tenant_user_create: card add failed for %s: %s", new_id, _card_err)

            return RedirectResponse(
                f"/tenant/users/{new_id}?flash=created" if new_id else "/tenant/users?flash=created",
                status_code=302
            )
        except Exception as e:
            logger.error("tenant_user_create error: %s", e)
            return templates.TemplateResponse("tenant_user_create.html", {
                "request": request, "user": user, "error": str(e), "active_tab": "users", **tc,
            })

    @app.get("/tenant/users/{printix_user_id}", response_class=HTMLResponse)
    async def tenant_user_detail(request: Request, printix_user_id: str, flash: str = ""):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        px_user = None
        cards   = []
        profiles = []
        error   = None
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            if not tenant or not (tenant.get("card_client_id") or tenant.get("shared_client_id")):
                error = "no_card_creds"
            else:
                try:
                    from cards.store import list_profiles, init_cards_tables
                    from cards.profiles import get_builtin_profiles
                    init_cards_tables()
                    builtin_profiles = get_builtin_profiles()
                    custom_profiles = list_profiles(tenant.get("id", ""))
                    profiles = builtin_profiles + custom_profiles
                except Exception as _profile_e:
                    logger.warning("tenant_user_detail profiles unavailable: %s", _profile_e)
                client = _make_printix_client(tenant)
                px_user_raw = client.get_user(printix_user_id)
                # Printix API returns {"user": {...}, "success": true} — unwrap nested
                px_user = px_user_raw.get("user", px_user_raw) if isinstance(px_user_raw, dict) else {}
                try:
                    cards_data = client.list_user_cards(printix_user_id)
                    raw_cards = cards_data.get("cards", cards_data.get("content", []))
                    # Extract card ID from _links.self.href (no "id" field in API response)
                    cards = []
                    from cards.store import get_mapping_by_card, init_cards_tables
                    init_cards_tables()
                    tenant_id = tenant.get("id", "")
                    for c in raw_cards:
                        href = (c.get("_links") or {}).get("self", {}).get("href", "")
                        c["card_id"] = href.split("/")[-1] if href else c.get("id", "")
                        mapping = get_mapping_by_card(tenant_id, printix_user_id, c["card_id"])
                        if mapping:
                            c["display_value"] = mapping.get("display_value") or mapping.get("local_value")
                            c["final_value"] = mapping.get("final_value", "")
                            c["printix_secret_value"] = mapping.get("printix_secret_value", "")
                            c["working_value"] = mapping.get("working_value", "")
                            c["hex_value"] = mapping.get("hex_value", "")
                            c["hex_reversed_value"] = mapping.get("hex_reversed_value", "")
                            c["decimal_value"] = mapping.get("decimal_value", "")
                            c["decimal_reversed_value"] = mapping.get("decimal_reversed_value", "")
                        cards.append(c)
                except Exception:
                    cards = []
        except Exception as e:
            logger.error("tenant_user_detail error: %s", e)
            error = str(e)
        # v6.7.13: Flash-Secrets ausspielen (einmalig) — Printix-auto-Pin/IdCode/
        # Password + MCP-Portal-Credentials falls Mail nicht verschickt wurde.
        flash_secrets = None
        try:
            sess_secrets = request.session.pop("_created_user_secrets", None)
            if sess_secrets and sess_secrets.get("user_id") == printix_user_id:
                flash_secrets = sess_secrets.get("values") or None
        except Exception:
            flash_secrets = None

        return templates.TemplateResponse("tenant_user_detail.html", {
            "request": request, "user": user,
            "px_user": px_user, "cards": cards, "profiles": profiles,
            "printix_user_id": printix_user_id,
            "flash": flash, "error": error,
            "flash_secrets": flash_secrets,
            "active_tab": "users",
            **tc,
        })

    @app.post("/tenant/users/{printix_user_id}/add-card")
    async def tenant_user_add_card(
        request: Request,
        printix_user_id: str,
        card_number: str = Form(...),
        raw_value: str = Form(""),
        normalized_value: str = Form(""),
        final_value: str = Form(""),
        profile_id: str = Form(""),
    ):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
        try:
            tenant = _cards_tenant_for_user(user)
            client = _make_printix_client(tenant)
            source_raw = (raw_value or "").strip()
            source_normalized = (normalized_value or "").strip() or source_raw
            sent_value = (final_value or card_number or "").strip()
            final_to_store = sent_value
            if profile_id:
                from cards.store import get_profile, init_cards_tables
                from cards.transform import transform_card_value
                init_cards_tables()
                prof = get_profile(profile_id, tenant.get("id", ""))
                rules = (prof or {}).get("rules_json", {}) if prof else {}
                if isinstance(rules, str):
                    import json as _json
                    try:
                        rules = _json.loads(rules or "{}")
                    except Exception:
                        rules = {}
                preview = transform_card_value(source_raw or sent_value, **rules)
                source_raw = source_raw or preview.get("raw", "") or sent_value
                source_normalized = preview.get("normalized") or source_normalized
                sent_value = preview.get("final_submit_value") or sent_value
                final_to_store = sent_value
            else:
                from cards.transform import transform_card_value
                preview = transform_card_value(source_raw or sent_value)
                source_raw = source_raw or preview.get("raw", "") or sent_value
                source_normalized = preview.get("normalized") or source_normalized
                final_to_store = sent_value

            before = client.list_user_cards(printix_user_id)
            before_ids = set()
            for c in before.get("cards", before.get("content", [])):
                cid = _extract_card_id(c)
                if cid:
                    before_ids.add(cid)
            client.register_card(printix_user_id, sent_value)

            after = client.list_user_cards(printix_user_id)
            after_cards = after.get("cards", after.get("content", []))
            new_card_id = _find_new_card_id(before_ids, after_cards)

            if not new_card_id:
                try:
                    card_obj = client.search_card(card_number=sent_value)
                    candidate_id = _extract_card_id(card_obj)
                    owner_href = (((card_obj.get("_links") or {}).get("owner") or {}).get("href", "") if isinstance(card_obj, dict) else "")
                    if candidate_id and (not owner_href or owner_href.endswith("/" + printix_user_id)):
                        new_card_id = candidate_id
                except Exception as _search_e:
                    logger.warning("card search fallback failed: %s", _search_e)

            if new_card_id:
                from cards.store import save_mapping, init_cards_tables
                init_cards_tables()
                save_mapping(
                    tenant.get("id", ""),
                    printix_user_id,
                    new_card_id,
                    source_raw,
                    final_to_store,
                    source_normalized,
                    "tenant_user_add_card",
                    "",
                    profile_id or "",
                    preview=preview,
                )
                return RedirectResponse(f"/tenant/users/{printix_user_id}?flash=card_added", status_code=302)
            return RedirectResponse(f"/tenant/users/{printix_user_id}?flash=error&errmsg=Card%20created%20in%20Printix%20but%20local%20mapping%20failed", status_code=302)
        except Exception as e:
            logger.error("tenant_user_add_card error: %s", e)
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(
                f"/tenant/users/{printix_user_id}?flash=error&errmsg={_qp(str(e)[:80])}",
                status_code=302,
            )

    @app.post("/tenant/users/{printix_user_id}/delete-card")
    async def tenant_user_delete_card(
        request: Request,
        printix_user_id: str,
        card_id: str = Form(...),
    ):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            # Use user-scoped delete: DELETE /users/{uid}/cards/{card_id}
            client.delete_card(card_id, user_id=printix_user_id)
            try:
                from cards.store import search_mappings, delete_mapping
                for m in search_mappings(tenant.get("id",""), card_id):
                    if m.get("printix_card_id") == card_id and m.get("printix_user_id") == printix_user_id:
                        delete_mapping(m["id"], tenant.get("id",""))
            except Exception as _map_del_e:
                logger.warning("local card mapping delete failed: %s", _map_del_e)
            return RedirectResponse(
                f"/tenant/users/{printix_user_id}?flash=card_deleted", status_code=302
            )
        except Exception as e:
            logger.error("tenant_user_delete_card error: %s", e)
            return RedirectResponse(
                f"/tenant/users/{printix_user_id}?flash=error", status_code=302
            )


    @app.post("/tenant/users/{printix_user_id}/save-local-card-value")
    async def tenant_user_save_local_card_value(
        request: Request,
        printix_user_id: str,
        card_id: str = Form(...),
        local_value: str = Form(...),
    ):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
        try:
            from db import get_tenant_full_by_user_id
            from cards.transform import transform_card_value
            from cards.store import save_mapping, init_cards_tables
            tenant = get_tenant_full_by_user_id(user["id"])
            init_cards_tables()
            preview = transform_card_value(local_value.strip())
            save_mapping(
                tenant.get("id",""),
                printix_user_id,
                card_id,
                local_value.strip(),
                preview.get("final_submit_value",""),
                preview.get("normalized",""),
                "tenant_user_manual",
                "",
                preview=preview,
            )
            return RedirectResponse(f"/tenant/users/{printix_user_id}?flash=card_added", status_code=302)
        except Exception as e:
            logger.error("tenant_user_save_local_card_value error: %s", e)
            return RedirectResponse(f"/tenant/users/{printix_user_id}?flash=error", status_code=302)

    @app.post("/tenant/users/{printix_user_id}/generate-id-code")
    async def tenant_user_gen_code(request: Request, printix_user_id: str):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)
            result = client.generate_id_code(printix_user_id)
            code = result.get("idCode", result.get("code", ""))
            return RedirectResponse(
                f"/tenant/users/{printix_user_id}?flash=code_generated&idcode={code}", status_code=302
            )
        except Exception as e:
            logger.error("tenant_user_gen_code error: %s", e)
            return RedirectResponse(
                f"/tenant/users/{printix_user_id}?flash=error", status_code=302
            )

    @app.post("/tenant/users/{printix_user_id}/delete")
    async def tenant_user_delete(request: Request, printix_user_id: str):
        user = require_login(request)
        if user is None:
            return JSONResponse({"ok": False, "error": "not logged in"}, status_code=401)
        try:
            from db import get_tenant_full_by_user_id, _conn
            tenant = get_tenant_full_by_user_id(user["id"])
            client = _make_printix_client(tenant)

            # 1) In Printix löschen
            client.delete_user(printix_user_id)
            local_tenant_id = tenant.get("id", "")

            # 2) Karten-Mappings lokal aufräumen
            try:
                from cards.store import delete_mappings_for_user
                delete_mappings_for_user(local_tenant_id, printix_user_id)
            except Exception as _map_user_del_e:
                logger.warning("local user card mappings cleanup failed: %s", _map_user_del_e)

            # 3) v6.0.0: In-Memory-Cache (TenantCache) invalidieren
            _invalidate_tenant_user_cache(local_tenant_id)

            # 4) v6.7.12: Alle lokalen DB-Spuren des Printix-Users entfernen.
            #    ─────────────────────────────────────────────────────────
            #    a) cached_printix_users   (persistenter Printix-Cache)
            #    b) users (MCP-Employee-Spiegel via printix_user_id)
            #       + zugehörige delegations (owner_user_id ODER delegate_user_id)
            #    c) cloudprint_jobs bleiben erhalten (Audit-Historie)
            deleted_counts = {"cached": 0, "employees": 0, "delegations": 0}
            try:
                with _conn() as conn:
                    # a) Persistenter Cache
                    cur = conn.execute(
                        """DELETE FROM cached_printix_users
                           WHERE tenant_id = ? AND printix_user_id = ?""",
                        (local_tenant_id, printix_user_id),
                    )
                    deleted_counts["cached"] = cur.rowcount or 0

                    # b) Spiegel-Employee-Accounts finden
                    emp_rows = conn.execute(
                        """SELECT id FROM users
                           WHERE printix_user_id = ?
                             AND role_type = 'employee'""",
                        (printix_user_id,),
                    ).fetchall()
                    emp_ids = [r[0] for r in emp_rows]

                    # b.1) Delegations wo der Employee Owner oder Delegate ist
                    if emp_ids:
                        placeholders = ",".join("?" * len(emp_ids))
                        cur = conn.execute(
                            f"""DELETE FROM delegations
                                WHERE owner_user_id IN ({placeholders})
                                   OR delegate_user_id IN ({placeholders})""",
                            emp_ids + emp_ids,
                        )
                        deleted_counts["delegations"] = cur.rowcount or 0

                    # b.2) MCP-Employee(s) löschen
                    if emp_ids:
                        placeholders = ",".join("?" * len(emp_ids))
                        cur = conn.execute(
                            f"DELETE FROM users WHERE id IN ({placeholders})",
                            emp_ids,
                        )
                        deleted_counts["employees"] = cur.rowcount or 0

                logger.info(
                    "Delete Printix-User %s: cache=%d employees=%d delegations=%d",
                    printix_user_id,
                    deleted_counts["cached"],
                    deleted_counts["employees"],
                    deleted_counts["delegations"],
                )
            except Exception as _db_clean_err:
                logger.warning(
                    "Local DB cleanup für Printix-User %s teilweise fehlgeschlagen: %s",
                    printix_user_id, _db_clean_err,
                )

            return RedirectResponse("/tenant/users?flash=deleted", status_code=302)
        except Exception as e:
            logger.error("tenant_user_delete error: %s", e)
            return RedirectResponse(
                f"/tenant/users/{printix_user_id}?flash=error", status_code=302
            )


    # ── Demo-Daten-Register (v3.5.0) ─────────────────────────────────────────
    import uuid as _uuid
    import time as _time
    import asyncio as _asyncio
    import functools as _functools
    _demo_jobs: dict = {}  # job_id → {status, error, session_id, started}

    @app.get("/tenant/demo/status")
    async def tenant_demo_status(request: Request, job_id: str = ""):
        user = require_login(request)
        if user is None:
            return JSONResponse({"status": "error", "error": "not_logged_in"})
        job = _demo_jobs.get(job_id)
        if not job:
            return JSONResponse({"status": "unknown"})
        # Tenant-Isolation: nur eigene Jobs anzeigen
        if job.get("user_id") and job["user_id"] != user["id"]:
            return JSONResponse({"status": "unknown"})
        return JSONResponse({
            "status": job.get("status"),
            "error": job.get("error", ""),
            "session_id": job.get("session_id", ""),
        })

    @app.get("/tenant/demo", response_class=HTMLResponse)
    async def tenant_demo(request: Request):
        """
        Demo-Übersicht.

        v4.4.0: Demo-Daten liegen jetzt lokal in SQLite — kein Azure SQL
        Schreibzugriff mehr nötig. Die Seite rendert sofort, Sessions
        werden per JS aus /tenant/demo/sessions nachgeladen.
        """
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        tc = t_ctx(request)
        flash     = request.query_params.get("flash")
        flash_msg = request.query_params.get("errmsg", request.query_params.get("flash_msg", ""))
        job_id    = request.query_params.get("job_id", "")
        # v4.4.0: Demo funktioniert auch OHNE SQL — lokale SQLite reicht
        has_sql = False
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            has_sql = bool(tenant and tenant.get("sql_server"))
        except Exception as e:
            logger.warning("tenant_demo tenant lookup error: %s", e)
        return templates.TemplateResponse("tenant_demo.html", {
            "request": request, "user": user,
            "has_sql": has_sql,
            # sessions + schema_ready werden per XHR nachgeladen
            "sessions": None,
            "schema_ready": None,
            "flash": flash, "flash_msg": flash_msg,
            "job_id": job_id,
            "form_defaults": {}, "form": {}, "active_tab": "demo", **tc,
        })

    # ── In-Memory-Cache für Demo-Sessions (per Tenant, 30s TTL) ──────────
    # Hauptzweck: zweiter Aufruf der Demo-Seite spart einen Azure-SQL-RTT.
    # Größe ist beschränkt auf wenige Tenants — kein Memory-Leak.
    _demo_session_cache: dict = {}  # tenant_id -> (expires_ts, payload)

    @app.get("/tenant/demo/sessions")
    async def tenant_demo_sessions(request: Request):
        """
        Liefert {schema_ready, sessions} als JSON.
        v4.4.0: Liest aus lokaler SQLite — kein Azure SQL nötig.
        """
        user = require_login(request)
        if user is None:
            return JSONResponse({"error": "not_logged_in"}, status_code=401)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            tid = (tenant or {}).get("printix_tenant_id", "")
            if not tid:
                return JSONResponse({"schema_ready": True, "sessions": [], "has_sql": False})

            cached = _demo_session_cache.get(tid)
            if cached and cached[0] > _time.time():
                return JSONResponse(cached[1])

            # v4.4.0: Lese Demo-Sessions aus lokaler SQLite
            from reporting.local_demo_db import get_demo_sessions
            sessions = get_demo_sessions(tid)[:20]

            has_sql = bool(tenant and tenant.get("sql_server"))

            # ISO-Strings für JSON
            def _ser(s):
                out = dict(s)
                if out.get("created_at") is not None:
                    out["created_at"] = str(out["created_at"])
                pj = out.get("params_json") or ""
                try:
                    import json as _j
                    pdata = _j.loads(pj) if pj else {}
                    out["preset"] = pdata.get("preset", "custom")
                    out["queue_count"] = pdata.get("queue_count", 0)
                except Exception:
                    out["preset"] = "custom"
                    out["queue_count"] = 0
                out.pop("params_json", None)
                return out

            payload = {
                "has_sql": has_sql,
                "schema_ready": True,  # Lokale SQLite ist immer bereit
                "sessions": [_ser(s) for s in sessions],
            }
            _demo_session_cache[tid] = (_time.time() + 30, payload)
            return JSONResponse(payload)
        except Exception as e:
            logger.warning("tenant_demo_sessions error: %s", e)
            return JSONResponse({"error": str(e)[:200]}, status_code=500)

    def _demo_cache_invalidate(tenant_id: str) -> None:
        """Aufruf nach generate / delete / rollback, damit der nächste GET frisch lädt."""
        _demo_session_cache.pop(tenant_id, None)

    @app.post("/tenant/demo/setup", response_class=HTMLResponse)
    async def tenant_demo_setup(request: Request):
        """v4.4.0: Initialisiert lokale SQLite Demo-DB — kein Azure SQL nötig."""
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        try:
            from reporting.demo_generator import setup_schema
            result = setup_schema()
            if result.get("success"):
                return RedirectResponse("/tenant/demo?flash=setup_ok", status_code=302)
            errmsg = "; ".join(e.get("error","") for e in result.get("errors", []))[:200]
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(f"/tenant/demo?flash=error&errmsg={_qp(errmsg)}", status_code=302)
        except Exception as e:
            logger.error("tenant_demo_setup error: %s", e)
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(f"/tenant/demo?flash=error&errmsg={_qp(str(e)[:200])}", status_code=302)

    @app.post("/tenant/demo/generate", response_class=HTMLResponse)
    async def tenant_demo_generate(request: Request):
        """v4.4.0: Generiert Demo-Daten in lokaler SQLite — kein Azure SQL nötig."""
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            tenant = get_tenant_full_by_user_id(user["id"])
            tid = (tenant or {}).get("printix_tenant_id", "")
            if not tid:
                return RedirectResponse("/tenant/demo?flash=error&errmsg=Kein+Tenant+konfiguriert", status_code=302)
            form_data = await request.form()
            user_count    = max(1, min(200, int(form_data.get("user_count",    10))))
            printer_count = max(1, min(50,  int(form_data.get("printer_count",  4))))
            queue_count   = max(1, min(5,   int(form_data.get("queue_count",    2))))
            months        = max(1, min(36,  int(form_data.get("months",        12))))
            jobs_per_day  = max(0.5, min(15.0, float(form_data.get("jobs_per_day", 2.0))))
            languages     = form_data.getlist("languages") or ["de", "en"]
            sites_raw     = form_data.get("sites", "")
            sites         = [s.strip() for s in sites_raw.split(",") if s.strip()] or ["Hauptsitz"]
            demo_tag      = (form_data.get("demo_tag") or "").strip()[:80]
            preset        = (form_data.get("preset") or "custom").strip()[:20]
            if preset not in ("small", "medium", "large", "custom"):
                preset = "custom"
            # Hintergrund-Task: sofort Redirect, Browser pollt /tenant/demo/status
            job_id = _uuid.uuid4().hex[:10]
            _demo_jobs[job_id] = {"status": "running", "started": _time.time(),
                                  "user_id": user["id"]}
            async def _bg_generate():
                import json as _json, sys as _sys, os as _os
                output_file = f"/tmp/demo_result_{job_id}.json"
                try:
                    demo_params = {
                        "tenant_id":         tid,
                        "user_count":        user_count,
                        "printer_count":     printer_count,
                        "queue_count":       queue_count,
                        "months":            months,
                        "jobs_per_user_day": jobs_per_day,
                        "languages":         languages,
                        "sites":             sites,
                        "demo_tag":          demo_tag or f"Demo {__import__('datetime').date.today()}",
                        "preset":            preset,
                    }
                    env = dict(_os.environ)
                    env["DEMO_PARAMS"]        = _json.dumps(demo_params)
                    env["DEMO_OUTPUT_FILE"]   = output_file
                    proc = await _asyncio.create_subprocess_exec(
                        _sys.executable, "/app/reporting/demo_worker.py",
                        env=env,
                        stdout=_asyncio.subprocess.PIPE,
                        stderr=_asyncio.subprocess.PIPE,
                    )
                    stdout, stderr = await proc.communicate()
                    if proc.returncode == 0:
                        try:
                            with open(output_file) as _f:
                                result = _json.load(_f)
                        except Exception:
                            result = {}
                        if result.get("error"):
                            _demo_jobs[job_id] = {"status": "error", "error": str(result["error"])[:200]}
                        else:
                            _demo_jobs[job_id] = {"status": "done", "session_id": result.get("session_id", "")}
                            _demo_cache_invalidate(tid)
                    else:
                        err = ""
                        try:
                            with open(output_file) as _f:
                                err = _json.load(_f).get("error", "")[:200]
                        except Exception:
                            pass
                        if not err:
                            err = (stderr.decode("utf-8", errors="replace") or "").strip()[-200:]
                        if not err:
                            err = f"Worker exit {proc.returncode}"
                        logger.error("Demo-Worker exit %d: %s", proc.returncode, err)
                        _demo_jobs[job_id] = {"status": "error", "error": err}
                except Exception as exc:
                    logger.error("bg_generate error: %s", exc)
                    _demo_jobs[job_id] = {"status": "error", "error": str(exc)[:200]}
                finally:
                    try:
                        _os.unlink(output_file)
                    except Exception:
                        pass
            _asyncio.create_task(_bg_generate())
            return RedirectResponse(f"/tenant/demo?job_id={job_id}", status_code=302)
        except Exception as e:
            logger.error("tenant_demo_generate error: %s", e)
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(
                f"/tenant/demo?flash=error&errmsg={_qp(str(e)[:100])}",
                status_code=302,
            )

    @app.post("/tenant/demo/delete/{session_id}", response_class=HTMLResponse)
    async def tenant_demo_delete(request: Request, session_id: str):
        """
        Löscht alle Demo-Datenzeilen einer einzelnen Session.
        v4.4.0: Arbeitet auf lokaler SQLite statt Azure SQL.
        """
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            from reporting.local_demo_db import rollback_demo_session
            tenant = get_tenant_full_by_user_id(user["id"])
            tid = (tenant or {}).get("printix_tenant_id", "")
            rollback_demo_session(session_id)
            _demo_cache_invalidate(tid)
            return RedirectResponse("/tenant/demo?flash=deleted", status_code=302)
        except Exception as e:
            logger.error("tenant_demo_delete error: %s", e)
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(
                f"/tenant/demo?flash=error&errmsg={_qp(str(e)[:100])}",
                status_code=302,
            )

    @app.post("/tenant/demo/rollback", response_class=HTMLResponse)
    async def tenant_demo_rollback(request: Request):
        """
        Rollback per demo_tag — löscht ALLE Sessions mit dem angegebenen Tag.
        v4.4.0: Arbeitet auf lokaler SQLite statt Azure SQL.
        """
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            from reporting.demo_generator import rollback_demo
            tenant = get_tenant_full_by_user_id(user["id"])
            tid = (tenant or {}).get("printix_tenant_id", "")
            form_data = await request.form()
            demo_tag = (form_data.get("demo_tag") or "").strip()[:80]
            if not demo_tag:
                return RedirectResponse("/tenant/demo?flash=error&flash_msg=missing_tag",
                                        status_code=302)
            result = await _asyncio.to_thread(rollback_demo, tid, demo_tag)
            logger.info("Demo-Rollback (tag=%s): %d Zeilen gelöscht",
                        demo_tag, result.get("total_deleted", 0))
            _demo_cache_invalidate(tid)
            return RedirectResponse("/tenant/demo?flash=rollback_ok", status_code=302)
        except Exception as e:
            logger.error("tenant_demo_rollback error: %s", e)
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(
                f"/tenant/demo?flash=error&flash_msg={_qp(str(e)[:120])}",
                status_code=302,
            )


    @app.post("/tenant/demo/rollback-all", response_class=HTMLResponse)
    async def tenant_demo_rollback_all(request: Request):
        """
        Löscht ALLE Demo-Daten des Tenants.
        v4.4.0: Arbeitet auf lokaler SQLite statt Azure SQL.
        """
        user = require_login(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        try:
            from db import get_tenant_full_by_user_id
            from reporting.demo_generator import rollback_demo_all
            tenant = get_tenant_full_by_user_id(user["id"])
            tid = (tenant or {}).get("printix_tenant_id", "")
            result = rollback_demo_all(tid)
            logger.info("Demo-Rollback-All: %d Zeilen gelöscht", result.get("total_deleted", 0))
            _demo_cache_invalidate(tid)
            return RedirectResponse("/tenant/demo?flash=rollback_ok", status_code=302)
        except Exception as e:
            logger.error("tenant_demo_rollback_all error: %s", e)
            from urllib.parse import quote_plus as _qp
            return RedirectResponse(
                f"/tenant/demo?flash=error&flash_msg={_qp(str(e)[:120])}",
                status_code=302,
            )

    # ── Reports-Register (v3.0.0) ─────────────────────────────────────────────
    try:
        from web.reports_routes import register_reports_routes
        register_reports_routes(app, templates, t_ctx, require_login)
    except Exception as _re:
        logger.error("Reports-Routen konnten nicht registriert werden: %s", _re)

    # ── Capture Store (v4.4.0) ────────────────────────────────────────────────
    try:
        from web.capture_routes import register_capture_routes
        register_capture_routes(app, templates, t_ctx, require_login)
    except Exception as _ce:
        logger.error("Capture-Routen konnten nicht registriert werden: %s", _ce)

    # ── Cloud Print Port — Employee & Delegation (v5.12.0) ────────────────────
    try:
        from cloudprint.db_extensions import init_cloudprint_schema
        init_cloudprint_schema()
        from web.employee_routes import register_employee_routes
        register_employee_routes(app, templates, t_ctx, require_login)
    except Exception as _ep:
        logger.error("Employee-Routen konnten nicht registriert werden: %s", _ep)

    # ── Roadmap (v6.7.26) ────────────────────────────────────────────────────
    try:
        from roadmap import init_roadmap_schema
        init_roadmap_schema()
        from web.roadmap_routes import register_roadmap_routes
        register_roadmap_routes(app, templates, t_ctx, require_login)
    except Exception as _rp:
        logger.error("Roadmap-Routen konnten nicht registriert werden: %s", _rp)

    # ── Desktop-Client-API (v6.7.31) ─────────────────────────────────────────
    try:
        from desktop_auth import init_desktop_schema
        init_desktop_schema()
        from web.desktop_routes import register_desktop_routes
        from app_version import APP_VERSION as _APP_V
        register_desktop_routes(app, lambda: _APP_V)
    except Exception as _dp:
        logger.error("Desktop-Routen konnten nicht registriert werden: %s", _dp)

    # ── Desktop Management (iOS Mgmt-Tab, v6.7.66) ──────────────────────────
    try:
        from web.desktop_management_routes import register_desktop_management_routes
        register_desktop_management_routes(app)
        logger.info("Desktop-Management-Routen registriert: /desktop/management/*")
    except Exception as _dmp:
        logger.error("Desktop-Management-Routen konnten nicht registriert werden: %s", _dmp)

    # ── Desktop Cards (iOS Karten-Tab, v6.7.90) ─────────────────────────────
    try:
        from web.desktop_cards_routes import register_desktop_cards_routes
        register_desktop_cards_routes(app)
        logger.info("Desktop-Cards-Routen registriert: /desktop/cards/*")
    except Exception as _dcp:
        logger.error("Desktop-Cards-Routen konnten nicht registriert werden: %s", _dcp)

    # ── Cloud Print Port — IPP/IPPS Endpoint (v6.5.0) ─────────────────────────
    try:
        from cloudprint.ipp_server import register_ipp_routes
        register_ipp_routes(app)
        logger.info("IPP/IPPS-Endpoint registriert: POST /ipp/<tenant_id>")
    except Exception as _ipp_err:
        logger.error("IPP-Endpoint konnte nicht registriert werden: %s", _ipp_err)

    # v6.6.0: LPR-Server komplett entfernt — IPPS ist der einzige Cloud-Print-Eingang.

    # ── IPP/IPPS zusätzlicher Uvicorn-Listener (v6.5.0) ─────────────────────
    # Wenn unter Admin → Settings ein IPPS-Port gesetzt ist der sich vom
    # Haupt-WEB_PORT unterscheidet, starten wir einen zweiten Uvicorn-Server
    # der die gleiche FastAPI-App auf dem IPP-Port hostet. So kann der User
    # seinen Cloudflare-TCP-Tunnel auf z.B. 192.168.1.179:621 richten und
    # unser IPP-Endpoint ist dort erreichbar.
    @app.on_event("startup")
    async def _start_ipp_listener():
        import asyncio
        # v6.6.0-fix: Der zweite Uvicorn-Server hostet die gleiche FastAPI-App,
        # also triggert dessen Startup unseren @on_event("startup") erneut. Das
        # würde rekursiv immer weitere Listener auf Port 621 zu spawnen versuchen
        # → "address already in use". Wir markieren den Spawn process-weit via
        # Env-Variable und überspringen weitere Durchläufe.
        if os.environ.get("_IPP_LISTENER_SPAWNED") == "1":
            return

        try:
            from db import get_setting
            ipp_port_str = get_setting("ipps_port", "") or os.environ.get("IPP_PORT", "")
        except Exception:
            ipp_port_str = os.environ.get("IPP_PORT", "")
        if not ipp_port_str or not ipp_port_str.strip():
            return
        try:
            ipp_port = int(ipp_port_str.strip())
        except ValueError:
            logger.warning("IPP-Listener: ungültiger Port '%s'", ipp_port_str)
            return

        web_port = int(os.environ.get("WEB_PORT", "8080"))
        if ipp_port == web_port:
            logger.info("IPP-Listener: nutzt Haupt-WEB_PORT %d, kein separater Listener", web_port)
            return

        # v6.7.0: TLS-Termination direkt im Uvicorn — Printix spricht IPPS
        # (= IPP über TLS), wir brauchen also Cert+Key. Pfade kommen aus
        # Addon-Optionen (`ipps_certfile`/`ipps_keyfile`) oder Default
        # `/ssl/fullchain.pem` + `/ssl/privkey.pem` (= HA-Standard, vom
        # Let's-Encrypt-/DuckDNS-Addon befüllt). Wenn beide Dateien existieren
        # → TLS, sonst → plain HTTP (für Tests / hinter externem Proxy).
        try:
            from db import get_setting as _gs
            cert_file = (_gs("ipps_certfile", "") or
                         os.environ.get("IPPS_CERTFILE", "") or
                         "/ssl/fullchain.pem")
            key_file  = (_gs("ipps_keyfile", "") or
                         os.environ.get("IPPS_KEYFILE", "") or
                         "/ssl/privkey.pem")
        except Exception:
            cert_file = os.environ.get("IPPS_CERTFILE", "/ssl/fullchain.pem")
            key_file  = os.environ.get("IPPS_KEYFILE",  "/ssl/privkey.pem")

        tls_enabled = (
            cert_file and key_file
            and os.path.exists(cert_file)
            and os.path.exists(key_file)
        )

        try:
            import uvicorn
            uvicorn_kwargs = dict(
                host=os.environ.get("IPP_HOST", "0.0.0.0"),
                port=ipp_port,
                log_level=os.environ.get("MCP_LOG_LEVEL", "info").lower(),
                # v6.6.2: access_log EIN — damit jede Verbindung (auch 404)
                # im Log auftaucht. Unverzichtbar um zu sehen ob Printix den
                # Endpoint überhaupt erreicht.
                access_log=True,
            )
            if tls_enabled:
                uvicorn_kwargs["ssl_certfile"] = cert_file
                uvicorn_kwargs["ssl_keyfile"]  = key_file
                logger.info(
                    "IPP/IPPS-Listener Startup: port=%d TLS=ENABLED "
                    "cert=%s key=%s", ipp_port, cert_file, key_file,
                )
            else:
                logger.warning(
                    "IPP/IPPS-Listener Startup: port=%d TLS=DISABLED "
                    "(cert=%s key=%s — eine oder beide Dateien fehlen). "
                    "Printix spricht IPPS=TLS, ohne Cert wird Connect "
                    "fehlschlagen!",
                    ipp_port, cert_file, key_file,
                )
            config = uvicorn.Config(app, **uvicorn_kwargs)
            server = uvicorn.Server(config)
            os.environ["_IPP_LISTENER_SPAWNED"] = "1"
            asyncio.create_task(server.serve())
        except Exception as _ipp_err:
            logger.error("IPP-Listener konnte nicht gestartet werden: %s", _ipp_err)

    def _cards_tenant_for_user(user_dict):
        from db import get_tenant_full_by_user_id
        return get_tenant_full_by_user_id(user_dict["id"])

    def _extract_card_id(card_obj):
        if not isinstance(card_obj, dict):
            return ""
        href = ((card_obj.get("_links") or {}).get("self") or {}).get("href", "")
        if href:
            return href.split("/")[-1]
        return card_obj.get("card_id", "") or card_obj.get("id", "") or ""

    def _find_new_card_id(before_ids, after_cards):
        for c in after_cards or []:
            cid = _extract_card_id(c)
            if cid and cid not in before_ids:
                return cid
        return ""

    # ── Cards & Codes ─────────────────────────────────────────────────────────
    @app.get("/cards", response_class=HTMLResponse)
    async def cards_tool_get(request: Request, q: str = ""):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        from cards.store import init_cards_tables, search_mappings, list_profiles
        from cards.profiles import get_builtin_profiles
        tc = t_ctx(request)
        try:
            init_cards_tables()
            # v6.7.28: Defensiv — Tenant/Config kann fehlen (User ohne Printix-
            # Credentials, frisch registrierter Admin). Vorher crashte die
            # Seite mit Internal Server Error statt einen Hinweis anzuzeigen.
            tenant = _cards_tenant_for_user(user) or {}
            has_card_creds = bool(
                tenant.get("card_client_id") or tenant.get("shared_client_id")
            )
            tid = tenant.get("id", "")
            mappings = search_mappings(tid, q) if tid else []
            builtin_profiles = get_builtin_profiles()
            custom_profiles = list_profiles(tid) if tid else []
            profiles = builtin_profiles + custom_profiles
            default_card_profile_id = tenant.get("default_card_profile_id", "") or ""
            return templates.TemplateResponse("cards_tool.html", {
                "request": request, **tc, "user": user, "mappings": mappings,
                "profiles": profiles, "builtin_profiles": builtin_profiles,
                "custom_profiles": custom_profiles, "query": q,
                "has_card_creds": has_card_creds,
                "tenant_configured": bool(tid),
                "default_card_profile_id": default_card_profile_id,
            })
        except Exception as e:
            logger.error("cards_tool_get error: %s", e)
            return templates.TemplateResponse("cards_tool.html", {
                "request": request, **tc, "user": user,
                "mappings": [], "profiles": [], "builtin_profiles": [],
                "custom_profiles": [], "query": q,
                "has_card_creds": False, "tenant_configured": False,
                "error": str(e)[:200],
            })

    @app.post("/cards/mappings/save", response_class=RedirectResponse)
    async def cards_mapping_save(request: Request):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        from cards.store import init_cards_tables, save_mapping, get_profile
        from cards.transform import transform_card_value
        init_cards_tables()
        tenant = _cards_tenant_for_user(user)
        tid = tenant.get("id", "")
        form = await request.form()
        uid = (form.get("printix_user_id", "") or "").strip()
        cid = (form.get("printix_card_id", "") or "").strip()
        raw_value = (form.get("raw_value", "") or form.get("local_value", "") or "").strip()
        normalized_value = (form.get("normalized_value", "") or "").strip()
        final_value = (form.get("final_value", "") or "").strip()
        profile_id = (form.get("profile_id", "") or "").strip()
        notes = (form.get("notes", "") or "").strip()
        source = (form.get("source", "") or "cards_tool").strip()
        if not (uid and cid and raw_value):
            return RedirectResponse("/cards?flash=mapping_error", status_code=303)
        if profile_id:
            prof = get_profile(profile_id, tid)
            rules = (prof or {}).get("rules_json", {}) if prof else {}
            if isinstance(rules, str):
                import json as _json
                try:
                    rules = _json.loads(rules or "{}")
                except Exception:
                    rules = {}
            preview = transform_card_value(raw_value, **rules)
            normalized_value = normalized_value or preview.get("normalized", "")
            final_value = final_value or preview.get("final_submit_value", "")
        else:
            preview = transform_card_value(raw_value)
            normalized_value = normalized_value or preview.get("normalized", "")
            final_value = final_value or preview.get("final_submit_value", "")
        save_mapping(tid, uid, cid, raw_value, final_value, normalized_value, source, notes, profile_id, preview=preview)
        return RedirectResponse("/cards?flash=mapping_saved", status_code=303)

    @app.post("/cards/mappings/{mapping_id}/delete", response_class=RedirectResponse)
    async def cards_mapping_delete(request: Request, mapping_id: int):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        from cards.store import delete_mapping, init_cards_tables
        init_cards_tables()
        tenant = _cards_tenant_for_user(user)
        delete_mapping(mapping_id, tenant.get("id", ""))
        return RedirectResponse("/cards?flash=mapping_deleted", status_code=303)

    @app.post("/cards/profiles/save", response_class=RedirectResponse)
    async def cards_profile_save(request: Request):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        from cards.store import upsert_profile, init_cards_tables
        init_cards_tables()
        form = await request.form()
        tenant = _cards_tenant_for_user(user)
        tid = tenant.get("id", "")
        upsert_profile(tid, form.get("name",""), form.get("vendor",""), form.get("reader_model",""), form.get("mode","plain"), form.get("description",""), form.get("rules_json","{}"), form.get("profile_id",""))
        return RedirectResponse("/cards?flash=profile_saved", status_code=303)

    @app.post("/cards/profiles/{profile_id}/delete", response_class=RedirectResponse)
    async def cards_profile_delete(request: Request, profile_id: str):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        from cards.store import delete_profile, init_cards_tables
        init_cards_tables()
        tenant = _cards_tenant_for_user(user)
        delete_profile(profile_id, tenant.get("id", ""))
        return RedirectResponse("/cards", status_code=303)

    @app.post("/cards/default-profile/save", response_class=RedirectResponse)
    async def cards_default_profile_save(request: Request):
        """Firmen-Default-Profil setzen — wird von der iOS-App (und anderen
        Clients ueber GET /desktop/cards/profiles) automatisch uebernommen,
        so dass Mitarbeiter beim Karten-Anlegen kein Profil waehlen
        muessen. Leere profile_id = Default entfernen."""
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        tenant = _cards_tenant_for_user(user)
        tid = tenant.get("id", "") if tenant else ""
        if not tid:
            return RedirectResponse("/cards?flash=default_profile_error", status_code=303)
        form = await request.form()
        profile_id = (form.get("profile_id", "") or "").strip()
        from db import _conn as _db_conn
        with _db_conn() as conn:
            conn.execute(
                "UPDATE tenants SET default_card_profile_id=? WHERE id=?",
                (profile_id, tid)
            )
        return RedirectResponse("/cards?flash=default_profile_saved", status_code=303)

    @app.post("/cards/sync-import", response_class=RedirectResponse)
    async def cards_sync_import(request: Request):
        user = require_login(request)
        if user is None: return RedirectResponse("/login", status_code=302)
        from cards.store import init_cards_tables, save_mapping
        from cards.transform import decode_printix_secret_value, transform_card_value
        from cards.profiles import get_builtin_profiles
        init_cards_tables()
        tenant = _cards_tenant_for_user(user)
        tid = tenant.get("id", "")
        client = _make_printix_client(tenant)
        imported = 0
        id_only = 0
        builtin_profiles = {p.get("id"): p for p in get_builtin_profiles()}
        for role in ("USER", "GUEST_USER"):
            try:
                users_data = client.list_users(role=role, page=0, page_size=200)
            except Exception:
                continue
            raw_users = users_data.get("users", users_data.get("content", users_data if isinstance(users_data, list) else []))
            for u in raw_users or []:
                uid = u.get("id", "")
                if not uid:
                    continue
                try:
                    cards_data = client.list_user_cards(uid)
                except Exception:
                    continue
                for c in cards_data.get("cards", cards_data.get("content", [])) or []:
                    cid = _extract_card_id(c)
                    if not cid:
                        continue
                    raw_secret = c.get("secret") or c.get("cardNumber") or c.get("number") or ""
                    local_value = ""
                    profile_hint = ""
                    if raw_secret:
                        decoded = decode_printix_secret_value(raw_secret)
                        local_value = decoded.get("decoded_text", "") or ""
                        profile_hint = decoded.get("profile_hint", "") or ""
                    if local_value and local_value != cid:
                        preview = {
                            "raw": local_value,
                            "normalized": local_value,
                            "final_submit_value": raw_secret or local_value,
                            "printix_secret_value": raw_secret or "",
                        }
                        if profile_hint and profile_hint in builtin_profiles:
                            rules = builtin_profiles[profile_hint].get("rules_json", {}) or {}
                            preview = transform_card_value(local_value, **rules)
                        save_mapping(
                            tid, uid, cid, local_value, raw_secret or local_value, local_value,
                            "printix_import", "", profile_hint,
                            preview=preview, printix_secret_value=raw_secret or ""
                        )
                        imported += 1
                    else:
                        save_mapping(
                            tid, uid, cid, cid, cid, cid,
                            "printix_import_id_only", "Printix did not provide original card value",
                            preview={"raw": cid, "normalized": cid, "final_submit_value": cid}
                        )
                        id_only += 1
        return RedirectResponse(f"/cards?flash=sync_ok&imported={imported}&id_only={id_only}", status_code=303)

    return app
