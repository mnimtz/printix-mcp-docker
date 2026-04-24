"""Guest-Print Scheduler-Wiring.

Haengt einen einzigen Meta-Tick-Job an den bestehenden APScheduler (aus
reporting/scheduler.py). Der Tick laeuft alle 60s und entscheidet pro
Mailbox selbst, ob deren `poll_interval_sec` seit `last_poll_at` erreicht
ist — so muessen wir nicht fuer jede Mailbox einen eigenen APScheduler-Job
anlegen und das UI muss keinen Reschedule bei Edit/Create triggern.
Aenderungen an Postfaechern werden beim naechsten Tick automatisch
beruecksichtigt.

Kein Per-Mailbox-Job heisst auch: UI-Code bleibt schlank, und ein Absturz
des Scheduler-Prozesses fuehrt nicht zu verlorenen APScheduler-Jobs
(APScheduler ist nicht persistent konfiguriert).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_JOB_ID    = "guestprint_meta_tick"
_TICK_SEC  = 60  # Meta-Tick-Intervall — begrenzt die Poll-Granularitaet nach unten


# ─── Tenant-Iterator ─────────────────────────────────────────────────────────

def _list_tenant_ids_with_enabled_mailboxes() -> list[str]:
    """Einmalige Abfrage: alle tenant_ids, die mindestens ein aktives Postfach
    haben. Wir vermeiden damit das Laden/Entschluesseln von Tenants, die
    gar nichts zu pollen haben."""
    import db
    try:
        with db._conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM guestprint_mailbox WHERE enabled = 1"
            ).fetchall()
        return [r["tenant_id"] for r in rows if r["tenant_id"]]
    except Exception as e:
        logger.warning("Guest-Print Scheduler: tenant-id-query fehlgeschlagen: %s", e)
        return []


def _load_tenant_full(tenant_id: str) -> Optional[dict]:
    """Tenant-Full-Record via user_id holen (fuer Printix-Credentials)."""
    import db
    try:
        with db._conn() as conn:
            row = conn.execute(
                "SELECT user_id FROM tenants WHERE id = ?", (tenant_id,)
            ).fetchone()
        if not row or not row["user_id"]:
            return None
        return db.get_tenant_full_by_user_id(row["user_id"])
    except Exception as e:
        logger.warning("Guest-Print Scheduler: tenant-load fehlgeschlagen (%s): %s",
                        tenant_id, e)
        return None


# ─── Meta-Tick ───────────────────────────────────────────────────────────────

def _iso_to_epoch(iso_str: str) -> float:
    """Parsed einen db._now()-Timestamp (ISO-8601 UTC ohne TZ) zu epoch seconds.
    Leere/ungueltige Strings -> 0.0 (= "nie gepolled")."""
    if not iso_str:
        return 0.0
    try:
        # db._now() liefert z.B. '2025-11-23T15:04:12.123456'
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def tick() -> dict:
    """EIN Meta-Tick — iteriert alle Tenants mit aktiven Mailboxes und
    polled diejenigen, deren poll_interval abgelaufen ist.

    Returns: {"tenants": N, "mailboxes_polled": N, "errors": [...]}.
    """
    import db
    from . import poller as gp_poller

    summary = {"tenants": 0, "mailboxes_polled": 0, "errors": []}
    now_epoch = datetime.now(timezone.utc).timestamp()
    tenant_ids = _list_tenant_ids_with_enabled_mailboxes()
    if not tenant_ids:
        return summary

    # PrintixClient-Factory: wir importieren lazy, damit das Modul auch
    # ohne konfigurierten Client ladebar bleibt (z.B. Test/Import-Zeit).
    import os, sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from printix_client import PrintixClient

    for tid in tenant_ids:
        summary["tenants"] += 1
        tenant = _load_tenant_full(tid)
        if not tenant:
            continue

        try:
            client = PrintixClient(
                tenant_id           = tenant.get("printix_tenant_id", ""),
                print_client_id     = tenant.get("print_client_id") or None,
                print_client_secret = tenant.get("print_client_secret") or None,
                card_client_id      = tenant.get("card_client_id") or None,
                card_client_secret  = tenant.get("card_client_secret") or None,
                ws_client_id        = tenant.get("ws_client_id") or None,
                ws_client_secret    = tenant.get("ws_client_secret") or None,
                um_client_id        = tenant.get("um_client_id") or None,
                um_client_secret    = tenant.get("um_client_secret") or None,
                shared_client_id    = tenant.get("shared_client_id") or None,
                shared_client_secret= tenant.get("shared_client_secret") or None,
            )
        except Exception as e:
            summary["errors"].append(f"tenant {tid}: client init: {e}")
            continue

        try:
            mailboxes = db.list_guestprint_mailboxes(tid, only_enabled=True)
        except Exception as e:
            summary["errors"].append(f"tenant {tid}: list_mailboxes: {e}")
            continue

        for mb in mailboxes:
            interval = int(mb.get("poll_interval_sec") or 60)
            last_epoch = _iso_to_epoch(mb.get("last_poll_at", ""))
            if last_epoch and (now_epoch - last_epoch) < interval:
                continue  # noch nicht wieder faellig
            try:
                gp_poller.process_mailbox(mb, client)
                summary["mailboxes_polled"] += 1
            except Exception as e:
                # process_mailbox wirft eigentlich nicht — falls doch, das
                # mailbox.last_poll_at bleibt stehen, damit der naechste
                # Tick nicht sofort nochmal in den Fehler laeuft.
                summary["errors"].append(
                    f"mailbox {mb.get('upn','?')}: {e}"
                )

    if summary["errors"]:
        logger.warning(
            "Guest-Print Tick: %d Tenants, %d Mailboxes gepollt, %d Fehler",
            summary["tenants"], summary["mailboxes_polled"], len(summary["errors"]),
        )
    elif summary["mailboxes_polled"]:
        logger.info(
            "Guest-Print Tick: %d Mailboxes gepollt",
            summary["mailboxes_polled"],
        )
    return summary


# ─── Bootstrap ───────────────────────────────────────────────────────────────

def start_guestprint_scheduler() -> bool:
    """Registriert den Meta-Tick-Job im bestehenden APScheduler.

    Returns: True, wenn der Job eingehaengt wurde (oder schon lief);
             False, wenn APScheduler nicht verfuegbar ist.
    """
    try:
        from reporting.scheduler import APSCHEDULER_AVAILABLE, _get_scheduler
    except Exception as e:
        logger.warning("Guest-Print Scheduler: reporting.scheduler nicht ladbar: %s", e)
        return False
    if not APSCHEDULER_AVAILABLE:
        logger.info("Guest-Print Scheduler: APScheduler nicht installiert — skip")
        return False
    try:
        sched = _get_scheduler()
    except Exception as e:
        logger.warning("Guest-Print Scheduler: _get_scheduler fehlgeschlagen: %s", e)
        return False

    # Idempotent: falls schon gesetzt, lassen wir's stehen.
    if sched.get_job(_JOB_ID):
        return True

    from apscheduler.triggers.interval import IntervalTrigger
    sched.add_job(
        tick,
        trigger=IntervalTrigger(seconds=_TICK_SEC),
        id=_JOB_ID,
        name="Guest-Print Meta-Tick",
        replace_existing=True,
    )
    logger.info("Guest-Print Scheduler: Meta-Tick alle %ds registriert", _TICK_SEC)
    return True


def stop_guestprint_scheduler() -> None:
    """Entfernt den Meta-Tick (fuer Tests / kontrollierten Shutdown)."""
    try:
        from reporting.scheduler import _get_scheduler
        sched = _get_scheduler()
        if sched.get_job(_JOB_ID):
            sched.remove_job(_JOB_ID)
            logger.info("Guest-Print Scheduler: Meta-Tick entfernt")
    except Exception as e:
        logger.warning("Guest-Print Scheduler: stop fehlgeschlagen: %s", e)
