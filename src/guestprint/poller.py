"""Guest-Print Mail-Poller.

Orchestriert einen Poll-Durchlauf fuer ein ueberwachtes Postfach:

    Graph ungelesene Inbox -> Match-Sender gegen Allowlist ->
    pro Anhang: Printix-Guest provisionieren (falls noetig) + drucken ->
    Nachricht nach Processed/Skipped verschieben + Job-Log schreiben.

Idempotenz:
  * Dedupe ueber guestprint_job UNIQUE(mailbox, message, attachment). Wenn
    ein Job schon 'ok' ist, wird nicht neu gedruckt.
  * Folder-Move geschieht erst nach Attachment-Bearbeitung; ein Crash
    zwischen "gedruckt" und "verschoben" fuehrt beim naechsten Poll nur
    zu einem Dedupe-Skip, nicht zu einem Doppel-Druck.
  * Die Graph-Query filtert auf isRead=false — sobald Graph die Mail als
    gelesen markiert (passiert durch get /attachments), wird sie nicht
    wieder eingesammelt. Der Move ist also zur Aufraeumung, nicht fuer
    Korrektheit.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import db

from . import graph
from .printer import PrintFailed, PrintSkip, is_printable, print_attachment
from .printix import provision_guest

logger = logging.getLogger(__name__)


class MailboxPollResult(dict):
    """Zaehler-Dict, der beim Poll befuellt wird (fuer UI + Logs)."""


def _empty_result() -> MailboxPollResult:
    return MailboxPollResult(
        messages_seen     = 0,
        messages_matched  = 0,
        messages_skipped  = 0,  # nicht in Allowlist
        attachments_ok    = 0,
        attachments_failed= 0,
        attachments_skipped=0,  # kein PDF / leer / zu gross
        errors            = [], # list[str] — max 20
    )


def _err(result: MailboxPollResult, msg: str) -> None:
    errs: list = result["errors"]
    if len(errs) < 20:
        errs.append(msg)
    logger.warning("Guest-Print: %s", msg)


# ─── Haupt-Entry ─────────────────────────────────────────────────────────────

def process_mailbox(mailbox: dict, printix_client) -> MailboxPollResult:
    """Fuehrt einen Poll-Durchlauf fuer EIN Postfach durch.

    Args:
        mailbox:        Row aus guestprint_mailbox (db._mailbox_row).
        printix_client: printix_client.PrintixClient (fuer User- und Print-API).

    Returns: MailboxPollResult mit Zaehlern und Errors. Wirft NICHT — alle
             Fehler landen im Result.errors und werden bei `mailbox.last_error`
             gespeichert. Ein Aufrufer (Scheduler) kann sich darauf verlassen,
             dass der Poll fuer andere Mailboxen weitergeht.
    """
    result = _empty_result()
    mid = mailbox["id"]
    upn = mailbox["upn"]
    max_bytes = int(mailbox.get("max_attachment_bytes") or 26214400)

    # 1) Zielordner vorbereiten (best-effort cache pro Poll)
    try:
        processed_fid = graph.ensure_folder_path(
            upn, mailbox.get("folder_processed", "GuestPrint/Processed"),
        )
        skipped_fid = graph.ensure_folder_path(
            upn, mailbox.get("folder_skipped", "GuestPrint/Skipped"),
        )
    except Exception as e:
        _err(result, f"Folder-Setup fehlgeschlagen: {e}")
        _finalize(mid, result)
        return result

    # 2) Ungelesene Mails mit Anhaengen holen
    try:
        messages = graph.list_unread_with_attachments(upn, top=50)
    except Exception as e:
        _err(result, f"list_unread_with_attachments: {e}")
        _finalize(mid, result)
        return result

    result["messages_seen"] = len(messages)

    for msg in messages:
        try:
            _process_message(
                mailbox=mailbox,
                message=msg,
                processed_fid=processed_fid,
                skipped_fid=skipped_fid,
                max_bytes=max_bytes,
                printix_client=printix_client,
                result=result,
            )
        except Exception as e:
            # Schutznetz — einzelne Message soll Poll nicht kippen
            _err(result, f"msg={msg.get('id','?')}: unerwartet: {e}")

    _finalize(mid, result)
    return result


def _finalize(mailbox_id: str, result: MailboxPollResult) -> None:
    """Schreibt last_poll_at/last_error zurueck in die DB."""
    err_summary = "; ".join(result["errors"][:3])
    try:
        db.update_guestprint_mailbox(
            mailbox_id,
            last_poll_at=db._now() if hasattr(db, "_now") else "",
            last_error=err_summary,
        )
    except Exception as e:
        logger.warning("Poller: mailbox last_poll update fehlgeschlagen: %s", e)


# ─── Eine Nachricht ──────────────────────────────────────────────────────────

def _process_message(
    *,
    mailbox: dict,
    message: dict,
    processed_fid: str,
    skipped_fid: str,
    max_bytes: int,
    printix_client,
    result: MailboxPollResult,
) -> None:
    mid      = mailbox["id"]
    upn      = mailbox["upn"]
    msg_id   = message.get("id", "")
    sender   = (message.get("from_email") or "").strip().lower()
    subject  = message.get("subject", "") or ""

    if not msg_id:
        return

    # 1) Sender-Match
    guest = db.find_guestprint_guest_by_sender(mid, sender) if sender else None
    if not guest:
        # Nicht in Allowlist — in Skipped-Ordner verschieben, damit Inbox leer bleibt.
        result["messages_skipped"] += 1
        try:
            graph.move_message(upn, msg_id, skipped_fid)
        except Exception as e:
            _err(result, f"move(skipped) msg={msg_id}: {e}")
        return

    result["messages_matched"] += 1

    # 2) Anhaenge listen
    try:
        atts = graph.list_attachments(upn, msg_id)
    except Exception as e:
        _err(result, f"list_attachments msg={msg_id}: {e}")
        return

    # Kein fileAttachment / nur inline -> als skipped behandeln
    file_atts = [a for a in atts if not a.get("is_inline")]
    if not file_atts:
        try:
            graph.move_message(upn, msg_id, skipped_fid)
        except Exception as e:
            _err(result, f"move(no-att) msg={msg_id}: {e}")
        return

    # 3) Drucker auswaehlen — Guest-spezifisch > Mailbox-Default
    printer_id = (guest.get("printer_id") or mailbox.get("default_printer_id") or "").strip()
    queue_id   = (guest.get("queue_id")   or mailbox.get("default_queue_id")   or "").strip()
    if not (printer_id and queue_id):
        _err(result, f"msg={msg_id}: kein Drucker konfiguriert (guest + mailbox leer)")
        # Trotzdem aus der Inbox ziehen, sonst wird die Mail bei jedem Poll
        # erneut versucht. Respektiert dabei das Mailbox-`on_success`-Setting.
        _finalize_success(
            upn=upn,
            msg_id=msg_id,
            mode=(mailbox.get("on_success") or "move"),
            processed_fid=processed_fid,
            result=result,
        )
        return

    # 4) Guest ggf. in Printix provisionieren — nur EINMAL pro Guest-Zeile
    try:
        printix_user_id, owner_email = _ensure_guest_provisioned(
            printix_client, guest, result,
        )
    except Exception as e:
        _err(result, f"provision guest={guest['sender_email']}: {e}")
        # Job-Log fuer jedes Attachment trotzdem schreiben (als 'failed'),
        # damit der Admin das im Verlauf sieht.
        for a in file_atts:
            _record_failed(mid, msg_id, sender, subject, guest["id"], a, str(e))
            result["attachments_failed"] += 1
        # Nicht moven — naechster Poll darf retry versuchen (z.B. Netz-Zuckler).
        return

    # 5) Anhaenge verarbeiten
    any_processed = False
    for a in file_atts:
        try:
            status = _process_attachment(
                mailbox=mailbox,
                message=message,
                guest=guest,
                attachment=a,
                printer_id=printer_id,
                queue_id=queue_id,
                owner_email=owner_email,
                max_bytes=max_bytes,
                printix_client=printix_client,
                result=result,
            )
            if status in ("ok", "skipped", "duplicate"):
                any_processed = True
        except Exception as e:
            _err(result, f"attachment {a.get('name')}: {e}")

    # 6) last_match_at aktualisieren (auch bei skipped-attachments — der Sender
    #    hat ja *angeklopft*, das ist fuer "ist der Gast noch aktiv?"-Reports relevant).
    try:
        db.update_guestprint_guest(guest["id"], last_match_at=db._now())
    except Exception:
        pass

    # 7) Mail nach Erfolg behandeln — Mailbox-Setting `on_success`:
    #    'move'   -> in folder_processed verschieben (Default, Altverhalten)
    #    'keep'   -> in Inbox lassen, aber als gelesen markieren
    #    'delete' -> loeschen (Graph DELETE verschiebt nach 'Deleted Items')
    #    Nur wenn mindestens ein Attachment bearbeitet wurde, sonst Retry.
    if any_processed:
        _finalize_success(
            upn=upn,
            msg_id=msg_id,
            mode=(mailbox.get("on_success") or "move"),
            processed_fid=processed_fid,
            result=result,
        )


def _finalize_success(
    *,
    upn: str,
    msg_id: str,
    mode: str,
    processed_fid: str,
    result: MailboxPollResult,
) -> None:
    """Wendet die `on_success`-Regel auf eine erfolgreich verarbeitete Mail an."""
    mode = (mode or "move").lower()
    try:
        if mode == "keep":
            graph.mark_message_read(upn, msg_id)
        elif mode == "delete":
            graph.delete_message(upn, msg_id)
        else:  # 'move' + unbekannt -> Default
            graph.move_message(upn, msg_id, processed_fid)
    except Exception as e:
        _err(result, f"on_success={mode} msg={msg_id}: {e}")


# ─── Guest-Provisionierung ───────────────────────────────────────────────────

def _ensure_guest_provisioned(
    printix_client, guest: dict, result: MailboxPollResult,
) -> tuple[str, str]:
    """Sorgt dafuer, dass der Gast in Printix existiert.

    Wenn guest.printix_user_id leer ist: via provision_guest() anlegen
    oder bestehenden uebernehmen, und die DB-Zeile aktualisieren.

    Returns: (printix_user_id, owner_email)
    Raises: Exception bei API-Fehlern.
    """
    pxid  = (guest.get("printix_user_id") or "").strip()
    email = (guest.get("printix_guest_email") or guest.get("sender_email") or "").strip().lower()

    if pxid:
        return pxid, email

    info = provision_guest(
        printix_client,
        sender_email=guest.get("sender_email", ""),
        full_name=guest.get("full_name", "") or "",
        expiration_days=int(guest.get("expiration_days") or 7),
    )
    pxid  = info.get("printix_user_id", "") or ""
    email = info.get("printix_guest_email", "") or email
    if not pxid:
        raise RuntimeError("provision_guest lieferte keine user id")

    try:
        db.update_guestprint_guest(
            guest["id"],
            printix_user_id=pxid,
            printix_guest_email=email,
            expires_at=info.get("expires_at", "") or "",
        )
    except Exception as e:
        # User ist in Printix, DB-Sync schlaegt fehl — loggen und weiter,
        # naechster Poll versucht's nochmal ueber `if pxid: return` (pxid kann
        # dann ggf. ueber provision_guest.existing wiedergefunden werden).
        _err(result, f"DB-Sync nach provision: {e}")

    return pxid, email


# ─── Ein Anhang ──────────────────────────────────────────────────────────────

def _process_attachment(
    *,
    mailbox: dict,
    message: dict,
    guest: dict,
    attachment: dict,
    printer_id: str,
    queue_id: str,
    owner_email: str,
    max_bytes: int,
    printix_client,
    result: MailboxPollResult,
) -> str:
    """Verarbeitet EINEN Anhang. Returns: 'ok'|'failed'|'skipped'|'duplicate'."""
    mid     = mailbox["id"]
    upn     = mailbox["upn"]
    msg_id  = message.get("id", "")
    subject = message.get("subject", "") or ""
    sender  = (message.get("from_email") or "").strip().lower()
    name    = attachment.get("name", "") or ""
    ctype   = attachment.get("content_type", "") or ""
    size    = int(attachment.get("size") or 0)
    att_id  = attachment.get("id", "")

    # 1) Dedupe ueber UNIQUE(mailbox, message, attachment_name)
    job = db.create_guestprint_job(
        mailbox_id=mid,
        message_id=msg_id,
        attachment_name=name,
        guest_id=guest["id"],
        sender_email=sender,
        subject=subject,
        attachment_bytes=size,
        status="pending",
    )
    if not job:
        _err(result, f"Job-Row konnte nicht angelegt werden: msg={msg_id} name={name}")
        return "failed"

    # Schon erledigt? Dann nicht nochmal senden.
    if job.get("status") == "ok":
        return "duplicate"

    # 2) Filter — nicht-druckbar oder zu gross?
    if not is_printable(name, ctype):
        db.update_guestprint_job(job["id"], status="skipped",
                                  error=f"nicht druckbar ({ctype or 'unknown'})")
        result["attachments_skipped"] += 1
        return "skipped"
    if size > max_bytes > 0:
        db.update_guestprint_job(job["id"], status="skipped",
                                  error=f"Anhang zu gross ({size} > {max_bytes})")
        result["attachments_skipped"] += 1
        return "skipped"

    # 3) Download
    try:
        dl_name, dl_ctype, data = graph.download_attachment(upn, msg_id, att_id)
    except Exception as e:
        db.update_guestprint_job(job["id"], status="failed",
                                  error=f"download: {e}")
        result["attachments_failed"] += 1
        return "failed"

    if not data:
        db.update_guestprint_job(job["id"], status="skipped",
                                  error="Anhang leer nach Download")
        result["attachments_skipped"] += 1
        return "skipped"

    # 4) Printen
    title = dl_name or name or subject or "Guest-Print"
    try:
        pxjob_id = print_attachment(
            printix_client,
            printer_id=printer_id,
            queue_id=queue_id,
            title=title,
            file_bytes=data,
            content_type=dl_ctype or ctype or "application/pdf",
            owner_email=owner_email,
        )
    except PrintSkip as e:
        db.update_guestprint_job(job["id"], status="skipped", error=e.reason)
        result["attachments_skipped"] += 1
        return "skipped"
    except PrintFailed as e:
        db.update_guestprint_job(job["id"], status="failed", error=str(e))
        result["attachments_failed"] += 1
        return "failed"
    except Exception as e:
        db.update_guestprint_job(job["id"], status="failed",
                                  error=f"unerwartet: {e}")
        result["attachments_failed"] += 1
        return "failed"

    db.update_guestprint_job(job["id"], status="ok",
                              printix_job_id=pxjob_id, error="")
    result["attachments_ok"] += 1
    logger.info(
        "Guest-Print OK: mailbox=%s sender=%s att=%s printix_job=%s",
        mid, sender, name, pxjob_id,
    )
    return "ok"


def _record_failed(mailbox_id: str, message_id: str, sender: str, subject: str,
                    guest_id: str, attachment: dict, reason: str) -> None:
    """Legt fuer einen nicht-bearbeitbaren Anhang einen 'failed'-Job-Eintrag an
    (fuer Admin-Verlauf sichtbar)."""
    try:
        job = db.create_guestprint_job(
            mailbox_id=mailbox_id,
            message_id=message_id,
            attachment_name=attachment.get("name", "") or "",
            guest_id=guest_id,
            sender_email=sender,
            subject=subject,
            attachment_bytes=int(attachment.get("size") or 0),
            status="failed",
        )
        if job and job.get("status") != "ok":
            db.update_guestprint_job(job["id"], status="failed", error=reason)
    except Exception as e:
        logger.warning("Poller: _record_failed fehlgeschlagen: %s", e)


# ─── Multi-Mailbox-Tick (fuer Scheduler) ─────────────────────────────────────

def tick_all(tenant_id: str, printix_client) -> list[tuple[str, MailboxPollResult]]:
    """Polled alle aktivierten Postfaecher eines Tenants.

    Args:
        tenant_id:      Tenant-ID (Mehrmandanten-Trennung).
        printix_client: gemeinsam fuer alle Mailboxen, da Printix-Tenant identisch.

    Returns: list[(mailbox_name, MailboxPollResult)]
    """
    out: list[tuple[str, MailboxPollResult]] = []
    try:
        mailboxes = db.list_guestprint_mailboxes(tenant_id, only_enabled=True)
    except Exception as e:
        logger.error("tick_all: list_guestprint_mailboxes: %s", e)
        return out
    for m in mailboxes:
        try:
            res = process_mailbox(m, printix_client)
        except Exception as e:
            res = _empty_result()
            _err(res, f"process_mailbox crashed: {e}")
        out.append((m.get("name") or m.get("upn") or m["id"], res))
    return out
