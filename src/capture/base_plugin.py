"""
Base Plugin — Abstrakte Basisklasse für Capture-Ziel-Plugins
============================================================
Jedes Plugin implementiert:
  - config_schema(): Felder für die UI-Konfiguration
  - validate_config(): Prüft ob die Konfiguration vollständig ist
  - process_document(): Verarbeitet ein eingehendes Dokument
  - test_connection(): Testet die Verbindung zum Ziel
"""

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger(__name__)


class CapturePlugin(ABC):
    """Abstrakte Basisklasse für alle Capture-Ziel-Plugins."""

    # Plugin-Metadaten (von Subklassen überschreiben)
    plugin_id: str = ""
    plugin_name: str = ""
    plugin_icon: str = "📄"
    plugin_description: str = ""
    plugin_color: str = "#6366f1"

    def __init__(self, config_json: str = "{}"):
        try:
            self.config = json.loads(config_json) if config_json else {}
        except (json.JSONDecodeError, TypeError):
            self.config = {}

    @abstractmethod
    def config_schema(self) -> list[dict]:
        """
        Gibt die Konfigurationsfelder für die UI zurück.
        Jedes Feld: {"key": "...", "label": "...", "type": "text|url|password|number",
                      "required": True/False, "hint": "...", "default": "..."}
        """
        ...

    def validate_config(self) -> tuple[bool, str]:
        """Prüft ob alle Pflichtfelder gesetzt sind. Gibt (ok, error_msg) zurück."""
        for field in self.config_schema():
            if field.get("required") and not self.config.get(field["key"]):
                return False, f"Missing required field: {field['label']}"
        return True, ""

    @abstractmethod
    async def process_document(
        self,
        document_url: str,
        filename: str,
        metadata: dict[str, Any],
        event_data: dict,
    ) -> tuple[bool, str]:
        """
        Verarbeitet ein eingehendes Dokument.
        Args:
            document_url: Azure Blob SAS URL zum Dokument
            filename: Originaler Dateiname
            metadata: Capture-Metadaten (Index-Felder)
            event_data: Vollständiger Webhook-Body
        Returns:
            (success: bool, message: str)
        """
        ...

    async def ingest_bytes(
        self,
        data: bytes,
        filename: str,
        metadata: dict[str, Any],
    ) -> tuple[bool, str]:
        """
        Dokument als Bytes direkt an das Ziel-System weiterleiten
        (z.B. aus Desktop-Send/Web-Upload — ohne Azure Blob SAS URL).

        Default: wirft NotImplementedError. Plugins, die diesen Pfad
        unterstützen sollen, überschreiben die Methode und laden die
        Bytes direkt ins Ziel hoch. Für Webhook-basierte Flows wird
        weiterhin `process_document(document_url, …)` benutzt.

        Returns:
            (success: bool, message: str)
        """
        raise NotImplementedError(
            f"Plugin '{self.plugin_id}' unterstützt kein direktes Ingest von "
            "Bytes (nur Webhook-basiertes process_document)."
        )

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """
        Testet die Verbindung zum Ziel-System.
        Returns:
            (success: bool, message: str)
        """
        ...


# ─── Plugin Registry ────────────────────────────────────────────────────────

_PLUGINS: dict[str, type[CapturePlugin]] = {}


def register_plugin(cls: type[CapturePlugin]) -> type[CapturePlugin]:
    """Decorator: Registriert eine Plugin-Klasse im globalen Registry."""
    if cls.plugin_id:
        _PLUGINS[cls.plugin_id] = cls
    return cls


def get_plugin_class(plugin_id: str) -> type[CapturePlugin] | None:
    """Gibt die Plugin-Klasse für eine Plugin-ID zurück."""
    return _PLUGINS.get(plugin_id)


def get_all_plugins() -> dict[str, type[CapturePlugin]]:
    """Gibt alle registrierten Plugins zurück."""
    return dict(_PLUGINS)


def create_plugin_instance(plugin_id: str, config_json: str = "{}") -> CapturePlugin | None:
    """Erstellt eine Plugin-Instanz mit der gegebenen Konfiguration."""
    cls = _PLUGINS.get(plugin_id)
    if not cls:
        return None
    return cls(config_json)
