"""Guest-Print Entra-App-Konfiguration.

Eigene App, getrennt von der SSO-App (`entra_*`). Gleiche Speicher-
Konvention wie dort: tenant_id/client_id plaintext, client_secret
Fernet-verschluesselt ueber db._enc/_dec.

Settings-Keys:
    guestprint_entra_tenant_id     — Azure-Tenant-GUID oder UPN-Domain
    guestprint_entra_client_id     — App-Registration-ID (GUID)
    guestprint_entra_client_secret — Client Secret Value (verschluesselt)
"""

from __future__ import annotations

from db import _dec, _enc, get_setting, set_setting

_KEY_TENANT_ID     = "guestprint_entra_tenant_id"
_KEY_CLIENT_ID     = "guestprint_entra_client_id"
_KEY_CLIENT_SECRET = "guestprint_entra_client_secret"


def get_config() -> dict:
    """Liest die Guest-Print Entra-App-Config aus den Settings.

    Returns:
        dict mit tenant_id, client_id, client_secret (entschluesselt).
        Leere Strings, wenn nicht konfiguriert.
    """
    secret_enc = get_setting(_KEY_CLIENT_SECRET, "")
    try:
        secret = _dec(secret_enc) if secret_enc else ""
    except Exception:
        secret = secret_enc
    return {
        "tenant_id":     get_setting(_KEY_TENANT_ID, ""),
        "client_id":     get_setting(_KEY_CLIENT_ID, ""),
        "client_secret": secret,
    }


def set_config(tenant_id: str, client_id: str, client_secret: str) -> None:
    """Speichert die Guest-Print Entra-App-Config.

    Leerer client_secret-Parameter = "nicht aendern" (fuer Admin-Forms,
    wo der Secret aus Sicherheitsgruenden nicht per GET angezeigt wird).
    """
    set_setting(_KEY_TENANT_ID, (tenant_id or "").strip())
    set_setting(_KEY_CLIENT_ID, (client_id or "").strip())
    if client_secret:
        set_setting(_KEY_CLIENT_SECRET, _enc(client_secret.strip()))


def is_configured() -> bool:
    """True wenn alle drei Felder gesetzt sind — der Token-Fetch
    waere sonst garantiert ein 400/401."""
    cfg = get_config()
    return bool(cfg["tenant_id"] and cfg["client_id"] and cfg["client_secret"])
