"""
Preset Templates — Vordefinierte Report-Vorlagen basierend auf Printix PowerBI-Template
========================================================================================
Alle 17 Seiten des offiziellen Printix PowerBI-Templates (v2025.4.0) sind hier als
Preset-Definitionen hinterlegt.

Jedes Preset enthält:
  key              — Eindeutiger Bezeichner (URL-safe)
  name             — Anzeigename in der UI
  description      — Kurzbeschreibung was dieser Report zeigt
  icon             — Emoji-Icon für die UI
  pbi_page         — Entsprechende Seite im PowerBI-Template (Referenz)
  query_type       — query_tools-Funktion (oder None wenn noch nicht implementiert)
  query_params     — Standardparameter für die Query
  output_formats   — Standardausgabeformate
  mail_subject     — Vorschlag für E-Mail-Betreff
  schedule_suggestion — Empfohlener Schedule-Rhythmus
  tag              — Kategorie-Tag für die UI
  available        — True wenn mit aktuellen query_tools ausführbar
  coming_soon      — Beschreibung der fehlenden Implementierung (wenn nicht available)
"""

from typing import Any, Optional

# ── Unterstützte Ausgabeformate ───────────────────────────────────────────────────
#
#  Format   | Status       | Versand         | Engine
#  ---------|--------------|-----------------|---------------------------
#  html     | ✅ verfügbar | E-Mail-Body     | report_engine.render_html()
#  csv      | ✅ verfügbar | Anhang .csv     | report_engine.render_csv()
#  json     | ✅ verfügbar | Anhang .json    | report_engine.render_json()
#  pdf      | 🔜 v2.15    | Anhang .pdf     | WeasyPrint (geplant)
#  xlsx     | 🔜 v2.15    | Anhang .xlsx    | openpyxl (geplant)
#
#  Mehrfachauswahl möglich: z.B. ["html", "csv"] sendet Mail-Body + CSV-Anhang.
#  mail_client.send_report() unterstützt Anhänge via attachments=[{filename, content, content_type}]

ALL_FORMATS = [
    {"key": "html",  "label": "📧 HTML (E-Mail-Body)",  "available": True},
    {"key": "csv",   "label": "📊 CSV (Tabelle)",        "available": True},
    {"key": "json",  "label": "{} JSON (Rohdaten)",     "available": True},
    {"key": "pdf",   "label": "📄 PDF",   "available": True},
    {"key": "xlsx",  "label": "📗 Excel (XLSX)", "available": True},
]

# ── Preset-Definitionen ──────────────────────────────────────────────────────────

PRESETS: dict[str, dict[str, Any]] = {

    # ── 1. Monatlicher Überblick (→ Overview) ─────────────────────────────────
    "overview_monthly": {
        "name": "Monatlicher Drucküberblick",
        "description": "Gesamtübersicht aller Druckaktivitäten: Seitenvolumen, "
                       "Farb-/S/W-Anteil, Duplex-Quote und Jobanzahl des letzten Monats.",
        "icon": "📋",
        "pbi_page": "Overview",
        "query_type": "print_stats",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "day",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Drucküberblick {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Überblick",
        "available": True,
    },

    # ── 2. Wochen-Trend ────────────────────────────────────────────────────────
    "trend_weekly": {
        "name": "Wöchentlicher Drucktrend",
        "description": "Entwicklung des Druckvolumens der letzten 4 Wochen, "
                       "aufgeschlüsselt nach Woche. Ideal für regelmäßiges Monitoring.",
        "icon": "📈",
        "pbi_page": "Overview",
        "query_type": "trend",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "week",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Drucktrend KW{week}",
        "schedule_suggestion": {"frequency": "weekly", "day": 0, "time": "07:00"},
        "tag": "Trend",
        "available": True,
    },

    # ── 3. Drucker-Übersicht (→ Printers - Overview) ──────────────────────────
    "printers_overview": {
        "name": "Drucker-Übersicht",
        "description": "Top-Drucker nach Druckvolumen, Farb-/S/W-Anteil je Drucker, "
                       "Duplex-Quote und Modellverteilung im Überblick.",
        "icon": "🖨️",
        "pbi_page": "Printers - Overview",
        "query_type": "top_printers",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "limit":      20,
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Drucker-Übersicht {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:30"},
        "tag": "Drucker",
        "available": True,
    },

    # ── 4. Drucker-Verlauf (→ Printer - History) ──────────────────────────────
    "printer_history": {
        "name": "Drucker-Verlauf (einzeln)",
        "description": "Jobverlauf und Seitenvolumen eines Druckers über Zeit. "
                       "Filterbar nach Drucker-ID und Standort.",
        "icon": "📅",
        "pbi_page": "Printer - History",
        "query_type": "printer_history",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "month",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Drucker-Verlauf {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "09:00"},
        "tag": "Drucker",
        "available": True,
    },

    # ── 5. Drucker Service Status (→ Printers - Service Status) ───────────────
    "printer_service_status": {
        "name": "Drucker Service-Status",
        "description": "Auslastung und letzte Aktivität aller Drucker. "
                       "Hinweis: Toner-Füllstände sind nicht in SQL — werden über Printix API abgerufen.",
        "icon": "🔧",
        "pbi_page": "Printers - Service Status",
        "query_type": "device_readings",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Drucker Service-Status",
        "schedule_suggestion": {"frequency": "weekly", "day": 0, "time": "07:00"},
        "tag": "Service",
        "available": True,
    },

    # ── 6. Job-Verlauf (→ Job - History) ──────────────────────────────────────
    "job_history": {
        "name": "Job-Verlauf",
        "description": "Vollständige Druckjob-Liste mit Benutzer, Drucker, "
                       "Seitenanzahl, Farbe, Duplex und Zeitstempel. Paginiert, filterbar.",
        "icon": "📄",
        "pbi_page": "Job - History",
        "query_type": "job_history",
        "query_params": {
            "start_date":    "last_month_start",
            "end_date":      "last_month_end",
            "page":          0,
            "page_size":     100,
            "status_filter": "ok",
        },
        "output_formats": ["html", "csv", "xlsx"],
        "mail_subject": "Printix Job-Verlauf {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Jobs",
        "available": True,
    },

    # ── 7. Druckregeln (→ Print Rules - Overview) ─────────────────────────────
    "print_rules_overview": {
        "name": "Druckregeln-Übersicht",
        "description": "Verteilung nach Papierformat, Farbe und Duplex — "
                       "zeigt den Druckmix (A4/A3, S/W-Anteil, Duplex-Quote).",
        "icon": "📐",
        "pbi_page": "Print Rules - Overview",
        "query_type": "queue_stats",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Druckregeln-Übersicht",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "09:00"},
        "tag": "Verwaltung",
        "available": True,
    },

    # ── 8. Standort-Übersicht (→ Locations - Overview) ────────────────────────
    "locations_overview": {
        "name": "Standort-Übersicht",
        "description": "Druckvolumen und Kostenverteilung nach Standort/Netzwerk. "
                       "Zeigt welche Standorte am meisten drucken.",
        "icon": "📍",
        "pbi_page": "Locations - Overview",
        "query_type": "print_stats",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "site",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Standort-Auswertung {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Standort",
        "available": True,
    },

    # ── 9. Benutzer-Übersicht (→ Users - Overview) ────────────────────────────
    "users_overview": {
        "name": "Benutzer-Übersicht",
        "description": "Top-Nutzer nach Druckvolumen, Gruppen-Zugehörigkeit, "
                       "Abteilung und Druckmuster. Kompakte Übersicht für HR/IT.",
        "icon": "👥",
        "pbi_page": "Users - Overview",
        "query_type": "top_users",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "limit":      20,
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Benutzer-Übersicht {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Benutzer",
        "available": True,
    },

    # ── 10. Benutzer Druckdetails (→ User - Print Details) ────────────────────
    "user_print_details": {
        "name": "Benutzer Druckdetails",
        "description": "Detaillierter Druckverlauf eines Benutzers: Zeitverlauf, "
                       "Farb-/S/W-Anteil und Duplexquote.",
        "icon": "🔍",
        "pbi_page": "User - Print Details",
        "query_type": "user_detail",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "month",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Druckdetails {month}",
        "schedule_suggestion": None,
        "tag": "Benutzer",
        "available": True,
    },

    # ── 11. Benutzer Kopierdetails (→ User - Copy Details) ────────────────────
    "user_copy_details": {
        "name": "Benutzer Kopier-Details",
        "description": "Kopiervolumen pro Benutzer aus jobs_copy/jobs_copy_details: "
                       "Seiten, Farbe, Duplex, zeitlicher Verlauf.",
        "icon": "📑",
        "pbi_page": "User - Copy Details",
        "query_type": "user_copy_detail",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "month",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Kopier-Details {month}",
        "schedule_suggestion": None,
        "tag": "Benutzer",
        "available": True,
    },

    # ── 12. Benutzer Scan-Details (→ User - Scan Details) ─────────────────────
    "user_scan_details": {
        "name": "Benutzer Scan-Details",
        "description": "Scan-Aktivitäten pro Benutzer aus jobs_scan: "
                       "Scan-Jobs, Seitenanzahl, Farbe, zeitlicher Verlauf.",
        "icon": "🔎",
        "pbi_page": "User - Scan Details",
        "query_type": "user_scan_detail",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "month",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Scan-Details {month}",
        "schedule_suggestion": None,
        "tag": "Benutzer",
        "available": True,
    },

    # ── 13. Workstation-Übersicht (→ Workstations - Overview) ─────────────────
    "workstations_overview": {
        "name": "Workstation-Übersicht",
        "description": "Alle registrierten Workstations mit Druckvolumen und letzter Aktivität. "
                       "Gibt Hinweis falls workstations-Tabelle im Schema fehlt.",
        "icon": "💻",
        "pbi_page": "Workstations - Overview",
        "query_type": "workstation_overview",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Workstation-Übersicht",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "09:00"},
        "tag": "Infrastruktur",
        "available": True,
    },

    # ── 14. Workstation-Details (→ Workstation - Details) ─────────────────────
    # v4.6.11: available=False — Vorschau nicht möglich ohne Workstation-ID.
    # Nutzbar über MCP-Tool oder als gespeicherter Report mit konkreter ID.
    "workstation_details": {
        "name": "Workstation-Details (einzeln)",
        "description": "Druckverlauf einer einzelnen Workstation über Zeit. "
                       "Benötigt Workstation-ID als Parameter — nur über MCP-Tool "
                       "oder gespeicherten Report mit konkreter ID nutzbar.",
        "icon": "🖥️",
        "pbi_page": "Workstation - Details",
        "query_type": "workstation_detail",
        "query_params": {
            "start_date":     "last_month_start",
            "end_date":       "last_month_end",
            "workstation_id": "",
            "group_by":       "month",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Workstation-Details",
        "schedule_suggestion": None,
        "tag": "Infrastruktur",
        "available": False,
    },

    # ── 15. Tree-O-Meter / Nachhaltigkeit (→ Tree-O-Meter) ────────────────────
    "tree_o_meter": {
        "name": "Nachhaltigkeits-Report (Tree-O-Meter)",
        "description": "Eingesparte Blätter durch Duplex-Druck, umgerechnet in Bäume "
                       "(1 Baum = 8333 Blätter A4). Duplex-Quote und Einsparpotenzial.",
        "icon": "🌳",
        "pbi_page": "Tree-O-Meter",
        "query_type": "tree_meter",
        "query_params": {
            "start_date":      "last_month_start",
            "end_date":        "last_month_end",
            "sheets_per_tree": 8333,
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Nachhaltigkeits-Report {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "09:00"},
        "tag": "Nachhaltigkeit",
        "available": True,
    },

    # ── 16. Kostenanalyse (→ Cost) ────────────────────────────────────────────
    "cost_report_monthly": {
        "name": "Monatliche Kostenanalyse",
        "description": "Druckkosten aufgeschlüsselt nach Benutzer, Drucker und "
                       "Abteilung. Basiert auf konfigurierbaren Blatt- und Tonerkosten.",
        "icon": "💶",
        "pbi_page": "Cost",
        "query_type": "cost_report",
        "query_params": {
            "start_date":      "last_month_start",
            "end_date":        "last_month_end",
            "cost_per_sheet":  0.01,
            "cost_per_mono":   0.02,
            "cost_per_color":  0.08,
            "group_by":        "user",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Kostenanalyse {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Kosten",
        "available": True,
    },

    # ── 17. Service Desk (→ Service Desk) ─────────────────────────────────────
    "service_desk": {
        "name": "Service Desk Report",
        "description": "Fehlgeschlagene und abgebrochene Druckjobs für den IT-Service-Desk. "
                       "Gruppierbar nach Fehlertyp, Drucker oder Benutzer.",
        "icon": "🛎️",
        "pbi_page": "Service Desk",
        "query_type": "service_desk",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "group_by":   "status",
        },
        "output_formats": ["html", "csv", "xlsx"],
        "mail_subject": "Printix Service Desk Report {month}",
        "schedule_suggestion": {"frequency": "weekly", "day": 0, "time": "07:00"},
        "tag": "Service",
        "available": True,
    },

    # ── 18. Sensible Dokumente (v3.8.0, Compliance) ───────────────────────────
    "sensitive_documents": {
        "name": "Sensible Dokumente",
        "description": "Scannt Druck- und Scan-Jobs nach sensiblen Dateinamen "
                       "(Personal, Finanzen, Vertraulich, Gesundheit, Recht, "
                       "Ausweisdokumente). Keyword-Sets und Freitext "
                       "individuell kombinierbar.",
        "icon": "🛡️",
        "pbi_page": None,
        "query_type": "sensitive_documents",
        "query_params": {
            "start_date":      "last_month_start",
            "end_date":        "last_month_end",
            # Default: alle sechs Preset-Sets aktiv (entspricht
            # SENSITIVE_KEYWORD_SETS in query_tools.py)
            "keyword_sets":    ["hr", "finance", "confidential",
                                "health", "legal", "pii"],
            "custom_keywords": [],
            "include_scans":   True,
            "page":            0,
            "page_size":       500,
        },
        "output_formats": ["html", "csv", "xlsx"],
        "mail_subject": "Printix Sensible Dokumente {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Compliance",
        "available": True,
    },

    # ── 19. Nutzung Stunde × Wochentag (v3.8.1, visuelle Heatmap) ─────────────
    "hour_dow_heatmap": {
        "name": "Nutzung Stunde × Wochentag",
        "description": "Visuelle Heatmap der Druckaktivität pro Stunde und "
                       "Wochentag. Zeigt Spitzenzeiten, Off-Hours-Nutzung "
                       "und typische Lastprofile auf einen Blick.",
        "icon": "🗓️",
        "pbi_page": None,
        "query_type": "hour_dow_heatmap",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
        },
        "output_formats": ["html", "pdf"],
        "mail_subject": "Printix Nutzungs-Heatmap {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:00"},
        "tag": "Analyse",
        "available": True,
    },

    # ── v3.9.0: Admin-Audit-Trail (Governance) ────────────────────────────────
    "audit_log": {
        "name": "Admin-Audit-Trail",
        "description": "Protokolliert alle administrativen Änderungen (Benutzer, Gruppen, "
                       "Sites, Netzwerke, SNMP, Karten, Reports). Zeigt wer, wann, was "
                       "geändert hat — wichtig für Compliance und Governance.",
        "icon": "🛡️",
        "pbi_page": None,
        "query_type": "audit_log",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
            "limit":      1000,
        },
        "output_formats": ["html", "pdf", "xlsx", "csv"],
        "mail_subject": "Printix Admin-Audit-Trail {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:30"},
        "tag": "Governance",
        "available": True,
    },

    # ── v3.9.0: Druck außerhalb der Geschäftszeiten ───────────────────────────
    "off_hours_print": {
        "name": "Off-Hours-Druck",
        "description": "Zeigt Druckaktivität außerhalb der regulären Arbeitszeit "
                       "(standardmäßig 18:00–07:00 und Wochenende). Dient der "
                       "Compliance- und Sicherheitsüberwachung.",
        "icon": "🌙",
        "pbi_page": None,
        "query_type": "off_hours_print",
        "query_params": {
            "start_date":            "last_month_start",
            "end_date":              "last_month_end",
            "business_start_hour":   7,
            "business_end_hour":     18,
            "include_weekends_as_off_hours": True,
        },
        "output_formats": ["html", "pdf"],
        "mail_subject": "Printix Off-Hours-Druck {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "08:45"},
        "tag": "Compliance",
        "available": True,
    },

    # ── Bonus: Anomalie-Erkennung (eigenständig, kein direktes PBI-Äquivalent) ─
    "anomaly_detection": {
        "name": "Anomalie-Erkennung",
        "description": "Automatische Erkennung ungewöhnlicher Druckmuster: "
                       "plötzliche Volumenspitzen, untypische Druckzeiten, Ausreißer.",
        "icon": "⚠️",
        "pbi_page": None,
        "query_type": "anomalies",
        "query_params": {
            "start_date": "last_month_start",
            "end_date":   "last_month_end",
        },
        "output_formats": ["html"],
        "mail_subject": "Printix Anomalie-Report {month}",
        "schedule_suggestion": {"frequency": "monthly", "day": 1, "time": "09:00"},
        "tag": "Analyse",
        "available": True,
    },
}


# ── Public API ────────────────────────────────────────────────────────────────────

def get_preset(key: str) -> Optional[dict[str, Any]]:
    """Gibt ein einzelnes Preset zurück (None wenn nicht gefunden)."""
    return PRESETS.get(key)


def list_presets(only_available: bool = False) -> list[dict[str, Any]]:
    """
    Gibt alle Presets als Liste zurück.

    Args:
        only_available: Wenn True, werden nur sofort ausführbare Presets zurückgegeben.
    """
    result = []
    for key, preset in PRESETS.items():
        if only_available and not preset.get("available", False):
            continue
        entry = dict(preset)
        entry["key"] = key
        result.append(entry)
    return result


def list_presets_by_tag(tag: str) -> list[dict[str, Any]]:
    """Filtert Presets nach Kategorie-Tag."""
    return [
        dict(p, key=k)
        for k, p in PRESETS.items()
        if p.get("tag") == tag
    ]


def get_available_tags() -> list[str]:
    """Gibt alle vorhandenen Tags in stabiler Reihenfolge zurück."""
    seen = []
    for p in PRESETS.values():
        t = p.get("tag", "")
        if t and t not in seen:
            seen.append(t)
    return seen


def preset_to_template_defaults(key: str, owner_user_id: str = "") -> Optional[dict[str, Any]]:
    """
    Wandelt ein Preset in ein Template-Dict um (bereit für template_store.save_template).
    Gibt None zurück wenn Preset nicht gefunden.

    Der Rückgabe-Dict entspricht den Parametern von save_template() und kann
    direkt als Formular-Vorausfüllung oder für die API genutzt werden.
    """
    preset = get_preset(key)
    if not preset:
        return None

    return {
        "name":           preset["name"],
        "query_type":     preset.get("query_type") or "",
        "query_params":   dict(preset.get("query_params", {})),
        "output_formats": list(preset.get("output_formats", ["html"])),
        "layout": {
            "primary_color": "#0078D4",
            "company_name":  "",
            "footer_text":   f"Erstellt aus Preset: {preset['name']}",
            "logo_base64":   "",
        },
        "schedule":       dict(preset["schedule_suggestion"])
                          if preset.get("schedule_suggestion") else None,
        "recipients":     [],
        "mail_subject":   preset.get("mail_subject", f"Printix Report: {preset['name']}"),
        "created_prompt": f"Aus Preset erstellt: {preset['name']} "
                          f"(PowerBI-Seite: {preset.get('pbi_page', 'n/a')})",
        "owner_user_id":  owner_user_id,
    }


# ── Statistiken ───────────────────────────────────────────────────────────────────

def get_stats() -> dict[str, Any]:
    """Gibt Zusammenfassung der Preset-Bibliothek zurück."""
    total = len(PRESETS)
    available = sum(1 for p in PRESETS.values() if p.get("available", False))
    tags = get_available_tags()
    return {
        "total":      total,
        "available":  available,
        "coming_soon": total - available,
        "tags":       tags,
        "pbi_version": "2025.4.0",
    }
