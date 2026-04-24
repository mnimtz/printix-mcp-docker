"""
Template Store — Persistente Report-Definitionen
=================================================
Speichert Report-Templates als JSON in /data/report_templates.json.
Die Datei überlebt Add-on-Updates (liegt im /data-Volume).

Template-Schema:
  report_id      — UUID
  name           — Lesbarer Name
  created_prompt — Ursprüngliche Nutzeranfrage
  query_type     — print_stats | cost_report | top_users | top_printers | anomalies | trend
  query_params   — Query-Parameter als Dict
  output_formats — Liste: html | pdf | xlsx | csv | json
  layout         — Dict mit logo_base64, primary_color, company_name, footer_text
  schedule       — Dict mit frequency, day, time (oder None wenn kein Schedule)
  recipients     — Liste von E-Mail-Adressen
  mail_subject   — Betreffzeile
  owner_user_id  — User-ID des Tenant-Besitzers (für Scheduler-Credential-Lookup)
  created_at     — ISO-Timestamp der Erstellung
  updated_at     — ISO-Timestamp der letzten Änderung
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from .design_presets import normalize_layout

logger = logging.getLogger(__name__)

TEMPLATES_FILE = os.environ.get("TEMPLATES_PATH", "/data/report_templates.json")


def _migrate_layout(layout: dict[str, Any]) -> dict[str, Any]:
    """
    Auto-Migration v3.7.x → v3.8.0.

    - Templates hatten früher `logo_url` (Webadresse); v3.8.0 nutzt `logo_base64`
      und erwartet einen eingebetteten data-URI.
    - Wenn das alte Template nur `logo_url` (und kein `logo_base64`) hatte,
      behalten wir den URL-Wert als Legacy-Fallback, aber füllen `logo_base64`
      mit leerem String auf, damit das UI den Upload-Dialog zeigen kann.
    - Alle neuen Felder (theme_id, font_family, header_variant, density, …)
      bekommen über `normalize_layout()` sinnvolle Defaults.

    Die Funktion mutiert das Original NICHT — liefert ein neues Dict.
    """
    if not layout:
        return normalize_layout({})

    migrated = dict(layout)

    # Legacy: logo_url vorhanden, logo_base64 leer → logo_url behalten, base64
    # leer lassen. Das neue Rendering bevorzugt logo_base64; wenn leer, nutzt
    # es als Fallback die logo_url (nur falls noch aktiv).
    if migrated.get("logo_url") and not migrated.get("logo_base64"):
        logger.debug("Template-Migration: logo_url gefunden, logo_base64 leer")
        # beides behalten — Renderer entscheidet

    return normalize_layout(migrated)


def _load() -> dict[str, Any]:
    """Lädt alle Templates aus der JSON-Datei und migriert Layouts on-the-fly."""
    if not os.path.exists(TEMPLATES_FILE):
        return {}
    try:
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error("Fehler beim Laden der Templates: %s", e)
        return {}

    # Migration: alle Layouts durch normalize_layout schicken.
    # Wir schreiben die Migration NICHT zurück in die Datei — das passiert erst
    # wenn der Benutzer das Template explizit speichert. So bleibt _load()
    # seiteneffektfrei.
    for tpl in data.values():
        if isinstance(tpl.get("layout"), dict):
            tpl["layout"] = _migrate_layout(tpl["layout"])
    return data


def _save(data: dict[str, Any]) -> None:
    """Schreibt alle Templates in die JSON-Datei."""
    try:
        os.makedirs(os.path.dirname(TEMPLATES_FILE) or ".", exist_ok=True)
        with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
    except Exception as e:
        logger.error("Fehler beim Speichern der Templates: %s", e)
        raise RuntimeError(f"Template konnte nicht gespeichert werden: {e}") from e


def save_template(
    name: str,
    query_type: str,
    query_params: dict[str, Any],
    recipients: list[str],
    mail_subject: str,
    output_formats: Optional[list[str]] = None,
    layout: Optional[dict[str, Any]] = None,
    schedule: Optional[dict[str, Any]] = None,
    created_prompt: str = "",
    report_id: Optional[str] = None,
    owner_user_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Speichert ein neues Template oder überschreibt ein bestehendes (bei report_id).

    Returns:
        Das gespeicherte Template-Dict mit allen Feldern.
    """
    templates = _load()
    now = datetime.now(timezone.utc).isoformat()

    # Alle Layouts laufen durch normalize_layout() → konsistente Defaults,
    # gültige Theme-Farben, auto-migration logo_url → logo_base64.
    normalized_layout = normalize_layout(layout) if layout is not None else None

    if report_id and report_id in templates:
        # Update: bestehende Felder übernehmen, nur geänderte überschreiben
        template = templates[report_id]
        template.update({
            "name":           name,
            "query_type":     query_type,
            "query_params":   query_params,
            "output_formats": output_formats or ["html"],
            "layout":         normalized_layout if normalized_layout is not None
                              else _migrate_layout(template.get("layout", {})),
            "schedule":       schedule,
            "recipients":     recipients,
            "mail_subject":   mail_subject,
            "updated_at":     now,
        })
        if created_prompt:
            template["created_prompt"] = created_prompt
        # owner_user_id nur setzen wenn übergeben (nicht überschreiben wenn leer)
        if owner_user_id:
            template["owner_user_id"] = owner_user_id
    else:
        # Neu anlegen
        rid = report_id or str(uuid.uuid4())
        template = {
            "report_id":      rid,
            "name":           name,
            "created_prompt": created_prompt,
            "query_type":     query_type,
            "query_params":   query_params,
            "output_formats": output_formats or ["html"],
            "layout":         normalized_layout if normalized_layout is not None
                              else normalize_layout({}),
            "schedule":       schedule,
            "recipients":     recipients,
            "mail_subject":   mail_subject,
            "owner_user_id":  owner_user_id or "",
            "created_at":     now,
            "updated_at":     now,
        }
        templates[template["report_id"]] = template

    _save(templates)
    logger.info("Template gespeichert: %s (%s)", template["name"], template["report_id"])
    return template


def list_templates() -> list[dict[str, Any]]:
    """Gibt alle Templates als Liste zurück (ohne layout.logo_base64 für Lesbarkeit)."""
    templates = _load()
    result = []
    for t in templates.values():
        summary = {k: v for k, v in t.items() if k != "layout"}
        if "layout" in t:
            layout_copy = dict(t["layout"])
            layout_copy.pop("logo_base64", None)
            summary["layout"] = layout_copy
        result.append(summary)
    return sorted(result, key=lambda x: x.get("created_at", ""))


def get_template(report_id: str) -> Optional[dict[str, Any]]:
    """Gibt ein einzelnes Template zurück (None wenn nicht gefunden)."""
    return _load().get(report_id)


def delete_template(report_id: str) -> bool:
    """
    Löscht ein Template.

    Returns:
        True wenn gelöscht, False wenn nicht gefunden.
    """
    templates = _load()
    if report_id not in templates:
        return False
    del templates[report_id]
    _save(templates)
    logger.info("Template gelöscht: %s", report_id)
    return True


def get_scheduled_templates() -> list[dict[str, Any]]:
    """Gibt alle Templates mit aktivem Schedule zurück."""
    return [t for t in _load().values() if t.get("schedule")]


def list_templates_by_user(user_id: str) -> list[dict[str, Any]]:
    """Gibt alle Templates eines bestimmten Owners zurück (ohne layout.logo_base64)."""
    all_templates = list_templates()
    if not user_id:
        return all_templates
    return [t for t in all_templates if t.get("owner_user_id", "") == user_id]
