"""
Mail Client — Resend API
=========================
Versendet Report-E-Mails und Log-Alerts über die Resend REST-API.

Konfiguration (Priorität):
  1. Explizite Parameter an send_report()/send_alert()
  2. Modul-Level-Override via set_credentials()  (gesetzt beim Server-Start aus DB)
  3. Umgebungsvariablen MAIL_API_KEY / MAIL_FROM  (Fallback für direkte Deployments)

From-Header-Format:
  Wenn mail_from_name gesetzt ist, wird "Name <email>" verwendet — verhindert
  Spam-Klassifizierung durch fehlenden Anzeigenamen.
  Beispiel: "Printix Reports <noreply@firma.de>"
"""

import json
import logging
import os
from typing import Optional

import requests

from .email_parser import validate_recipients

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Modul-Level-Override: gesetzt via set_credentials() aus Tenant-DB
_override_api_key:       Optional[str] = None
_override_mail_from:     Optional[str] = None
_override_mail_from_name: Optional[str] = None


def set_credentials(api_key: str, mail_from: str, mail_from_name: str = "") -> None:
    """
    Setzt Modul-Level-Credentials (z.B. beim Server-Start aus Tenant-DB).
    Überschreibt Env-Vars, wird selbst von expliziten Parametern überschrieben.
    """
    global _override_api_key, _override_mail_from, _override_mail_from_name
    _override_api_key        = api_key        or None
    _override_mail_from      = mail_from      or None
    _override_mail_from_name = mail_from_name or None
    logger.debug("Mail-Credentials gesetzt: from=%s, name=%s, key=%s…",
                 mail_from, mail_from_name, (api_key or "")[:8])


def _resolve_api_key(api_key: Optional[str] = None) -> str:
    return api_key or _override_api_key or os.environ.get("MAIL_API_KEY", "")


def _resolve_mail_from(mail_from: Optional[str] = None) -> str:
    return mail_from or _override_mail_from or os.environ.get("MAIL_FROM", "")


def _resolve_mail_from_name(mail_from_name: Optional[str] = None) -> str:
    return mail_from_name or _override_mail_from_name or os.environ.get("MAIL_FROM_NAME", "")


def _build_from_header(mail_from: str, mail_from_name: str) -> str:
    """
    Baut den From-Header.
    Mit Name:     "Printix Reports <noreply@firma.de>"
    Ohne Name:    "noreply@firma.de"
    Ein Anzeigename verhindert Spam-Klassifizierung durch fehlenden Absendernamen.
    """
    name = mail_from_name.strip()
    addr = mail_from.strip()
    if name:
        # RFC 5322: Anzeigename mit Sonderzeichen in Anführungszeichen
        if any(c in name for c in (',', '"', '<', '>', '@', ';', ':')):
            name = f'"{name}"'
        return f"{name} <{addr}>"
    return addr


def is_configured(api_key: Optional[str] = None, mail_from: Optional[str] = None) -> bool:
    """Prüft ob Mail-Versand konfiguriert ist (inkl. DB-Override und Env-Vars)."""
    return bool(_resolve_api_key(api_key) and _resolve_mail_from(mail_from))


def send_report(
    recipients: list[str],
    subject: str,
    html_body: str,
    attachments: Optional[list[dict]] = None,
    api_key: Optional[str] = None,
    mail_from: Optional[str] = None,
    mail_from_name: Optional[str] = None,
) -> dict:
    """
    Versendet einen Report per E-Mail über Resend.

    Args:
        recipients:     Liste von Empfänger-Adressen
        subject:        Betreffzeile
        html_body:      HTML-Inhalt der Mail
        attachments:    Optional — Liste von {filename, content (base64), content_type}
        api_key:        Optional — überschreibt Modul-Override und Env-Var
        mail_from:      Optional — Absender-E-Mail
        mail_from_name: Optional — Absender-Anzeigename (z.B. "Printix Reports")

    Returns:
        Resend API Response als Dict (enthält 'id' bei Erfolg)

    Raises:
        RuntimeError: bei Konfigurationsfehler oder API-Fehler
    """
    _api_key        = _resolve_api_key(api_key)
    _mail_from      = _resolve_mail_from(mail_from)
    _mail_from_name = _resolve_mail_from_name(mail_from_name)

    if not _api_key:
        raise RuntimeError(
            "MAIL_API_KEY nicht konfiguriert. "
            "Bitte Resend API-Key in den Add-on-Einstellungen hinterlegen."
        )
    if not _mail_from:
        raise RuntimeError(
            "MAIL_FROM nicht konfiguriert. "
            "Bitte Absenderadresse in den Add-on-Einstellungen hinterlegen."
        )
    if not recipients:
        raise ValueError("Keine Empfänger angegeben.")

    # Pre-Validierung: Resend lehnt ungültige Adressen mit 422 ab.
    # Besser ist ein klarer Fehler VOR dem API-Call, damit der User weiß,
    # welcher Empfänger-Eintrag kaputt ist.
    valid_recipients, errors = validate_recipients(recipients)
    if errors:
        raise ValueError(
            "Ungültige Empfänger-Adresse(n): "
            + "; ".join(errors)
            + ". Erwartetes Format: 'name@firma.de' oder 'Max Mustermann <name@firma.de>'."
        )
    if not valid_recipients:
        raise ValueError("Keine gültigen Empfänger nach Validierung.")

    from_header = _build_from_header(_mail_from, _mail_from_name)

    payload: dict = {
        "from":    from_header,
        "to":      valid_recipients,
        "subject": subject,
        "html":    html_body,
    }

    if attachments:
        payload["attachments"] = attachments

    att_names = [a.get("filename","?") for a in (attachments or [])]
    logger.info("Sende Mail: '%s' -> %s (from: %s) | Anhaenge (%d): %s", subject, ", ".join(valid_recipients), from_header, len(att_names), ", ".join(att_names))

    try:
        response = requests.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {_api_key}",
                "Content-Type":  "application/json",
            },
            data=json.dumps(payload),
            timeout=30,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"HTTP-Fehler beim Mail-Versand: {e}") from e

    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Resend API Fehler {response.status_code}: {response.text[:300]}"
        )

    result = response.json()
    logger.info("Mail versendet, ID: %s", result.get("id"))
    return result


def send_alert(
    recipients: list[str],
    subject: str,
    text_body: str,
    api_key: Optional[str] = None,
    mail_from: Optional[str] = None,
    mail_from_name: Optional[str] = None,
) -> dict:
    """
    Versendet eine einfache Text-Alert-Mail (z.B. für kritische Log-Einträge).
    Wrapper um send_report() mit auto-generiertem HTML.
    """
    html = f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:#fee2e2;border-left:4px solid #dc2626;padding:16px;border-radius:4px;">
    <h2 style="margin:0 0 8px;color:#991b1b;">⚠️ Printix MCP Alert</h2>
    <pre style="margin:0;white-space:pre-wrap;color:#1f2937;font-size:.9em;">{text_body}</pre>
  </div>
  <p style="color:#6b7280;font-size:.8em;margin-top:16px;">
    Automatische Benachrichtigung vom Printix MCP Add-on
  </p>
</body></html>"""
    return send_report(
        recipients=recipients,
        subject=subject,
        html_body=html,
        api_key=api_key,
        mail_from=mail_from,
        mail_from_name=mail_from_name,
    )
