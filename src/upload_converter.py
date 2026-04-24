"""
Upload-Konverter (v6.7.28)
===========================
Konvertiert verschiedene Dokument-Formate zu PDF, damit der Web-Upload
nicht nur PDFs akzeptiert sondern auch Office-Dateien, Bilder und Text.

Konverter-Chain:
  - application/pdf                → passthrough (keine Konvertierung nötig)
  - image/png, jpg, gif, bmp, tiff → Pillow → PDF
  - text/plain                     → Pillow/einfacher Renderer → PDF
  - docx, xlsx, pptx, odt, ods, odp, rtf
    + alles andere Office-ähnliche → LibreOffice headless → PDF

Alle Konverter arbeiten über ein temporäres Verzeichnis und geben die
fertige PDF als bytes zurück. Bei Fehler wird eine ConversionError
ausgelöst die der Caller als 502/Benutzer-Feedback weiterreichen kann.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from typing import Optional

logger = logging.getLogger("printix.upload_converter")


class ConversionError(Exception):
    """Konvertierung fehlgeschlagen — Nachricht user-lesbar."""


# ─── Format-Detection ────────────────────────────────────────────────────────

# Magic-Bytes → (mime, file_extension-hint)
_MAGIC_SIGNATURES: list[tuple[bytes, str, str]] = [
    (b"%PDF",                         "application/pdf",            "pdf"),
    (b"\x89PNG\r\n\x1a\n",            "image/png",                  "png"),
    (b"\xff\xd8\xff",                 "image/jpeg",                 "jpg"),
    (b"GIF87a",                       "image/gif",                  "gif"),
    (b"GIF89a",                       "image/gif",                  "gif"),
    (b"BM",                           "image/bmp",                  "bmp"),
    (b"II*\x00",                      "image/tiff",                 "tif"),
    (b"MM\x00*",                      "image/tiff",                 "tif"),
    (b"PK\x03\x04",                   "application/zip",            "zip"),  # docx/xlsx/pptx/odt start so
    (b"{\\rtf",                       "application/rtf",            "rtf"),
]


def detect_format(data: bytes, filename_hint: str = "") -> tuple[str, str]:
    """Ermittelt (mime, extension) anhand Magic-Bytes + Dateinamen.

    Für ZIP-basierte Office-Dokumente unterscheiden wir via Dateinamens-
    Endung zwischen docx/xlsx/pptx/odt/etc., weil der ZIP-Header gleich ist.
    """
    if len(data) < 4:
        return "application/octet-stream", ""
    for magic, mime, ext in _MAGIC_SIGNATURES:
        if data.startswith(magic):
            if mime == "application/zip":
                # Office-Format aus Dateinamen ableiten
                name = (filename_hint or "").lower()
                office_map = {
                    ".docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx"),
                    ".xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",       "xlsx"),
                    ".pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation","pptx"),
                    ".odt":  ("application/vnd.oasis.opendocument.text",                                 "odt"),
                    ".ods":  ("application/vnd.oasis.opendocument.spreadsheet",                          "ods"),
                    ".odp":  ("application/vnd.oasis.opendocument.presentation",                        "odp"),
                }
                for sfx, (m, e) in office_map.items():
                    if name.endswith(sfx):
                        return m, e
                return mime, ext
            return mime, ext
    # Fallback: Plaintext? Alles ASCII/UTF-8-druckbar → text
    try:
        sample = data[:4096].decode("utf-8")
        if all(c.isprintable() or c in "\r\n\t" for c in sample):
            return "text/plain", "txt"
    except UnicodeDecodeError:
        pass
    return "application/octet-stream", ""


# ─── Konverter-Implementierungen ─────────────────────────────────────────────

def _convert_image_to_pdf(data: bytes) -> bytes:
    """PNG/JPG/GIF/BMP/TIFF → PDF via Pillow."""
    try:
        from PIL import Image
    except ImportError as e:
        raise ConversionError("Pillow (python3-pil) nicht installiert") from e
    import io
    img = Image.open(io.BytesIO(data))
    # Manche Bilder haben Transparenz/Palette → nach RGB
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=150.0)
    return out.getvalue()


def _convert_text_to_pdf(data: bytes) -> bytes:
    """Plaintext → PDF (einfaches Monospaced-Layout via Pillow+Rendering).

    Wir rendern kein echtes typesetting — für lange Texte eignet sich
    LibreOffice besser. Hier nur für Notizen/TXT-Snippets.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as e:
        raise ConversionError("Pillow (python3-pil) nicht installiert") from e
    import io
    text = data.decode("utf-8", errors="replace")
    # A4 @150dpi = ~1240 x 1754 Pixel
    W, H = 1240, 1754
    margin = 60
    img = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", 14)
    except Exception:
        font = ImageFont.load_default()
    y = margin
    line_height = 20
    max_chars_per_line = (W - 2 * margin) // 8  # grobe Schätzung
    for raw_line in text.splitlines() or [""]:
        # Lange Zeilen umbrechen
        while raw_line:
            chunk = raw_line[:max_chars_per_line]
            raw_line = raw_line[max_chars_per_line:]
            draw.text((margin, y), chunk, fill="black", font=font)
            y += line_height
            if y > H - margin:
                break
        if y > H - margin:
            break
    out = io.BytesIO()
    img.save(out, format="PDF", resolution=150.0)
    return out.getvalue()


def _convert_libreoffice(data: bytes, src_ext: str) -> bytes:
    """Office-Formate → PDF via `libreoffice --headless`.

    Spec:
      - Schreibt Eingabe in /tmp/<uuid>.<src_ext>
      - Ruft `libreoffice --headless --convert-to pdf --outdir /tmp/<uuid>_out <input>`
      - Liest die produzierte PDF zurück
      - Räumt danach /tmp auf
    """
    if not shutil.which("libreoffice") and not shutil.which("soffice"):
        raise ConversionError(
            "LibreOffice ist im Container nicht installiert — "
            "dieses Format kann nicht konvertiert werden."
        )
    binary = shutil.which("libreoffice") or shutil.which("soffice")

    import uuid
    work = tempfile.mkdtemp(prefix="printix-conv-")
    try:
        in_path = os.path.join(work, f"input.{src_ext}")
        out_dir = os.path.join(work, "out")
        os.makedirs(out_dir, exist_ok=True)
        with open(in_path, "wb") as f:
            f.write(data)

        # LibreOffice braucht HOME (sonst Profil-Fehler in Containern)
        env = os.environ.copy()
        env["HOME"] = work
        proc = subprocess.run(
            [binary, "--headless", "--convert-to", "pdf",
             "--outdir", out_dir, in_path],
            # v6.7.40: 120s war beim ersten DOCX-Send zu knapp — LibreOffice-
            # Coldstart im Container frisst ~60-90s nur fürs Profil-Init, dann
            # kommt noch die eigentliche Konvertierung. 300s gibt genug Luft.
            env=env, timeout=300,
            capture_output=True,
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", errors="replace")[:500]
            raise ConversionError(f"LibreOffice-Konvertierung fehlgeschlagen: {err}")

        # PDF im out_dir finden
        produced = [f for f in os.listdir(out_dir) if f.lower().endswith(".pdf")]
        if not produced:
            raise ConversionError("LibreOffice produzierte keine PDF-Datei")
        out_path = os.path.join(out_dir, produced[0])
        with open(out_path, "rb") as f:
            return f.read()
    finally:
        try:
            shutil.rmtree(work, ignore_errors=True)
        except Exception:
            pass


# ─── Orchestrierung ──────────────────────────────────────────────────────────

def convert_to_pdf(data: bytes, filename: str = "") -> tuple[bytes, str]:
    """Haupt-Entry: erkennt Format, ruft passenden Konverter auf.

    Returns: (pdf_bytes, source_format_label)
    Raises: ConversionError falls das Format nicht konvertierbar ist
    """
    mime, ext = detect_format(data, filename)
    logger.info("Upload-Konverter: Input erkannt — mime=%s ext=%s size=%d",
                mime, ext, len(data))

    if mime == "application/pdf":
        return data, "pdf (passthrough)"

    if mime.startswith("image/"):
        pdf = _convert_image_to_pdf(data)
        return pdf, f"image ({ext}) → pdf"

    if mime == "text/plain":
        pdf = _convert_text_to_pdf(data)
        return pdf, "text → pdf"

    # Office-Formate + RTF → LibreOffice
    libreoffice_formats = {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/rtf",
    }
    if mime in libreoffice_formats or ext in ("docx","xlsx","pptx","odt","ods","odp","doc","xls","ppt","rtf"):
        pdf = _convert_libreoffice(data, ext or "bin")
        return pdf, f"libreoffice ({ext or mime}) → pdf"

    raise ConversionError(
        f"Format nicht unterstützt: mime={mime} ext={ext}. "
        f"Erlaubt: PDF, docx/xlsx/pptx, odt/ods/odp, rtf, TXT, png/jpg/gif/bmp/tiff."
    )


def is_libreoffice_available() -> bool:
    """Für UI-Hinweise nützlich."""
    return bool(shutil.which("libreoffice") or shutil.which("soffice"))
