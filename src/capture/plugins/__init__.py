"""
Capture Plugins — Auto-Discovery
=================================
Beim Import dieses Pakets werden automatisch alle Plugin-Module geladen,
damit sich jedes Plugin via @register_plugin im globalen Registry eintraegt.

Neues Plugin hinzufuegen:
  1. Neue Datei in diesem Ordner erstellen (z.B. sharepoint.py)
  2. Plugin-Klasse von CapturePlugin ableiten
  3. @register_plugin Decorator verwenden
  4. Fertig — wird automatisch erkannt und im Capture Store angezeigt
"""

import importlib
import logging
import pkgutil

logger = logging.getLogger(__name__)

# Auto-discover: Alle .py Module in diesem Verzeichnis importieren
_package_path = __path__
_package_name = __name__

for _importer, _modname, _ispkg in pkgutil.iter_modules(_package_path):
    try:
        importlib.import_module(f"{_package_name}.{_modname}")
        logger.debug("Plugin module loaded: %s", _modname)
    except Exception as exc:
        logger.warning("Failed to load plugin module '%s': %s", _modname, exc)
