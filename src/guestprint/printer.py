"""Print-Pipeline fuer Guest-Print.

Ein Anhang -> ein Printix-Secure-Print-Job mit Owner = Gast-Email.

Ablauf (baut auf dem etablierten Submit-Flow in cloudprint/ipp_server.py auf):
  0. convert_to_pdf(attachment)     -> Office/Bilder/TXT -> PDF via LibreOffice/Pillow
  1. submit_print_job(printer_id, queue_id, title, release_immediately=False)
     -> bekommt job_id + uploadUrl/uploadLinks
  2. upload_file_to_url(upload_url, bytes, content_type)
  3. complete_upload(job_id)         -> triggert Printix zum Verarbeiten
  4. change_job_owner(job_id, email) -> Job landet in der Release-Queue
     des Gasts, NICHT im Generic-System-Manager-Postfach

release_immediately=False ist essentiell: Secure-Print heisst, der Gast
loest den Job an einem Drucker seiner Wahl aus, nicht direkt rausgedruckt.

Formatunterstuetzung:
  Wir akzeptieren alles, was upload_converter.convert_to_pdf() umwandeln
  kann — PDF (passthrough), Bilder (png/jpg/gif/bmp/tif via Pillow), Plain-
  Text und Office-Dokumente (docx/xlsx/pptx/odt/ods/odp/rtf/doc/xls/ppt via
  LibreOffice headless). Der Konverter steckt schon im Container (siehe
  Dockerfile: libreoffice-core/writer/calc/impress + Pillow) und wird vom
  Web-Upload bereits benutzt.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# upload_converter liegt auf src/-Ebene (Schwester von guestprint/).
# Wir importieren lazy in den Funktionen, damit Unit-Tests ohne PIL/Libre-
# Office laufen koennen — aber wir haengen den Pfad hier einmalig ein.
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_ROOT not in sys.path:
    sys.path.insert(0, _SRC_ROOT)

# Akzeptierte MIME-Typen (direkt oder via Konverter). Reine MIME-Pruefung
# reicht nicht immer — Graph liefert fuer Office oft 'application/octet-
# stream' — deshalb zusaetzlich der Extension-Fallback.
_ACCEPTED_TYPES = {
    "application/pdf",
    "application/postscript",
    "text/plain",
    "application/rtf",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.oasis.opendocument.text",
    "application/vnd.oasis.opendocument.spreadsheet",
    "application/vnd.oasis.opendocument.presentation",
    "image/png",
    "image/jpeg",
    "image/gif",
    "image/bmp",
    "image/tiff",
}
_ACCEPTED_EXTS = (
    ".pdf", ".ps",
    ".txt", ".rtf",
    ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".odt", ".ods", ".odp",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".tif", ".tiff",
)


class PrintSkip(Exception):
    """Anhang uebersprungen (nicht-fatal). reason wird in den Job-Log geschrieben."""
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class PrintFailed(Exception):
    """Print fehlgeschlagen (Printix-API-Fehler, Netzwerk, etc.)."""
    pass


def is_printable(name: str, content_type: str) -> bool:
    """True, wenn wir den Anhang akzeptieren (direkt PDF oder konvertierbar).

    Prueft zuerst den MIME-Type, dann den Dateinamens-Suffix — Graph liefert
    fuer viele Office-Anhaenge 'application/octet-stream', deshalb ist der
    Extension-Fallback kein Nice-to-have sondern der Normalfall.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype in _ACCEPTED_TYPES:
        return True
    if ctype.startswith("image/"):
        return True
    if name and name.lower().endswith(_ACCEPTED_EXTS):
        return True
    return False


def _ensure_pdf(file_bytes: bytes, filename: str, content_type: str) -> tuple[bytes, str]:
    """Konvertiert den Anhang bei Bedarf zu PDF. Returns (pdf_bytes, label).

    PDF wird passthrough durchgereicht. Alles andere laeuft durch
    upload_converter.convert_to_pdf — Fehler dort werfen wir als
    PrintSkip (konvertierung fehlgeschlagen -> Admin-Verlauf), nicht
    als PrintFailed, da der Printix-Submit-Flow gar nicht erst lief.
    """
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype == "application/pdf" or (filename or "").lower().endswith(".pdf"):
        return file_bytes, "pdf (passthrough)"

    try:
        from upload_converter import ConversionError, convert_to_pdf
    except ImportError as e:
        raise PrintSkip(f"Konverter nicht verfuegbar: {e}")

    try:
        pdf_bytes, label = convert_to_pdf(file_bytes, filename)
    except ConversionError as e:
        raise PrintSkip(f"Konvertierung fehlgeschlagen: {e}")
    except Exception as e:  # defensiver Catch — Pillow/LO kann breit werfen
        raise PrintSkip(f"Konvertierung unerwartet: {e}")

    logger.info(
        "Guest-Print Konvertierung: %s (%d bytes) -> PDF (%d bytes) [%s]",
        filename, len(file_bytes), len(pdf_bytes), label,
    )
    return pdf_bytes, label


def _extract_upload(submit_resp: Any) -> tuple[str, str, dict]:
    """Fischt (job_id, upload_url, upload_headers) aus der Submit-Response —
    Printix liefert das mal als 'uploadUrl' direkt, mal als uploadLinks-Liste."""
    if not isinstance(submit_resp, dict):
        return "", "", {}
    job = submit_resp.get("job", submit_resp)
    job_id = ""
    if isinstance(job, dict):
        job_id = job.get("id", "") or ""
    upload_url = submit_resp.get("uploadUrl", "") or ""
    headers: dict = {}
    if not upload_url:
        links = submit_resp.get("uploadLinks") or []
        if links and isinstance(links[0], dict):
            upload_url = links[0].get("url", "") or ""
            headers = links[0].get("headers") or {}
    return job_id, upload_url, headers


def print_attachment(
    client,
    *,
    printer_id: str,
    queue_id: str,
    title: str,
    file_bytes: bytes,
    content_type: str,
    owner_email: str,
) -> str:
    """Druckt EINEN Anhang und transferiert die Ownership.

    Args:
        client:       printix_client.PrintixClient
        printer_id:   Ziel-Printer (Release-Queue-Printer)
        queue_id:     Ziel-Queue-ID
        title:        Job-Titel (= Attachment-Dateiname, im UI sichtbar)
        file_bytes:   Rohdaten des Anhangs
        content_type: MIME-Type (fuer Upload-Header + submit-PDL)
        owner_email:  Email des Gasts (muss in Printix existieren)

    Returns: printix_job_id

    Raises:
        PrintSkip     — Anhang nicht druckbar (z.B. kein PDF, leer).
        PrintFailed   — Printix-API-Fehler im submit/upload/complete/change_owner.
    """
    if not file_bytes:
        raise PrintSkip("Anhang leer")
    if not (printer_id and queue_id):
        raise PrintFailed("Kein Drucker/Queue konfiguriert")
    if not owner_email:
        raise PrintFailed("Kein Owner-Email")

    # 0) Konvertierung (Bild/Office/TXT -> PDF). PDF wird passthrough gereicht.
    pdf_bytes, _conv_label = _ensure_pdf(file_bytes, title or "", content_type)
    pdf_ctype = "application/pdf"

    # 1) Submit
    try:
        resp = client.submit_print_job(
            printer_id=printer_id,
            queue_id=queue_id,
            title=title or "Guest-Print",
            pdl=pdf_ctype,
            release_immediately=False,
        )
    except Exception as e:
        raise PrintFailed(f"submit_print_job: {e}") from e

    job_id, upload_url, upload_headers = _extract_upload(resp)
    if not job_id:
        raise PrintFailed(f"Submit lieferte keine job id: {resp}")
    if not upload_url:
        raise PrintFailed(f"Submit lieferte keine uploadUrl (job_id={job_id})")

    # 2) Upload (immer PDF nach Konvertierung)
    try:
        client.upload_file_to_url(
            upload_url, pdf_bytes,
            pdf_ctype,
            upload_headers,
        )
    except Exception as e:
        raise PrintFailed(f"upload_file_to_url: {e}") from e

    # 3) Complete — triggert die Verarbeitung
    try:
        client.complete_upload(job_id)
    except Exception as e:
        raise PrintFailed(f"complete_upload: {e}") from e

    # 4) Change Owner — der submitted user= Parameter wird ignoriert,
    #    deshalb separat umschreiben (sonst landet der Job als
    #    System-Manager in keiner Release-Queue des Gasts).
    try:
        client.change_job_owner(job_id, owner_email)
    except Exception as e:
        # Job ist schon in Printix, aber mit falschem Owner — das ist
        # leider nicht mehr korrigierbar vom Gast aus. Wir loggen hart
        # und markieren als failed.
        raise PrintFailed(
            f"change_job_owner nach erfolgreichem Upload fehlgeschlagen "
            f"(job_id={job_id}, owner={owner_email}): {e}"
        ) from e

    logger.info(
        "Guest-Print OK: job_id=%s owner=%s title=%s (%d bytes in, %d bytes out)",
        job_id, owner_email, title, len(file_bytes), len(pdf_bytes),
    )
    return job_id
