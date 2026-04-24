"""
Scheduler — Zeitgesteuerte Report-Ausführung
=============================================
Läuft als Background-Thread im MCP-Server-Prozess.
"""

import logging
import os
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False
    logger.warning("apscheduler nicht installiert — automatische Scheduling-Funktion nicht verfügbar")

_scheduler: Optional[Any] = None


def _get_scheduler() -> Any:
    global _scheduler
    if _scheduler is None:
        if not APSCHEDULER_AVAILABLE:
            raise RuntimeError("apscheduler nicht installiert.")
        _scheduler = BackgroundScheduler(
            job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 3600},
            timezone="UTC",
        )
        _scheduler.start()
        logger.info("APScheduler gestartet")
    return _scheduler


def _build_cron_trigger(schedule: dict) -> Any:
    freq = schedule.get("frequency", "monthly")
    time_str = schedule.get("time", "08:00")
    try:
        hour, minute = [int(x) for x in time_str.split(":")]
    except (ValueError, AttributeError):
        hour, minute = 8, 0
    if freq == "daily":
        return CronTrigger(hour=hour, minute=minute)
    elif freq == "weekly":
        day_of_week = int(schedule.get("day", 0))
        dow_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
        return CronTrigger(day_of_week=dow_map.get(day_of_week, "mon"), hour=hour, minute=minute)
    else:
        day = max(1, min(28, int(schedule.get("day", 1))))
        return CronTrigger(day=day, hour=hour, minute=minute)


def _load_tenant_mail_credentials(owner_user_id: str) -> tuple[str, str, str]:
    """v6.7.25: 3-stufige Fallback-Resolution (Tenant → Global-Admin → Env).

    Wenn der Tenant keine eigenen Mail-Credentials hinterlegt hat, fallen wir
    auf die im Admin-Settings hinterlegten globalen Credentials zurück.
    """
    if not owner_user_id:
        return "", "", ""
    try:
        import sys
        if "/app" not in sys.path:
            sys.path.insert(0, "/app")
        from db import get_tenant_full_by_user_id
        tenant = get_tenant_full_by_user_id(owner_user_id)
        if not tenant:
            logger.warning("Kein Tenant fuer owner_user_id=%s gefunden", owner_user_id)
            tenant = {}

        from reporting.notify_helper import resolve_mail_credentials
        creds = resolve_mail_credentials(tenant)
        logger.debug("Mail-Credentials für %s aufgelöst via source=%s",
                     owner_user_id, creds.get("source"))
        return (
            creds.get("api_key", ""),
            creds.get("mail_from", ""),
            creds.get("mail_from_name", ""),
        )
    except Exception as e:
        logger.error("Fehler beim Laden der Tenant-Mail-Credentials: %s", e)
        return "", "", ""


def _resolve_subject(subject: str, params: dict) -> str:
    """
    Ersetzt Platzhalter im Mail-Betreff:
      {year}, {month}, {month_name}, {quarter}, {period}
    """
    from datetime import date as _d
    today = _d.today()
    month_names = ["", "Januar", "Februar", "Maerz", "April", "Mai", "Juni",
                   "Juli", "August", "September", "Oktober", "November", "Dezember"]
    quarter = (today.month - 1) // 3 + 1
    start = params.get("start_date", "")
    end   = params.get("end_date", "")
    period = f"{start} - {end}" if start and end else ""
    try:
        return subject.format(
            year=today.year,
            month=f"{today.month:02d}",
            month_name=month_names[today.month],
            quarter=quarter,
            period=period,
        )
    except (KeyError, IndexError):
        return subject


def _resolve_dynamic_dates(params: dict) -> dict:
    """
    Loest relative Datumswerte auf:
      last_month_start/end, this_month_start, today,
      last_year_start/end, this_year_start,
      last_week_start/end, last_quarter_start/end
    """
    from datetime import date, timedelta
    import calendar

    today = date.today()
    first_this_month = today.replace(day=1)
    last_month_end   = first_this_month - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)

    last_year_start = date(today.year - 1, 1, 1)
    last_year_end   = date(today.year - 1, 12, 31)
    this_year_start = date(today.year, 1, 1)

    last_week_end   = today - timedelta(days=today.weekday() + 1)
    last_week_start = last_week_end - timedelta(days=6)

    current_quarter = (today.month - 1) // 3 + 1
    if current_quarter == 1:
        lq_start = date(today.year - 1, 10, 1)
        lq_end   = date(today.year - 1, 12, 31)
    else:
        lq_month_start = (current_quarter - 2) * 3 + 1
        lq_start = date(today.year, lq_month_start, 1)
        lq_end_month = lq_month_start + 2
        lq_end = date(today.year, lq_end_month, calendar.monthrange(today.year, lq_end_month)[1])

    magic = {
        "last_month_start":   str(last_month_start),
        "last_month_end":     str(last_month_end),
        "this_month_start":   str(first_this_month),
        "today":              str(today),
        "last_year_start":    str(last_year_start),
        "last_year_end":      str(last_year_end),
        "this_year_start":    str(this_year_start),
        "last_week_start":    str(last_week_start),
        "last_week_end":      str(last_week_end),
        "last_quarter_start": str(lq_start),
        "last_quarter_end":   str(lq_end),
    }

    resolved = {}
    for k, v in params.items():
        resolved[k] = magic.get(str(v), v)
    return resolved


def _build_attachments(outputs: dict) -> list[dict]:
    """
    Baut die Anhang-Liste fuer send_report().
    CSV wird als ZIP verpackt, da viele E-Mail-Provider .csv-Dateien filtern.
    """
    import base64 as _b64
    import zipfile as _zf
    import io as _io

    content_types = {
        "json": "application/json",
        "pdf":  "application/pdf",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "zip":  "application/zip",
    }

    attachments = []
    for fmt, content in outputs.items():
        if fmt in ("html", "pdf_error", "xlsx_error") or not content:
            continue
        raw = content if isinstance(content, bytes) else content.encode("utf-8")

        if fmt == "csv":
            # CSV in ZIP packen — verhindert Filterung durch E-Mail-Provider
            zbuf = _io.BytesIO()
            with _zf.ZipFile(zbuf, "w", _zf.ZIP_DEFLATED) as z:
                z.writestr("report.csv", raw)
            attachments.append({
                "filename":     "report_csv.zip",
                "content":      _b64.b64encode(zbuf.getvalue()).decode("ascii"),
                "content_type": "application/zip",
            })
        else:
            attachments.append({
                "filename":     f"report.{fmt}",
                "content":      _b64.b64encode(raw).decode("ascii"),
                "content_type": content_types.get(fmt, "application/octet-stream"),
            })

    logger.info("Anhaenge gebaut: %s", [a["filename"] for a in attachments])
    return attachments


def _run_report_job(report_id: str) -> None:
    """Fuehrt einen geplanten Report aus und versendet ihn per Mail."""
    from .template_store import get_template
    from .report_engine  import generate_report
    from .mail_client    import send_report
    from . import query_tools

    logger.info("Starte geplanten Report: %s", report_id)

    template = get_template(report_id)
    if not template:
        logger.error("Template nicht gefunden: %s", report_id)
        return

    query_type    = template.get("query_type", "")
    params        = template.get("query_params", {})
    layout        = template.get("layout", {})
    recipients    = template.get("recipients", [])
    subject       = template.get("mail_subject", f"Printix Report: {template.get('name','')}")
    formats       = template.get("output_formats", ["html"])
    owner_user_id = template.get("owner_user_id", "")

    mail_api_key, mail_from, mail_from_name = _load_tenant_mail_credentials(owner_user_id)
    if not mail_api_key:
        logger.warning("Report '%s': kein mail_api_key — Mail nicht versendet", template.get("name"))

    params = _resolve_dynamic_dates(params)

    try:
        # v3.7.9: run_query dispatcht Stufe 1 + Stufe 2 (17 Query-Typen).
        # Vorher waren nur 6 Typen hardcoded, Schedules f\u00fcr Stufe-2-Reports
        # (service_desk, workstation_*, printer_history \u2026) sind still gescheitert.
        try:
            data = query_tools.run_query(query_type=query_type, **params)
        except ValueError as ve:
            logger.error("Unbekannter Query-Typ im Schedule: %s", ve)
            return
        period = f"{params.get('start_date','?')} - {params.get('end_date','?')}"

        # v6.5.0: Sprache + rpt_eng_*-Labels aus i18n laden damit
        # Titel nicht als roher Key durchrutschen.
        _sched_lang = layout.get("language") or "en"
        try:
            from web.i18n import TRANSLATIONS as _SCH_TR
            _sched_labels = {
                k: v for k, v in (_SCH_TR.get(_sched_lang) or _SCH_TR.get("en") or {}).items()
                if k.startswith("rpt_eng_")
            }
        except Exception:
            _sched_labels = None

        outputs = generate_report(
            query_type=query_type,
            data=data,
            period=period,
            layout=layout,
            output_formats=formats,
            currency=layout.get("currency", "EUR"),
            query_params=params,
            lang=_sched_lang,
            labels=_sched_labels,
        )

        html_body   = outputs.get("html", "<p>Report generiert.</p>")
        attachments = _build_attachments(outputs)
        resolved_subject = _resolve_subject(subject, params)

        if recipients and mail_api_key:
            send_report(
                recipients=recipients,
                subject=resolved_subject,
                html_body=html_body,
                api_key=mail_api_key,
                mail_from=mail_from,
                mail_from_name=mail_from_name,
                attachments=attachments if attachments else None,
            )
            logger.info("Report '%s' versendet an: %s (%d Anhaenge)",
                        template.get("name"), ", ".join(recipients), len(attachments))
        elif recipients:
            logger.warning("Report '%s' — Mail nicht versendet (kein API-Key)", template.get("name"))
        else:
            logger.warning("Report '%s' — keine Empfaenger", template.get("name"))

    except Exception as e:
        logger.error("Fehler beim Report '%s': %s", template.get("name"), e, exc_info=True)


def schedule_report(report_id: str, schedule: dict) -> None:
    """Legt einen Cron-Job fuer ein Report-Template an."""
    sched = _get_scheduler()
    if sched.get_job(report_id):
        sched.remove_job(report_id)
    trigger = _build_cron_trigger(schedule)
    sched.add_job(
        _run_report_job,
        trigger=trigger,
        id=report_id,
        args=[report_id],
        name=f"report:{report_id[:8]}",
        replace_existing=True,
    )
    logger.info("Report %s eingeplant: %s %s", report_id, schedule.get("frequency"), schedule.get("time"))


def unschedule_report(report_id: str) -> bool:
    if not APSCHEDULER_AVAILABLE:
        return False
    try:
        sched = _get_scheduler()
        if sched.get_job(report_id):
            sched.remove_job(report_id)
            logger.info("Report %s aus Schedule entfernt", report_id)
            return True
        return False
    except Exception as e:
        logger.error("Fehler beim Entfernen des Schedules %s: %s", report_id, e)
        return False


def list_scheduled_jobs() -> list[dict]:
    if not APSCHEDULER_AVAILABLE:
        return []
    try:
        sched = _get_scheduler()
        return [
            {"job_id": j.id, "name": j.name,
             "next_run_utc": str(j.next_run_time) if j.next_run_time else None,
             "trigger": str(j.trigger)}
            for j in sched.get_jobs()
        ]
    except Exception as e:
        logger.error("Fehler beim Abrufen der Scheduler-Jobs: %s", e)
        return []


def init_scheduler_from_templates() -> int:
    if not APSCHEDULER_AVAILABLE:
        logger.warning("APScheduler nicht verfuegbar")
        return 0
    from .template_store import get_scheduled_templates
    templates = get_scheduled_templates()
    count = 0
    for template in templates:
        try:
            schedule_report(template["report_id"], template["schedule"])
            count += 1
        except Exception as e:
            logger.error("Fehler beim Einplanen von Template %s: %s", template.get("report_id"), e)
    logger.info("%d Report-Schedules aus Templates geladen", count)
    return count


def run_report_now(
    report_id: str,
    mail_api_key: str = "",
    mail_from: str = "",
    mail_from_name: str = "",
) -> dict:
    """Fuehrt einen Report sofort aus (on-demand, ohne Schedule)."""
    from .template_store import get_template
    from .report_engine  import generate_report
    from .mail_client    import send_report
    from . import query_tools

    template = get_template(report_id)
    if not template:
        raise ValueError(f"Template {report_id} nicht gefunden.")

    query_type = template.get("query_type", "")
    params     = _resolve_dynamic_dates(template.get("query_params", {}))
    layout     = template.get("layout", {})
    recipients = template.get("recipients", [])
    subject    = template.get("mail_subject", f"Printix Report: {template.get('name','')}")
    formats    = template.get("output_formats", ["html"])

    if not mail_api_key:
        owner_user_id = template.get("owner_user_id", "")
        mail_api_key, mail_from, mail_from_name = _load_tenant_mail_credentials(owner_user_id)

    # v3.7.9: run_query dispatcht Stufe 1 + Stufe 2 (17 Query-Typen).
    # Vorher waren nur 6 Typen hardcoded; "Jetzt senden" f\u00fcr Stufe-2-Reports
    # (service_desk, workstation_*, printer_history \u2026) brach vorher ab.
    data   = query_tools.run_query(query_type=query_type, **params)
    period = f"{params.get('start_date','?')} - {params.get('end_date','?')}"

    # v6.5.0: Labels + lang auch beim Sofort-Senden
    _now_lang = layout.get("language") or "en"
    try:
        from web.i18n import TRANSLATIONS as _NOW_TR
        _now_labels = {
            k: v for k, v in (_NOW_TR.get(_now_lang) or _NOW_TR.get("en") or {}).items()
            if k.startswith("rpt_eng_")
        }
    except Exception:
        _now_labels = None

    outputs = generate_report(
        query_type=query_type,
        data=data,
        period=period,
        layout=layout,
        output_formats=formats,
        currency=layout.get("currency", "EUR"),
        query_params=params,
        lang=_now_lang,
        labels=_now_labels,
    )

    mail_sent = False
    mail_error = None
    if recipients and mail_api_key:
        try:
            html_body        = outputs.get("html", "<p>Report</p>")
            attachments      = _build_attachments(outputs)
            resolved_subject = _resolve_subject(subject, params)

            send_report(
                recipients=recipients,
                subject=resolved_subject,
                html_body=html_body,
                api_key=mail_api_key,
                mail_from=mail_from,
                mail_from_name=mail_from_name or None,
                attachments=attachments if attachments else None,
            )
            mail_sent = True
        except Exception as e:
            mail_error = str(e)
            logger.error("Mail-Versand fehlgeschlagen: %s", e)
    elif recipients:
        mail_error = "Kein mail_api_key konfiguriert"

    return {
        "status":       "ok",
        "report_name":  template.get("name"),
        "rows":         len(data) if isinstance(data, list) else "n/a",
        "mail_sent":    mail_sent,
        "mail_error":   mail_error,
        "recipients":   recipients,
        "html_preview": outputs.get("html", "")[:800] + "..." if "html" in outputs else None,
    }
