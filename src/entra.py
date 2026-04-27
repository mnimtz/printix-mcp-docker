"""
Entra ID (Azure AD) SSO — Login via Microsoft Account
=====================================================
Ermöglicht Benutzern die Anmeldung über ihr Microsoft-Konto (Entra ID).
Konfiguration erfolgt über Admin-Settings (settings-Tabelle).

Unterstützt Multi-Tenant: eine App-Registration in einem beliebigen
Entra-Tenant, Login für Benutzer aus jedem Entra-Tenant möglich.

Auto-Setup (v4.3.0): Device Code Flow mit Azure-CLI-Client-ID — der Admin
klickt einen Button, gibt einen Code bei Microsoft ein, und die SSO-App
wird automatisch via Graph API erstellt. Keine Bootstrap-App nötig.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
from urllib.parse import urlencode

import requests as _requests

logger = logging.getLogger(__name__)

# Microsoft Identity Platform v2.0 Endpoints
_AUTH_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_DEVICE_CODE_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/devicecode"
_SCOPES = "openid profile email"

# Fuer den nativen-App-PKCE-Flow brauchen wir zusaetzlich Graph
# `User.Read`, weil wir nach dem Token-Exchange `/v1.0/me` aufrufen,
# um oid/email/name zu holen. Ohne diesen Scope antwortet Graph mit
# 403 — das schmale `_SCOPES` (nur ID-Token-Claims) reicht nicht.
# `offline_access` damit Microsoft auch refresh_tokens ausstellt
# (zukuenftige Token-Erneuerung ohne erneuten Login).
_SCOPES_GRAPH_USER_READ = (
    "https://graph.microsoft.com/User.Read "
    "offline_access openid email profile"
)

# Graph API
_GRAPH_URL = "https://graph.microsoft.com/v1.0"

# Azure CLI well-known client_id — kept for reference but NOT used for device code.
# _AZURE_CLI_CLIENT_ID = "04b07795-a710-4f83-a962-d65c70e4e3c2"

# Microsoft Graph Command Line Tools (first-party Microsoft app).
# Supports device code flow + dynamic consent for Graph API permissions.
# Used by Microsoft Graph CLI (mgc) and PowerShell SDK.
_GRAPH_CLI_CLIENT_ID = "14d82eec-204b-4c2f-b7e8-296a70dab67e"


# ─── Configuration ───────────────────────────────────────────────────────────

def get_config() -> dict:
    """Liest die Entra-ID-Konfiguration aus der Settings-Tabelle."""
    from db import get_setting
    secret_enc = get_setting("entra_client_secret", "")
    # Secret ist Fernet-verschlüsselt gespeichert
    if secret_enc:
        try:
            from db import _dec
            secret = _dec(secret_enc)
        except Exception:
            secret = secret_enc
    else:
        secret = ""
    return {
        "enabled":       get_setting("entra_enabled", "0") == "1",
        "tenant_id":     get_setting("entra_tenant_id", ""),
        "client_id":     get_setting("entra_client_id", ""),
        "client_secret": secret,
        "auto_approve":  get_setting("entra_auto_approve", "0") == "1",
    }


def is_enabled() -> bool:
    """Prüft ob Entra-Login aktiviert und konfiguriert ist."""
    cfg = get_config()
    return cfg["enabled"] and bool(cfg["client_id"]) and bool(cfg["client_secret"])


# ─── OAuth2 Authorization Code Flow ─────────────────────────────────────────

def generate_state() -> str:
    """Generiert einen CSRF-State-Token für den OAuth-Flow."""
    return secrets.token_urlsafe(32)


def build_authorize_url(redirect_uri: str, state: str) -> str:
    """Baut die Microsoft-Login-URL für den Authorization Code Flow."""
    cfg = get_config()
    tenant = cfg["tenant_id"] or "common"
    params = {
        "client_id":     cfg["client_id"],
        "response_type": "code",
        "redirect_uri":  redirect_uri,
        "scope":         _SCOPES,
        "response_mode": "query",
        "state":         state,
        "prompt":        "select_account",
    }
    return _AUTH_URL.format(tenant=tenant) + "?" + urlencode(params)


def exchange_code_for_user(code: str, redirect_uri: str) -> dict | None:
    """
    Tauscht den Authorization Code gegen Tokens und extrahiert User-Info
    aus dem id_token.

    Returns dict mit keys: oid, email, name, tid — oder None bei Fehler.
    """
    cfg = get_config()
    tenant = cfg["tenant_id"] or "common"

    try:
        resp = _requests.post(
            _TOKEN_URL.format(tenant=tenant),
            data={
                "client_id":     cfg["client_id"],
                "client_secret": cfg["client_secret"],
                "code":          code,
                "redirect_uri":  redirect_uri,
                "grant_type":    "authorization_code",
                "scope":         _SCOPES,
            },
            timeout=15,
        )
    except Exception as e:
        logger.error("Entra token exchange Netzwerkfehler: %s", e)
        return None

    if resp.status_code != 200:
        logger.error("Entra token exchange fehlgeschlagen: %s %s",
                      resp.status_code, resp.text[:500])
        return None

    data = resp.json()
    id_token = data.get("id_token", "")
    if not id_token:
        logger.error("Entra: kein id_token in der Antwort")
        return None

    # Decode JWT payload — Signatur wird nicht separat validiert, da Token
    # direkt über HTTPS vom Microsoft Token-Endpoint empfangen wurde
    payload = _decode_jwt_payload(id_token)
    if not payload:
        return None

    email = (
        payload.get("email", "")
        or payload.get("preferred_username", "")
        or payload.get("upn", "")
    )

    return {
        "oid":   payload.get("oid", ""),
        "email": email,
        "name":  payload.get("name", ""),
        "tid":   payload.get("tid", ""),
    }


# ─── PKCE Authorization Code Flow (Native Mobile Apps, v7.1.4+) ──────────────
#
# Für die iOS-App: statt Device Code Flow (Code abtippen auf zweitem Gerät)
# benutzen wir den Standard-OAuth-Flow für native Mobile Apps —
# Authorization Code mit PKCE. Der iOS-Client öffnet eine
# ASWebAuthenticationSession (in-app Safari-Sheet), der User meldet sich
# direkt bei Microsoft an, MS redirected zurück zum Custom-URL-Scheme,
# der Client schickt den Code an unseren Server, wir tauschen ihn gegen
# Tokens.
#
# Wichtig: code_verifier wird SERVER-SEITIG generiert und gespeichert,
# NIE an den Client geschickt — sonst hätte PKCE keinen Sicherheitsgewinn
# gegenüber dem reinen Auth-Code-Flow.
#
# Voraussetzung in der Entra App-Registration:
#   Authentication → Mobile and desktop applications → Add URI:
#     z.B. printixmobileprint://oauth/callback


def generate_pkce_pair() -> tuple[str, str]:
    """Erzeugt ein (code_verifier, code_challenge)-Paar nach RFC 7636.

    code_verifier:  43-128 Zeichen URL-safe Random.
    code_challenge: SHA256(verifier), base64url ohne Padding.

    Den Verifier behalten wir auf dem Server, die Challenge geht in die
    Microsoft-Auth-URL.
    """
    verifier = secrets.token_urlsafe(64)[:96]   # ~96 Zeichen, deutlich im Limit
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_url_pkce(redirect_uri: str,
                             state: str,
                             code_challenge: str,
                             *,
                             prompt: str = "select_account",
                             scope: str = _SCOPES_GRAPH_USER_READ) -> str:
    """Baut die Microsoft-Login-URL für den Authorization Code Flow mit
    PKCE — gedacht für die iOS-App und andere native Clients.

    Im Gegensatz zu `build_authorize_url` zusätzlich `code_challenge` +
    `code_challenge_method=S256`. Default-Prompt = `select_account`,
    damit der User die richtige Identität wählen kann. Default-Scope
    fordert `User.Read` an, damit das anschliessende Graph `/me`
    funktioniert (sonst 403 Forbidden).
    """
    cfg = get_config()
    tenant = cfg["tenant_id"] or "common"
    params = {
        "client_id":             cfg["client_id"],
        "response_type":         "code",
        "redirect_uri":          redirect_uri,
        "scope":                 scope,
        "response_mode":         "query",
        "state":                 state,
        "prompt":                prompt,
        "code_challenge":        code_challenge,
        "code_challenge_method": "S256",
    }
    return _AUTH_URL.format(tenant=tenant) + "?" + urlencode(params)


def exchange_code_pkce(code: str,
                       redirect_uri: str,
                       code_verifier: str,
                       *,
                       scope: str = _SCOPES_GRAPH_USER_READ) -> dict | None:
    """Tauscht den Authorization Code gegen Tokens — PKCE-Variante.

    Holt zusätzlich Profil-Daten (oid, email, name) via Microsoft Graph
    `/me`, damit das Mapping auf MCP-User identisch zum Device-Code-Flow
    läuft (siehe desktop_routes `/desktop/auth/entra/poll`).

    Returns dict mit keys: oid, email, name, tid — oder None bei Fehler.
    """
    cfg = get_config()
    tenant = cfg["tenant_id"] or "common"

    # WICHTIG: KEIN client_secret beim PKCE-Flow fuer Mobile/Desktop-Apps!
    # Wenn die Redirect-URI ein Custom-Scheme ist (z.B.
    # `printixmobileprint://...`) erkennt Microsoft das als
    # Public-Client-Flow und lehnt jeden Request mit client_secret ab:
    #   AADSTS700025: Client is public so neither 'client_assertion' nor
    #                 'client_secret' should be presented
    # PKCE (code_verifier) uebernimmt hier die Sicherheits-Garantie
    # statt des Geheimnisses. Fuer den Web-Auth-Code-Flow
    # (`exchange_code_for_user`) brauchen wir das Secret weiterhin —
    # diese Funktion ist explizit fuer den Native-App-Flow gedacht.
    try:
        resp = _requests.post(
            _TOKEN_URL.format(tenant=tenant),
            data={
                "client_id":     cfg["client_id"],
                "code":          code,
                "redirect_uri":  redirect_uri,
                "grant_type":    "authorization_code",
                "scope":         scope,
                "code_verifier": code_verifier,
            },
            timeout=15,
        )
    except Exception as e:
        logger.error("Entra PKCE token exchange Netzwerkfehler: %s", e)
        return None

    if resp.status_code != 200:
        logger.error("Entra PKCE token exchange fehlgeschlagen: %s %s",
                      resp.status_code, resp.text[:500])
        return None

    data = resp.json()
    access_token = data.get("access_token", "")
    if not access_token:
        logger.error("Entra PKCE: kein access_token in der Antwort")
        return None

    # Profil aus Microsoft Graph holen — wie im Device-Code-Flow, damit
    # das User-Mapping konsistent ist.
    try:
        me = _requests.get(
            "https://graph.microsoft.com/v1.0/me",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=15,
        )
        me.raise_for_status()
        me_data = me.json()
    except Exception as e:
        logger.error("Entra PKCE: Graph /me Abruf fehlgeschlagen: %s", e)
        return None

    return {
        "oid":   me_data.get("id", ""),
        "email": (me_data.get("mail") or
                  me_data.get("userPrincipalName") or ""),
        "name":  (me_data.get("displayName") or
                  me_data.get("givenName") or ""),
        "tid":   "",
    }


# ─── Device Code Flow (Auto App Registration) ────────────────────────────────
#
# Truly automatic one-click setup using Microsoft Graph CLI's client_id.
# No bootstrap app or pre-registered credentials needed.
#
# Ablauf:
#   1. Admin klickt "Automatisch einrichten"
#   2. Server startet Device Code Flow (POST devicecode endpoint)
#   3. Seite zeigt user_code + verification_uri
#   4. Admin oeffnet URL, gibt Code ein, meldet sich an, erteilt Consent
#   5. Server pollt Token-Endpoint bis Token empfangen
#   6. Graph API erstellt neue SSO-App + Secret
#   7. Credentials werden in Settings gespeichert
# ─────────────────────────────────────────────────────────────────────────────

# Graph API scopes for device code flow (app registration)
_GRAPH_SCOPES_DEVICE = (
    "https://graph.microsoft.com/Application.ReadWrite.All "
    "https://graph.microsoft.com/Organization.Read.All"
)

# Zusaetzliche Scopes fuer den Guest-Print-Auto-Setup: wir brauchen
# AppRoleAssignment.ReadWrite.All um nach dem App-Create programmatisch
# Admin-Consent fuer Mail.ReadWrite (Application) zu erteilen, und
# User.Read.All, um direkt danach die Postfachliste des Tenants zu laden.
_GRAPH_SCOPES_GUESTPRINT = (
    "https://graph.microsoft.com/Application.ReadWrite.All "
    "https://graph.microsoft.com/Organization.Read.All "
    "https://graph.microsoft.com/AppRoleAssignment.ReadWrite.All "
    "https://graph.microsoft.com/User.Read.All"
)

# Well-known IDs fuer Microsoft Graph (App-Only Permissions):
#   resourceAppId           = Microsoft Graph's well-known App-ID
#   MAIL_READWRITE_APP_ROLE = Role-ID der Application-Permission "Mail.ReadWrite"
_GRAPH_APP_ID = "00000003-0000-0000-c000-000000000000"
_GRAPH_MAIL_READWRITE_APP_ROLE = "e2a3a72e-5f79-4c64-b1b1-878b674786c9"


def start_device_code_flow(tenant: str = "common",
                             scopes: str | None = None) -> dict | None:
    """
    Startet den Device Code Flow mit der Microsoft Graph CLI Client-ID.

    Args:
        tenant: Azure-Tenant (Default "common" für Multi-Tenant-Login)
        scopes: space-separierte Scope-Liste. Default = Application.ReadWrite
                (für Auto-App-Registration im Admin-Setup). Für Desktop-Login
                übergibt man z.B. "https://graph.microsoft.com/User.Read offline_access".

    Returns dict mit keys: device_code, user_code, verification_uri,
    expires_in, interval — oder None bei Fehler.
    """
    effective_scope = scopes if scopes else _GRAPH_SCOPES_DEVICE
    try:
        resp = _requests.post(
            _DEVICE_CODE_URL.format(tenant=tenant),
            data={
                "client_id": _GRAPH_CLI_CLIENT_ID,
                "scope":     effective_scope,
            },
            timeout=15,
        )
    except Exception as e:
        logger.error("Device Code Flow Netzwerkfehler: %s", e)
        return None

    if resp.status_code != 200:
        logger.error("Device Code Flow fehlgeschlagen: %s %s",
                      resp.status_code, resp.text[:500])
        return None

    data = resp.json()
    return {
        "device_code":      data.get("device_code", ""),
        "user_code":        data.get("user_code", ""),
        "verification_uri": data.get("verification_uri", ""),
        "expires_in":       data.get("expires_in", 900),
        "interval":         data.get("interval", 5),
        "message":          data.get("message", ""),
    }


def poll_device_code_token(device_code: str, tenant: str = "common") -> dict:
    """
    Pollt den Token-Endpoint fuer den Device Code Flow (ein einzelner Versuch).

    Returns dict mit:
      - status: "pending" | "success" | "error" | "expired"
      - access_token: (nur bei status="success")
      - error: (bei status="error")
    """
    try:
        resp = _requests.post(
            _TOKEN_URL.format(tenant=tenant),
            data={
                "client_id":  _GRAPH_CLI_CLIENT_ID,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            timeout=15,
        )
    except Exception as e:
        logger.error("Device Code Poll Netzwerkfehler: %s", e)
        return {"status": "error", "error": str(e)}

    data = resp.json()

    if resp.status_code == 200:
        access_token = data.get("access_token", "")
        if access_token:
            return {"status": "success", "access_token": access_token}
        return {"status": "error", "error": "Kein access_token in Antwort"}

    # Microsoft returns 400 for pending/expired/declined
    error = data.get("error", "")
    if error in ("authorization_pending", "slow_down"):
        return {"status": "pending"}
    elif error == "expired_token":
        return {"status": "expired"}
    elif error == "authorization_declined":
        return {"status": "error", "error": "authorization_declined"}
    else:
        desc = data.get("error_description", error)
        logger.error("Device Code Poll Fehler: %s — %s", error, desc)
        return {"status": "error", "error": desc}


def auto_register_app(
    access_token: str,
    sso_redirect_uri: str,
    app_name: str = "Printix Management Console",
) -> dict | None:
    """
    Erstellt eine neue SSO-App-Registration im Entra-Tenant des Admins
    über die Microsoft Graph API.

    Args:
        access_token:     Graph API Access Token (aus Device Code Flow)
        sso_redirect_uri: Redirect URI für die neue SSO-App (z.B. .../auth/entra/callback)
        app_name:         Anzeigename der App in Azure

    Returns:
        dict mit tenant_id, client_id, client_secret — oder None bei Fehler.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # Tenant-ID des eingeloggten Admins ermitteln
    tenant_id = ""
    try:
        me_resp = _requests.get(
            f"{_GRAPH_URL}/organization",
            headers=headers,
            timeout=10,
        )
        if me_resp.status_code == 200:
            orgs = me_resp.json().get("value", [])
            if orgs:
                tenant_id = orgs[0].get("id", "")
    except Exception as e:
        logger.warning("Konnte Tenant-ID nicht ermitteln: %s", e)

    # 1. App erstellen
    app_body = {
        "displayName": app_name,
        "signInAudience": "AzureADMultipleOrgs",
        "web": {
            "redirectUris": [sso_redirect_uri],
            "implicitGrantSettings": {
                "enableIdTokenIssuance": True,
            },
        },
        "requiredResourceAccess": [
            {
                "resourceAppId": "00000003-0000-0000-c000-000000000000",
                "resourceAccess": [
                    {"id": "37f7f235-527c-4136-accd-4a02d197296e", "type": "Scope"},  # openid
                    {"id": "14dad69e-099b-42c9-810b-d002981feec1", "type": "Scope"},  # profile
                    {"id": "64a6cdd6-aab1-4aaf-94b8-3cc8405e90d0", "type": "Scope"},  # email
                ],
            }
        ],
    }

    try:
        resp = _requests.post(
            f"{_GRAPH_URL}/applications",
            headers=headers,
            json=app_body,
            timeout=15,
        )
        if resp.status_code not in (200, 201):
            logger.error("Graph: App-Erstellung fehlgeschlagen: %s %s",
                          resp.status_code, resp.text[:500])
            return None

        app_data = resp.json()
        app_id = app_data["appId"]       # = client_id
        obj_id = app_data["id"]          # = object_id (für weitere API-Calls)

        # 2. Client Secret erstellen
        secret_body = {
            "passwordCredential": {
                "displayName": "Printix MCP Auto-Generated",
                "endDateTime": "2099-12-31T23:59:59Z",
            }
        }
        resp2 = _requests.post(
            f"{_GRAPH_URL}/applications/{obj_id}/addPassword",
            headers=headers,
            json=secret_body,
            timeout=15,
        )
        if resp2.status_code not in (200, 201):
            logger.error("Graph: Secret-Erstellung fehlgeschlagen: %s %s",
                          resp2.status_code, resp2.text[:500])
            return {"tenant_id": tenant_id, "client_id": app_id, "client_secret": ""}

        secret_data = resp2.json()
        client_secret = secret_data.get("secretText", "")

        logger.info("Entra SSO-App automatisch erstellt: %s (client_id=%s, tenant=%s)",
                     app_name, app_id, tenant_id)
        return {
            "tenant_id":     tenant_id,
            "client_id":     app_id,
            "client_secret": client_secret,
        }

    except Exception as e:
        logger.error("Graph API Fehler bei Auto-Setup: %s", e)
        return None


# ─── Guest-Print Auto-Setup ──────────────────────────────────────────────────
#
# Analog zum SSO-Auto-Setup oben, aber mit anderen Permissions:
#   - `Mail.ReadWrite` als **Application-Role** (nicht Delegated-Scope), weil
#     der Poller mit App-Only-Token laeuft — kein User im Loop.
#   - Admin-Consent wird programmatisch erteilt via
#     POST /servicePrincipals/{gp_sp_id}/appRoleAssignments.
#     Dazu muss der Device-Code-Admin Global-Admin (oder Privileged Role
#     Admin) sein und der Token muss AppRoleAssignment.ReadWrite.All haben.
# ─────────────────────────────────────────────────────────────────────────────


def start_device_code_flow_guestprint(tenant: str = "common") -> dict | None:
    """Device Code Flow fuer den Guest-Print-Auto-Setup. Unterscheidet sich
    vom SSO-Flow durch die breiteren Scopes (AppRoleAssignment.ReadWrite.All
    fuer programmatischen Consent, User.Read.All fuer den Mailbox-Picker)."""
    return start_device_code_flow(tenant=tenant, scopes=_GRAPH_SCOPES_GUESTPRINT)


def _get_service_principal_by_app_id(access_token: str, app_id: str) -> str:
    """Liefert die ObjectId (id) des ServicePrincipals zu einem appId.
    Leerstring wenn nicht gefunden."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = _requests.get(
            f"{_GRAPH_URL}/servicePrincipals",
            headers=headers,
            params={"$filter": f"appId eq '{app_id}'"},
            timeout=15,
        )
    except Exception as e:
        logger.warning("Graph: servicePrincipals lookup fehlgeschlagen: %s", e)
        return ""
    if resp.status_code != 200:
        return ""
    items = resp.json().get("value", [])
    return items[0].get("id", "") if items else ""


def auto_register_guestprint_app(
    access_token: str,
    app_name: str = "Printix Guest-Print",
) -> dict | None:
    """Erstellt eine Entra-App mit Mail.ReadWrite Application-Role + Admin-
    Consent im Tenant des Device-Code-Admins.

    Returns: dict mit tenant_id, client_id, client_secret — oder None bei Fehler.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }

    # 1) Tenant-ID ermitteln
    tenant_id = ""
    try:
        me_resp = _requests.get(f"{_GRAPH_URL}/organization",
                                  headers=headers, timeout=10)
        if me_resp.status_code == 200:
            orgs = me_resp.json().get("value", [])
            if orgs:
                tenant_id = orgs[0].get("id", "")
    except Exception as e:
        logger.warning("Guest-Print Auto-Setup: Tenant-ID nicht ermittelbar: %s", e)

    # 2) App-Registration erstellen — Single-Tenant (die App ist nur
    # im Kunden-Tenant benutzbar, das passt zum Minimal-Scope-Gedanken).
    app_body = {
        "displayName": app_name,
        "signInAudience": "AzureADMyOrg",
        "requiredResourceAccess": [
            {
                "resourceAppId": _GRAPH_APP_ID,
                "resourceAccess": [
                    {"id": _GRAPH_MAIL_READWRITE_APP_ROLE, "type": "Role"},
                ],
            }
        ],
    }
    try:
        resp = _requests.post(f"{_GRAPH_URL}/applications",
                                headers=headers, json=app_body, timeout=15)
        if resp.status_code not in (200, 201):
            logger.error("Guest-Print Graph: App-Create fehlgeschlagen: %s %s",
                          resp.status_code, resp.text[:500])
            return None
        app_data = resp.json()
        app_id = app_data["appId"]
        obj_id = app_data["id"]
    except Exception as e:
        logger.error("Guest-Print Graph: App-Create Exception: %s", e)
        return None

    # 3) Client-Secret generieren
    client_secret = ""
    try:
        secret_body = {"passwordCredential": {
            "displayName": "Printix Guest-Print Auto-Generated",
            "endDateTime": "2099-12-31T23:59:59Z",
        }}
        resp2 = _requests.post(
            f"{_GRAPH_URL}/applications/{obj_id}/addPassword",
            headers=headers, json=secret_body, timeout=15,
        )
        if resp2.status_code in (200, 201):
            client_secret = resp2.json().get("secretText", "") or ""
        else:
            logger.error("Guest-Print Graph: Secret-Create fehlgeschlagen: %s %s",
                          resp2.status_code, resp2.text[:500])
    except Exception as e:
        logger.error("Guest-Print Graph: Secret-Create Exception: %s", e)

    # 4) ServicePrincipal fuer die neue App anlegen (notwendig, damit Consent
    # ueberhaupt granted werden kann — eine App ohne SP ist im Tenant nicht
    # auffindbar).
    gp_sp_id = ""
    try:
        sp_resp = _requests.post(
            f"{_GRAPH_URL}/servicePrincipals",
            headers=headers, json={"appId": app_id}, timeout=15,
        )
        if sp_resp.status_code in (200, 201):
            gp_sp_id = sp_resp.json().get("id", "")
        else:
            # 409 = existiert schon (z.B. bei Retry) — per Lookup nachholen
            gp_sp_id = _get_service_principal_by_app_id(access_token, app_id)
            if not gp_sp_id:
                logger.error("Guest-Print Graph: SP-Create fehlgeschlagen: %s %s",
                              sp_resp.status_code, sp_resp.text[:500])
    except Exception as e:
        logger.error("Guest-Print Graph: SP-Create Exception: %s", e)

    # 5) Admin-Consent fuer Mail.ReadWrite App-Role erteilen.
    # Braucht AppRoleAssignment.ReadWrite.All im Device-Code-Token + einen
    # Admin mit Privileged-Role (Global Admin oder Privileged Role Admin).
    consent_ok = False
    if gp_sp_id:
        graph_sp_id = _get_service_principal_by_app_id(access_token, _GRAPH_APP_ID)
        if graph_sp_id:
            try:
                ass_resp = _requests.post(
                    f"{_GRAPH_URL}/servicePrincipals/{gp_sp_id}/appRoleAssignments",
                    headers=headers,
                    json={
                        "principalId": gp_sp_id,
                        "resourceId":  graph_sp_id,
                        "appRoleId":   _GRAPH_MAIL_READWRITE_APP_ROLE,
                    },
                    timeout=15,
                )
                consent_ok = ass_resp.status_code in (200, 201)
                if not consent_ok:
                    # 400 mit "Permission being assigned already exists" ist OK
                    body = (ass_resp.text or "")[:300]
                    if "already exists" in body.lower():
                        consent_ok = True
                    else:
                        logger.warning(
                            "Guest-Print Graph: Admin-Consent-Grant fehlgeschlagen: %s %s",
                            ass_resp.status_code, body,
                        )
            except Exception as e:
                logger.warning("Guest-Print Graph: Admin-Consent Exception: %s", e)

    logger.info(
        "Guest-Print Auto-Setup: app_id=%s tenant=%s consent=%s",
        app_id, tenant_id, "ok" if consent_ok else "manuell erforderlich",
    )
    return {
        "tenant_id":     tenant_id,
        "client_id":     app_id,
        "client_secret": client_secret,
        "consent_ok":    consent_ok,
        "object_id":     obj_id,
        "service_principal_id": gp_sp_id,
    }


def list_tenant_mailboxes(access_token: str, top: int = 200) -> list[dict]:
    """Listet Postfaecher (Mail-enabled Users) im Tenant via delegated Token.

    Rueckgabe: list[{id, upn, display_name, mail}] — sortiert alphabetisch.
    Leere Liste bei Fehler oder leerem Tenant.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        resp = _requests.get(
            f"{_GRAPH_URL}/users",
            headers=headers,
            params={
                "$select": "id,userPrincipalName,displayName,mail",
                # Filter weggelassen — Graph erlaubt "mail ne null" nicht
                # als simpler Filter; wir filtern stattdessen client-side.
                "$top":    str(min(max(top, 1), 999)),
                "$orderby": "displayName",
            },
            timeout=20,
        )
    except Exception as e:
        logger.warning("Guest-Print list_tenant_mailboxes Netzwerk: %s", e)
        return []
    if resp.status_code != 200:
        logger.warning("Guest-Print list_tenant_mailboxes: %s %s",
                        resp.status_code, resp.text[:300])
        return []
    out: list[dict] = []
    for u in resp.json().get("value", []):
        mail = u.get("mail") or u.get("userPrincipalName") or ""
        if not mail:
            continue
        out.append({
            "id":           u.get("id", ""),
            "upn":          u.get("userPrincipalName", "") or mail,
            "display_name": u.get("displayName", "") or mail,
            "mail":         mail,
        })
    out.sort(key=lambda x: (x["display_name"] or x["upn"]).lower())
    return out


# ─── JWT Decode ──────────────────────────────────────────────────────────────

def _decode_jwt_payload(token: str) -> dict | None:
    """
    Dekodiert den Payload eines JWT ohne Signatur-Validierung.

    Sicher, da das Token direkt über HTTPS vom Microsoft Token-Endpoint
    empfangen wurde (Transport-Level-Authentizität).
    """
    try:
        parts = token.split(".")
        if len(parts) != 3:
            logger.error("JWT: ungültiges Format (erwartet 3 Teile, bekommen %d)", len(parts))
            return None
        payload_b64 = parts[1]
        # Base64url → Base64: Padding ergänzen
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        return json.loads(base64.urlsafe_b64decode(payload_b64))
    except Exception as e:
        logger.error("JWT Decode-Fehler: %s", e)
        return None
