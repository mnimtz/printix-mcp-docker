"""
Cloud Print — Delegate-Forwarder (v6.6.0)
=========================================
Protokoll-agnostischer Delegate-Forwarding-Helper. Wird ausschliesslich vom
IPP-Server (`ipp_server.py`) aufgerufen. Der frühere LPR-Server wurde in
v6.6.0 komplett entfernt — IPPS ist der einzige Cloud-Print-Eingang.

Semantik:
  Wenn User A an User B delegiert hat, soll B den Druckjob von A am Drucker
  releasen können. Da Printix Jobs per Owner sichtbar macht, senden wir den
  Job mehrfach — einmal pro Delegate — mit deren E-Mail als `user=` Parameter.

Fehler bei einzelnen Delegates stoppen die anderen nicht. Jede Kopie wird
als separater Kind-Eintrag in `cloudprint_jobs` getrackt
(parent_job_id = original job_id, delegated_from = owner_identity).
"""

from __future__ import annotations

import logging
import os
import uuid as _uuid

logger = logging.getLogger("printix.cloudprint.forwarder")


def tenant_log(tenant_id: str, level: str, msg: str) -> None:
    """Schreibt in die Tenant-Logs (sichtbar im Web-UI unter /logs)."""
    try:
        from db import add_tenant_log
        add_tenant_log(tenant_id, level, "CLOUDPRINT", msg)
    except Exception:
        pass  # Logging darf nie crashen


def forward_to_delegates(
    client,
    tenant_id_for_log: str,
    printix_tenant_id: str,
    parent_job_id: str,
    owner_identity: str,
    original_printix_job_id: str,
    target_queue: str,
    printer_id: str,
    job,
    data_format: str,
    file_bytes,
    data_path: str,
) -> None:
    """Erstellt pro aktivem Delegate eine zusätzliche Job-Kopie in Printix.

    Das Aufrufer-``job``-Objekt muss folgende Attribute haben:
      job_id, queue_name, hostname, job_name
    (siehe ipp_server.py -> _JobShim).
    """
    if not owner_identity:
        logger.debug("Delegate-Forwarding skip — kein Owner identifiziert")
        return

    from cloudprint.db_extensions import (
        get_active_delegates_for_identity, create_cloudprint_job,
        update_cloudprint_job_status,
    )

    # v6.7.17: Owner-Display-Name aus Printix-Cache ziehen für schönere
    # Titel ("delegiert von Marcus Nimtz" statt "(delegiert von
    # marcus.nimtz@marcus-nimtz.de)").
    owner_display = owner_identity
    try:
        from cloudprint.printix_cache_db import find_printix_user_by_identity
        pxowner = find_printix_user_by_identity(owner_identity)
        if pxowner:
            owner_display = (pxowner.get("full_name")
                             or pxowner.get("username")
                             or owner_identity)
    except Exception:
        pass

    delegates = get_active_delegates_for_identity(tenant_id_for_log, owner_identity)
    if not delegates:
        logger.debug("Delegate-Forwarding skip — keine aktiven Delegates für '%s'",
                     owner_identity)
        return

    logger.info("Delegate-Forwarding — %d Delegate(s) für '%s'",
                len(delegates), owner_identity)

    file_size = len(file_bytes) if file_bytes else (
        os.path.getsize(data_path) if data_path and os.path.exists(data_path) else 0
    )

    # v6.7.7: Validation — prüfe ob die Delegate-Email überhaupt als
    # Printix-User existiert. Sonst akzeptiert Printix den Submit zwar mit
    # HTTP 200, der Job ist aber für niemanden sichtbar (Black-Hole).
    from cloudprint.printix_cache_db import find_printix_user_by_identity

    for d in delegates:
        delegate_email = (d.get("email") or "").strip()
        delegate_name = d.get("full_name") or d.get("username") or delegate_email
        if not delegate_email:
            logger.debug("Delegate %s hat keine E-Mail — skip", d.get("id"))
            continue

        # Validation: existiert der Delegate als Printix-User?
        printix_match = find_printix_user_by_identity(delegate_email)
        if not printix_match:
            err = (
                f"Delegate '{delegate_email}' existiert nicht als Printix-User "
                f"in diesem Tenant. Submit würde im Black-Hole landen — Skip. "
                f"Lösung: User in Printix anlegen + Cache neu syncen "
                f"(POST /tenant/cache/refresh-users)."
            )
            logger.warning("Delegate-Validation: %s", err)
            tenant_log(tenant_id_for_log, "WARNING",
                       f"Delegate-Print für '{delegate_email}' übersprungen: {err}")
            continue

        child_job_id = f"{job.job_id}-d{_uuid.uuid4().hex[:6]}"
        # v6.7.19: Englischer Neutral-Text damit der Titel in der Printix-UI
        # (typisch englisch) konsistent wirkt. Em-Dash als Separator.
        child_title = f"{job.job_name or 'CloudPrint-Job'} — delegated by {owner_display}"

        # Kind-Tracking-Eintrag vor dem Submit anlegen
        try:
            create_cloudprint_job(
                job_id=child_job_id,
                tenant_id=tenant_id_for_log,
                queue_name=job.queue_name,
                username=delegate_email,
                hostname=job.hostname,
                job_name=child_title,
                data_size=file_size,
                data_format=data_format,
                detected_identity=delegate_email,
                identity_source="delegate-of:" + owner_identity,
                parent_job_id=job.job_id,
                delegated_from=owner_identity,
                status="forwarding",
            )
        except Exception as _ce:
            logger.warning("Delegate-Tracking-Insert fehlgeschlagen: %s", _ce)

        try:
            # v6.7.10: release_immediately=False — siehe ipp_server.py.
            # Job landet in der Release-Queue des Delegate, der ihn dann
            # am Drucker seiner Wahl freigibt.
            sub_result = client.submit_print_job(
                printer_id=printer_id,
                queue_id=target_queue,
                title=child_title,
                user=delegate_email,
                pdl=data_format,
                release_immediately=False,
            )
            sub_job = sub_result.get("job", sub_result) if isinstance(sub_result, dict) else {}
            sub_pjid = sub_job.get("id", "") if isinstance(sub_job, dict) else ""

            sub_upload_url = ""
            sub_upload_headers = {}
            if isinstance(sub_result, dict):
                sub_upload_url = sub_result.get("uploadUrl", "") or ""
                sub_links = sub_result.get("uploadLinks") or []
                if not sub_upload_url and sub_links and isinstance(sub_links[0], dict):
                    sub_upload_url = sub_links[0].get("url", "") or ""
                    sub_upload_headers = sub_links[0].get("headers") or {}

            if sub_pjid and sub_upload_url:
                data_to_upload = file_bytes
                if data_to_upload is None and data_path and os.path.exists(data_path):
                    with open(data_path, "rb") as f:
                        data_to_upload = f.read()
                if data_to_upload:
                    client.upload_file_to_url(
                        sub_upload_url, data_to_upload, data_format, sub_upload_headers,
                    )
                client.complete_upload(sub_pjid)

            # v6.7.15: Owner-Wechsel auf den Delegate. Ohne diesen Call bleibt
            # der Job im Besitz des OAuth-App-Owners (System-Manager) und
            # landet nie in der Release-Queue des Delegate.
            if sub_pjid and delegate_email and "@" in delegate_email:
                try:
                    client.change_job_owner(sub_pjid, delegate_email)
                    logger.info(
                        "Delegate-Kopie: Owner-Wechsel OK → Job %s gehört jetzt %s",
                        sub_pjid, delegate_email,
                    )
                except Exception as _co_err:
                    logger.warning(
                        "Delegate-Kopie: Owner-Wechsel fehlgeschlagen "
                        "für %s (%s): %s",
                        sub_pjid, delegate_email, _co_err,
                    )

            if sub_pjid:
                update_cloudprint_job_status(
                    child_job_id, "forwarded",
                    printix_job_id=sub_pjid, target_queue=target_queue,
                    detected_identity=delegate_email,
                    identity_source="delegate-of:" + owner_identity,
                )
                tenant_log(tenant_id_for_log, "INFO",
                    f"Delegate-Print: Kopie für '{delegate_name}' "
                    f"→ Printix-Job {sub_pjid} (Original: {original_printix_job_id}, "
                    f"Owner: {owner_identity})")
                logger.info("Delegate-Kopie OK → %s für %s", sub_pjid, delegate_email)
            else:
                err = f"Printix API gab keinen Job für Delegate {delegate_email} zurück"
                update_cloudprint_job_status(child_job_id, "error", error_message=err)
                logger.warning("%s", err)

        except Exception as de:
            err = str(de)[:300]
            update_cloudprint_job_status(child_job_id, "error", error_message=err)
            logger.warning("Delegate-Submit fehlgeschlagen für %s: %s",
                           delegate_email, de)
            tenant_log(tenant_id_for_log, "WARNING",
                       f"Delegate-Print: Kopie für {delegate_email} fehlgeschlagen — {err}")
