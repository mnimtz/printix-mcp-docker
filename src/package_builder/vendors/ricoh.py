"""
Package Builder — Ricoh Adapter
Patcht Printix Go Ricoh DALP-Dateien für Clientless / Zero Trust Deployment.

Unterstützte Pakettypen:
  1. Einfaches Go-Ricoh-ZIP: enthält .dalp + .apk direkt
     → Offizieller Download von printix.net → Software → Ricoh (ZIP)
  2. Installer-Paket (PrintixGoRicohInstaller): verschachtelte ZIP-Struktur
     → Enthält deploysetting.json + rxspServletPackage-*.zip

DALP-Patch-Ziel: <app-extension>-Sektion
  Laut offizieller Printix-Doku müssen folgende Tags in <app-extension>
  hinzugefügt oder aktualisiert werden:
    <EnableRegistration>true</EnableRegistration>
    <ClientID>...</ClientID>
    <ClientSecret>...</ClientSecret>
    <TenantId>...</TenantId>
    <TenantUrl>...</TenantUrl>

  Credentials stammen aus einer Printix-Anwendung vom Typ "Go registration"
  (NICHT Print API!) — erstellt unter Applications im Printix-Admin.
"""
from __future__ import annotations

import fnmatch
import io
import json
import logging
import re
import zipfile
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

from ..models import AnalysisResult, FieldSchema, PatchResult, PatchSummary, StructureInfo
from .base import VendorBase

logger = logging.getLogger("printix.package_builder.ricoh")

# ── Erkennungskonstanten ───────────────────────────────────────────────────────

RICOH_ROOT_FOLDER = "PrintixGoRicohInstaller"
RICOH_DEPLOY_SETTING = "deploysetting.json"

# Muster für versionierte Dateinamen (Installer-Paket)
INNER_PKG_PATTERN = "rxspServletPackage-*.zip"
SERVLET_ZIP_PATTERN = "rxspServlet-*.zip"
SOP_ZIP_PATTERN = "rxspservletsop-*.zip"

# ── DALP <app-extension> Tag-Mapping ──────────────────────────────────────────
# XML-Tagname → Feldschlüssel im UI
APP_EXT_TAGS: Dict[str, str] = {
    "EnableRegistration": "enable_registration",
    "ClientID": "go_client_id",
    "ClientSecret": "go_client_secret",
    "TenantId": "tenant_id",
    "TenantUrl": "tenant_url",
}


class RicohVendor(VendorBase):
    """Herstelleradapter für Ricoh Printix Go Installerpakete."""

    VENDOR_ID = "ricoh"
    VENDOR_DISPLAY = "Ricoh"
    VENDOR_DESCRIPTION = "Printix Go für Ricoh-Geräte (DALP / Clientless)"

    # ── Erkennung ──────────────────────────────────────────────────────────────

    def detect(self, zip_namelist: List[str]) -> bool:
        """
        Erkennt zwei Pakettypen:
        1. Einfaches Go-Ricoh-ZIP: enthält *.dalp direkt
        2. Installer: PrintixGoRicohInstaller/ mit deploysetting.json
        """
        # Typ 1: einfaches ZIP mit DALP
        if any(n.lower().endswith(".dalp") for n in zip_namelist):
            return True
        # Typ 2: Installer-Paket
        has_folder = any(n.startswith(RICOH_ROOT_FOLDER + "/") for n in zip_namelist)
        has_deploy = any(
            n.split("/")[-1] == RICOH_DEPLOY_SETTING
            for n in zip_namelist
        )
        return has_folder and has_deploy

    # ── Feldschema ─────────────────────────────────────────────────────────────

    def get_fields(self, tr=None) -> List[FieldSchema]:
        _ = tr or (lambda key, **kwargs: key)
        return [
            FieldSchema(
                key="enable_registration",
                label=_("fleet_builder_field_enable_registration_label"),
                field_type="checkbox",
                required=False,
                default="true",
                help_text=_("fleet_builder_field_enable_registration_help"),
                group=_("fleet_builder_group_registration"),
                order=10,
            ),
            FieldSchema(
                key="go_client_id",
                label=_("fleet_builder_field_go_client_id_label"),
                field_type="text",
                required=True,
                placeholder="236b1f58-adab-4888-ba05-acfc9a804523",
                help_text=_("fleet_builder_field_go_client_id_help"),
                group=_("fleet_builder_group_go_registration"),
                order=20,
            ),
            FieldSchema(
                key="go_client_secret",
                label=_("fleet_builder_field_go_client_secret_label"),
                field_type="password",
                required=True,
                placeholder="",
                help_text=_("fleet_builder_field_go_client_secret_help"),
                group=_("fleet_builder_group_go_registration"),
                order=30,
            ),
            FieldSchema(
                key="tenant_id",
                label=_("fleet_builder_field_tenant_id_label"),
                field_type="text",
                required=True,
                placeholder="cbd7e0b5-da2a-4cb6-b7f7-a04ee31cac90",
                help_text=_("fleet_builder_field_tenant_id_help"),
                group=_("fleet_builder_group_tenant"),
                order=40,
            ),
            FieldSchema(
                key="tenant_url",
                label=_("fleet_builder_field_tenant_url_label"),
                field_type="url",
                required=True,
                placeholder="https://acme.printix.net",
                help_text=_("fleet_builder_field_tenant_url_help"),
                group=_("fleet_builder_group_tenant"),
                order=50,
            ),
        ]

    # ── Vorbelegung ────────────────────────────────────────────────────────────

    def prefill_from_tenant(self, tenant: Dict) -> Dict[str, str]:
        prefill: Dict[str, str] = {}
        if not tenant:
            return prefill
        # Enable Registration: Standard true
        prefill["enable_registration"] = "true"
        # Tenant ID
        tid = tenant.get("tenant_id") or tenant.get("printix_tenant_id") or ""
        if tid:
            prefill["tenant_id"] = str(tid)
        # Tenant URL: direkt aus den Portal-Einstellungen (Pflichtfeld)
        tenant_url = tenant.get("tenant_url") or ""
        if tenant_url:
            prefill["tenant_url"] = tenant_url
        # Go Client ID / Secret: NICHT aus Print-API-Credentials vorbelegen,
        # da Go Registration eine eigene Anwendung in Printix ist.
        return prefill

    # ── Analyse ────────────────────────────────────────────────────────────────

    def analyze(self, outer_zip_path: str, tr=None) -> AnalysisResult:
        try:
            return self._do_analyze(outer_zip_path, tr=tr)
        except Exception as exc:
            logger.warning("Ricoh analyze Fehler: %s", exc, exc_info=True)
            if tr:
                return AnalysisResult(ok=False, error=tr("fleet_builder_error_analysis_exception", message=str(exc)))
            return AnalysisResult(ok=False, error=f"Analyse-Fehler: {exc}")

    def _do_analyze(self, outer_zip_path: str, tr=None) -> AnalysisResult:
        warnings: List[str] = []
        notes: List[str] = []
        found_files: List[str] = []
        _ = tr or (lambda key, **kwargs: key)

        with zipfile.ZipFile(outer_zip_path, "r") as zf:
            namelist = zf.namelist()

        pkg_type, dalp_info = _detect_package_type(outer_zip_path)

        if pkg_type == "simple":
            # Einfaches Go-Ricoh-ZIP (DALP + APK)
            dalp_name = dalp_info["dalp_name"]
            found_files.append(_("fleet_builder_found_dalp", value=dalp_name))
            apk_name = dalp_info.get("apk_name")
            if apk_name:
                found_files.append(_("fleet_builder_found_apk", value=apk_name))
            # Version aus Dateiname
            version = _extract_version(dalp_name) or "?"
            # DALP analysieren
            with zipfile.ZipFile(outer_zip_path, "r") as zf:
                dalp_bytes = zf.read(dalp_name)
            dalp_analysis = _analyze_dalp_content(dalp_bytes, tr=tr)
            found_files.extend(dalp_analysis["found"])
            warnings.extend(dalp_analysis["warnings"])
            notes.append(_("fleet_builder_note_simple_package"))
            notes.append(_("fleet_builder_note_package_version", value=version))

        elif pkg_type == "installer":
            # Installer-Paket mit verschachtelten ZIPs
            found_files.append(_("fleet_builder_found_deploysetting"))
            inner_info = dalp_info.get("inner_info", {})
            version = inner_info.get("version", "?")
            found_files.append(_("fleet_builder_found_rxsp_package", value=version))
            if inner_info.get("servlet_zip"):
                found_files.append(_("fleet_builder_found_servlet_zip", value=_extract_version(inner_info["servlet_zip"])))
            if inner_info.get("sop_zip"):
                found_files.append(_("fleet_builder_found_sop_zip", value=_extract_version(inner_info["sop_zip"])))
            # DALP aus tiefster Ebene analysieren
            dalp_bytes = inner_info.get("dalp_bytes")
            if dalp_bytes:
                dalp_analysis = _analyze_dalp_content(dalp_bytes, tr=tr)
                found_files.extend(dalp_analysis["found"])
                warnings.extend(dalp_analysis["warnings"])
            else:
                warnings.append(_("fleet_builder_warn_no_dalp_installer"))
            notes.append(_("fleet_builder_note_installer_package"))
            notes.append(_("fleet_builder_note_package_version", value=version))

        else:
            return AnalysisResult(
                ok=False,
                error=_("fleet_builder_error_structure_unknown"),
            )

        structure = StructureInfo(
            vendor=self.VENDOR_ID,
            vendor_display=self.VENDOR_DISPLAY,
            package_version=version,
            found_files=found_files,
            warnings=warnings,
            notes=notes,
            raw_info={"type": pkg_type, **dalp_info},
        )

        return AnalysisResult(
            ok=True,
            structure=structure,
            fields=self.get_fields(tr=tr),
        )

    # ── Patch ──────────────────────────────────────────────────────────────────

    def patch(
        self,
        outer_zip_path: str,
        field_values: Dict[str, str],
        output_path: str,
    ) -> PatchResult:
        try:
            return self._do_patch(outer_zip_path, field_values, output_path)
        except Exception as exc:
            logger.error("Ricoh patch Fehler: %s", exc, exc_info=True)
            return PatchResult(ok=False, error=f"Patch-Fehler: {exc}")

    def _do_patch(
        self,
        outer_zip_path: str,
        field_values: Dict[str, str],
        output_path: str,
    ) -> PatchResult:
        summary = PatchSummary()
        pkg_type, dalp_info = _detect_package_type(outer_zip_path)

        if pkg_type == "simple":
            self._patch_simple(outer_zip_path, field_values, output_path, dalp_info, summary)
        elif pkg_type == "installer":
            self._patch_installer(outer_zip_path, field_values, output_path, dalp_info, summary)
        else:
            return PatchResult(ok=False, error="Paketstruktur nicht erkannt.")

        logger.info(
            "Ricoh patch erfolgreich: %d Felder in %s",
            len(summary.patched_fields),
            summary.patched_logical_files,
        )
        return PatchResult(ok=True, summary=summary)

    # ── Patch: einfaches Go-Ricoh-ZIP ──────────────────────────────────────────

    def _patch_simple(
        self,
        outer_zip_path: str,
        field_values: Dict[str, str],
        output_path: str,
        dalp_info: Dict,
        summary: PatchSummary,
    ):
        dalp_name = dalp_info["dalp_name"]
        files: Dict[str, bytes] = {}
        files = _read_zip_file_entries(outer_zip_path)

        # DALP patchen
        dalp_bytes = files[dalp_name]["data"]
        patched_dalp = _patch_dalp_app_extension(dalp_bytes, field_values, summary)
        files[dalp_name]["data"] = patched_dalp
        summary.patched_logical_files.append(dalp_name)

        # ZIP neu bauen
        _write_zip(files, output_path)

    # ── Patch: Installer-Paket (verschachtelt) ─────────────────────────────────

    def _patch_installer(
        self,
        outer_zip_path: str,
        field_values: Dict[str, str],
        output_path: str,
        dalp_info: Dict,
        summary: PatchSummary,
    ):
        inner_info = dalp_info.get("inner_info", {})
        rxsp_file_path = inner_info.get("rxsp_file_path")
        servlet_zip_name = inner_info.get("servlet_zip")

        # Äußeres ZIP einlesen
        outer_files = _read_zip_file_entries(outer_zip_path)

        if not rxsp_file_path or rxsp_file_path not in outer_files:
            summary.notes.append("Inneres RXSP-Paket nicht gefunden — kann nicht patchen.")
            _write_zip(outer_files, output_path)
            return

        # Inneres ZIP öffnen und patchen
        inner_files = _read_zip_entries(outer_files[rxsp_file_path]["data"])

        if servlet_zip_name and servlet_zip_name in inner_files:
            # Sub-ZIP öffnen, DALP finden und patchen
            sub_files = _read_zip_entries(inner_files[servlet_zip_name]["data"])
            dalp_name = _find_dalp_in_names(list(sub_files.keys()))
            if dalp_name:
                patched = _patch_dalp_app_extension(sub_files[dalp_name]["data"], field_values, summary)
                sub_files[dalp_name]["data"] = patched
                summary.patched_logical_files.append(dalp_name)
            else:
                summary.notes.append("DALP-Datei nicht im Servlet-ZIP gefunden.")
            # Sub-ZIP neu bauen
            inner_files[servlet_zip_name]["data"] = _build_zip_bytes(sub_files)

        # Inneres ZIP neu bauen
        outer_files[rxsp_file_path]["data"] = _build_zip_bytes(inner_files)

        # Äußeres ZIP neu bauen
        _write_zip(outer_files, output_path)

    # ── Installationshinweise ──────────────────────────────────────────────────

    def get_install_notes(self, field_values: Dict[str, str], tr=None) -> List[str]:
        _ = tr or (lambda key, **kwargs: key)
        tenant_url = field_values.get("tenant_url", "")
        notes = [
            _("fleet_builder_install_intro"),
            _("fleet_builder_install_step_1"),
            _("fleet_builder_install_step_2"),
            _("fleet_builder_install_step_3"),
        ]
        notes.append(_("fleet_builder_install_step_4a"))
        notes.append(_("fleet_builder_install_step_4b"))
        notes.append(_("fleet_builder_install_endpoints"))
        if tenant_url:
            notes.append(_("fleet_builder_install_tenant_url", value=tenant_url))
        notes.append(_("fleet_builder_install_secret_note"))
        return notes


# ═══════════════════════════════════════════════════════════════════════════════
# Hilfsfunktionen
# ═══════════════════════════════════════════════════════════════════════════════

def _detect_package_type(zip_path: str) -> Tuple[str, Dict]:
    """
    Erkennt ob es sich um ein einfaches Go-Ricoh-ZIP oder ein Installer-Paket handelt.
    Gibt (typ, info_dict) zurück.
    """
    with zipfile.ZipFile(zip_path, "r") as zf:
        namelist = zf.namelist()

        # Typ 1: einfaches ZIP mit DALP + APK
        dalp_name = _find_dalp_in_names(namelist)
        if dalp_name and not any(n.startswith(RICOH_ROOT_FOLDER + "/") for n in namelist):
            apk_name = next(
                (n for n in namelist if n.lower().endswith(".apk")),
                None,
            )
            return "simple", {"dalp_name": dalp_name, "apk_name": apk_name}

        # Typ 2: Installer-Paket
        deploy_path = _find_in_zip(namelist, RICOH_DEPLOY_SETTING)
        if deploy_path:
            inner_info = _analyze_installer_structure(zf, deploy_path)
            return "installer", {"deploy_path": deploy_path, "inner_info": inner_info}

        # Fallback: vielleicht hat das ZIP trotzdem eine DALP-Datei?
        if dalp_name:
            apk_name = next(
                (n for n in namelist if n.lower().endswith(".apk")),
                None,
            )
            return "simple", {"dalp_name": dalp_name, "apk_name": apk_name}

    return "unknown", {}


def _analyze_installer_structure(outer_zf: zipfile.ZipFile, deploy_path: str) -> Dict:
    """Analysiert die verschachtelte Installer-Struktur."""
    info: Dict = {}
    namelist = outer_zf.namelist()

    try:
        deploy_data = json.loads(outer_zf.read(deploy_path).decode("utf-8"))
    except Exception as e:
        logger.warning("deploysetting.json nicht parsebar: %s", e)
        return info

    # Inneres RXSP-Paket finden
    rxsp_file_path = _resolve_rxsp_path(deploy_data, namelist)
    if not rxsp_file_path:
        return info

    info["rxsp_file_path"] = rxsp_file_path
    info["version"] = _extract_version(rxsp_file_path)

    # Inneres ZIP öffnen
    try:
        inner_bytes = outer_zf.read(rxsp_file_path)
        with zipfile.ZipFile(io.BytesIO(inner_bytes), "r") as inner_zf:
            inner_names = inner_zf.namelist()
            info["servlet_zip"] = _glob_find(inner_names, SERVLET_ZIP_PATTERN)
            info["sop_zip"] = _glob_find(inner_names, SOP_ZIP_PATTERN)

            # DALP-Bytes aus Servlet-ZIP extrahieren für Analyse
            if info["servlet_zip"]:
                servlet_bytes = inner_zf.read(info["servlet_zip"])
                with zipfile.ZipFile(io.BytesIO(servlet_bytes), "r") as sub_zf:
                    sub_names = sub_zf.namelist()
                    dalp_name = _find_dalp_in_names(sub_names)
                    if dalp_name:
                        info["dalp_bytes"] = sub_zf.read(dalp_name)
                        info["dalp_name"] = dalp_name
    except Exception as e:
        logger.warning("Installer-Struktur Analyse-Fehler: %s", e)

    return info


def _patch_dalp_app_extension(
    dalp_bytes: bytes,
    field_values: Dict[str, str],
    summary: PatchSummary,
    tr=None,
) -> bytes:
    """
    Patcht die <app-extension>-Sektion einer DALP-Datei.
    Tags werden erstellt wenn sie nicht existieren, oder aktualisiert.
    """
    encoding, has_decl = _detect_xml_encoding(dalp_bytes)
    try:
        root = ET.fromstring(dalp_bytes.decode(encoding))
    except ET.ParseError as e:
        summary.notes.append(f"XML-Parse-Fehler: {e}")
        return dalp_bytes

    # <app-extension> finden oder erstellen
    app_ext = root.find(".//app-extension")
    if app_ext is None:
        app_ext = ET.SubElement(root, "app-extension")
        summary.notes.append("<app-extension> erstellt (war nicht vorhanden).")

    # Jeden Tag setzen
    for tag_name, field_key in APP_EXT_TAGS.items():
        value = field_values.get(field_key, "")
        if not value:
            if field_key not in ("enable_registration",):  # Checkbox: leer = false
                summary.skipped_fields.append(field_key)
            continue

        elem = app_ext.find(tag_name)
        if elem is None:
            # Tag existiert nicht → erstellen
            elem = ET.SubElement(app_ext, tag_name)
        elem.text = value
        # Nicht loggen welcher Wert für Secrets gesetzt wurde
        if "secret" in field_key.lower():
            summary.patched_fields.append(f"{tag_name} (gesetzt)")
        else:
            summary.patched_fields.append(tag_name)

    # Validierung: gepatchtes XML muss parsebar sein
    patched = _serialize_xml(root, encoding, has_decl)
    try:
        ET.fromstring(patched.decode(encoding))
    except ET.ParseError as e:
        summary.notes.append(f"Patch-Validierungsfehler: {e} — Original wird beibehalten.")
        return dalp_bytes

    return patched


def _analyze_dalp_content(dalp_bytes: bytes, tr=None) -> Dict:
    """Analysiert den Inhalt einer DALP-Datei und gibt found/warnings zurück."""
    result: Dict = {"found": [], "warnings": []}
    translate = tr or (lambda key, **kwargs: key)
    try:
        encoding, has_decl = _detect_xml_encoding(dalp_bytes)
        root = ET.fromstring(dalp_bytes.decode(encoding))

        # Produktinfo
        title_elem = root.find(".//information/title")
        if title_elem is not None and title_elem.text:
            result["found"].append(translate("fleet_builder_found_app", value=title_elem.text))

        vendor_elem = root.find(".//information/vendor")
        if vendor_elem is not None and vendor_elem.text:
            result["found"].append(translate("fleet_builder_found_vendor", value=vendor_elem.text))

        ver_elem = root.find(".//information/application-ver")
        if ver_elem is not None and ver_elem.text:
            result["found"].append(translate("fleet_builder_found_app_version", value=ver_elem.text))

        # Bestehende <app-extension>
        app_ext = root.find(".//app-extension")
        if app_ext is not None:
            existing_tags = [child.tag for child in app_ext]
            if existing_tags:
                result["found"].append(
                    translate("fleet_builder_found_app_extension_tags", value=", ".join(existing_tags))
                )
            # Prüfen welche Tags schon Werte haben
            for tag_name in APP_EXT_TAGS:
                elem = app_ext.find(tag_name)
                if elem is not None and elem.text and elem.text.strip():
                    if "secret" in tag_name.lower():
                        result["found"].append(f"  {tag_name}: ******* (vorhanden)")
                    else:
                        result["found"].append(f"  {tag_name}: {elem.text}")
        else:
            result["found"].append(translate("fleet_builder_found_app_extension_missing"))

    except ET.ParseError as e:
        result["warnings"].append(f"DALP XML nicht parsebar: {e}")
    return result


# ── ZIP/Datei-Hilfsfunktionen ──────────────────────────────────────────────────

def _find_in_zip(namelist: List[str], filename: str) -> Optional[str]:
    """Findet eine Datei im ZIP (exakter basename, beliebiger Pfad)."""
    for name in namelist:
        if name.split("/")[-1] == filename:
            return name
    return None


def _find_dalp_in_names(namelist: List[str]) -> Optional[str]:
    """Findet die erste .dalp-Datei in einer Namensliste."""
    for name in namelist:
        if name.lower().endswith(".dalp") and not name.startswith("__MACOSX"):
            return name
    return None


def _glob_find(namelist: List[str], pattern: str) -> Optional[str]:
    """Findet die erste Datei die dem Glob-Muster entspricht (nur basename)."""
    for name in namelist:
        basename = name.split("/")[-1]
        if fnmatch.fnmatch(basename, pattern):
            return name
    return None


def _extract_version(filename: str) -> str:
    """Extrahiert die Versionsnummer aus einem Dateinamen."""
    match = re.search(r"(\d+\.\d+(?:\.\d+)*)", filename)
    return match.group(1) if match else "?"


def _resolve_rxsp_path(deploy_data: Dict, namelist: List[str]) -> Optional[str]:
    """
    Ermittelt den Pfad des inneren RXSP-Pakets aus deploysetting.json.
    Fallback: Glob-Suche.
    """
    rxsp_path: Optional[str] = None
    servlet_cfg = deploy_data.get("servlet", {})
    if isinstance(servlet_cfg, dict):
        rxsp_path = servlet_cfg.get("rxsp_file_path") or servlet_cfg.get("rxspFilePath")
    if not rxsp_path:
        rxsp_path = deploy_data.get("rxsp_file_path") or deploy_data.get("rxspFilePath")

    if rxsp_path:
        basename = rxsp_path.replace("\\", "/").split("/")[-1]
        exact = next((n for n in namelist if n.split("/")[-1] == basename), None)
        if exact:
            return exact
        logger.info("Exakter Pfad '%s' nicht gefunden, Glob-Fallback.", rxsp_path)

    return _glob_find(namelist, INNER_PKG_PATTERN)


def _clone_zip_info(info: zipfile.ZipInfo) -> zipfile.ZipInfo:
    cloned = zipfile.ZipInfo(filename=info.filename, date_time=info.date_time)
    cloned.comment = info.comment
    cloned.extra = info.extra
    cloned.create_system = info.create_system
    cloned.create_version = info.create_version
    cloned.extract_version = info.extract_version
    cloned.flag_bits = info.flag_bits
    cloned.volume = info.volume
    cloned.internal_attr = info.internal_attr
    cloned.external_attr = info.external_attr
    cloned.compress_type = info.compress_type
    return cloned


def _read_zip_entries(zip_bytes: bytes):
    """Liest ein ZIP aus Bytes inkl. ZipInfo-Metadaten ein."""
    with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as zf:
        entries = OrderedDict()
        for info in zf.infolist():
            data = b"" if info.is_dir() else zf.read(info.filename)
            entries[info.filename] = {"info": _clone_zip_info(info), "data": data}
        return entries


def _read_zip_file_entries(zip_path: str):
    with zipfile.ZipFile(zip_path, "r") as zf:
        entries = OrderedDict()
        for info in zf.infolist():
            data = b"" if info.is_dir() else zf.read(info.filename)
            entries[info.filename] = {"info": _clone_zip_info(info), "data": data}
        return entries


def _build_zip_bytes(files) -> bytes:
    """Baut ein ZIP aus Einträgen mit erhaltener Metadatenstruktur als Bytes."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, entry in files.items():
            info = _clone_zip_info(entry["info"])
            info.filename = name
            zf.writestr(info, entry["data"])
    return buf.getvalue()


def _write_zip(files, output_path: str):
    """Schreibt Einträge als ZIP-Datei auf Disk und erhält Dateinamen/Attribute bestmöglich."""
    with zipfile.ZipFile(output_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, entry in files.items():
            info = _clone_zip_info(entry["info"])
            info.filename = name
            zf.writestr(info, entry["data"])


def _detect_xml_encoding(xml_bytes: bytes) -> Tuple[str, bool]:
    """Erkennt das Encoding aus der XML-Deklaration."""
    try:
        header = xml_bytes[:200].decode("ascii", errors="replace")
        enc_match = re.search(r'encoding=["\']([^"\']+)["\']', header)
        encoding = enc_match.group(1).lower() if enc_match else "utf-8"
        has_decl = header.strip().startswith("<?xml")
        return encoding, has_decl
    except Exception:
        return "utf-8", True


def _serialize_xml(root: ET.Element, encoding: str = "utf-8", include_declaration: bool = True) -> bytes:
    """Serialisiert einen ElementTree-Root zurück zu Bytes."""
    buf = io.BytesIO()
    tree = ET.ElementTree(root)
    if include_declaration:
        tree.write(buf, encoding=encoding, xml_declaration=True)
    else:
        tree.write(buf, encoding=encoding)
    return buf.getvalue()
