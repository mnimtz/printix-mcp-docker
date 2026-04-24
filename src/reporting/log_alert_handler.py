"""
Log Alert Handler — E-Mail-Benachrichtigung bei kritischen Log-Einträgen
========================================================================
Hängt sich als logging.Handler in den Root-Logger ein.
Iteriert alle Tenants mit konfiguriertem alert_recipients und sendet
eine Alert-Mail über deren eigenen Resend-API-Key.

Konfiguration pro Tenant (in der Web-UI):
  alert_recipients  — kommagetrennte Empfänger-Adressen
  alert_min_level   — Mindest-Log-Level: WARNING | ERROR | CRITICAL (default: ERROR)

Rate-Limiting: max. 1 Alert-Mail pro Tenant alle 5 Minuten.
"""

import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Globale Rate-Limit-Tabelle: {user_id: last_sent_timestamp}
_last_sent: dict[str, float] = {}
_lock = threading.Lock()

# Minimaler Abstand zwischen zwei Alert-Mails pro Tenant (Sekunden)
RATE_LIMIT_SECONDS = 300


def _level_num(level_name: str) -> int:
    """Gibt den numerischen Log-Level zurück (default: ERROR)."""
    return getattr(logging, level_name.upper(), logging.ERROR)


class PrintixAlertHandler(logging.Handler):
    """
    Logging-Handler der bei Erreichen des konfigurierten Log-Levels
    Alert-Mails an alle Tenants mit alert_recipients-Konfiguration sendet.

    Wird einmal beim Server-Start registriert.
    Verhindert rekursive Auslösung durch eigene Log-Nachrichten.
    """

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self._in_emit = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        # Rekursionsschutz — eigene Logs nicht weiter verarbeiten
        if getattr(self._in_emit, "active", False):
            return
        self._in_emit.active = True
        try:
            self._dispatch(record)
        finally:
            self._in_emit.active = False

    def _dispatch(self, record: logging.LogRecord) -> None:
        """Lädt alle Tenants und sendet Alert-Mails wenn Bedingungen erfüllt sind."""
        try:
            import sys
            if "/app" not in sys.path:
                sys.path.insert(0, "/app")

            from db import get_all_users, get_tenant_full_by_user_id
            from reporting.mail_client import send_alert

            users = get_all_users()
            now = time.monotonic()

            for user in users:
                try:
                    uid = user.get("id", "")
                    if not uid:
                        continue

                    tenant = get_tenant_full_by_user_id(uid)
                    if not tenant:
                        continue

                    alert_recipients = tenant.get("alert_recipients", "").strip()
                    alert_min_level  = tenant.get("alert_min_level", "ERROR").strip()
                    mail_api_key     = tenant.get("mail_api_key", "").strip()
                    mail_from        = tenant.get("mail_from", "").strip()
                    mail_from_name   = tenant.get("mail_from_name", "").strip()

                    # Prüfen ob Alert-Mail konfiguriert ist
                    if not alert_recipients or not mail_api_key:
                        continue

                    # Prüfen ob log_error-Ereignis in notify_events aktiviert ist
                    notify_events_raw = tenant.get("notify_events", '["log_error"]') or '["log_error"]'
                    try:
                        import json as _json
                        notify_events = _json.loads(notify_events_raw)
                    except Exception:
                        notify_events = ["log_error"]
                    if "log_error" not in notify_events:
                        continue

                    # Log-Level-Schwelle prüfen
                    if record.levelno < _level_num(alert_min_level):
                        continue

                    # Rate-Limiting prüfen
                    with _lock:
                        last = _last_sent.get(uid, 0.0)
                        if now - last < RATE_LIMIT_SECONDS:
                            continue
                        _last_sent[uid] = now

                    # Empfänger aus kommagetrennte Liste
                    recipients = [r.strip() for r in alert_recipients.split(",") if r.strip()]
                    if not recipients:
                        continue

                    # Alert versenden
                    subject = f"⚠️ Printix MCP Alert [{record.levelname}]"
                    send_alert(
                        recipients=recipients,
                        subject=subject,
                        text_body=self.format(record),
                        api_key=mail_api_key,
                        mail_from=mail_from,
                        mail_from_name=mail_from_name,
                    )

                except Exception as inner:
                    # Fehler pro Tenant still ignorieren (kein rekursiver Loop)
                    pass

        except Exception as e:
            # Fehler im Handler nie nach oben propagieren
            pass


# ─── Registrierungs-Hilfsfunktion ────────────────────────────────────────────

_handler_instance: Optional[PrintixAlertHandler] = None


def register_alert_handler() -> None:
    """
    Hängt den Alert-Handler in den Root-Logger ein.
    Idempotent — kann mehrfach aufgerufen werden ohne Doppelregistrierung.
    """
    global _handler_instance
    root = logging.getLogger()

    # Bereits registriert?
    for h in root.handlers:
        if isinstance(h, PrintixAlertHandler):
            return

    _handler_instance = PrintixAlertHandler()
    _handler_instance.setLevel(logging.WARNING)
    root.addHandler(_handler_instance)
    logger.info("Printix Log-Alert-Handler registriert (Rate-Limit: %ds)", RATE_LIMIT_SECONDS)
