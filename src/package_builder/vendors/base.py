"""
Package Builder — Vendor Base Class
Alle Herstelleradapter müssen von VendorBase erben.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, List, Optional

from ..models import AnalysisResult, FieldSchema, PatchResult


class VendorBase(ABC):
    """Abstrakte Basisklasse für alle Herstelleradapter."""

    VENDOR_ID: str = ""           # z.B. "ricoh"
    VENDOR_DISPLAY: str = ""      # z.B. "Ricoh"
    VENDOR_DESCRIPTION: str = ""  # Kurzbeschreibung für die UI

    # ── Erkennung ──────────────────────────────────────────────────────────────

    @abstractmethod
    def detect(self, zip_namelist: List[str]) -> bool:
        """
        True wenn die ZIP-Dateiliste zu diesem Hersteller passt.
        Wird vor jeder Extraktion aufgerufen (billig, nur Namenscheck).
        """
        ...

    # ── Analyse ────────────────────────────────────────────────────────────────

    @abstractmethod
    def analyze(self, outer_zip_path: str, tr=None) -> AnalysisResult:
        """
        ZIP öffnen, Struktur validieren und AnalysisResult zurückgeben.
        Enthält: Feldschema + erkannte Struktur.
        Kein Tenant-Kontext — Vorbelegung wird separat gemacht.
        """
        ...

    # ── Feldschema ─────────────────────────────────────────────────────────────

    @abstractmethod
    def get_fields(self, tr=None) -> List[FieldSchema]:
        """Vollständiges Feldschema für diesen Hersteller zurückgeben."""
        ...

    # ── Vorbelegung ────────────────────────────────────────────────────────────

    def prefill_from_tenant(self, tenant: Dict) -> Dict[str, str]:
        """
        Bekannte Werte aus dem Tenant-Kontext extrahieren.
        Gibt {feldschlüssel: wert} zurück.
        In Unterklasse überschreiben für herstellerspezifisches Mapping.
        """
        return {}

    # ── Patch ──────────────────────────────────────────────────────────────────

    @abstractmethod
    def patch(
        self,
        outer_zip_path: str,
        field_values: Dict[str, str],
        output_path: str,
    ) -> PatchResult:
        """
        ZIP von outer_zip_path lesen, Patches aus field_values anwenden,
        Ergebnis nach output_path schreiben.
        Secrets dürfen NICHT geloggt werden.
        """
        ...

    # ── Installationshinweise ──────────────────────────────────────────────────

    def get_install_notes(self, field_values: Dict[str, str], tr=None) -> List[str]:
        """
        Menschenlesbare Installationshinweise nach dem Download.
        In Unterklasse überschreiben.
        """
        return []

    # ── UI-Metadaten ───────────────────────────────────────────────────────────

    def to_ui_dict(self) -> Dict:
        """Für die Vendor-Auswahlseite."""
        return {
            "id": self.VENDOR_ID,
            "display": self.VENDOR_DISPLAY,
            "description": self.VENDOR_DESCRIPTION,
        }
