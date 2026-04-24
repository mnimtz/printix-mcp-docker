"""
Multi-Tenant OAuth 2.0 Authorization Code Server für Printix MCP.

Implementiert den Authorization Code Flow (RFC 6749):
  1. GET  /oauth/authorize  → Bestätigungsseite (zeigt Tenant-Name)
  2. POST /oauth/authorize  → User klickt "Erlauben" → Redirect mit Code
  3. POST /oauth/token      → Code gegen Bearer Token tauschen

Alle OAuth-Credentials (client_id, client_secret) und der ausgestellte
Bearer Token werden pro Tenant in der SQLite-DB verwaltet.
Der Tenant wird anhand der client_id aus dem Token-Request bestimmt.

Kompatibel mit:
  - claude.ai Konnektoren (Streamable HTTP /mcp)
  - ChatGPT MCP Connector (SSE /sse)
"""

import html as _html
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode, urlparse
from app_version import APP_VERSION

logger = logging.getLogger("printix.oauth")

# In-memory Authorization Code Store: {code: {tenant_id, client_id, redirect_uri, expires_at}}
_auth_codes: dict = {}
_request_count = 0


def _cleanup_codes():
    global _request_count
    _request_count += 1
    if _request_count % 50 == 0:
        now = time.time()
        expired = [k for k, v in _auth_codes.items() if v["expires_at"] < now]
        for k in expired:
            del _auth_codes[k]


# ─── Redirect-URI Whitelist (RFC 6749 §3.1.2) ─────────────────────────────────
#
# Verhindert Open-Redirect-Angriffe: nur bekannte OAuth-Client-Hostnames sind
# als Ziel für Authorization-Code-Redirects zugelassen. Die Defaults decken
# claude.ai und ChatGPT als primäre MCP-Clients ab; weitere Hosts können
# per Environment-Variable OAUTH_ALLOWED_REDIRECT_HOSTS (Komma-separiert)
# ergänzt werden, ohne Code-Änderung.

_DEFAULT_ALLOWED_REDIRECT_HOSTS = (
    "claude.ai",
    "chat.openai.com",
    "chatgpt.com",
    "localhost",
    "127.0.0.1",
    "::1",
)


def _allowed_redirect_hosts() -> set:
    """Default-Whitelist vereint mit OAUTH_ALLOWED_REDIRECT_HOSTS (env)."""
    hosts = set(_DEFAULT_ALLOWED_REDIRECT_HOSTS)
    extra = os.environ.get("OAUTH_ALLOWED_REDIRECT_HOSTS", "").strip()
    if extra:
        for h in extra.split(","):
            h = h.strip().lower()
            if h:
                hosts.add(h)
    return hosts


def _is_allowed_redirect_uri(uri: str) -> bool:
    """
    Prüft, ob eine redirect_uri ein sicheres Ziel für den OAuth-Redirect ist.

    Regeln (RFC 6749 §3.1.2 + Defense-in-Depth):
      - Scheme muss https sein (oder http nur für Loopback)
      - Host muss in der Whitelist stehen
      - Keine userinfo ("user:pass@host")
      - Kein Fragment
    """
    if not uri:
        return False
    try:
        p = urlparse(uri)
    except Exception:
        return False
    if p.fragment:
        return False
    if "@" in (p.netloc or ""):
        return False
    host = (p.hostname or "").lower()
    if not host:
        return False
    allowed = _allowed_redirect_hosts()
    if p.scheme == "https":
        return host in allowed
    if p.scheme == "http":
        return host in ("localhost", "127.0.0.1", "::1")
    return False


def _build_redirect(redirect_uri: str, params: dict) -> str:
    """
    Hängt sauber URL-encodierte Query-Parameter an redirect_uri an.
    Bewahrt eine bereits vorhandene Query-String.
    """
    if not params:
        return redirect_uri
    sep = "&" if "?" in redirect_uri else "?"
    return f"{redirect_uri}{sep}{urlencode(params)}"


# ─── HTML Authorize-Seite ──────────────────────────────────────────────────────

_AUTHORIZE_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Printix Management Console – Zugriff erlauben</title>
  <style>
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif;
      background: #f0f2f5;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh;
    }}
    .card {{
      background: #fff; border-radius: 16px; padding: 40px 36px;
      max-width: 440px; width: 100%;
      box-shadow: 0 8px 32px rgba(0,0,0,.10);
    }}
    .logo {{ font-size: 2.4em; margin-bottom: 12px; }}
    h1 {{ font-size: 1.4em; color: #111; margin-bottom: 8px; }}
    .sub {{ color: #555; font-size: .95em; line-height: 1.55; margin-bottom: 24px; }}
    .tenant-box {{
      background: #eff6ff; border: 1px solid #bfdbfe; border-radius: 8px;
      padding: 12px 16px; margin-bottom: 28px;
      font-size: .9em; color: #1d4ed8; font-weight: 600;
    }}
    .btn {{
      display: block; width: 100%; padding: 14px;
      border: none; border-radius: 10px; font-size: 1em;
      font-weight: 600; cursor: pointer; transition: background .15s;
    }}
    .btn-approve {{ background: #2563eb; color: #fff; margin-bottom: 10px; }}
    .btn-approve:hover {{ background: #1d4ed8; }}
    .btn-deny {{ background: #f1f5f9; color: #374151; }}
    .btn-deny:hover {{ background: #e2e8f0; }}
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">Printix</div>
    <h1>Printix Management Console</h1>
    <p class="sub">
      Eine externe App möchte auf deine Printix Management Console zugreifen
      und Printix-Ressourcen in deinem Namen verwalten.
    </p>
    <div class="tenant-box">Tenant: {tenant_name}<br><small>App: {client_id}</small></div>

    <form method="post" action="/oauth/authorize">
      <input type="hidden" name="client_id"    value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state"        value="{state}">
      <input type="hidden" name="approved"     value="true">
      <button type="submit" class="btn btn-approve">✓ Zugriff erlauben</button>
    </form>

    <form method="post" action="/oauth/authorize">
      <input type="hidden" name="client_id"    value="{client_id}">
      <input type="hidden" name="redirect_uri" value="{redirect_uri}">
      <input type="hidden" name="state"        value="{state}">
      <input type="hidden" name="approved"     value="false">
      <button type="submit" class="btn btn-deny">✗ Ablehnen</button>
    </form>
  </div>
</body>
</html>"""


# ─── OAuth ASGI Middleware ─────────────────────────────────────────────────────

class OAuthMiddleware:
    """
    ASGI Middleware: Multi-Tenant OAuth 2.0 Authorization Code Flow.

    Tenant-Lookup erfolgt per client_id aus der SQLite-DB.
    Kein festes client_id/secret mehr in env vars.

    Routet:
      GET/POST /oauth/authorize  → Authorize-Logik
      POST     /oauth/token      → Token-Endpunkt (gibt tenant.bearer_token zurück)
      GET      /.well-known/*    → OAuth Discovery (RFC 8414 / 9728)
      GET      /health           → Health-Check
      *                          → BearerAuthMiddleware → DualTransportApp → MCP
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET")

        if path == "/health":
            await self._health(send)
        elif path.startswith("/.well-known/"):
            await self._well_known(path, send)
        elif path == "/oauth/authorize":
            if method == "GET":
                await self._authorize_get(scope, send)
            elif method == "POST":
                await self._authorize_post(scope, receive, send)
            else:
                await self._json(send, 405, {"error": "method_not_allowed"})
        elif path == "/oauth/token" and method == "POST":
            await self._token(scope, receive, send)
        else:
            await self.app(scope, receive, send)

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _read_body(self, receive) -> bytes:
        body = b""
        more = True
        while more:
            msg = await receive()
            body += msg.get("body", b"")
            more = msg.get("more_body", False)
        return body

    @staticmethod
    def _parse_form(raw: bytes) -> dict:
        from urllib.parse import parse_qs
        parsed = parse_qs(raw.decode("utf-8", errors="ignore"))
        return {k: v[0] for k, v in parsed.items()}

    @staticmethod
    def _parse_query(qs: bytes) -> dict:
        from urllib.parse import parse_qs
        parsed = parse_qs(qs.decode("utf-8", errors="ignore"))
        return {k: v[0] for k, v in parsed.items()}

    async def _json(self, send, status: int, data: dict):
        body = json.dumps(data).encode()
        await send({"type": "http.response.start", "status": status,
                    "headers": [[b"content-type", b"application/json"],
                                 [b"content-length", str(len(body)).encode()]]})
        await send({"type": "http.response.body", "body": body})

    async def _redirect(self, send, location: str):
        await send({"type": "http.response.start", "status": 302,
                    "headers": [[b"location", location.encode()],
                                 [b"content-length", b"0"]]})
        await send({"type": "http.response.body", "body": b""})

    async def _health(self, send):
        body = json.dumps({
            "status": "ok",
            "service": "printix-mcp",
            "version": APP_VERSION,
        }).encode()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [[b"content-type", b"application/json"],
                                 [b"content-length", str(len(body)).encode()]]})
        await send({"type": "http.response.body", "body": body})

    def _lookup_tenant_by_client(self, client_id: str) -> dict | None:
        """Findet Tenant anhand der OAuth client_id in der DB."""
        try:
            from db import get_tenant_by_oauth_client_id
            return get_tenant_by_oauth_client_id(client_id)
        except Exception as e:
            logger.error("DB-Fehler bei OAuth-Client-Lookup: %s", e)
            return None

    # ── OAuth Discovery ────────────────────────────────────────────────────────

    async def _well_known(self, path: str, send):
        base = os.environ.get("MCP_PUBLIC_URL", "").rstrip("/") or "http://localhost:8765"

        if "oauth-authorization-server" in path or "openid-configuration" in path:
            data = {
                "issuer": base,
                "authorization_endpoint": f"{base}/oauth/authorize",
                "token_endpoint": f"{base}/oauth/token",
                "token_endpoint_auth_methods_supported": ["client_secret_post"],
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code"],
                "code_challenge_methods_supported": [],
            }
        elif "oauth-protected-resource" in path:
            data = {
                "resource": f"{base}/mcp",
                "resource_documentation": f"{base}/mcp",
                "authorization_servers": [base],
            }
        else:
            await self._json(send, 404, {"error": "not_found"})
            return

        logger.debug("OAuth Discovery: %s", path)
        await self._json(send, 200, data)

    # ── OAuth Endpunkte ────────────────────────────────────────────────────────

    async def _authorize_get(self, scope, send):
        """Zeigt die Bestätigungsseite für den Tenant."""
        params = self._parse_query(scope.get("query_string", b""))
        client_id   = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state       = params.get("state", "")

        if not client_id or not redirect_uri:
            await self._json(send, 400, {"error": "invalid_request",
                                          "error_description": "client_id und redirect_uri erforderlich"})
            return

        # redirect_uri VOR jedem weiteren Schritt gegen Whitelist prüfen.
        # Kein 302-Redirect an eine ungültige URI — JSON-Fehler direkt zurück,
        # damit wir keinen Open-Redirect-Vektor öffnen.
        if not _is_allowed_redirect_uri(redirect_uri):
            logger.warning("OAuth: redirect_uri abgelehnt (authorize GET): %s", redirect_uri)
            await self._json(send, 400, {"error": "invalid_request",
                                          "error_description": "redirect_uri nicht erlaubt"})
            return

        # Tenant anhand client_id finden
        tenant = self._lookup_tenant_by_client(client_id)
        tenant_name = tenant.get("name", client_id) if tenant else client_id

        logger.info("OAuth: Authorize-Anfrage von client_id=%s (Tenant: %s)", client_id, tenant_name)
        # WICHTIG: Alle Werte HTML-escapen, bevor sie ins Template gehen —
        # sonst reflected XSS in hidden-Input-Feldern (value="...").
        html_body = _AUTHORIZE_HTML.format(
            client_id=_html.escape(client_id, quote=True),
            redirect_uri=_html.escape(redirect_uri, quote=True),
            state=_html.escape(state, quote=True),
            tenant_name=_html.escape(tenant_name, quote=True),
        )
        body = html_body.encode("utf-8")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [[b"content-type", b"text/html; charset=utf-8"],
                                 [b"content-length", str(len(body)).encode()]]})
        await send({"type": "http.response.body", "body": body})

    async def _authorize_post(self, scope, receive, send):
        """Verarbeitet Formular-Submit (Erlauben / Ablehnen)."""
        raw = await self._read_body(receive)
        params = self._parse_form(raw)

        client_id    = params.get("client_id", "")
        redirect_uri = params.get("redirect_uri", "")
        state        = params.get("state", "")
        approved     = params.get("approved", "false") == "true"

        # redirect_uri VOR jedem Redirect gegen Whitelist prüfen (wie oben).
        if not _is_allowed_redirect_uri(redirect_uri):
            logger.warning("OAuth: redirect_uri abgelehnt (authorize POST): %s", redirect_uri)
            await self._json(send, 400, {"error": "invalid_request",
                                          "error_description": "redirect_uri nicht erlaubt"})
            return

        if not approved:
            logger.info("OAuth: Zugriff abgelehnt für client_id=%s", client_id)
            await self._redirect(
                send,
                _build_redirect(redirect_uri, {"error": "access_denied", "state": state}),
            )
            return

        tenant = self._lookup_tenant_by_client(client_id)
        if not tenant:
            logger.warning("OAuth: Unbekannte client_id=%s", client_id)
            await self._redirect(
                send,
                _build_redirect(redirect_uri, {"error": "unauthorized_client", "state": state}),
            )
            return

        # Authorization Code generieren (gültig 10 Min.)
        code = secrets.token_urlsafe(32)
        _auth_codes[code] = {
            "tenant_id":    tenant["id"],
            "client_id":    client_id,
            "redirect_uri": redirect_uri,
            "expires_at":   time.time() + 600,
        }
        _cleanup_codes()

        logger.info("OAuth: Code ausgestellt für client_id=%s (Tenant: %s)", client_id, tenant.get("name", "?"))
        await self._redirect(
            send,
            _build_redirect(redirect_uri, {"code": code, "state": state}),
        )

    async def _token(self, scope, receive, send):
        """Tauscht Authorization Code gegen den Tenant-Bearer-Token."""
        raw = await self._read_body(receive)

        # Content-Type: form-encoded oder JSON
        ct = ""
        for k, v in scope.get("headers", []):
            if k == b"content-type":
                ct = v.decode("utf-8", errors="ignore")
                break

        if "application/json" in ct:
            try:
                params = json.loads(raw)
            except Exception:
                params = {}
        else:
            params = self._parse_form(raw)

        grant_type    = params.get("grant_type", "")
        code          = params.get("code", "")
        client_id     = params.get("client_id", "")
        client_secret = params.get("client_secret", "")

        # Client-Credentials + Tenant aus DB prüfen
        tenant = self._lookup_tenant_by_client(client_id)
        if not tenant:
            logger.warning("OAuth: Token-Anfrage mit unbekannter client_id=%s", client_id)
            await self._json(send, 401, {"error": "invalid_client",
                                          "error_description": "Unbekannte client_id"})
            return

        # client_secret validieren (stored in DB, plain — tenant hat eigenen OAuth-Secret)
        try:
            from db import verify_tenant_oauth_secret
            if not verify_tenant_oauth_secret(tenant["id"], client_secret):
                logger.warning("OAuth: Falsches client_secret für client_id=%s", client_id)
                await self._json(send, 401, {"error": "invalid_client",
                                              "error_description": "Falsches client_secret"})
                return
        except Exception as e:
            logger.error("OAuth Secret-Prüfung fehlgeschlagen: %s", e)
            await self._json(send, 500, {"error": "server_error"})
            return

        if grant_type != "authorization_code":
            await self._json(send, 400, {"error": "unsupported_grant_type",
                                          "error_description": "Nur authorization_code wird unterstützt"})
            return

        # Authorization Code validieren
        code_data = _auth_codes.pop(code, None)
        if not code_data or code_data["expires_at"] < time.time():
            logger.warning("OAuth: Ungültiger oder abgelaufener Code")
            await self._json(send, 400, {"error": "invalid_grant",
                                          "error_description": "Authorization Code ungültig oder abgelaufen"})
            return

        if code_data["tenant_id"] != tenant["id"]:
            logger.warning("OAuth: Code gehört zu anderem Tenant")
            await self._json(send, 400, {"error": "invalid_grant",
                                          "error_description": "Code ungültig"})
            return

        # RFC 6749 §4.1.3: "ensure that the authorization code was issued to the
        # authenticated confidential client". Defense-in-depth zusätzlich zum
        # Tenant-Check, falls die client_id→tenant Zuordnung jemals nicht-1:1 wird.
        if code_data.get("client_id") != client_id:
            logger.warning("OAuth: Code wurde für andere client_id ausgestellt")
            await self._json(send, 400, {"error": "invalid_grant",
                                          "error_description": "Code ungültig"})
            return

        # RFC 6749 §4.1.3: redirect_uri im Token-Request MUSS identisch zu der
        # im Authorization-Request sein. Sonst kann ein Angreifer einen fremden
        # Code via anderer redirect_uri einlösen.
        token_redirect_uri = params.get("redirect_uri", "")
        if token_redirect_uri != code_data.get("redirect_uri", ""):
            logger.warning(
                "OAuth: redirect_uri-Mismatch beim Token-Tausch (client_id=%s)",
                client_id,
            )
            await self._json(send, 400, {"error": "invalid_grant",
                                          "error_description": "redirect_uri mismatch"})
            return

        logger.info("OAuth: Access Token ausgestellt für Tenant '%s'", tenant.get("name", "?"))
        await self._json(send, 200, {
            "access_token": tenant["bearer_token"],
            "token_type":   "bearer",
            "expires_in":   31536000,  # 1 Jahr
        })
