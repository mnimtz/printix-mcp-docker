"""
Notify Helper — Event-basierte E-Mail-Benachrichtigungen
=========================================================
Prüft ob ein Ereignis für den Tenant aktiviert ist und versendet
die Benachrichtigungs-Mail über den konfigurierten Resend API-Key.

Event-Typen (in notify_events JSON-Array):
  log_error       — Kritische Log-Fehler (ERROR/CRITICAL)
  new_printer     — Neuer Drucker in Printix erkannt
  new_queue       — Neue Drucker-Queue in Printix erkannt
  new_guest_user  — Neuer Gast-Benutzer in Printix erkannt
  report_sent     — Automatischer Report wurde erfolgreich versendet
  user_registered — Neuer MCP-Benutzer hat sich registriert (Admin)
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Standard-Ereignisse die immer aktiv sind (wenn Mail konfiguriert)
DEFAULT_EVENTS: list[str] = ["log_error"]


# v6.7.25: Zentrale Mail-Credential-Resolution mit 3-stufigem Fallback.
# Wird von send_event_notification, send_employee_invitation, Reports etc.
# genutzt. Damit kann der Global-Admin im Addon einen Fallback-API-Key
# hinterlegen (`/admin/settings` → „Global Mail Fallback"), den alle Tenants
# mit-benutzen die selbst keinen hinterlegt haben.
def resolve_mail_credentials(tenant: Optional[dict]) -> dict:
    """Liefert `{api_key, mail_from, mail_from_name, source}` mit Fallback-Kette:
      1. Tenant-eigene Credentials aus der `tenants`-Tabelle
      2. Globaler Admin-Fallback aus `settings`-Tabelle
         (`global_mail_api_key`, `global_mail_from`, `global_mail_from_name`)
      3. Environment-Variablen `MAIL_API_KEY` / `MAIL_FROM`

    `source` gibt zurück wo die Credentials gefunden wurden: 'tenant',
    'global', 'env' oder 'none'. Nützlich für Logging.

    Api-Key ist in der DB verschlüsselt — wir entschlüsseln hier via db._dec.
    """
    tenant = tenant or {}

    # 1) Tenant-eigene Mail-Config
    t_key  = (tenant.get("mail_api_key") or "").strip()
    t_from = (tenant.get("mail_from") or "").strip()
    if t_key and t_from:
        return {
            "api_key": t_key,
            "mail_from": t_from,
            "mail_from_name": (tenant.get("mail_from_name") or "").strip(),
            "source": "tenant",
        }

    # 2) Globaler Admin-Fallback
    try:
        from db import get_setting, _dec
        g_key_enc = get_setting("global_mail_api_key", "")
        g_from    = (get_setting("global_mail_from", "") or "").strip()
        g_fromn   = (get_setting("global_mail_from_name", "") or "").strip()
        g_key = _dec(g_key_enc) if g_key_enc else ""
        if g_key and g_from:
            return {
                "api_key": g_key,
                "mail_from": g_from,
                "mail_from_name": g_fromn,
                "source": "global",
            }
    except Exception as _ge:
        logger.debug("Global-Mail-Fallback Resolution failed: %s", _ge)

    # 3) Env-Var-Fallback
    e_key  = (os.environ.get("MAIL_API_KEY") or "").strip()
    e_from = (os.environ.get("MAIL_FROM") or "").strip()
    if e_key and e_from:
        return {
            "api_key": e_key,
            "mail_from": e_from,
            "mail_from_name": (os.environ.get("MAIL_FROM_NAME") or "").strip(),
            "source": "env",
        }

    return {"api_key": "", "mail_from": "", "mail_from_name": "", "source": "none"}


def get_enabled_events(tenant: dict) -> list[str]:
    """Gibt die Liste der aktivierten Ereignis-Typen für diesen Tenant zurück."""
    raw = tenant.get("notify_events", "") or ""
    try:
        events = json.loads(raw)
        if isinstance(events, list):
            return [str(e) for e in events]
    except (json.JSONDecodeError, TypeError):
        pass
    return DEFAULT_EVENTS[:]


def is_event_enabled(tenant: dict, event_type: str) -> bool:
    """Prüft ob ein bestimmtes Ereignis für den Tenant aktiviert ist."""
    return event_type in get_enabled_events(tenant)


def send_event_notification(
    tenant: dict,
    event_type: str,
    subject: str,
    html_body: str,
    check_enabled: bool = True,
) -> bool:
    """
    Versendet eine Ereignis-Benachrichtigung wenn:
      1. check_enabled=True → event_type ist in notify_events
      2. alert_recipients ist konfiguriert
      3. Mail-Credentials (mail_api_key, mail_from) sind vorhanden

    Args:
        tenant:        Tenant-Dict aus get_tenant_full_by_user_id()
        event_type:    Ereignis-Schlüssel (z.B. 'new_printer')
        subject:       Betreffzeile der E-Mail
        html_body:     HTML-Body der E-Mail
        check_enabled: Wenn True, wird notify_events geprüft (Standard)

    Returns:
        True wenn Mail versendet, False wenn übersprungen oder Fehler
    """
    # Ereignis aktiviert?
    if check_enabled and not is_event_enabled(tenant, event_type):
        return False

    # Empfänger konfiguriert?
    recipients_str = tenant.get("alert_recipients", "") or ""
    recipients = [r.strip() for r in recipients_str.split(",") if r.strip()]
    if not recipients:
        logger.debug("Kein alert_recipients konfiguriert für Ereignis '%s'", event_type)
        return False

    # v6.7.25: Mail-Credentials via 3-stufiger Fallback-Resolution
    creds = resolve_mail_credentials(tenant)
    if not creds["api_key"] or not creds["mail_from"]:
        logger.debug("Keine Mail-Credentials (Kette: tenant → global → env) "
                     "für Ereignis '%s'", event_type)
        return False

    try:
        from reporting.mail_client import send_report
        send_report(
            recipients=recipients,
            subject=subject,
            html_body=html_body,
            api_key=creds["api_key"],
            mail_from=creds["mail_from"],
            mail_from_name=creds["mail_from_name"],
        )
        logger.info("Ereignis-Benachrichtigung '%s' versendet (source=%s) → %s",
                    event_type, creds["source"], ", ".join(recipients))
        return True
    except Exception as e:
        logger.error("Ereignis-Benachrichtigung '%s' fehlgeschlagen: %s", event_type, e)
        return False


# ─── HTML-Templates für häufige Ereignisse ───────────────────────────────────

def _base_html(title: str, color: str, icon: str, body_html: str) -> str:
    return f"""<!DOCTYPE html>
<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto;padding:20px;">
  <div style="background:{color};border-left:4px solid #374151;padding:16px;border-radius:4px;">
    <h2 style="margin:0 0 8px;color:#111827;">{icon} {title}</h2>
    {body_html}
  </div>
  <p style="color:#6b7280;font-size:.8em;margin-top:16px;">
    Automatische Benachrichtigung vom Printix MCP Add-on
  </p>
</body></html>"""


def html_new_printer(printer_name: str, printer_id: str, tenant_name: str = "") -> str:
    tenant_hint = f" (Tenant: {tenant_name})" if tenant_name else ""
    body = f"""<p style="margin:4px 0;color:#1f2937;">
      Neuer Drucker erkannt{tenant_hint}:<br>
      <strong>{printer_name}</strong><br>
      <small style="color:#6b7280;">ID: {printer_id}</small>
    </p>"""
    return _base_html("Neuer Drucker erkannt", "#dbeafe", "🖨️", body)


def html_new_queue(queue_name: str, queue_id: str, printer_name: str = "", tenant_name: str = "") -> str:
    printer_hint = f" am Drucker <em>{printer_name}</em>" if printer_name else ""
    tenant_hint  = f" (Tenant: {tenant_name})" if tenant_name else ""
    body = f"""<p style="margin:4px 0;color:#1f2937;">
      Neue Queue erkannt{printer_hint}{tenant_hint}:<br>
      <strong>{queue_name}</strong><br>
      <small style="color:#6b7280;">ID: {queue_id}</small>
    </p>"""
    return _base_html("Neue Drucker-Queue erkannt", "#d1fae5", "📋", body)


def html_new_guest_user(display_name: str, email: str, user_id: str, tenant_name: str = "") -> str:
    tenant_hint = f" (Tenant: {tenant_name})" if tenant_name else ""
    body = f"""<p style="margin:4px 0;color:#1f2937;">
      Neuer Gast-Benutzer erkannt{tenant_hint}:<br>
      <strong>{display_name}</strong><br>
      <small style="color:#6b7280;">{email} · ID: {user_id}</small>
    </p>"""
    return _base_html("Neuer Gast-Benutzer erkannt", "#fef9c3", "👤", body)


def html_report_sent(report_name: str, recipients: list[str], tenant_name: str = "") -> str:
    tenant_hint = f" (Tenant: {tenant_name})" if tenant_name else ""
    recip_str = ", ".join(recipients)
    body = f"""<p style="margin:4px 0;color:#1f2937;">
      Report erfolgreich versendet{tenant_hint}:<br>
      <strong>{report_name}</strong><br>
      <small style="color:#6b7280;">Empfänger: {recip_str}</small>
    </p>"""
    return _base_html("Report versendet", "#f0fdf4", "📊", body)


def html_user_registered(username: str, email: str, company: str = "") -> str:
    company_hint = f" ({company})" if company else ""
    body = f"""<p style="margin:4px 0;color:#1f2937;">
      Neuer Benutzer hat sich registriert{company_hint}:<br>
      <strong>{username}</strong><br>
      <small style="color:#6b7280;">{email}</small>
    </p>
    <p style="margin:8px 0 0;color:#374151;font-size:.9em;">
      Bitte in der Admin-Oberfläche prüfen und genehmigen oder ablehnen.
    </p>"""
    return _base_html("Neuer Benutzer registriert", "#fef3c7", "🔔", body)


# v6.7.13: Willkommens-Mail für neu angelegte Printix-User mit MCP-Mirror
def html_employee_invitation(
    full_name: str,
    username: str,
    password: str,
    login_url: str,
    admin_name: str = "",
    admin_company: str = "",
) -> str:
    """Willkommens-Mail für einen frisch angelegten Printix-User, der
    zugleich einen MCP-Self-Service-Account bekommen hat."""
    from_who = admin_name or admin_company or "dein Printix-Administrator"
    company_line = (f"<p style='margin:4px 0;color:#6b7280;font-size:.9em;'>"
                    f"{admin_company}</p>" if admin_company else "")
    body = f"""
    <p style="margin:4px 0 12px;color:#1f2937;">
      Hallo {full_name or username},
    </p>
    <p style="margin:4px 0;color:#1f2937;">
      {from_who} hat für dich einen Zugang zum <strong>Printix MCP Self-Service-Portal</strong>
      angelegt. Hier kannst du deine Cloud-Print-Jobs einsehen, Delegationen verwalten
      und deine Druck-Berechtigungen pflegen.
    </p>

    <div style="background:#f3f4f6;border-radius:8px;padding:14px 16px;margin:16px 0;">
      <div style="margin:4px 0;">
        <strong style="color:#6b7280;font-size:.85em;">Login-URL</strong><br>
        <a href="{login_url}" style="color:#2563eb;">{login_url}</a>
      </div>
      <div style="margin:10px 0 4px;">
        <strong style="color:#6b7280;font-size:.85em;">Benutzername</strong><br>
        <code style="font-size:1em;">{username}</code>
      </div>
      <div style="margin:10px 0 4px;">
        <strong style="color:#6b7280;font-size:.85em;">Initial-Passwort</strong><br>
        <code style="font-size:1em;">{password}</code><br>
        <small style="color:#6b7280;">Bitte beim ersten Login ändern.</small>
      </div>
    </div>

    <p style="margin:12px 0 4px;color:#374151;font-size:.9em;">
      Deine Druckaufträge landen in deiner persönlichen Queue — freigeben kannst
      du sie an jedem Printix-Drucker deiner Organisation, egal wo du bist.
    </p>
    {company_line}
    """
    return _base_html("Dein Printix-Portal-Zugang", "#dbeafe", "👋", body)


def send_employee_invitation(
    tenant: dict,
    recipient_email: str,
    full_name: str,
    username: str,
    password: str,
    login_url: str,
    admin_name: str = "",
) -> bool:
    """Versendet die Willkommens-Mail für einen neu angelegten MCP-Employee.

    Läuft NICHT über den Event-Gating-Mechanismus (`notify_events`) — das ist
    eine Einladung, keine Admin-Alert-Mail. Empfänger ist der frische User
    selbst (nicht `alert_recipients`).

    Returns True bei Versand-Erfolg, False wenn Mail nicht konfiguriert war
    oder Versand-Fehler auftrat. In beiden Fällen bleibt der Aufrufer
    verantwortlich das Passwort alternativ anzuzeigen (Flash-UI etc.).
    """
    # v6.7.25: Mail-Credentials via 3-stufiger Fallback-Resolution
    creds = resolve_mail_credentials(tenant)
    api_key        = creds["api_key"]
    mail_from      = creds["mail_from"]
    mail_from_name = creds["mail_from_name"]
    if not api_key or not mail_from or not recipient_email:
        logger.debug(
            "Employee-Invite skip — source=%s mail=%s from=%s recipient=%s",
            creds["source"], bool(api_key), bool(mail_from), bool(recipient_email),
        )
        return False
    company = (tenant.get("company") or "").strip()
    html = html_employee_invitation(
        full_name=full_name,
        username=username,
        password=password,
        login_url=login_url,
        admin_name=admin_name,
        admin_company=company,
    )
    subject = f"Willkommen beim Printix-Portal{f' — {company}' if company else ''}"
    try:
        from reporting.mail_client import send_report
        send_report(
            recipients=[recipient_email],
            subject=subject,
            html_body=html,
            api_key=api_key,
            mail_from=mail_from,
            mail_from_name=mail_from_name,
        )
        logger.info("Employee-Invite versendet → %s", recipient_email)
        return True
    except Exception as e:
        logger.error("Employee-Invite fehlgeschlagen (%s): %s", recipient_email, e)
        return False
