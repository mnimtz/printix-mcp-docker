"""
Package Builder — Data Models
Vendor-neutrale Datenstrukturen für den Package-Builder-Wizard.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class FieldSchema:
    """Beschreibt ein einzelnes Eingabefeld im Wizard-UI."""
    key: str                          # interner Schlüssel
    label: str                        # Beschriftung für die UI
    field_type: str = "text"          # text | password | url | checkbox | textarea
    required: bool = True
    default: str = ""
    placeholder: str = ""
    help_text: str = ""
    group: str = "Konfiguration"      # UI-Abschnitt / Gruppen-Header
    order: int = 0                    # Sortierung innerhalb der Gruppe


@dataclass
class StructureInfo:
    """Beschreibt die erkannte Paketstruktur."""
    vendor: str
    vendor_display: str
    package_version: str = ""
    found_files: List[str] = field(default_factory=list)   # logische Rollen
    warnings: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    raw_info: Dict[str, Any] = field(default_factory=dict)  # Debug-Infos


@dataclass
class AnalysisResult:
    """Vollständiges Ergebnis des Analyse-Schritts."""
    ok: bool
    structure: Optional[StructureInfo] = None
    fields: List[FieldSchema] = field(default_factory=list)
    prefill: Dict[str, str] = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "structure": {
                "vendor": self.structure.vendor if self.structure else "",
                "vendor_display": self.structure.vendor_display if self.structure else "",
                "package_version": self.structure.package_version if self.structure else "",
                "found_files": self.structure.found_files if self.structure else [],
                "warnings": self.structure.warnings if self.structure else [],
                "notes": self.structure.notes if self.structure else [],
            } if self.structure else None,
            "fields": [
                {
                    "key": f.key,
                    "label": f.label,
                    "field_type": f.field_type,
                    "required": f.required,
                    "default": f.default,
                    "placeholder": f.placeholder,
                    "help_text": f.help_text,
                    "group": f.group,
                    "order": f.order,
                }
                for f in self.fields
            ],
            "prefill": self.prefill,
        }


@dataclass
class PatchSummary:
    """Menschenlesbare Zusammenfassung — ohne Secrets."""
    patched_logical_files: List[str] = field(default_factory=list)
    patched_fields: List[str] = field(default_factory=list)
    skipped_fields: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "patched_logical_files": self.patched_logical_files,
            "patched_fields": self.patched_fields,
            "skipped_fields": self.skipped_fields,
            "notes": self.notes,
        }


@dataclass
class PatchResult:
    """Ergebnis des Patch-Schritts."""
    ok: bool
    session_id: str = ""
    output_filename: str = ""
    summary: Optional[PatchSummary] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "output_filename": self.output_filename,
            "summary": self.summary.to_dict() if self.summary else None,
            "error": self.error,
        }
