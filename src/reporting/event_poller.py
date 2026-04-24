"""
Event Poller — Printix API Änderungserkennung per Hintergrund-Job
=================================================================
Läuft alle 30 Minuten als APScheduler-Job.
Erkennt pro Tenant neue Drucker, Queues und Gast-Benutzer und
versendet Benachrichtigungs-Mails wenn die entsprechenden Ereignisse
in notify_events aktiviert sind.

Zustand (letzte bekannte IDs) wird in der DB-Spalte poller_state
als JSON gespeichert und überlebt Neustarts.

Struktur von poller_state:
  {
    "printer_ids": ["id1", "id2"],
    "queue_ids":   ["qid1", "qid2"],
    "guest_user_ids": ["uid1"]
  }
"""

import json
import logging
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

# Polling-Intervall in Minuten
POLL_INTERVAL_MINUTES = 30

# Globale Instanz
_poller_instance: Optional["PrintixEventPoller"] = None
_poller_lock = threading.Lock()


class PrintixEventPoller:
    """
    Hintergrund-Poller: prüft alle 30 min Änderungen in der Printix API
    und sendet Benachrichtigungen für konfigurierte Ereignisse.
    """

    def __init__(self):
        self._scheduler = None

    def start(self, scheduler=None) -> None:
        """
        Startet den Polling-Job.
        Wenn ein bestehender APScheduler übergeben wird, wird der Job dort hinzugefügt.
        Sonst wird ein eigener BackgroundScheduler erstellt.
        """
        from apscheduler.schedulers.background import BackgroundScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        if scheduler is not None:
            self._scheduler = scheduler
            use_external = True
        else:
            self._scheduler = BackgroundScheduler(daemon=True)
            use_external = False

        self._scheduler.add_job(
            func=_run_poll_job,
            trigger=IntervalTrigger(minutes=POLL_INTERVAL_MINUTES),
            id="printix_event_poller",
            name="Printix Event Poller",
            replace_existing=True,
            misfire_grace_time=300,
        )

        if not use_external:
            self._scheduler.start()

        logger.info(
            "Printix Event Poller registriert (Intervall: %d min)",
            POLL_INTERVAL_MINUTES,
        )


def _run_poll_job() -> None:
    """Polling-Job: wird vom Scheduler aufgerufen."""
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")

    try:
        from db import get_all_users, get_tenant_full_by_user_id, update_poller_state
    except Exception as e:
        logger.error("Event Poller: DB-Import fehlgeschlagen: %s", e)
        return

    try:
        users = get_all_users()
    except Exception as e:
        logger.error("Event Poller: get_all_users fehlgeschlagen: %s", e)
        return

    for user in users:
        uid = user.get("id", "")
        if not uid:
            continue
        try:
            _poll_tenant(uid, get_tenant_full_by_user_id, update_poller_state)
        except Exception as e:
            logger.error("Event Poller: Fehler für Tenant %s: %s", uid, e)


def _poll_tenant(uid: str, get_tenant_fn, update_state_fn) -> None:
    """Pollt einen einzelnen Tenant und versendet ggf. Benachrichtigungen."""
    from reporting.notify_helper import (
        is_event_enabled,
        send_event_notification,
        html_new_printer,
        html_new_queue,
        html_new_guest_user,
    )

    tenant = get_tenant_fn(uid)
    if not tenant:
        return

    # Benötigt mindestens ein Printix-Polling-Ereignis sei aktiv
    wants_printer    = is_event_enabled(tenant, "new_printer")
    wants_queue      = is_event_enabled(tenant, "new_queue")
    wants_guest_user = is_event_enabled(tenant, "new_guest_user")

    if not (wants_printer or wants_queue or wants_guest_user):
        return

    # Mail muss konfiguriert sein
    if not tenant.get("mail_api_key") or not tenant.get("alert_recipients"):
        return

    # Printix API-Client erstellen
    try:
        from printix_client import PrintixClient, PrintixAPIError
        pc = PrintixClient(
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
    except Exception as e:
        logger.error("Event Poller: PrintixClient-Erstellung fehlgeschlagen: %s", e)
        return

    # Gespeicherten Zustand laden
    try:
        state = json.loads(tenant.get("poller_state", "{}") or "{}")
    except Exception:
        state = {}

    known_printer_ids    = set(state.get("printer_ids", []))
    known_queue_ids      = set(state.get("queue_ids", []))
    known_guest_user_ids = set(state.get("guest_user_ids", []))

    tenant_name = tenant.get("name", "")

    # ── Drucker und Queues pollen ────────────────────────────────────────────
    if wants_printer or wants_queue:
        try:
            printers_resp = pc.list_printers(page=0, size=200)
            _process_printers(
                printers_resp, tenant, tenant_name,
                known_printer_ids, known_queue_ids,
                wants_printer, wants_queue,
                send_event_notification, html_new_printer, html_new_queue,
            )
        except Exception as e:
            logger.warning("Event Poller: Drucker-Abfrage fehlgeschlagen für %s: %s", uid, e)

    # ── Gast-Benutzer pollen ─────────────────────────────────────────────────
    if wants_guest_user:
        try:
            users_resp = pc.list_users(role="GUEST_USER", page=0, page_size=200)
            _process_guest_users(
                users_resp, tenant, tenant_name,
                known_guest_user_ids,
                send_event_notification, html_new_guest_user,
            )
        except Exception as e:
            logger.warning("Event Poller: Gast-Benutzer-Abfrage fehlgeschlagen für %s: %s", uid, e)

    # ── Zustand speichern ─────────────────────────────────────────────────────
    new_state = {
        "printer_ids":    list(known_printer_ids),
        "queue_ids":      list(known_queue_ids),
        "guest_user_ids": list(known_guest_user_ids),
    }
    try:
        update_state_fn(uid, new_state)
    except Exception as e:
        logger.error("Event Poller: Zustand-Speicherung fehlgeschlagen für %s: %s", uid, e)


def _process_printers(
    resp, tenant, tenant_name,
    known_printer_ids: set, known_queue_ids: set,
    wants_printer: bool, wants_queue: bool,
    send_fn, html_printer_fn, html_queue_fn,
) -> None:
    """Verarbeitet die Drucker-Antwort der Printix API."""
    printers = []

    if isinstance(resp, dict):
        embedded = resp.get("_embedded", {})
        if isinstance(embedded, dict):
            printers = embedded.get("printers", [])
        if not printers:
            printers = resp.get("content", []) or resp.get("printers", [])
    elif isinstance(resp, list):
        printers = resp

    for printer in printers:
        if not isinstance(printer, dict):
            continue

        printer_id = _extract_id(printer)
        printer_name = printer.get("name", printer_id or "Unbekannt")

        if printer_id and printer_id not in known_printer_ids:
            known_printer_ids.add(printer_id)
            if wants_printer:
                try:
                    send_fn(
                        tenant=tenant,
                        event_type="new_printer",
                        subject=f"🖨️ Neuer Drucker: {printer_name}",
                        html_body=html_printer_fn(printer_name, printer_id, tenant_name),
                    )
                except Exception as e:
                    logger.error("Event Poller: Drucker-Benachrichtigung fehlgeschlagen: %s", e)

        # Queues verarbeiten (falls embedded)
        if wants_queue:
            queues = []
            if "_embedded" in printer and "queues" in printer["_embedded"]:
                queues = printer["_embedded"]["queues"]
            elif "queues" in printer:
                queues = printer["queues"]

            for queue in queues:
                if not isinstance(queue, dict):
                    continue
                queue_id   = _extract_id(queue)
                queue_name = queue.get("name", queue_id or "Unbekannt")
                if queue_id and queue_id not in known_queue_ids:
                    known_queue_ids.add(queue_id)
                    try:
                        send_fn(
                            tenant=tenant,
                            event_type="new_queue",
                            subject=f"📋 Neue Queue: {queue_name}",
                            html_body=html_queue_fn(queue_name, queue_id, printer_name, tenant_name),
                        )
                    except Exception as e:
                        logger.error("Event Poller: Queue-Benachrichtigung fehlgeschlagen: %s", e)


def _process_guest_users(
    resp, tenant, tenant_name,
    known_guest_user_ids: set,
    send_fn, html_fn,
) -> None:
    """Verarbeitet die Gast-Benutzer-Antwort der Printix API."""
    users = []
    if isinstance(resp, dict):
        users = resp.get("content", []) or resp.get("users", [])
        embedded = resp.get("_embedded", {})
        if isinstance(embedded, dict) and not users:
            users = embedded.get("users", [])
    elif isinstance(resp, list):
        users = resp

    for user in users:
        if not isinstance(user, dict):
            continue
        user_id      = user.get("id", "")
        display_name = user.get("displayName", "") or user.get("display_name", "") or user_id
        email        = user.get("email", "")

        if user_id and user_id not in known_guest_user_ids:
            known_guest_user_ids.add(user_id)
            try:
                send_fn(
                    tenant=tenant,
                    event_type="new_guest_user",
                    subject=f"👤 Neuer Gast-Benutzer: {display_name or email}",
                    html_body=html_fn(display_name, email, user_id, tenant_name),
                )
            except Exception as e:
                logger.error("Event Poller: Gast-Benutzer-Benachrichtigung fehlgeschlagen: %s", e)


def _extract_id(obj: dict) -> str:
    """Extrahiert eine ID aus einem Printix API-Objekt (verschiedene Formate)."""
    if "id" in obj:
        return str(obj["id"])
    links = obj.get("_links", {})
    if isinstance(links, dict):
        self_link = links.get("self", {})
        href = self_link.get("href", "") if isinstance(self_link, dict) else ""
        if href:
            return href.rstrip("/").split("/")[-1]
    return ""


# ─── Registrierungs-Hilfsfunktion ────────────────────────────────────────────

def register_event_poller(scheduler=None) -> None:
    """
    Registriert den Event Poller.
    Idempotent — kann mehrfach aufgerufen werden ohne Doppelregistrierung.

    Args:
        scheduler: Optionaler externer APScheduler (z.B. aus reporting.scheduler).
                   Wenn None, wird ein eigener BackgroundScheduler gestartet.
    """
    global _poller_instance
    with _poller_lock:
        if _poller_instance is not None:
            return
        _poller_instance = PrintixEventPoller()
        _poller_instance.start(scheduler=scheduler)
