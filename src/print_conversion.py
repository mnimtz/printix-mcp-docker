"""
Print Conversion Helpers (v6.8.8+)
====================================
Konvertiert eingehende Datei-Bytes (PDF/PS/PCL/Text) in das fuer die
Printix-API akzeptable PDL-Format. Wird von den Workflow-Tools
print_self, print_to_recipients, session_print und send_to_user
benutzt — vorher wurden rohe PDF-Bytes an die Drucker-Queue
geschickt, was bei Druckern ohne eingebauten PDF-RIP zu
"Hieroglyphen-Druck" fuehrte (PDF-Source als ASCII-Text gerendert).

Akzeptierte Printix-PDL-Werte (von submit_print_job):
    PCL5 | PCLXL | POSTSCRIPT | UFRII | TEXT | XPS
PDF ist NICHT dabei — wir muessen serverseitig konvertieren.

Default-Target = PCL XL (pxlcolor). Das ist der universellste moderne
Drucker-Standard, kompatibel mit HP / Konica Minolta / Ricoh / Xerox /
Canon / Brother. Wer expliziet PostScript will, setzt `target="POSTSCRIPT"`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)


# Erkannte Eingabeformate. UNKNOWN heisst: nichts gepasst.
DetectedPDL = Literal["PDF", "POSTSCRIPT", "PCLXL", "PCL5", "TEXT", "UNKNOWN"]
# Mögliche Ziel-Formate fuer die Konvertierung.
TargetPDL   = Literal["PCLXL", "PCL5", "POSTSCRIPT", "PASSTHROUGH"]


def detect_pdl(file_bytes: bytes) -> DetectedPDL:
    """Magic-Byte-Detection fuer eingehende Print-Dateien.

    Erkennt:
      %PDF...           → PDF
      %!PS-Adobe        → PostScript
      ESC % -12345X     → PJL/PCL-Stream (mit PJL-Header)
      ESC E             → PCL5
      ) HP-PCL XL       → PCL XL
      Sonst: TEXT (wenn ASCII-druckbar) oder UNKNOWN.
    """
    if not file_bytes:
        return "UNKNOWN"
    head = file_bytes[:128]
    if head.startswith(b"%PDF"):
        return "PDF"
    if head.startswith(b"%!PS") or b"%!PS-Adobe" in head:
        return "POSTSCRIPT"
    # PCL XL / PJL: oft "\x1b%-12345X@PJL ..." oder direkt ") HP-PCL XL"
    if b"HP-PCL XL" in head or head.startswith(b") HP-PCL XL"):
        return "PCLXL"
    if head.startswith(b"\x1b%-12345X"):
        return "PCLXL"  # PJL-Wrapper, fast immer PCL XL drinnen
    if head.startswith(b"\x1bE") or head.startswith(b"\x1b&") or head.startswith(b"\x1b*"):
        return "PCL5"
    # Heuristik: druckbare ASCII-Sequenz → reiner Text
    try:
        sample = head.decode("utf-8")
        if all(c.isprintable() or c in "\r\n\t " for c in sample[:64]):
            return "TEXT"
    except UnicodeDecodeError:
        pass
    return "UNKNOWN"


def _gs_available() -> bool:
    """Ist Ghostscript installiert?"""
    return shutil.which("gs") is not None


class ConversionError(RuntimeError):
    """Konvertierung fehlgeschlagen — wird vom Tool als Fehler an den User gegeben."""


def _run_gs(args: list[str], input_bytes: bytes, timeout_s: int = 60) -> bytes:
    """Fuehrt Ghostscript mit args aus, leitet input_bytes auf stdin und
    sammelt stdout. Wirft ConversionError bei Fehler."""
    if not _gs_available():
        raise ConversionError("Ghostscript ('gs') ist im Container nicht installiert. "
                              "Image rebuilden (Dockerfile installiert es ab v6.8.8).")
    cmd = ["gs"] + args
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            capture_output=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError(f"Ghostscript-Timeout nach {timeout_s}s — Datei zu komplex?")
    except FileNotFoundError as e:
        raise ConversionError(f"Ghostscript-Aufruf fehlgeschlagen: {e}")
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")[:1500]
        raise ConversionError(f"Ghostscript-Fehler (rc={proc.returncode}): {stderr}")
    return proc.stdout


def pdf_to_pclxl(pdf_bytes: bytes, *, color: bool = True) -> bytes:
    """PDF → PCL XL via Ghostscript (`pxlcolor` / `pxlmono`)."""
    device = "pxlcolor" if color else "pxlmono"
    return _run_gs(
        [
            "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            f"-sDEVICE={device}",
            "-sOutputFile=-",
            "-",  # stdin
        ],
        pdf_bytes,
    )


def pdf_to_pcl5(pdf_bytes: bytes, *, color: bool = False) -> bytes:
    """PDF → PCL5e/c. PCL5 hat keinen color-Variantengeraet
    (pcl5c ist abgekuendigt) — wir nehmen ljet4 (mono) oder
    cdjcolor (DeskJet-Color) als pragmatische Defaults."""
    device = "cdjcolor" if color else "ljet4"
    return _run_gs(
        [
            "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            f"-sDEVICE={device}",
            "-sOutputFile=-",
            "-",
        ],
        pdf_bytes,
    )


def pdf_to_postscript(pdf_bytes: bytes) -> bytes:
    """PDF → PostScript (Level 2/3) via Ghostscript `ps2write`."""
    return _run_gs(
        [
            "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER",
            "-sDEVICE=ps2write",
            "-sOutputFile=-",
            "-",
        ],
        pdf_bytes,
    )


def text_to_postscript(text: str) -> bytes:
    """Plaintext → minimales A4-PostScript via Ghostscript-Hilfsprogramm
    `enscript`. Falls enscript fehlt: hand-baut einen einfachen
    PS-Wrapper.

    Wir nutzen die ueberall-vorhandene Manuell-Variante (kein extra
    Paket). Fuer wirklich schoene Text-Drucke besser PDF erzeugen +
    pdf_to_pclxl().
    """
    safe = (text or "").replace("(", "[").replace(")", "]")
    # Sehr einfaches PS-Skelett, ein Helvetica-Block mit Zeilenumbruch
    lines = safe.splitlines() or [""]
    body_ops = []
    y = 760
    for line in lines:
        if y < 60:
            break
        body_ops.append(f"72 {y} moveto ({line[:120]}) show")
        y -= 14
    body = "\n".join(body_ops)
    ps = (
        "%!PS-Adobe-3.0\n"
        "%%Pages: 1\n"
        "/Helvetica findfont 11 scalefont setfont\n"
        f"{body}\n"
        "showpage\n"
        "%%EOF\n"
    )
    return ps.encode("utf-8")


def prepare_for_print(file_bytes: bytes,
                       target: TargetPDL = "PCLXL",
                       color: bool = True) -> tuple[bytes, str]:
    """Hauptentry der Workflow-Tools.

    Detected source PDL → konvertiert zu Ziel-PDL. Bei PASSTHROUGH wird
    nichts angefasst (User explizit gewuenscht). Returns
    (output_bytes, pdl_label) wo `pdl_label` einer von
    "PCLXL"/"PCL5"/"POSTSCRIPT"/"TEXT"/"PCL5" ist — direkt brauchbar
    fuer printix_client.submit_print_job(pdl=...).

    Wirft ConversionError mit klarer Message bei Problemen — die Tools
    fangen das und reichen es als sauberen `{"error": "..."}` an den
    User durch (statt silent rohe Bytes zu schicken).
    """
    src = detect_pdl(file_bytes)
    logger.info("prepare_for_print: detected=%s target=%s color=%s size=%d",
                src, target, color, len(file_bytes or b""))

    if target == "PASSTHROUGH":
        # User uebernimmt Verantwortung; wir mappen nur das Detected-Label.
        if src in ("PDF", "POSTSCRIPT", "PCLXL", "PCL5", "TEXT"):
            mapped = "POSTSCRIPT" if src == "PDF" else src  # PDF darf direkt nicht
            if src == "PDF":
                logger.warning("PASSTHROUGH mit PDF-Input — die Printix-API "
                                "akzeptiert PDF nicht als PDL. Drucker werden "
                                "wahrscheinlich Hieroglyphen drucken. "
                                "Nutze target=PCLXL fuer automatische Konvertierung.")
            return file_bytes, mapped
        return file_bytes, "POSTSCRIPT"

    # Source bereits passend zum Ziel → durchreichen
    if src == target:
        return file_bytes, target

    # Konvertierungs-Matrix (alles geht ueber Ghostscript)
    if src == "PDF":
        if target == "PCLXL":
            return pdf_to_pclxl(file_bytes, color=color), "PCLXL"
        if target == "PCL5":
            return pdf_to_pcl5(file_bytes, color=color), "PCL5"
        if target == "POSTSCRIPT":
            return pdf_to_postscript(file_bytes), "POSTSCRIPT"

    if src == "POSTSCRIPT":
        if target == "PCLXL":
            return pdf_to_pclxl(file_bytes, color=color), "PCLXL"  # gs nimmt PS direkt
        if target == "PCL5":
            return pdf_to_pcl5(file_bytes, color=color), "PCL5"
        if target == "POSTSCRIPT":
            return file_bytes, "POSTSCRIPT"

    if src == "TEXT":
        # Text → PostScript-Wrapper, dann zum Ziel weiter
        ps = text_to_postscript(file_bytes.decode("utf-8", errors="replace"))
        if target == "POSTSCRIPT":
            return ps, "POSTSCRIPT"
        if target == "PCLXL":
            return pdf_to_pclxl(ps, color=color), "PCLXL"
        if target == "PCL5":
            return pdf_to_pcl5(ps, color=color), "PCL5"

    if src in ("PCLXL", "PCL5"):
        # PCL bleibt PCL — nicht reverse-konvertieren, das ist fragil.
        # Wenn user PCLXL will und wir haben PCL5 (oder umgekehrt), durchreichen
        # mit eigenem Label; die meisten modernen Drucker akzeptieren beides.
        return file_bytes, src

    raise ConversionError(
        f"Eingabeformat nicht erkannt (Magic-Bytes: {file_bytes[:16].hex()}). "
        f"Akzeptiert: PDF, PostScript, PCL5, PCLXL, ASCII-Text. "
        f"Andere Formate (DOCX/XLSX/PNG/JPG) bitte vorher zu PDF konvertieren."
    )


__all__ = [
    "ConversionError",
    "DetectedPDL",
    "TargetPDL",
    "detect_pdl",
    "pdf_to_pclxl",
    "pdf_to_pcl5",
    "pdf_to_postscript",
    "text_to_postscript",
    "prepare_for_print",
]
