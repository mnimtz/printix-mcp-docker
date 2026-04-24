"""
Package Builder — Vendor Registry
Auto-Discovery aller Herstelleradapter im vendors/ Unterordner.
"""
from __future__ import annotations
import importlib
import logging
import pkgutil
from typing import Dict, List, Optional

from .base import VendorBase

logger = logging.getLogger("printix.package_builder.vendors")

# Globale Vendor-Registry
_REGISTRY: Dict[str, VendorBase] = {}


def _load_vendors():
    """Lädt alle Vendor-Module per pkgutil.iter_modules (Auto-Discovery)."""
    for _importer, modname, _ispkg in pkgutil.iter_modules(__path__):
        if modname == "base":
            continue
        try:
            mod = importlib.import_module(f"{__name__}.{modname}")
            for attr_name in dir(mod):
                obj = getattr(mod, attr_name)
                if (
                    isinstance(obj, type)
                    and issubclass(obj, VendorBase)
                    and obj is not VendorBase
                    and obj.VENDOR_ID
                ):
                    instance = obj()
                    _REGISTRY[instance.VENDOR_ID] = instance
                    logger.debug("Vendor registriert: %s", instance.VENDOR_ID)
        except Exception as exc:
            logger.warning("Vendor-Modul '%s' konnte nicht geladen werden: %s", modname, exc)


_load_vendors()


def get_vendor(vendor_id: str) -> Optional[VendorBase]:
    return _REGISTRY.get(vendor_id)


def list_vendors() -> Dict[str, VendorBase]:
    return dict(_REGISTRY)


def detect_vendor(zip_namelist: List[str]) -> Optional[VendorBase]:
    """Gibt den ersten Vendor zurück, der das ZIP erkennt."""
    for vendor in _REGISTRY.values():
        try:
            if vendor.detect(zip_namelist):
                return vendor
        except Exception as exc:
            logger.warning("Vendor %s detect() Fehler: %s", vendor.VENDOR_ID, exc)
    return None
