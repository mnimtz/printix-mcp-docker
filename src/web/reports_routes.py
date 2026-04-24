"""
Reports Routes — Web-UI für Report-Template-Verwaltung (v3.0.0)
===============================================================
Registriert alle /reports-Routen in der FastAPI-App.

Aufruf aus app.py:
    from web.reports_routes import register_reports_routes
    register_reports_routes(app, templates, t_ctx, require_login)

Routen:
  GET  /reports                   → Template-Liste + Preset-Bibliothek
  GET  /reports/new               → Neues Template (leer oder aus Preset)
  POST /reports/new               → Template speichern (neu)
  GET  /reports/{id}/edit         → Template bearbeiten
  POST /reports/{id}/edit         → Template speichern (Update)
  POST /reports/{id}/run          → Report sofort ausführen
  POST /reports/{id}/delete       → Template löschen
"""

import json
import logging
from typing import Any, Callable, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from reporting.email_parser import parse_and_validate

logger = logging.getLogger("printix.reports")

# Query-Typen mit Labels
# Reihenfolge bestimmt die Anzeige im <select>-Feld des Report-Formulars.
# Stufe 1 (6 Typen) + Stufe 2 (11 Typen) = 17 unterst\u00fctzte Query-Typen.
# ACHTUNG: Bei Erweiterung auch i18n-Keys rpt_type_* und rpt_eng_title_* erg\u00e4nzen.
QUERY_TYPE_LABELS = {
    # Stufe 1 \u2014 Original (v1.x)
    "print_stats":          "Druckvolumen-Statistik",
    "cost_report":          "Kostenanalyse",
    "top_users":            "Top-Benutzer",
    "top_printers":         "Top-Drucker",
    "anomalies":            "Anomalie-Erkennung",
    "trend":                "Drucktrend",
    # Stufe 2 \u2014 PowerBI-Parity (v3.7.9+)
    "printer_history":      "Drucker-Verlauf",
    "device_readings":      "Drucker Service-Status",
    "job_history":          "Job-Verlauf",
    "queue_stats":          "Druckregeln-\u00dcbersicht",
    "user_detail":          "Benutzer Druckdetails",
    "user_copy_detail":     "Benutzer Kopier-Details",
    "user_scan_detail":     "Benutzer Scan-Details",
    "workstation_overview": "Workstation-\u00dcbersicht",
    "workstation_detail":   "Workstation-Details",
    "tree_meter":           "Nachhaltigkeits-Report",
    "service_desk":         "Service Desk Report",
    # Stufe 2 \u2014 v3.8.0 (Compliance)
    "sensitive_documents":  "Sensible Dokumente",
    # Stufe 2 \u2014 v3.8.1 (Visual)
    "hour_dow_heatmap":     "Nutzung Stunde \u00d7 Wochentag",
    # Stufe 2 \u2014 v3.9.0 (Audit & Governance)
    "audit_log":            "Admin-Audit-Trail",
    "off_hours_print":      "Druck au\u00dferhalb Gesch\u00e4ftszeiten",
}

# Query-Typen die nicht durch das Standard-Formular (group_by/limit/cost)
# abgedeckt werden. F\u00fcr diese wird das Preset-qp komplett \u00fcbernommen und
# nur start_date/end_date aus dem Formular \u00fcberschrieben.
STUFE2_QUERY_TYPES = frozenset({
    "printer_history", "device_readings", "job_history", "queue_stats",
    "user_detail", "user_copy_detail", "user_scan_detail",
    "workstation_overview", "workstation_detail", "tree_meter", "service_desk",
    # v3.8.0
    "sensitive_documents",
    # v3.8.1
    "hour_dow_heatmap",
    # v3.9.0
    "audit_log", "off_hours_print",
})

# Frequenz-Labels
FREQ_LABELS = {
    "daily":   "Täglich",
    "weekly":  "Wöchentlich",
    "monthly": "Monatlich",
}

DOW_LABELS = {
    "0": "Montag", "1": "Dienstag", "2": "Mittwoch", "3": "Donnerstag",
    "4": "Freitag", "5": "Samstag", "6": "Sonntag",
}


def register_reports_routes(
    app: FastAPI,
    templates: Jinja2Templates,
    t_ctx: Callable,
    require_login: Callable,
) -> None:
    """Registriert alle /reports-Routen in der übergebenen FastAPI-App."""

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _redirect_login() -> RedirectResponse:
        return RedirectResponse("/login", status_code=302)

    def _flash(request: Request, msg: str, kind: str = "success") -> None:
        request.session["flash_msg"]  = msg
        request.session["flash_kind"] = kind

    def _pop_flash(request: Request) -> tuple[str, str]:
        msg  = request.session.pop("flash_msg",  "")
        kind = request.session.pop("flash_kind", "success")
        return msg, kind

    def _reporting_available(tenant: dict | None = None) -> bool:
        """
        Prüft ob die SQL-Konfiguration vorhanden ist.
        Setzt vorher den ContextVar aus dem übergebenen Tenant — die Web-Routen
        haben keinen automatischen Middleware-Pfad dafür (BearerAuthMiddleware
        läuft nur im MCP-Server-Prozess).
        """
        try:
            from reporting.sql_client import is_configured, set_config_from_tenant
            if tenant:
                try:
                    set_config_from_tenant(tenant)
                except Exception:
                    pass
            return is_configured()
        except Exception:
            return False

    def _mail_configured(tenant: dict) -> bool:
        return bool(tenant.get("mail_api_key") and tenant.get("mail_from"))

    def _get_tenant(user: dict) -> dict:
        try:
            from db import get_tenant_full_by_user_id
            t = get_tenant_full_by_user_id(user["id"])
            return t or {}
        except Exception:
            return {}

    def _resolve_logo(logo_remove: str, logo_base64: str,
                      logo_mime: str, logo_url: str) -> tuple[str, str, str]:
        """
        Entscheidet anhand der vom Form-POST gelieferten Werte, welches Logo
        gespeichert wird. Reihenfolge:
          1. logo_remove=1          → alles leer (Logo entfernen)
          2. neue Base64-Daten      → Base64+MIME speichern, URL leeren
          3. sonst Legacy-URL       → nur URL
        Liefert (base64, mime, url) fertig zum Speichern in layout.
        """
        if logo_remove and logo_remove.strip() == "1":
            return ("", "image/png", "")

        b64  = (logo_base64 or "").strip()
        mime = (logo_mime   or "image/png").strip() or "image/png"
        url  = (logo_url    or "").strip()

        if b64:
            # Neu hochgeladenes Logo hat Vorrang; URL wird verworfen, damit
            # der Legacy-Pfad im Engine-Renderer nicht als Fallback greift.
            # Safety: mime muss image/* sein
            if not mime.startswith("image/"):
                mime = "image/png"
            # Safety: Base64-Gr\u00f6\u00dfen-Cap (Rohbytes \u2248 0.75 \u00d7 len(b64))
            approx_raw = int(len(b64) * 0.75)
            if approx_raw > 1024 * 1024:   # 1 MB hart
                logger.warning("Logo-Upload abgelehnt: %d bytes > 1MB", approx_raw)
                return ("", "image/png", "")
            return (b64, mime, "")

        return ("", "image/png", url)

    def _parse_csv_list(raw: str) -> list[str]:
        """Hilft bei Komma/Semikolon/Whitespace-getrennten Listen (v3.8.0)."""
        if not raw:
            return []
        import re as _re
        parts = _re.split(r"[,;\n]+", raw)
        return [p.strip() for p in parts if p and p.strip()]

    def _merge_query_params(
        query_type: str,
        start_date: str,
        end_date: str,
        group_by: str,
        limit: str,
        cost_per_sheet: str,
        cost_per_mono: str,
        cost_per_color: str,
        preset_qp_json: str,
        existing_qp: Optional[dict] = None,
        keyword_sets: str = "",
        custom_keywords: str = "",
        include_scans: str = "",
        user_email: str = "",
    ) -> dict[str, Any]:
        """
        Baut ein sauberes query_params-Dict f\u00fcr ein Template.

        Stufe 1 (print_stats, cost_report, top_*, trend, anomalies) wird
        ausschlie\u00dflich aus den Formularfeldern bef\u00fcllt.

        Stufe 2 (printer_history, service_desk, workstation_*, \u2026) hat
        erweiterte Parameter die das Standard-Formular nicht abbildet
        (workstation_id, status_filter, sheets_per_tree, \u2026). F\u00fcr diese wird
        das Preset-qp aus dem Hidden-Field \u00fcbernommen und nur start_date/
        end_date aus dem Formular \u00fcberschrieben.

        existing_qp (beim Edit) hat Vorrang \u00fcber preset_qp_json, damit
        Benutzer-\u00c4nderungen nicht verloren gehen.
        """
        qp: dict[str, Any] = {}

        # Basis: bestehendes qp (Edit-Pfad) oder preset qp (Neu-Pfad)
        base: dict[str, Any] = {}
        if existing_qp:
            base = dict(existing_qp)
        elif preset_qp_json:
            try:
                parsed = json.loads(preset_qp_json)
                if isinstance(parsed, dict):
                    base = parsed
            except Exception as e:
                logger.warning("preset_qp_json nicht parsebar: %s", e)

        qp.update(base)

        # Start/Ende kommen immer vom Formular
        qp["start_date"] = start_date
        qp["end_date"]   = end_date

        # Stufe 1: sichtbare Widgets \u00fcberschreiben explizit
        if query_type in ("print_stats", "trend"):
            qp["group_by"] = group_by
        elif query_type == "cost_report":
            qp["group_by"]       = group_by
            qp["cost_per_sheet"] = float(cost_per_sheet or 0.01)
            qp["cost_per_mono"]  = float(cost_per_mono  or 0.02)
            qp["cost_per_color"] = float(cost_per_color or 0.08)
        elif query_type in ("top_users", "top_printers"):
            qp["limit"] = int(limit or 10)
        elif query_type in STUFE2_QUERY_TYPES:
            # Stufe 2: group_by darf aus dem Formular \u00fcbernommen werden, wenn
            # das Preset das Feld \u00fcberhaupt kennt \u2014 sonst entfernen.
            if "group_by" in qp and group_by:
                qp["group_by"] = group_by

            # v3.8.0 \u2014 sensitive_documents: Keyword-Sets + Freitext-Keywords
            # kommen aus dedizierten Form-Feldern und \u00fcberschreiben ggf. die
            # Default-Listen aus preset_qp_json.
            if query_type == "sensitive_documents":
                if keyword_sets:
                    qp["keyword_sets"] = _parse_csv_list(keyword_sets)
                elif "keyword_sets" not in qp:
                    qp["keyword_sets"] = []
                if custom_keywords:
                    qp["custom_keywords"] = _parse_csv_list(custom_keywords)
                elif "custom_keywords" not in qp:
                    qp["custom_keywords"] = []
                # include_scans als "1"/""-Checkbox; wenn Feld \u00fcberhaupt nicht
                # gesendet wurde (weil Template noch kein UI-Feld hat), Default
                # aus preset belassen.
                if include_scans != "":
                    qp["include_scans"] = include_scans == "1"
                elif "include_scans" not in qp:
                    qp["include_scans"] = True

            # v6.3.0 — user_detail / user_copy_detail / user_scan_detail:
            # E-Mail-Adresse zum Filtern eines einzelnen Benutzers. Leer
            # lassen = aggregierte Übersicht (ohne WHERE u.email = …).
            if query_type in ("user_detail", "user_copy_detail", "user_scan_detail"):
                qp["user_email"] = (user_email or "").strip()

        return qp

    def _schedule_label(schedule: Optional[dict]) -> str:
        if not schedule:
            return "—"
        freq = schedule.get("frequency", "monthly")
        time = schedule.get("time", "08:00")
        day  = schedule.get("day", 1)
        fl = FREQ_LABELS.get(freq, freq)
        if freq == "weekly":
            return f"{fl} {DOW_LABELS.get(str(day), str(day))} {time}"
        elif freq == "monthly":
            return f"{fl}, {day}. {time} Uhr"
        else:
            return f"{fl} {time} Uhr"

    # ── GET /reports — Übersicht ──────────────────────────────────────────────

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_list_get(request: Request):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)
        flash_msg, flash_kind = _pop_flash(request)
        tenant = _get_tenant(user)

        from reporting.template_store import list_templates_by_user
        from reporting.preset_templates import list_presets, get_available_tags
        from reporting.scheduler import list_scheduled_jobs

        user_templates = list_templates_by_user(user["id"])
        presets = list_presets()
        tags = get_available_tags()

        # Scheduled job IDs für is_scheduled-Flag
        scheduled_ids = {j["job_id"] for j in list_scheduled_jobs()}
        for t in user_templates:
            t["is_scheduled"]   = t.get("report_id", "") in scheduled_ids
            t["schedule_label"] = _schedule_label(t.get("schedule"))

        # Presets nach Tags gruppieren
        presets_by_tag: dict[str, list] = {}
        for tag in tags:
            presets_by_tag[tag] = [p for p in presets if p.get("tag") == tag]

        return templates.TemplateResponse("reports_list.html", {
            "request":          request,
            "user":             user,
            "tenant":           tenant,
            "templates_list":   user_templates,
            "presets_by_tag":   presets_by_tag,
            "tags":             tags,
            "reporting_ok":     _reporting_available(tenant),
            "mail_ok":          _mail_configured(tenant),
            "flash_msg":        flash_msg,
            "flash_kind":       flash_kind,
            "query_type_labels": QUERY_TYPE_LABELS,
            **tc,
        })

    # ── GET /reports/new — Formular (leer oder aus Preset) ───────────────────

    @app.get("/reports/new", response_class=HTMLResponse)
    async def reports_new_get(request: Request):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)
        tenant = _get_tenant(user)

        preset_key = request.query_params.get("preset", "")
        prefill: dict[str, Any] = {}

        if preset_key:
            from reporting.preset_templates import preset_to_template_defaults
            prefill = preset_to_template_defaults(preset_key, user["id"]) or {}

        return templates.TemplateResponse("reports_form.html", {
            "request":    request,
            "user":       user,
            "tenant":     tenant,
            "report":     prefill,
            "is_edit":    False,
            "preset_key": preset_key,
            "error":      None,
            "query_type_labels": QUERY_TYPE_LABELS,
            **tc,
        })

    # ── POST /reports/new — Template speichern ────────────────────────────────

    @app.post("/reports/new", response_class=HTMLResponse)
    async def reports_new_post(
        request:       Request,
        name:          str  = Form(...),
        query_type:    str  = Form(...),
        mail_subject:  str  = Form(default=""),
        start_date:    str  = Form(default="last_month_start"),
        end_date:      str  = Form(default="last_month_end"),
        group_by:      str  = Form(default="day"),
        limit:         str  = Form(default="10"),
        cost_per_sheet: str = Form(default="0.01"),
        cost_per_mono:  str = Form(default="0.02"),
        cost_per_color: str = Form(default="0.08"),
        recipients:    str  = Form(default=""),
        company_name:  str  = Form(default=""),
        logo_url:      str  = Form(default=""),
        logo_base64:   str  = Form(default=""),
        logo_mime:     str  = Form(default="image/png"),
        logo_remove:   str  = Form(default=""),
        primary_color: str  = Form(default="#0078D4"),
        footer_text:   str  = Form(default=""),
        fmt_html:      str  = Form(default=""),
        fmt_csv:       str  = Form(default=""),
        fmt_json:      str  = Form(default=""),
        fmt_pdf:       str  = Form(default=""),
        fmt_xlsx:      str  = Form(default=""),
        schedule_enabled: str = Form(default=""),
        freq:          str  = Form(default="monthly"),
        sched_day:     str  = Form(default="1"),
        sched_time:    str  = Form(default="08:00"),
        preset_qp_json: str = Form(default=""),
        # v3.8.0 \u2014 sensitive_documents Form-Felder
        keyword_sets:    str = Form(default=""),
        custom_keywords: str = Form(default=""),
        include_scans:   str = Form(default=""),
        # v6.3.0 \u2014 user_detail Form-Feld
        user_email:      str = Form(default=""),
    ):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)

        # Ausgabeformate aus Checkboxen
        output_formats = []
        if fmt_html:  output_formats.append("html")
        if fmt_csv:   output_formats.append("csv")
        if fmt_json:  output_formats.append("json")
        if fmt_pdf:   output_formats.append("pdf")
        if fmt_xlsx:  output_formats.append("xlsx")
        if not output_formats:
            output_formats = ["html"]

        # Query-Parameter je nach Typ (Stufe 1 + Stufe 2) \u2014 siehe _merge_query_params
        qp = _merge_query_params(
            query_type=query_type,
            start_date=start_date, end_date=end_date,
            group_by=group_by, limit=limit,
            cost_per_sheet=cost_per_sheet, cost_per_mono=cost_per_mono,
            cost_per_color=cost_per_color,
            preset_qp_json=preset_qp_json,
            keyword_sets=keyword_sets,
            custom_keywords=custom_keywords,
            include_scans=include_scans,
            user_email=user_email,
        )

        # Schedule
        schedule = None
        if schedule_enabled:
            schedule = {
                "frequency": freq,
                "day":       int(sched_day or 1),
                "time":      sched_time or "08:00",
            }

        # Empfänger-Liste parsen + validieren
        # Unterstützt: "max@x.de", "Max <max@x.de>", '"Nimtz, Marcus" <m@x.de>'
        # Separator: Komma oder Semikolon (außerhalb von Quotes/Angle-Brackets)
        recip, recip_errors = parse_and_validate(recipients)
        if recip_errors:
            return templates.TemplateResponse("reports_form.html", {
                "request":    request,
                "user":       user,
                "tenant":     _get_tenant(user),
                "report":     {
                    "name": name, "query_type": query_type,
                    "recipients": [recipients],  # zurück ins Feld
                    "mail_subject": mail_subject,
                },
                "is_edit":    False,
                "preset_key": "",
                "error":      "Ungültige Empfänger: " + "; ".join(recip_errors)
                              + ". Format: name@firma.de oder Max Mustermann <name@firma.de>",
                "query_type_labels": QUERY_TYPE_LABELS,
                **tc,
            })

        from reporting.template_store import save_template
        from reporting.scheduler import schedule_report, unschedule_report

        # Logo: Entweder bleibt leer (logo_remove=1), oder Base64 aus Upload,
        # oder Legacy-URL als Fallback. Base64 hat Vorrang, URL nur wenn kein Bild.
        _lb64, _lmime, _lurl = _resolve_logo(
            logo_remove=logo_remove,
            logo_base64=logo_base64,
            logo_mime=logo_mime,
            logo_url=logo_url,
        )

        try:
            template = save_template(
                name=name,
                query_type=query_type,
                query_params=qp,
                recipients=recip,
                mail_subject=mail_subject or f"Printix Report: {name}",
                output_formats=output_formats,
                layout={
                    "primary_color": primary_color or "#0078D4",
                    "company_name":  company_name,
                    "footer_text":   footer_text,
                    "logo_url":      _lurl,
                    "logo_base64":   _lb64,
                    "logo_mime":     _lmime,
                },
                schedule=schedule,
                owner_user_id=user["id"],
            )
            if schedule:
                schedule_report(template["report_id"], schedule)
            else:
                unschedule_report(template["report_id"])

            _flash(request, tc["_"]("reports_saved"))
            return RedirectResponse("/reports", status_code=302)

        except Exception as e:
            logger.error("Fehler beim Speichern des Templates: %s", e)
            return templates.TemplateResponse("reports_form.html", {
                "request":    request,
                "user":       user,
                "tenant":     _get_tenant(user),
                "report":     {},
                "is_edit":    False,
                "preset_key": "",
                "error":      str(e),
                "query_type_labels": QUERY_TYPE_LABELS,
                **tc,
            })

    # ── GET /reports/{id}/edit — Bearbeitungsformular ─────────────────────────

    @app.get("/reports/{report_id}/edit", response_class=HTMLResponse)
    async def reports_edit_get(report_id: str, request: Request):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)
        tenant = _get_tenant(user)

        from reporting.template_store import get_template
        report = get_template(report_id)
        if not report or report.get("owner_user_id", "") != user["id"]:
            _flash(request, "Template nicht gefunden.", "error")
            return RedirectResponse("/reports", status_code=302)

        return templates.TemplateResponse("reports_form.html", {
            "request":    request,
            "user":       user,
            "tenant":     tenant,
            "report":     report,
            "is_edit":    True,
            "preset_key": "",
            "error":      None,
            "query_type_labels": QUERY_TYPE_LABELS,
            **tc,
        })

    # ── POST /reports/{id}/edit — Update speichern ────────────────────────────

    @app.post("/reports/{report_id}/edit", response_class=HTMLResponse)
    async def reports_edit_post(
        report_id:     str,
        request:       Request,
        name:          str  = Form(...),
        query_type:    str  = Form(...),
        mail_subject:  str  = Form(default=""),
        start_date:    str  = Form(default="last_month_start"),
        end_date:      str  = Form(default="last_month_end"),
        group_by:      str  = Form(default="day"),
        limit:         str  = Form(default="10"),
        cost_per_sheet: str = Form(default="0.01"),
        cost_per_mono:  str = Form(default="0.02"),
        cost_per_color: str = Form(default="0.08"),
        recipients:    str  = Form(default=""),
        company_name:  str  = Form(default=""),
        logo_url:      str  = Form(default=""),
        logo_base64:   str  = Form(default=""),
        logo_mime:     str  = Form(default="image/png"),
        logo_remove:   str  = Form(default=""),
        primary_color: str  = Form(default="#0078D4"),
        footer_text:   str  = Form(default=""),
        fmt_html:      str  = Form(default=""),
        fmt_csv:       str  = Form(default=""),
        fmt_json:      str  = Form(default=""),
        fmt_pdf:       str  = Form(default=""),
        fmt_xlsx:      str  = Form(default=""),
        schedule_enabled: str = Form(default=""),
        freq:          str  = Form(default="monthly"),
        sched_day:     str  = Form(default="1"),
        sched_time:    str  = Form(default="08:00"),
        preset_qp_json: str = Form(default=""),
        # v3.8.0 \u2014 sensitive_documents Form-Felder
        keyword_sets:    str = Form(default=""),
        custom_keywords: str = Form(default=""),
        include_scans:   str = Form(default=""),
        # v6.3.0 \u2014 user_detail Form-Feld
        user_email:      str = Form(default=""),
    ):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)

        from reporting.template_store import get_template, save_template
        from reporting.scheduler import schedule_report, unschedule_report

        existing = get_template(report_id)
        if not existing or existing.get("owner_user_id", "") != user["id"]:
            _flash(request, "Template nicht gefunden.", "error")
            return RedirectResponse("/reports", status_code=302)

        output_formats = []
        if fmt_html:  output_formats.append("html")
        if fmt_csv:   output_formats.append("csv")
        if fmt_json:  output_formats.append("json")
        if fmt_pdf:   output_formats.append("pdf")
        if fmt_xlsx:  output_formats.append("xlsx")
        if not output_formats:
            output_formats = ["html"]

        # qp: Edit \u2014 bestehende query_params als Basis, Stufe-2-Parameter wie
        # workstation_id bleiben so erhalten auch wenn der Form-Request nur die
        # Standardfelder liefert.
        qp = _merge_query_params(
            query_type=query_type,
            start_date=start_date, end_date=end_date,
            group_by=group_by, limit=limit,
            cost_per_sheet=cost_per_sheet, cost_per_mono=cost_per_mono,
            cost_per_color=cost_per_color,
            preset_qp_json=preset_qp_json,
            existing_qp=existing.get("query_params", {}),
            keyword_sets=keyword_sets,
            custom_keywords=custom_keywords,
            include_scans=include_scans,
            user_email=user_email,
        )

        schedule = None
        if schedule_enabled:
            schedule = {
                "frequency": freq,
                "day":       int(sched_day or 1),
                "time":      sched_time or "08:00",
            }

        # Empfänger-Liste parsen + validieren (robust gegen 'Name <email>' Format)
        recip, recip_errors = parse_and_validate(recipients)
        if recip_errors:
            return templates.TemplateResponse("reports_form.html", {
                "request":    request,
                "user":       user,
                "tenant":     _get_tenant(user),
                "report":     existing,
                "is_edit":    True,
                "preset_key": "",
                "error":      "Ungültige Empfänger: " + "; ".join(recip_errors)
                              + ". Format: name@firma.de oder Max Mustermann <name@firma.de>",
                "query_type_labels": QUERY_TYPE_LABELS,
                **tc,
            })

        # Logo: neuer Upload überschreibt Bestand; leerer Upload → Bestand behalten;
        # logo_remove=1 → beides löschen.
        _lb64, _lmime, _lurl = _resolve_logo(
            logo_remove=logo_remove,
            logo_base64=logo_base64,
            logo_mime=logo_mime,
            logo_url=logo_url,
        )

        # Layout aus bestehendem Template übernehmen, nur geänderte Felder überschreiben
        layout = dict(existing.get("layout", {}))
        layout.update({
            "primary_color": primary_color or "#0078D4",
            "company_name":  company_name,
            "footer_text":   footer_text,
            "logo_url":      _lurl,
            "logo_base64":   _lb64,
            "logo_mime":     _lmime,
        })

        try:
            template = save_template(
                name=name,
                query_type=query_type,
                query_params=qp,
                recipients=recip,
                mail_subject=mail_subject or f"Printix Report: {name}",
                output_formats=output_formats,
                layout=layout,
                schedule=schedule,
                report_id=report_id,
                owner_user_id=user["id"],
            )
            if schedule:
                schedule_report(template["report_id"], schedule)
            else:
                unschedule_report(template["report_id"])

            _flash(request, tc["_"]("reports_saved"))
            return RedirectResponse("/reports", status_code=302)

        except Exception as e:
            logger.error("Fehler beim Update des Templates %s: %s", report_id, e)
            _flash(request, str(e), "error")
            return RedirectResponse(f"/reports/{report_id}/edit", status_code=302)

    # ── POST /reports/{id}/run — Sofort ausführen ─────────────────────────────

    @app.post("/reports/{report_id}/run", response_class=RedirectResponse)
    async def reports_run_post(report_id: str, request: Request):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)

        from reporting.template_store import get_template
        report = get_template(report_id)
        if not report or report.get("owner_user_id", "") != user["id"]:
            _flash(request, "Template nicht gefunden.", "error")
            return RedirectResponse("/reports", status_code=302)

        tenant = _get_tenant(user)
        try:
            from reporting.sql_client import set_config_from_tenant
            set_config_from_tenant(tenant)
            from reporting.scheduler import run_report_now
            result = run_report_now(
                report_id,
                mail_api_key=tenant.get("mail_api_key", "") or "",
                mail_from=tenant.get("mail_from", "") or "",
            )
            if result.get("mail_sent"):
                recip = ", ".join(result.get("recipients", []))
                _flash(request, f"✓ Report versendet an: {recip}")
            elif result.get("mail_error"):
                _flash(request, f"Report generiert, aber Mail-Fehler: {result['mail_error']}", "warning")
            else:
                _flash(request, "Report generiert (keine Empfänger konfiguriert).", "info")
        except Exception as e:
            logger.error("Fehler bei run_report_now(%s): %s", report_id, e)
            _flash(request, f"Fehler: {e}", "error")

        return RedirectResponse("/reports", status_code=302)

    # ── GET /reports/{id}/preview — Report-Vorschau (HTML, kein Mail-Versand) ──

    @app.get("/reports/{report_id}/preview", response_class=HTMLResponse)
    async def reports_preview_get(report_id: str, request: Request):
        """
        Zeigt den generierten HTML-Report direkt im Browser ohne E-Mail-Versand.
        Nützlich zur Kontrolle vor dem ersten geplanten Versand.
        """
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)  # v3.7.10: für lang-Passthrough an generate_report

        from reporting.template_store import get_template
        report = get_template(report_id)
        if not report or report.get("owner_user_id", "") != user["id"]:
            return HTMLResponse("<h2>Report nicht gefunden.</h2>", status_code=404)

        tenant = _get_tenant(user)
        try:
            from reporting.sql_client import set_config_from_tenant, is_configured
            set_config_from_tenant(tenant)
        except Exception as e:
            return HTMLResponse(
                f"<h2>SQL nicht konfiguriert</h2><p>{e}</p>"
                "<p><a href='/reports'>← Zurück</a></p>",
                status_code=503,
            )

        if not _reporting_available():
            return HTMLResponse(
                "<h2>Kein SQL-Server konfiguriert</h2>"
                "<p>Bitte SQL-Credentials in den <a href='/settings'>Einstellungen</a> eintragen.</p>",
                status_code=503,
            )

        try:
            import sys as _sys, os as _os
            src_dir = _os.path.dirname(_os.path.dirname(__file__))
            if src_dir not in _sys.path:
                _sys.path.insert(0, src_dir)
            from reporting.query_tools import run_query
            from reporting.report_engine import generate_report
            from reporting.sql_client import get_tenant_id
            from reporting.scheduler import _resolve_dynamic_dates

            qp = _resolve_dynamic_dates(report.get("query_params", {}))
            data = await __import__("asyncio").to_thread(
                run_query,
                query_type=report["query_type"],
                tenant_id=get_tenant_id(),
                **qp,
            )
            layout = report.get("layout", {})
            # v6.5.0: Labels aus i18n-Dict mitgeben damit rpt_eng_*-Keys
            # in UI-Sprache gerendert werden (sonst kamen rohe Keys durch,
            # z.B. 'rpt_eng_title_hour_dow_heatmap' als Titel).
            _ui_lang = tc.get("lang") or "en"
            try:
                from i18n import TRANSLATIONS as _TR
                _labels = {
                    k: v for k, v in (_TR.get(_ui_lang) or _TR.get("en") or {}).items()
                    if k.startswith("rpt_eng_")
                }
            except Exception:
                _labels = None
            html = generate_report(
                query_type=report["query_type"],
                data=data,
                period=f'{qp.get("start_date","?")} – {qp.get("end_date","?")}',
                layout=layout,
                output_formats=["html"],
                query_params=qp,
                lang=_ui_lang,
                labels=_labels,
            ).get("html", "<p>Keine Daten.</p>")

            # Vorschau-Banner oben anhängen
            banner = (
                f'<div style="background:#1a73e8;color:#fff;padding:10px 20px;font-family:sans-serif;'
                f'font-size:13px;display:flex;justify-content:space-between;align-items:center;">'
                f'<span>👁 <strong>Vorschau</strong> — {report.get("name","Report")} '
                f'(kein Mail-Versand)</span>'
                f'<a href="/reports" style="color:#fff;text-decoration:underline">← Zurück zu Reports</a>'
                f'</div>'
            )
            return HTMLResponse(banner + html)

        except Exception as e:
            logger.error("Preview-Fehler für Report %s: %s", report_id, e, exc_info=True)
            return HTMLResponse(
                f"<h2>Vorschau fehlgeschlagen</h2><pre>{e}</pre>"
                "<p><a href='/reports'>← Zurück</a></p>",
                status_code=500,
            )

    # ── POST /reports/{id}/delete — Template löschen ──────────────────────────

    @app.post("/reports/{report_id}/delete", response_class=RedirectResponse)
    async def reports_delete_post(report_id: str, request: Request):
        user = require_login(request)
        if not user:
            return _redirect_login()
        tc = t_ctx(request)

        from reporting.template_store import get_template, delete_template
        from reporting.scheduler import unschedule_report

        report = get_template(report_id)
        if not report or report.get("owner_user_id", "") != user["id"]:
            _flash(request, "Template nicht gefunden.", "error")
            return RedirectResponse("/reports", status_code=302)

        try:
            unschedule_report(report_id)
            delete_template(report_id)
            _flash(request, tc["_"]("reports_deleted"))
        except Exception as e:
            logger.error("Fehler beim Löschen von Template %s: %s", report_id, e)
            _flash(request, f"Fehler: {e}", "error")

        return RedirectResponse("/reports", status_code=302)

    logger.info("Reports-Routen registriert (/reports, /reports/new, /reports/{id}/edit|run|delete)")
