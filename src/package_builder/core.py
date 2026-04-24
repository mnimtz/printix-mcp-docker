"""
Package Builder Core
Orchestriert Upload, Analyse, Patch und Download.

Sicherheit:
  - ZIPs werden NUR temporär gespeichert (tempfile.mkdtemp)
  - Ergebnis-ZIPs sind max. TTL_SECONDS verfügbar
  - Secrets dürfen nicht in Logs erscheinen
  - Nach dem Download wird die Session automatisch bereinigt
"""
from __future__ import annotations

import logging
import os
import secrets
import shutil
import tempfile
import time
import zipfile
from typing import Dict, List, Optional, Tuple

from .models import AnalysisResult, PatchResult
from .vendors import detect_vendor, get_vendor, list_vendors

logger = logging.getLogger("printix.package_builder")

# Maximale Lebenszeit einer Session (in Sekunden)
TTL_SECONDS = 3600  # 1 Stunde

# Maximale Upload-Größe (Bytes)
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB


class BuildSession:
    """Repräsentiert eine aktive Builder-Session."""

    def __init__(self, session_id: str, vendor_id: str, upload_path: str):
        self.session_id = session_id
        self.vendor_id = vendor_id
        self.upload_path = upload_path
        self.tmp_dir = os.path.dirname(upload_path)
        self.output_path: Optional[str] = None
        self.output_filename: Optional[str] = None
        self.created_at = time.time()

    def is_expired(self) -> bool:
        return time.time() - self.created_at > TTL_SECONDS

    def cleanup(self):
        try:
            if os.path.exists(self.tmp_dir):
                shutil.rmtree(self.tmp_dir, ignore_errors=True)
        except Exception:
            pass


class PackageBuilderCore:
    """
    Haupt-Orchestrator für den Package Builder.
    Eine Instanz wird beim App-Start erstellt und für alle Requests geteilt.
    """

    def __init__(self):
        self._sessions: Dict[str, BuildSession] = {}

    # ── Vendor-Informationen ──────────────────────────────────────────────────

    def get_vendors_list(self) -> List[Dict]:
        """Alle registrierten Vendoren für die UI."""
        return [v.to_ui_dict() for v in list_vendors().values()]

    # ── Upload & Analyse ──────────────────────────────────────────────────────

    def receive_upload(
        self,
        file_bytes: bytes,
        filename: str,
        vendor_id: Optional[str] = None,
    ) -> Tuple[Optional[str], str]:
        """
        Speichert die hochgeladene Datei temporär und gibt (session_id, error) zurück.
        Bei Fehler ist session_id=None.
        """
        self._cleanup_expired()

        if len(file_bytes) > MAX_UPLOAD_BYTES:
            return None, f"Datei zu groß ({len(file_bytes) // 1024 // 1024} MB > 100 MB)."

        if not file_bytes[:2] == b"PK":
            return None, "Datei ist kein gültiges ZIP-Archiv."

        tmp_dir = tempfile.mkdtemp(prefix="pkg_builder_")
        try:
            upload_path = os.path.join(tmp_dir, "upload.zip")
            with open(upload_path, "wb") as fh:
                fh.write(file_bytes)

            # Vendor-Erkennung
            with zipfile.ZipFile(upload_path, "r") as zf:
                namelist = zf.namelist()

            if vendor_id:
                vendor = get_vendor(vendor_id)
                if not vendor:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return None, f"Unbekannter Hersteller: {vendor_id}"
                if not vendor.detect(namelist):
                    logger.warning(
                        "Paket für Vendor '%s' erkannt, aber detect() = False.", vendor_id
                    )
            else:
                vendor = detect_vendor(namelist)
                if not vendor:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                    return None, (
                        "Hersteller nicht erkannt. Bitte stellen Sie sicher, dass Sie "
                        "das Original-Installerpaket hochgeladen haben."
                    )
                vendor_id = vendor.VENDOR_ID

            session_id = secrets.token_urlsafe(24)
            self._sessions[session_id] = BuildSession(session_id, vendor_id, upload_path)
            logger.info("Upload empfangen: vendor=%s session=%s", vendor_id, session_id)
            return session_id, ""

        except zipfile.BadZipFile:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None, "ZIP-Datei ist beschädigt oder ungültig."
        except Exception as exc:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            logger.error("Upload-Fehler: %s", exc, exc_info=True)
            return None, f"Upload-Fehler: {exc}"

    def analyze(self, session_id: str, tenant: Optional[Dict] = None) -> AnalysisResult:
        """
        Analysiert das hochgeladene ZIP und gibt AnalysisResult zurück.
        Ergänzt Prefill-Daten aus dem Tenant-Kontext.
        """
        session = self._get_session(session_id)
        if not session:
            return AnalysisResult(ok=False, error="Session nicht gefunden oder abgelaufen.")

        vendor = get_vendor(session.vendor_id)
        if not vendor:
            return AnalysisResult(ok=False, error=f"Vendor '{session.vendor_id}' nicht verfügbar.")

        result = vendor.analyze(session.upload_path)
        if result.ok and tenant:
            prefill = vendor.prefill_from_tenant(tenant)
            result.prefill = {**prefill, **result.prefill}
        return result

    def analyze_localized(self, session_id: str, tenant: Optional[Dict] = None, tr=None) -> AnalysisResult:
        session = self._get_session(session_id)
        if not session:
            return AnalysisResult(ok=False, error=(tr("fleet_builder_error_session_missing") if tr else "Session nicht gefunden oder abgelaufen."))

        vendor = get_vendor(session.vendor_id)
        if not vendor:
            return AnalysisResult(ok=False, error=(tr("fleet_builder_error_vendor_missing", vendor=session.vendor_id) if tr else f"Vendor '{session.vendor_id}' nicht verfügbar."))

        result = vendor.analyze(session.upload_path, tr=tr)
        if result.ok and tenant:
            prefill = vendor.prefill_from_tenant(tenant)
            result.prefill = {**prefill, **result.prefill}
        return result

    # ── Patch ─────────────────────────────────────────────────────────────────

    def patch(
        self,
        session_id: str,
        field_values: Dict[str, str],
        original_filename: str = "package.zip",
    ) -> PatchResult:
        """
        Patcht das Paket mit den gegebenen Feldwerten.
        Gibt PatchResult zurück; bei Erfolg ist output bereit für Download.
        """
        session = self._get_session(session_id)
        if not session:
            return PatchResult(ok=False, error="Session nicht gefunden oder abgelaufen.")

        vendor = get_vendor(session.vendor_id)
        if not vendor:
            return PatchResult(ok=False, error=f"Vendor '{session.vendor_id}' nicht verfügbar.")

        # Ausgabedateiname ableiten
        base = original_filename.rsplit(".", 1)[0] if "." in original_filename else original_filename
        output_filename = f"{base}_patched.zip"
        output_path = os.path.join(session.tmp_dir, output_filename)

        result = vendor.patch(session.upload_path, field_values, output_path)
        if result.ok:
            session.output_path = output_path
            session.output_filename = output_filename
            result.session_id = session_id
            result.output_filename = output_filename
            logger.info("Patch erfolgreich: session=%s vendor=%s", session_id, session.vendor_id)
        else:
            logger.error("Patch fehlgeschlagen: session=%s error=%s", session_id, result.error)
        return result

    # ── Download ──────────────────────────────────────────────────────────────

    def get_download_path(self, session_id: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Gibt (output_path, filename) zurück oder (None, None) wenn nicht verfügbar.
        """
        session = self._get_session(session_id)
        if not session or not session.output_path:
            return None, None
        if not os.path.exists(session.output_path):
            return None, None
        return session.output_path, session.output_filename

    def cleanup_session(self, session_id: str):
        """Session und temporäre Dateien bereinigen."""
        session = self._sessions.pop(session_id, None)
        if session:
            session.cleanup()
            logger.debug("Session bereinigt: %s", session_id)

    def get_install_notes(self, session_id: str, field_values: Dict[str, str]) -> List[str]:
        """Installationshinweise vom Vendor-Adapter."""
        session = self._get_session(session_id)
        if not session:
            return []
        vendor = get_vendor(session.vendor_id)
        return vendor.get_install_notes(field_values) if vendor else []

    def get_install_notes_localized(self, session_id: str, field_values: Dict[str, str], tr=None) -> List[str]:
        session = self._get_session(session_id)
        if not session:
            return []
        vendor = get_vendor(session.vendor_id)
        return vendor.get_install_notes(field_values, tr=tr) if vendor else []

    # ── Intern ────────────────────────────────────────────────────────────────

    def _get_session(self, session_id: str) -> Optional[BuildSession]:
        session = self._sessions.get(session_id)
        if session and session.is_expired():
            session.cleanup()
            del self._sessions[session_id]
            return None
        return session

    def _cleanup_expired(self):
        """Abgelaufene Sessions bereinigen (einmal pro Upload aufgerufen)."""
        expired = [sid for sid, s in self._sessions.items() if s.is_expired()]
        for sid in expired:
            self._sessions[sid].cleanup()
            del self._sessions[sid]
        if expired:
            logger.debug("%d abgelaufene Sessions bereinigt.", len(expired))
