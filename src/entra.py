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
