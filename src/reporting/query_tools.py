"""
Query Tools — Printix BI Datenabfragen
=======================================
Alle Abfragen gegen dbo-Schema der printix_bi_data_2_1 Datenbank.

Tabellenstruktur (aus PowerBI-Template extrahiert):
  dbo.tracking_data  — Druckaufträge (page_count, color, duplex, print_time, printer_id, job_id, tenant_id)
  dbo.jobs           — Jobs (id, tenant_id, color, duplex, page_count, paper_size, printer_id, submit_time, tenant_user_id, name)
  dbo.users          — Benutzer (id, tenant_id, email, name, department)
  dbo.printers       — Drucker (id, tenant_id, name, model_name, vendor_name, network_id, location)
  dbo.networks       — Netzwerke/Standorte (id, tenant_id, name)
  dbo.jobs_scan      — Scan-Jobs (id, tenant_id, printer_id, tenant_user_id, scan_time, page_count, color)
  dbo.jobs_copy      — Kopier-Jobs (id, tenant_id, printer_id, tenant_user_id, copy_time)
  dbo.jobs_copy_details — Kopier-Details (id, job_id, page_count, paper_size, duplex, color)

Kostenformel (aus PowerBI DAX):
  sheet_count  = CEIL(page_count / 2) wenn duplex, sonst page_count
  toner_color  = page_count × cost_per_color  (wenn color=True)
  toner_bw     = page_count × cost_per_mono   (wenn color=False)
  sheet_cost   = sheet_count × cost_per_sheet
  total_cost   = sheet_cost + toner_color + toner_bw
"""

import logging
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any, Optional

from .sql_client import query_fetchall, get_tenant_id

logger = logging.getLogger(__name__)


# ─── Demo-Daten Merge-Layer (v4.4.0) ────────────────────────────────────────
# Demo-Daten liegen lokal in SQLite, echte Daten in Azure SQL.
# Dieser Merge-Layer kombiniert beide Quellen für Reports.

def _has_active_demo() -> bool:
    """Prüft ob aktive lokale Demo-Daten für den aktuellen Tenant existieren."""
    try:
        from .local_demo_db import has_active_demo
        tid = get_tenant_id()
        return has_active_demo(tid) if tid else False
    except Exception:
        return False


def _get_demo_rows(start_date, end_date) -> list[dict]:
    """Holt Demo-Tracking-Rohdaten aus lokaler SQLite für den Merge."""
    try:
        from .local_demo_db import query_demo_tracking_data
        tid = get_tenant_id()
        if not tid:
            return []
        s = str(_fmt_date(start_date))
        e = str(_fmt_date(end_date))
        return query_demo_tracking_data(tid, s, e)
    except Exception as ex:
        logger.debug("Demo-Daten Merge fehlgeschlagen: %s", ex)
        return []




def _get_demo_scan_rows(start_date, end_date) -> list[dict]:
    """Holt Demo-Scan-Jobs aus lokaler SQLite (v4.4.14)."""
    try:
        from .local_demo_db import query_demo_scan_jobs
        tid = get_tenant_id()
        if not tid:
            return []
        return query_demo_scan_jobs(tid, str(_fmt_date(start_date)), str(_fmt_date(end_date)))
    except Exception as ex:
        logger.debug("Demo scan merge fehlgeschlagen: %s", ex)
        return []


def _get_demo_copy_rows(start_date, end_date) -> list[dict]:
    """Holt Demo-Copy-Jobs aus lokaler SQLite (v4.4.14)."""
    try:
        from .local_demo_db import query_demo_copy_jobs
        tid = get_tenant_id()
        if not tid:
            return []
        return query_demo_copy_jobs(tid, str(_fmt_date(start_date)), str(_fmt_date(end_date)))
    except Exception as ex:
        logger.debug("Demo copy merge fehlgeschlagen: %s", ex)
        return []


def _get_demo_job_rows(start_date, end_date) -> list[dict]:
    """Holt Demo-Jobs aus lokaler SQLite (v4.4.14)."""
    try:
        from .local_demo_db import query_demo_jobs
        tid = get_tenant_id()
        if not tid:
            return []
        return query_demo_jobs(tid, str(_fmt_date(start_date)), str(_fmt_date(end_date)))
    except Exception as ex:
        logger.debug("Demo job merge fehlgeschlagen: %s", ex)
        return []


def _parse_demo_date(val) -> date:
    """Parst ein Datum aus SQLite-Ergebnis."""
    if isinstance(val, date):
        return val
    s = str(val).strip()[:10]
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception:
        return date.today()


def _demo_week_start(d: date) -> date:
    """Montag der Woche."""
    return d - timedelta(days=d.weekday())


def _demo_month_start(d: date) -> date:
    """Erster Tag des Monats."""
    return d.replace(day=1)


def _aggregate_demo_print_stats(demo_rows: list[dict], group_by: str,
                                 site_id=None, user_email=None,
                                 printer_id=None) -> list[dict]:
    """
    Aggregiert Demo-Tracking-Rohdaten wie query_print_stats.
    Gibt list[dict] mit gleicher Struktur zurück.
    """
    # Filter anwenden
    filtered = demo_rows
    if site_id:
        filtered = [r for r in filtered if r.get("network_id") == site_id]
    if user_email:
        filtered = [r for r in filtered if r.get("user_email") == user_email]
    if printer_id:
        filtered = [r for r in filtered if r.get("printer_id") == printer_id]

    if not filtered:
        return []

    # Gruppierung
    groups: dict[str, list[dict]] = defaultdict(list)
    for r in filtered:
        pt = str(r.get("print_time", ""))
        d = _parse_demo_date(pt)
        if group_by == "day":
            key = str(d)
        elif group_by == "week":
            key = str(_demo_week_start(d))
        elif group_by == "month":
            key = str(_demo_month_start(d))
        elif group_by == "user":
            key = r.get("user_name") or r.get("user_email", "Unknown")
        elif group_by == "printer":
            key = r.get("printer_name", "Unknown")
        elif group_by == "site":
            key = r.get("network_name", "Unknown")
        else:
            key = str(d)
        groups[key].append(r)

    # Aggregation
    results = []
    for period, rows in groups.items():
        job_ids = set()
        total_pages = 0
        color_pages = 0
        bw_pages = 0
        duplex_pages = 0
        saved_sheets = 0
        for r in rows:
            jid = r.get("job_id", "")
            job_ids.add(jid)
            pc = int(r.get("page_count") or 0)
            is_color = bool(int(r.get("color") or 0))
            is_duplex = bool(int(r.get("duplex") or 0))
            total_pages += pc
            if is_color:
                color_pages += pc
            else:
                bw_pages += pc
            if is_duplex:
                duplex_pages += pc
                saved_sheets += pc - math.ceil(pc / 2)

        color_pct = round(color_pages * 100.0 / total_pages, 1) if total_pages else 0
        duplex_pct = round(duplex_pages * 100.0 / total_pages, 1) if total_pages else 0

        results.append({
            "period": period,
            "total_jobs": len(job_ids),
            "total_pages": total_pages,
            "color_pages": color_pages,
            "bw_pages": bw_pages,
            "duplex_pages": duplex_pages,
            "color_pct": color_pct,
            "duplex_pct": duplex_pct,
            "saved_sheets_duplex": saved_sheets,
        })

    return results


def _aggregate_demo_cost_report(demo_rows: list[dict], group_by: str,
                                 cost_per_sheet: float, cost_per_mono: float,
                                 cost_per_color: float,
                                 site_id=None) -> list[dict]:
    """Aggregiert Demo-Daten wie query_cost_report."""
    filtered = demo_rows
    if site_id:
        filtered = [r for r in filtered if r.get("network_id") == site_id]
    if not filtered:
        return []

    groups: dict[str, list[dict]] = defaultdict(list)
    for r in filtered:
        d = _parse_demo_date(str(r.get("print_time", "")))
        if group_by == "day":
            key = str(d)
        elif group_by == "week":
            key = str(_demo_week_start(d))
        elif group_by == "site":
            key = r.get("network_name", "Unknown")
        else:
            key = str(_demo_month_start(d))
        groups[key].append(r)

    results = []
    for period, rows in groups.items():
        total_pages = color_pages = bw_pages = 0
        total_sheets = 0.0
        toner_color = toner_bw = sheet_cost = total_cost = 0.0
        for r in rows:
            pc = int(r.get("page_count") or 0)
            is_color = bool(int(r.get("color") or 0))
            is_duplex = bool(int(r.get("duplex") or 0))
            total_pages += pc
            sheets = math.ceil(pc / 2) if is_duplex else pc
            total_sheets += sheets
            if is_color:
                color_pages += pc
                tc = pc * cost_per_color
                toner_color += tc
            else:
                bw_pages += pc
                tc = pc * cost_per_mono
                toner_bw += tc
            sc = sheets * cost_per_sheet
            sheet_cost += sc
            total_cost += sc + tc

        results.append({
            "period": period,
            "total_pages": total_pages,
            "color_pages": color_pages,
            "bw_pages": bw_pages,
            "total_sheets": int(total_sheets),
            "toner_cost_color": round(toner_color, 2),
            "toner_cost_bw": round(toner_bw, 2),
            "sheet_cost": round(sheet_cost, 2),
            "total_cost": round(total_cost, 2),
        })
    return results


def _aggregate_demo_top_users(demo_rows: list[dict], top_n: int,
                               metric: str, cost_per_sheet: float,
                               cost_per_mono: float, cost_per_color: float,
                               site_id=None) -> list[dict]:
    """Aggregiert Demo-Daten wie query_top_users."""
    filtered = demo_rows
    if site_id:
        filtered = [r for r in filtered if r.get("network_id") == site_id]
    if not filtered:
        return []

    users: dict[str, dict] = {}
    for r in filtered:
        email = r.get("user_email") or "unknown"
        if email not in users:
            users[email] = {
                "email": email, "name": r.get("user_name", ""),
                "department": r.get("department", ""),
                "job_ids": set(), "total_pages": 0, "color_pages": 0,
                "bw_pages": 0, "duplex_pages": 0, "total_cost": 0.0,
            }
        u = users[email]
        u["job_ids"].add(r.get("job_id", ""))
        pc = int(r.get("page_count") or 0)
        is_color = bool(int(r.get("color") or 0))
        is_duplex = bool(int(r.get("duplex") or 0))
        u["total_pages"] += pc
        if is_color:
            u["color_pages"] += pc
        else:
            u["bw_pages"] += pc
        if is_duplex:
            u["duplex_pages"] += pc
        sheets = math.ceil(pc / 2) if is_duplex else pc
        cost = sheets * cost_per_sheet
        cost += pc * (cost_per_color if is_color else cost_per_mono)
        u["total_cost"] += cost

    results = []
    for u in users.values():
        tp = u["total_pages"]
        results.append({
            "email": u["email"], "name": u["name"], "department": u["department"],
            "total_jobs": len(u["job_ids"]),
            "total_pages": tp,
            "color_pages": u["color_pages"],
            "bw_pages": u["bw_pages"],
            "duplex_pages": u["duplex_pages"],
            "color_pct": round(u["color_pages"] * 100.0 / tp, 1) if tp else 0,
            "total_cost": round(u["total_cost"], 2),
        })

    sort_key = {"pages": "total_pages", "cost": "total_cost",
                "jobs": "total_jobs", "color_pages": "color_pages"}.get(metric, "total_pages")
    results.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
    return results[:top_n]


def _aggregate_demo_top_printers(demo_rows: list[dict], top_n: int,
                                  metric: str, cost_per_sheet: float,
                                  cost_per_mono: float, cost_per_color: float,
                                  site_id=None) -> list[dict]:
    """Aggregiert Demo-Daten wie query_top_printers."""
    filtered = demo_rows
    if site_id:
        filtered = [r for r in filtered if r.get("network_id") == site_id]
    if not filtered:
        return []

    printers: dict[str, dict] = {}
    for r in filtered:
        pid = r.get("printer_id") or "unknown"
        if pid not in printers:
            printers[pid] = {
                "printer_name": r.get("printer_name", "Unknown"),
                "model_name": r.get("model_name", ""),
                "vendor_name": r.get("vendor_name", ""),
                "location": r.get("location", ""),
                "site_name": r.get("network_name", ""),
                "job_ids": set(), "total_pages": 0, "color_pages": 0,
                "bw_pages": 0, "total_cost": 0.0,
            }
        p = printers[pid]
        p["job_ids"].add(r.get("job_id", ""))
        pc = int(r.get("page_count") or 0)
        is_color = bool(int(r.get("color") or 0))
        is_duplex = bool(int(r.get("duplex") or 0))
        p["total_pages"] += pc
        if is_color:
            p["color_pages"] += pc
        else:
            p["bw_pages"] += pc
        sheets = math.ceil(pc / 2) if is_duplex else pc
        cost = sheets * cost_per_sheet + pc * (cost_per_color if is_color else cost_per_mono)
        p["total_cost"] += cost

    results = []
    for p in printers.values():
        tp = p["total_pages"]
        results.append({
            "printer_name": p["printer_name"], "model_name": p["model_name"],
            "vendor_name": p["vendor_name"], "location": p["location"],
            "site_name": p["site_name"],
            "total_jobs": len(p["job_ids"]),
            "total_pages": tp,
            "color_pages": p["color_pages"],
            "bw_pages": p["bw_pages"],
            "color_pct": round(p["color_pages"] * 100.0 / tp, 1) if tp else 0,
            "total_cost": round(p["total_cost"], 2),
        })

    sort_key = {"pages": "total_pages", "cost": "total_cost",
                "jobs": "total_jobs", "color_pages": "color_pages"}.get(metric, "total_pages")
    results.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
    return results[:top_n]


def _merge_aggregated(sql_rows: list[dict], demo_rows: list[dict],
                       key_field: str = "period") -> list[dict]:
    """
    Mergt SQL- und Demo-Ergebnisse. Bei gleichem Schlüssel (period/name)
    werden numerische Felder addiert.
    """
    if not demo_rows:
        return sql_rows

    merged: dict[str, dict] = {}
    for r in sql_rows:
        k = str(r.get(key_field, ""))
        merged[k] = dict(r)

    for r in demo_rows:
        k = str(r.get(key_field, ""))
        if k in merged:
            existing = merged[k]
            for field, val in r.items():
                if field == key_field:
                    continue
                if isinstance(val, (int, float)) and isinstance(existing.get(field), (int, float)):
                    existing[field] = existing[field] + val
            # Prozente neu berechnen
            tp = existing.get("total_pages", 0)
            if tp and "color_pct" in existing:
                existing["color_pct"] = round(existing.get("color_pages", 0) * 100.0 / tp, 1)
            if tp and "duplex_pct" in existing:
                existing["duplex_pct"] = round(existing.get("duplex_pages", 0) * 100.0 / tp, 1)
        else:
            merged[k] = dict(r)

    return list(merged.values())


# ─── Reporting-View Fallback ──────────────────────────────────────────────────

# v4.4.8: reporting.v_* Views werden NICHT mehr verwendet.
# Die Views machten UNION ALL aus dbo.* + demo.* — aber demo.* Tabellen
# existieren in Azure SQL nicht mehr (Demo-Daten liegen seit v4.4.0 auf
# lokaler SQLite und werden in Python gemerged).
# _V() gibt jetzt immer dbo.{table} zurück.


def _V(table: str) -> str:
    """Gibt den voll qualifizierten Tabellennamen zurück (immer dbo.{table})."""
    return f"dbo.{table}"


def invalidate_view_cache() -> None:
    """Kompatibilitäts-Stub (Views werden nicht mehr verwendet)."""
    pass


# ─── Hilfsfunktionen ──────────────────────────────────────────────────────────

def _fmt_date(d) -> date:
    """
    Konvertiert date/datetime/str in ein Python-date-Objekt.
    pyodbc übergibt date-Objekte als native ODBC DATE — kein FreeTDS varchar-Cast-Problem.
    """
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    s = str(d).strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        try:
            return datetime.strptime(s, "%d.%m.%Y").date()
        except ValueError:
            raise ValueError(f"Ungültiges Datumsformat: {s!r} — erwartet YYYY-MM-DD")


def _cost_columns(cost_per_sheet: float, cost_per_mono: float, cost_per_color: float) -> str:
    """
    Generiert SQL-Ausdrücke für die Kostenberechnung.
    Bildet die PowerBI DAX-Formeln in T-SQL nach.
    """
    return f"""
        -- Sheet count: ROUNDUP(page_count/2) bei Duplex, sonst page_count
        CASE WHEN td.duplex = 1
             THEN CEILING(CAST(td.page_count AS FLOAT) / 2)
             ELSE td.page_count
        END AS sheet_count,

        -- Toner Farbe
        CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END AS toner_cost_color,

        -- Toner S/W
        CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono} ELSE 0 END AS toner_cost_bw,

        -- Sheet-Kosten
        CASE WHEN td.duplex = 1
             THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
             ELSE td.page_count * {cost_per_sheet}
        END AS sheet_cost,

        -- Gesamtkosten
        (CASE WHEN td.duplex = 1
              THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
              ELSE td.page_count * {cost_per_sheet}
         END)
        + (CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END)
        + (CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono} ELSE 0 END)
        AS total_cost
    """


# ─── 1. Druckvolumen-Statistik ────────────────────────────────────────────────

def query_print_stats(
    start_date: str,
    end_date: str,
    group_by: str = "day",        # day | week | month | user | printer | site
    site_id: Optional[str] = None,
    user_email: Optional[str] = None,
    printer_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Druckvolumen nach Zeitraum, User, Drucker oder Standort.

    group_by-Optionen:
      day     — Tagesweise Aggregation
      week    — Wochenweise
      month   — Monatsweise
      user    — Nach Benutzer
      printer — Nach Drucker
      site    — Nach Netzwerk/Standort
    """
    tenant_id = get_tenant_id()

    # Bei Gruppierung nach Benutzer verwenden wir COALESCE(u.name, u.email),
    # damit die Reports den lesbaren Anzeigenamen ('Hans Müller') zeigen
    # statt nur den abgeschnittenen E-Mail-Local-Part ('hans.mu').
    group_expr = {
        "day":     "CAST(td.print_time AS DATE)",
        "week":    "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE))",
        "month":   "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)",
        "user":    "COALESCE(u.name, u.email)",
        "printer": "p.name",
        "site":    "n.name",
    }.get(group_by, "CAST(td.print_time AS DATE)")

    label_col = {
        "day":     "CAST(td.print_time AS DATE) AS period",
        "week":    "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE)) AS period",
        "month":   "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period",
        "user":    "COALESCE(u.name, u.email) AS period",
        "printer": "p.name AS period",
        "site":    "n.name AS period",
    }.get(group_by, "CAST(td.print_time AS DATE) AS period")

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)
    if user_email:
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)
    if printer_id:
        where_extra += " AND td.printer_id = ?"
        params_extra.append(printer_id)

    sql = f"""
        SELECT
            {label_col},
            COUNT(DISTINCT td.job_id)              AS total_jobs,
            SUM(td.page_count)                     AS total_pages,
            SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)  AS color_pages,
            SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)  AS bw_pages,
            SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END) AS duplex_pages,
            CAST(SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))      AS color_pct,
            CAST(SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))      AS duplex_pct,
            -- Eingespartes Papier durch Duplex
            SUM(CASE WHEN td.duplex = 1
                     THEN td.page_count - CEILING(CAST(td.page_count AS FLOAT)/2)
                     ELSE 0 END)                   AS saved_sheets_duplex
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('jobs')}     j ON j.id = td.job_id AND j.tenant_id = td.tenant_id
        LEFT JOIN {_V('users')}    u ON u.id = j.tenant_user_id AND u.tenant_id = td.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id AND n.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY {group_expr}
        ORDER BY {group_expr}
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (print_stats), using demo-only: %s", e)
        sql_results = []

    # v4.4.0: Demo-Daten aus lokaler SQLite mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            demo_agg = _aggregate_demo_print_stats(
                demo_rows, group_by, site_id=site_id,
                user_email=user_email, printer_id=printer_id)
            sql_results = _merge_aggregated(sql_results, demo_agg, "period")

    return sql_results


# ─── 2. Kostenaufstellung ─────────────────────────────────────────────────────

def query_cost_report(
    start_date: str,
    end_date: str,
    cost_per_sheet: float = 0.01,
    cost_per_mono: float  = 0.02,
    cost_per_color: float = 0.08,
    group_by: str = "month",
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Kostenaufstellung mit Farb-/S&W-Aufschlüsselung.
    """
    tenant_id = get_tenant_id()

    group_expr = {
        "day":   "CAST(td.print_time AS DATE)",
        "week":  "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE))",
        "month": "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)",
        "site":  "n.name",
    }.get(group_by, "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)")

    label_col = {
        "day":   "CAST(td.print_time AS DATE) AS period",
        "week":  "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE)) AS period",
        "month": "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period",
        "site":  "n.name AS period",
    }.get(group_by, "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period")

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            {label_col},
            SUM(td.page_count)                                            AS total_pages,
            SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)    AS color_pages,
            SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)    AS bw_pages,
            SUM(CASE WHEN td.duplex = 1
                     THEN CEILING(CAST(td.page_count AS FLOAT) / 2)
                     ELSE td.page_count END)                              AS total_sheets,
            SUM(CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END)   AS toner_cost_color,
            SUM(CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono}  ELSE 0 END)   AS toner_cost_bw,
            SUM(CASE WHEN td.duplex = 1
                     THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
                     ELSE td.page_count * {cost_per_sheet} END)           AS sheet_cost,
            SUM(
                (CASE WHEN td.duplex = 1
                      THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
                      ELSE td.page_count * {cost_per_sheet} END)
                + (CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END)
                + (CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono}  ELSE 0 END)
            )                                                             AS total_cost
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id  AND n.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY {group_expr}
        ORDER BY {group_expr}
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (cost_report), using demo-only: %s", e)
        sql_results = []

    # v4.4.0: Demo-Daten aus lokaler SQLite mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            demo_agg = _aggregate_demo_cost_report(
                demo_rows, group_by, cost_per_sheet, cost_per_mono,
                cost_per_color, site_id=site_id)
            sql_results = _merge_aggregated(sql_results, demo_agg, "period")

    return sql_results


# ─── 3. Top User ──────────────────────────────────────────────────────────────

def query_top_users(
    start_date: str,
    end_date: str,
    top_n: int = 10,
    metric: str = "pages",
    cost_per_sheet: float = 0.01,
    cost_per_mono: float  = 0.02,
    cost_per_color: float = 0.08,
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Ranking der aktivsten Nutzer nach Volumen oder Kosten."""
    tenant_id = get_tenant_id()

    order_col = {
        "pages":       "total_pages DESC",
        "cost":        "total_cost DESC",
        "jobs":        "total_jobs DESC",
        "color_pages": "color_pages DESC",
    }.get(metric, "total_pages DESC")

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT TOP {int(top_n)}
            u.email,
            u.name,
            u.department,
            COUNT(DISTINCT td.job_id)                                           AS total_jobs,
            SUM(td.page_count)                                                  AS total_pages,
            SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)          AS color_pages,
            SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)          AS bw_pages,
            SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END)         AS duplex_pages,
            CAST(SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))              AS color_pct,
            SUM(
                (CASE WHEN td.duplex = 1
                      THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
                      ELSE td.page_count * {cost_per_sheet} END)
                + (CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END)
                + (CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono}  ELSE 0 END)
            )                                                                   AS total_cost
        FROM {_V('tracking_data')} td
        JOIN  {_V('jobs')}     j ON j.id = td.job_id       AND j.tenant_id = td.tenant_id
        JOIN  {_V('users')}    u ON u.id = j.tenant_user_id AND u.tenant_id = td.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY u.email, u.name, u.department
        ORDER BY {order_col}
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (top_users), using demo-only: %s", e)
        sql_results = []

    # v4.4.0: Demo-Daten aus lokaler SQLite mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            demo_agg = _aggregate_demo_top_users(
                demo_rows, top_n, metric, cost_per_sheet,
                cost_per_mono, cost_per_color, site_id=site_id)
            # Für Top-User: Demo-User zur Liste hinzufügen, re-sortieren, top_n
            combined = list(sql_results) + demo_agg
            sort_key = {"pages": "total_pages", "cost": "total_cost",
                        "jobs": "total_jobs", "color_pages": "color_pages"}.get(metric, "total_pages")
            combined.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
            sql_results = combined[:top_n]

    return sql_results


# ─── 4. Top Drucker ───────────────────────────────────────────────────────────

def query_top_printers(
    start_date: str,
    end_date: str,
    top_n: int = 10,
    metric: str = "pages",
    cost_per_sheet: float = 0.01,
    cost_per_mono: float  = 0.02,
    cost_per_color: float = 0.08,
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Ranking der meistgenutzten Drucker nach Volumen oder Kosten."""
    tenant_id = get_tenant_id()

    order_col = {
        "pages":       "total_pages DESC",
        "cost":        "total_cost DESC",
        "jobs":        "total_jobs DESC",
        "color_pages": "color_pages DESC",
    }.get(metric, "total_pages DESC")

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT TOP {int(top_n)}
            p.name                                                              AS printer_name,
            p.model_name,
            p.vendor_name,
            p.location,
            n.name                                                              AS site_name,
            COUNT(DISTINCT td.job_id)                                           AS total_jobs,
            SUM(td.page_count)                                                  AS total_pages,
            SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)          AS color_pages,
            SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)          AS bw_pages,
            CAST(SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))              AS color_pct,
            SUM(
                (CASE WHEN td.duplex = 1
                      THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
                      ELSE td.page_count * {cost_per_sheet} END)
                + (CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END)
                + (CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono}  ELSE 0 END)
            )                                                                   AS total_cost
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id  AND n.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY p.name, p.model_name, p.vendor_name, p.location, n.name
        ORDER BY {order_col}
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (top_printers), using demo-only: %s", e)
        sql_results = []

    # v4.4.0: Demo-Daten aus lokaler SQLite mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            demo_agg = _aggregate_demo_top_printers(
                demo_rows, top_n, metric, cost_per_sheet,
                cost_per_mono, cost_per_color, site_id=site_id)
            combined = list(sql_results) + demo_agg
            sort_key = {"pages": "total_pages", "cost": "total_cost",
                        "jobs": "total_jobs", "color_pages": "color_pages"}.get(metric, "total_pages")
            combined.sort(key=lambda x: x.get(sort_key, 0), reverse=True)
            sql_results = combined[:top_n]

    return sql_results


# ─── 5. Anomalie-Erkennung ────────────────────────────────────────────────────

def query_anomalies(
    start_date: str,
    end_date: str,
    threshold_multiplier: float = 2.5,
) -> list[dict[str, Any]]:
    """Erkennt Ausreißer: Tage mit ungewöhnlich hohem Druckvolumen."""
    tenant_id = get_tenant_id()

    sql = f"""
        WITH daily AS (
            SELECT
                CAST(print_time AS DATE)   AS print_day,
                SUM(page_count)            AS daily_pages,
                COUNT(DISTINCT job_id)     AS daily_jobs
            FROM {_V('tracking_data')}
            WHERE tenant_id = ?
              AND print_time >= ?
              AND print_time <  DATEADD(day, 1, CAST(? AS DATE))
              AND print_job_status = 'PRINT_OK'
            GROUP BY CAST(print_time AS DATE)
        ),
        stats AS (
            SELECT
                AVG(CAST(daily_pages AS FLOAT))   AS avg_pages,
                STDEV(CAST(daily_pages AS FLOAT)) AS std_pages
            FROM daily
        )
        SELECT
            d.print_day,
            d.daily_pages,
            d.daily_jobs,
            ROUND(s.avg_pages, 0)              AS avg_pages,
            ROUND(s.std_pages, 0)              AS std_pages,
            ROUND(s.avg_pages + {threshold_multiplier} * s.std_pages, 0) AS threshold,
            ROUND((d.daily_pages - s.avg_pages) / NULLIF(s.std_pages, 0), 2) AS z_score,
            CASE WHEN d.daily_pages > s.avg_pages + {threshold_multiplier} * s.std_pages
                 THEN 'ANOMALIE_HOCH'
                 WHEN d.daily_pages < GREATEST(0, s.avg_pages - {threshold_multiplier} * s.std_pages)
                 THEN 'ANOMALIE_NIEDRIG'
                 ELSE 'NORMAL'
            END                                AS status
        FROM daily d
        CROSS JOIN stats s
        WHERE d.daily_pages > s.avg_pages + {threshold_multiplier} * s.std_pages
           OR d.daily_pages < GREATEST(0, s.avg_pages - {threshold_multiplier} * s.std_pages)
        ORDER BY ABS(d.daily_pages - s.avg_pages) DESC
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date))
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (anomalies), using demo-only: %s", e)
        sql_results = []

    # v4.4.15: Demo-Daten mergen — Anomalien muessen auf kombinierten
    # SQL+Demo Tageswerten komplett neu berechnet werden (z-Scores).
    # Die SQL-CTE liefert nur Anomalie-Tage, nicht alle. Daher:
    # 1) Separate SQL-Abfrage fuer ALLE Tages-Totals
    # 2) Demo-Tages-Totals aggregieren
    # 3) Kombinieren (key-based merge auf Tag)
    # 4) Statistik + z-Scores komplett neu berechnen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            # Step 1: Alle SQL-Tageswerte holen (nicht nur Anomalien)
            sql_daily: dict[str, dict] = {}
            try:
                daily_sql = f"""
                    SELECT
                        CAST(print_time AS DATE) AS print_day,
                        SUM(page_count) AS daily_pages,
                        COUNT(DISTINCT job_id) AS daily_jobs
                    FROM {_V('tracking_data')}
                    WHERE tenant_id = ?
                      AND print_time >= ?
                      AND print_time < DATEADD(day, 1, CAST(? AS DATE))
                      AND print_job_status = 'PRINT_OK'
                    GROUP BY CAST(print_time AS DATE)
                """
                sql_daily_rows = query_fetchall(daily_sql, params)
                for r in sql_daily_rows:
                    d = str(r.get("print_day", ""))[:10]
                    sql_daily[d] = {
                        "pages": int(r.get("daily_pages") or 0),
                        "jobs": int(r.get("daily_jobs") or 0),
                    }
            except Exception:
                pass  # SQL nicht verfuegbar — nur Demo-Daten

            # Step 2: Demo-Tageswerte aggregieren
            daily_demo: dict[str, dict] = {}
            for r in demo_rows:
                d = str(_parse_demo_date(str(r.get("print_time", ""))))
                if d not in daily_demo:
                    daily_demo[d] = {"pages": 0, "job_ids": set()}
                daily_demo[d]["pages"] += int(r.get("page_count") or 0)
                daily_demo[d]["job_ids"].add(r.get("job_id", ""))

            # Step 3: Key-based merge auf Tages-Ebene
            daily_all: dict[str, dict] = {}
            for d, v in sql_daily.items():
                daily_all[d] = {"pages": v["pages"], "jobs": v["jobs"]}
            for d, v in daily_demo.items():
                if d in daily_all:
                    daily_all[d]["pages"] += v["pages"]
                    daily_all[d]["jobs"] += len(v["job_ids"])
                else:
                    daily_all[d] = {"pages": v["pages"], "jobs": len(v["job_ids"])}

            # Step 4: Statistik + z-Scores komplett neu berechnen
            if daily_all:
                pages_list = [v["pages"] for v in daily_all.values()]
                n = len(pages_list)
                if n > 1:
                    avg_p = sum(pages_list) / n
                    std_p = (sum((x - avg_p) ** 2 for x in pages_list) / (n - 1)) ** 0.5
                    threshold = avg_p + threshold_multiplier * std_p
                    low_threshold = max(0, avg_p - threshold_multiplier * std_p)

                    combined_anomalies = []
                    for day, v in sorted(daily_all.items()):
                        z = round((v["pages"] - avg_p) / std_p, 2) if std_p else 0
                        if v["pages"] > threshold:
                            status = "ANOMALIE_HOCH"
                        elif v["pages"] < low_threshold:
                            status = "ANOMALIE_NIEDRIG"
                        else:
                            continue  # normal — skip
                        combined_anomalies.append({
                            "print_day": day, "daily_pages": v["pages"],
                            "daily_jobs": v["jobs"],
                            "avg_pages": round(avg_p), "std_pages": round(std_p),
                            "threshold": round(threshold), "z_score": z,
                            "status": status,
                        })
                    # Ersetze SQL-Ergebnis komplett — die neuen z-Scores basieren
                    # auf kombinierten Daten und sind daher korrekter
                    sql_results = combined_anomalies
                    sql_results.sort(key=lambda x: abs(x.get("daily_pages", 0) - x.get("avg_pages", 0)), reverse=True)

    return sql_results


# ─── 6. Trend-Vergleich ───────────────────────────────────────────────────────

def query_trend(
    period1_start: str,
    period1_end: str,
    period2_start: str,
    period2_end: str,
    cost_per_sheet: float = 0.01,
    cost_per_mono: float  = 0.02,
    cost_per_color: float = 0.08,
) -> dict[str, Any]:
    """Vergleich zweier Zeiträume."""
    tenant_id = get_tenant_id()

    def _period_sql(start: str, end: str) -> tuple[str, tuple]:
        sql = f"""
            SELECT
                COUNT(DISTINCT td.job_id)                                       AS total_jobs,
                SUM(td.page_count)                                              AS total_pages,
                SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)      AS color_pages,
                SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)      AS bw_pages,
                SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END)     AS duplex_pages,
                COUNT(DISTINCT j.tenant_user_id)                                AS active_users,
                COUNT(DISTINCT td.printer_id)                                   AS active_printers,
                SUM(
                    (CASE WHEN td.duplex = 1
                          THEN CEILING(CAST(td.page_count AS FLOAT) / 2) * {cost_per_sheet}
                          ELSE td.page_count * {cost_per_sheet} END)
                    + (CASE WHEN td.color = 1 THEN td.page_count * {cost_per_color} ELSE 0 END)
                    + (CASE WHEN td.color = 0 THEN td.page_count * {cost_per_mono}  ELSE 0 END)
                )                                                               AS total_cost
            FROM {_V('tracking_data')} td
            JOIN {_V('jobs')} j ON j.id = td.job_id AND j.tenant_id = td.tenant_id
            WHERE td.tenant_id = ?
              AND td.print_time >= ?
              AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
              AND td.print_job_status = 'PRINT_OK'
        """
        return sql, (tenant_id, _fmt_date(start), _fmt_date(end))

    sql1, params1 = _period_sql(period1_start, period1_end)
    sql2, params2 = _period_sql(period2_start, period2_end)

    from .sql_client import query_fetchone
    try:
        p1 = query_fetchone(sql1, params1) or {}
    except Exception as e:
        logger.warning("SQL query failed (trend p1): %s", e)
        p1 = {}
    try:
        p2 = query_fetchone(sql2, params2) or {}
    except Exception as e:
        logger.warning("SQL query failed (trend p2): %s", e)
        p2 = {}

    # v4.4.15: Demo-Daten mergen — distinct sets fuer active_users/printers
    def _get_sql_distinct_ids(start, end):
        """Holt distinct user_ids und printer_ids aus SQL fuer korrekte Merge."""
        try:
            sql_u = f"""
                SELECT DISTINCT j.tenant_user_id
                FROM {_V('tracking_data')} td
                JOIN {_V('jobs')} j ON j.id = td.job_id AND j.tenant_id = td.tenant_id
                WHERE td.tenant_id = ?
                  AND td.print_time >= ? AND td.print_time < DATEADD(day, 1, CAST(? AS DATE))
                  AND td.print_job_status = 'PRINT_OK'
            """
            sql_p = f"""
                SELECT DISTINCT td.printer_id
                FROM {_V('tracking_data')} td
                WHERE td.tenant_id = ?
                  AND td.print_time >= ? AND td.print_time < DATEADD(day, 1, CAST(? AS DATE))
                  AND td.print_job_status = 'PRINT_OK'
            """
            p = (tenant_id, _fmt_date(start), _fmt_date(end))
            u_rows = query_fetchall(sql_u, p)
            p_rows = query_fetchall(sql_p, p)
            sql_users = {str(r.get("tenant_user_id", "")) for r in u_rows if r.get("tenant_user_id")}
            sql_printers = {str(r.get("printer_id", "")) for r in p_rows if r.get("printer_id")}
            return sql_users, sql_printers
        except Exception:
            return set(), set()

    def _merge_trend_period(sql_row, start, end):
        if not _has_active_demo():
            return sql_row
        demo_rows = _get_demo_rows(start, end)
        if not demo_rows:
            return sql_row
        job_ids = set()
        demo_user_ids = set()
        demo_printer_ids = set()
        tp = cp = bp = dp = 0
        tc = 0.0
        for r in demo_rows:
            job_ids.add(r.get("job_id", ""))
            demo_user_ids.add(r.get("user_email", ""))
            demo_printer_ids.add(r.get("printer_id", ""))
            pc = int(r.get("page_count") or 0)
            is_color = bool(int(r.get("color") or 0))
            is_duplex = bool(int(r.get("duplex") or 0))
            tp += pc
            if is_color: cp += pc
            else: bp += pc
            if is_duplex: dp += pc
            sheets = math.ceil(pc / 2) if is_duplex else pc
            tc += sheets * cost_per_sheet + pc * (cost_per_color if is_color else cost_per_mono)
        # Distinct union fuer active_users/printers
        sql_users, sql_printers = _get_sql_distinct_ids(start, end)
        all_users = sql_users | demo_user_ids
        all_printers = sql_printers | demo_printer_ids
        merged = dict(sql_row)
        merged["total_jobs"] = (merged.get("total_jobs") or 0) + len(job_ids)
        merged["total_pages"] = (merged.get("total_pages") or 0) + tp
        merged["color_pages"] = (merged.get("color_pages") or 0) + cp
        merged["bw_pages"] = (merged.get("bw_pages") or 0) + bp
        merged["duplex_pages"] = (merged.get("duplex_pages") or 0) + dp
        merged["active_users"] = len(all_users) if all_users else (merged.get("active_users") or 0) + len(demo_user_ids)
        merged["active_printers"] = len(all_printers) if all_printers else (merged.get("active_printers") or 0) + len(demo_printer_ids)
        merged["total_cost"] = round((merged.get("total_cost") or 0) + tc, 2)
        return merged

    p1 = _merge_trend_period(p1, period1_start, period1_end)
    p2 = _merge_trend_period(p2, period2_start, period2_end)

    def _delta_pct(new_val, old_val):
        if not old_val:
            return None
        return round((new_val - old_val) / old_val * 100, 1)

    return {
        "period1": {"start": period1_start, "end": period1_end, **p1},
        "period2": {"start": period2_start, "end": period2_end, **p2},
        "delta": {
            "total_pages":    _delta_pct(p2.get("total_pages", 0), p1.get("total_pages", 0)),
            "color_pages":    _delta_pct(p2.get("color_pages", 0), p1.get("color_pages", 0)),
            "total_cost":     _delta_pct(p2.get("total_cost", 0),  p1.get("total_cost", 0)),
            "active_users":   _delta_pct(p2.get("active_users", 0), p1.get("active_users", 0)),
            "total_jobs":     _delta_pct(p2.get("total_jobs", 0),  p1.get("total_jobs", 0)),
        },
    }


# ─── 7. Drucker-Historie ──────────────────────────────────────────────────────

def query_printer_history(
    start_date: str,
    end_date: str,
    printer_id: Optional[str] = None,
    group_by: str = "month",
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Per-Drucker Druckvolumen über Zeit.
    printer_id: wenn gesetzt, nur dieser eine Drucker (UUID-String).
    group_by: day | week | month
    """
    tenant_id = get_tenant_id()

    group_expr = {
        "day":   "CAST(td.print_time AS DATE)",
        "week":  "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE))",
        "month": "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)",
    }.get(group_by, "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)")

    label_col = {
        "day":   "CAST(td.print_time AS DATE) AS period",
        "week":  "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE)) AS period",
        "month": "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period",
    }.get(group_by, "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period")

    where_extra = ""
    params_extra: list = []
    if printer_id:
        where_extra += " AND td.printer_id = ?"
        params_extra.append(printer_id)
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            {label_col},
            p.name                                                              AS printer_name,
            p.model_name,
            n.name                                                              AS site_name,
            COUNT(DISTINCT td.job_id)                                           AS total_jobs,
            SUM(td.page_count)                                                  AS total_pages,
            SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)          AS color_pages,
            SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)          AS bw_pages,
            SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END)         AS duplex_pages,
            CAST(SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))              AS duplex_pct
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id  AND n.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY {group_expr}, p.name, p.model_name, n.name
        ORDER BY {group_expr}, total_pages DESC
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (printer_history), using demo-only: %s", e)
        sql_results = []

    # v4.4.15: Demo-Daten key-based merge (period + printer_name)
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if printer_id:
                filtered = [r for r in filtered if r.get("printer_id") == printer_id]
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if filtered:
                demo_groups = defaultdict(list)
                for r in filtered:
                    d = _parse_demo_date(str(r.get("print_time", "")))
                    pk = str(d) if group_by == "day" else str(_demo_week_start(d)) if group_by == "week" else str(_demo_month_start(d))
                    pn = r.get("printer_name", "Unknown")
                    demo_groups[(pk, pn)].append(r)
                # Build index of existing SQL rows by (period, printer_name)
                sql_index: dict[tuple, dict] = {}
                for r in sql_results:
                    k = (str(r.get("period", ""))[:10], r.get("printer_name", ""))
                    sql_index[k] = r
                for (period, pn), rows in demo_groups.items():
                    job_ids = set()
                    tp = cp = bp = dp = 0
                    model = site = ""
                    for r in rows:
                        job_ids.add(r.get("job_id", ""))
                        pc = int(r.get("page_count") or 0)
                        tp += pc
                        if bool(int(r.get("color") or 0)): cp += pc
                        else: bp += pc
                        if bool(int(r.get("duplex") or 0)): dp += pc
                        model = model or r.get("model_name", "")
                        site = site or r.get("network_name", "")
                    key = (period, pn)
                    if key in sql_index:
                        e = sql_index[key]
                        e["total_jobs"] = (e.get("total_jobs") or 0) + len(job_ids)
                        e["total_pages"] = (e.get("total_pages") or 0) + tp
                        e["color_pages"] = (e.get("color_pages") or 0) + cp
                        e["bw_pages"] = (e.get("bw_pages") or 0) + bp
                        e["duplex_pages"] = (e.get("duplex_pages") or 0) + dp
                        mtp = e.get("total_pages", 0)
                        e["duplex_pct"] = round(e.get("duplex_pages", 0) * 100.0 / mtp, 1) if mtp else 0
                    else:
                        sql_results.append({
                            "period": period, "printer_name": pn,
                            "model_name": model, "site_name": site,
                            "total_jobs": len(job_ids), "total_pages": tp,
                            "color_pages": cp, "bw_pages": bp, "duplex_pages": dp,
                            "duplex_pct": round(dp * 100.0 / tp, 1) if tp else 0,
                        })

    return sql_results


# ─── 8. Gerätewerte / Device Overview ────────────────────────────────────────

def query_device_readings(
    start_date: str,
    end_date: str,
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Drucker-Übersicht: Auslastung + letzte Aktivität pro Gerät.
    Toner-Füllstände sind nicht in der SQL-Datenbank.
    Gibt alle Drucker zurück, auch inaktive (total_pages = 0).
    """
    tenant_id = get_tenant_id()

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            p.id                                                                  AS printer_id,
            p.name                                                                AS printer_name,
            p.model_name,
            p.vendor_name,
            p.location,
            n.name                                                                AS site_name,
            COUNT(DISTINCT td.job_id)                                             AS total_jobs,
            ISNULL(SUM(td.page_count), 0)                                         AS total_pages,
            ISNULL(SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END), 0)  AS color_pages,
            ISNULL(SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END), 0)  AS bw_pages,
            MAX(td.print_time)                                                    AS last_activity,
            COUNT(DISTINCT CAST(td.print_time AS DATE))                           AS active_days
        FROM {_V('printers')} p
        LEFT JOIN {_V('networks')} n
               ON n.id = p.network_id AND n.tenant_id = p.tenant_id
        LEFT JOIN {_V('tracking_data')} td
               ON td.printer_id = p.id
              AND td.tenant_id  = p.tenant_id
              AND td.print_time >= ?
              AND td.print_time  < DATEADD(day, 1, CAST(? AS DATE))
              AND td.print_job_status = 'PRINT_OK'
        WHERE p.tenant_id = ?
          {where_extra}
        GROUP BY p.id, p.name, p.model_name, p.vendor_name, p.location, n.name
        ORDER BY ISNULL(SUM(td.page_count), 0) DESC
    """
    params = (_fmt_date(start_date), _fmt_date(end_date), tenant_id) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (device_readings), using demo-only: %s", e)
        sql_results = []

    # v4.4.14: Demo-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if filtered:
                printers: dict[str, dict] = {}
                for r in filtered:
                    pid = r.get("printer_id") or "unknown"
                    if pid not in printers:
                        printers[pid] = {
                            "printer_id": pid, "printer_name": r.get("printer_name", "Unknown"),
                            "model_name": r.get("model_name", ""), "vendor_name": r.get("vendor_name", ""),
                            "location": r.get("location", ""), "site_name": r.get("network_name", ""),
                            "job_ids": set(), "total_pages": 0, "color_pages": 0, "bw_pages": 0,
                            "last_activity": "", "active_days": set(),
                        }
                    p = printers[pid]
                    p["job_ids"].add(r.get("job_id", ""))
                    pc = int(r.get("page_count") or 0)
                    p["total_pages"] += pc
                    if bool(int(r.get("color") or 0)): p["color_pages"] += pc
                    else: p["bw_pages"] += pc
                    pt = str(r.get("print_time", ""))
                    if pt > p["last_activity"]: p["last_activity"] = pt
                    p["active_days"].add(pt[:10])

                # Merge: add demo printers or augment existing
                existing = {str(r.get("printer_id", "")): r for r in sql_results}
                for pid, p in printers.items():
                    if pid in existing:
                        e = existing[pid]
                        e["total_jobs"] = (e.get("total_jobs") or 0) + len(p["job_ids"])
                        e["total_pages"] = (e.get("total_pages") or 0) + p["total_pages"]
                        e["color_pages"] = (e.get("color_pages") or 0) + p["color_pages"]
                        e["bw_pages"] = (e.get("bw_pages") or 0) + p["bw_pages"]
                        e["active_days"] = (e.get("active_days") or 0) + len(p["active_days"])
                    else:
                        sql_results.append({
                            "printer_id": pid, "printer_name": p["printer_name"],
                            "model_name": p["model_name"], "vendor_name": p["vendor_name"],
                            "location": p["location"], "site_name": p["site_name"],
                            "total_jobs": len(p["job_ids"]), "total_pages": p["total_pages"],
                            "color_pages": p["color_pages"], "bw_pages": p["bw_pages"],
                            "last_activity": p["last_activity"], "active_days": len(p["active_days"]),
                        })

    return sql_results


# ─── 9. Job-Verlauf (paginiert) ───────────────────────────────────────────────

def query_job_history(
    start_date: str,
    end_date: str,
    page: int = 0,
    page_size: int = 100,
    site_id: Optional[str] = None,
    user_email: Optional[str] = None,
    printer_id: Optional[str] = None,
    status_filter: str = "ok",   # ok | failed | all
) -> list[dict[str, Any]]:
    """
    Rohe Job-Liste mit Paginierung (OFFSET/FETCH NEXT).
    status_filter: 'ok' = nur PRINT_OK, 'failed' = Fehler, 'all' = alles
    """
    tenant_id = get_tenant_id()

    status_clause = ""
    if status_filter == "ok":
        status_clause = "AND td.print_job_status = 'PRINT_OK'"
    elif status_filter == "failed":
        status_clause = "AND td.print_job_status <> 'PRINT_OK'"

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)
    if user_email:
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)
    if printer_id:
        where_extra += " AND td.printer_id = ?"
        params_extra.append(printer_id)

    offset = max(0, int(page)) * max(1, int(page_size))
    fetch  = max(1, min(int(page_size), 1000))

    sql = f"""
        SELECT
            td.job_id,
            td.print_time,
            td.print_job_status                                                 AS status,
            u.email                                                             AS user_email,
            u.name                                                              AS user_name,
            p.name                                                              AS printer_name,
            n.name                                                              AS site_name,
            td.page_count,
            td.color,
            td.duplex,
            j.paper_size
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('jobs')}     j ON j.id = td.job_id          AND j.tenant_id = td.tenant_id
        LEFT JOIN {_V('users')}    u ON u.id = j.tenant_user_id   AND u.tenant_id = td.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id      AND p.tenant_id = td.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id       AND n.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          {status_clause}
          {where_extra}
        ORDER BY td.print_time DESC
        OFFSET {offset} ROWS FETCH NEXT {fetch} ROWS ONLY
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (job_history), using demo-only: %s", e)
        sql_results = []

    # v4.4.14: Demo-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if user_email:
                filtered = [r for r in filtered if r.get("user_email") == user_email]
            if printer_id:
                filtered = [r for r in filtered if r.get("printer_id") == printer_id]
            if status_filter == "ok":
                filtered = [r for r in filtered if r.get("print_job_status") == "PRINT_OK"]
            elif status_filter == "failed":
                filtered = [r for r in filtered if r.get("print_job_status") != "PRINT_OK"]
            for r in filtered:
                sql_results.append({
                    "job_id": r.get("job_id", ""),
                    "print_time": r.get("print_time", ""),
                    "status": r.get("print_job_status", "PRINT_OK"),
                    "user_email": r.get("user_email", ""),
                    "user_name": r.get("user_name", ""),
                    "printer_name": r.get("printer_name", ""),
                    "site_name": r.get("network_name", ""),
                    "page_count": int(r.get("page_count") or 0),
                    "color": int(r.get("color") or 0),
                    "duplex": int(r.get("duplex") or 0),
                    "paper_size": r.get("paper_size", "A4"),
                })
            # Re-sort and paginate
            sql_results.sort(key=lambda x: x.get("print_time", ""), reverse=True)
            sql_results = sql_results[offset:offset + fetch]

    return sql_results


# ─── 10. Queue-Statistik ──────────────────────────────────────────────────────

def query_queue_stats(
    start_date: str,
    end_date: str,
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Druckauftrags-Verteilung nach Papierformat, Farbe und Duplex-Modus.
    Zeigt Zusammensetzung des Druckvolumens (Papier-Mix, Farbanteil).
    """
    tenant_id = get_tenant_id()

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            ISNULL(j.paper_size, 'UNKNOWN')                                     AS paper_size,
            td.color,
            td.duplex,
            COUNT(DISTINCT td.job_id)                                           AS total_jobs,
            SUM(td.page_count)                                                  AS total_pages,
            CAST(SUM(td.page_count) * 100.0
                 / NULLIF(SUM(SUM(td.page_count)) OVER (), 0) AS DECIMAL(5,1)) AS pct_of_total
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('jobs')}     j ON j.id = td.job_id     AND j.tenant_id = td.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY j.paper_size, td.color, td.duplex
        ORDER BY total_pages DESC
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (queue_stats), using demo-only: %s", e)
        sql_results = []

    # v4.4.15: Demo-Daten key-based merge (paper_size + color + duplex)
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if filtered:
                combos: dict[tuple, dict] = {}
                for r in filtered:
                    ps = r.get("paper_size", "A4") or "UNKNOWN"
                    c = int(r.get("color") or 0)
                    d = int(r.get("duplex") or 0)
                    key = (ps, c, d)
                    if key not in combos:
                        combos[key] = {"paper_size": ps, "color": c, "duplex": d,
                                       "job_ids": set(), "total_pages": 0}
                    combos[key]["job_ids"].add(r.get("job_id", ""))
                    combos[key]["total_pages"] += int(r.get("page_count") or 0)
                # Build index of existing SQL rows by (paper_size, color, duplex)
                sql_index: dict[tuple, dict] = {}
                for r in sql_results:
                    k = (r.get("paper_size", "UNKNOWN"), int(r.get("color") or 0), int(r.get("duplex") or 0))
                    sql_index[k] = r
                for key, combo in combos.items():
                    if key in sql_index:
                        e = sql_index[key]
                        e["total_jobs"] = (e.get("total_jobs") or 0) + len(combo["job_ids"])
                        e["total_pages"] = (e.get("total_pages") or 0) + combo["total_pages"]
                    else:
                        sql_results.append({
                            "paper_size": combo["paper_size"], "color": combo["color"],
                            "duplex": combo["duplex"], "total_jobs": len(combo["job_ids"]),
                            "total_pages": combo["total_pages"], "pct_of_total": 0,
                        })
                # Recalculate pct_of_total on merged data
                grand = sum(r.get("total_pages", 0) for r in sql_results)
                if grand:
                    for r in sql_results:
                        r["pct_of_total"] = round(r.get("total_pages", 0) * 100.0 / grand, 1)

    return sql_results


# ─── 11. User-Detail ──────────────────────────────────────────────────────────

def query_user_detail(
    start_date: str,
    end_date: str,
    user_email: str = "",
    group_by: str = "month",
) -> list[dict[str, Any]]:
    """
    Detaillierter Druckverlauf eines einzelnen Benutzers über Zeit.
    Inkl. Scan- und Kopier-Jobs.

    v6.2.1: user_email ist jetzt optional — ohne Filter werden alle User
    gruppiert zurückgegeben (nützlich für die Vorschau bei Presets, wo
    der Nutzer den konkreten User noch nicht gewählt hat).
    """
    tenant_id = get_tenant_id()
    user_email = (user_email or "").strip()

    group_expr = {
        "day":   "CAST(td.print_time AS DATE)",
        "week":  "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE))",
        "month": "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)",
    }.get(group_by, "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1)")

    label_col = {
        "day":   "CAST(td.print_time AS DATE) AS period",
        "week":  "DATEADD(day, -(DATEPART(weekday, td.print_time) - 1), CAST(td.print_time AS DATE)) AS period",
        "month": "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period",
    }.get(group_by, "DATEFROMPARTS(YEAR(td.print_time), MONTH(td.print_time), 1) AS period")

    # v6.2.1: Bei leerem user_email WHERE-Filter weglassen → liefert eine
    # aggregierte Vorschau über alle User.
    user_filter_clause = "AND u.email = ?" if user_email else ""
    sql = f"""
        SELECT
            {label_col},
            u.email,
            u.name,
            u.department,
            COUNT(DISTINCT td.job_id)                                           AS print_jobs,
            SUM(td.page_count)                                                  AS print_pages,
            SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END)          AS color_pages,
            SUM(CASE WHEN td.color = 0 THEN td.page_count ELSE 0 END)          AS bw_pages,
            SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END)         AS duplex_pages,
            CAST(SUM(CASE WHEN td.color = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))              AS color_pct
        FROM {_V('tracking_data')} td
        JOIN  {_V('jobs')}  j ON j.id = td.job_id        AND j.tenant_id = td.tenant_id
        JOIN  {_V('users')} u ON u.id = j.tenant_user_id AND u.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {user_filter_clause}
        GROUP BY {group_expr}, u.email, u.name, u.department
        ORDER BY {group_expr}
    """
    params: tuple = (tenant_id, _fmt_date(start_date), _fmt_date(end_date))
    if user_email:
        params = params + (user_email,)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (user_detail), using demo-only: %s", e)
        sql_results = []

    # v4.4.14: Demo-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            # Wenn kein user_email angegeben ist, alle Rows nehmen
            if user_email:
                filtered = [r for r in demo_rows if r.get("user_email") == user_email]
            else:
                filtered = list(demo_rows)
            if filtered:
                groups: dict[str, list[dict]] = defaultdict(list)
                for r in filtered:
                    d = _parse_demo_date(str(r.get("print_time", "")))
                    pk = str(d) if group_by == "day" else str(_demo_week_start(d)) if group_by == "week" else str(_demo_month_start(d))
                    groups[pk].append(r)
                demo_agg = []
                for period, rows in groups.items():
                    job_ids = set()
                    tp = cp = bp = dp = 0
                    name = dept = ""
                    for r in rows:
                        job_ids.add(r.get("job_id", ""))
                        pc = int(r.get("page_count") or 0)
                        tp += pc
                        if bool(int(r.get("color") or 0)): cp += pc
                        else: bp += pc
                        if bool(int(r.get("duplex") or 0)): dp += pc
                        name = name or r.get("user_name", "")
                        dept = dept or r.get("department", "")
                    demo_agg.append({
                        "period": period, "email": user_email, "name": name,
                        "department": dept, "print_jobs": len(job_ids),
                        "print_pages": tp, "color_pages": cp, "bw_pages": bp,
                        "duplex_pages": dp,
                        "color_pct": round(cp * 100.0 / tp, 1) if tp else 0,
                    })
                sql_results = _merge_aggregated(sql_results, demo_agg, "period")

    return sql_results


# ─── 12. Kopier-Jobs pro User ─────────────────────────────────────────────────

def query_user_copy_detail(
    start_date: str,
    end_date: str,
    user_email: Optional[str] = None,
    group_by: str = "month",
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Kopier-Jobs (jobs_copy + jobs_copy_details) pro Benutzer über Zeit.
    """
    tenant_id = get_tenant_id()

    group_expr = {
        "day":   "CAST(jc.copy_time AS DATE)",
        "week":  "DATEADD(day, -(DATEPART(weekday, jc.copy_time) - 1), CAST(jc.copy_time AS DATE))",
        "month": "DATEFROMPARTS(YEAR(jc.copy_time), MONTH(jc.copy_time), 1)",
    }.get(group_by, "DATEFROMPARTS(YEAR(jc.copy_time), MONTH(jc.copy_time), 1)")

    label_col = {
        "day":   "CAST(jc.copy_time AS DATE) AS period",
        "week":  "DATEADD(day, -(DATEPART(weekday, jc.copy_time) - 1), CAST(jc.copy_time AS DATE)) AS period",
        "month": "DATEFROMPARTS(YEAR(jc.copy_time), MONTH(jc.copy_time), 1) AS period",
    }.get(group_by, "DATEFROMPARTS(YEAR(jc.copy_time), MONTH(jc.copy_time), 1) AS period")

    where_extra = ""
    params_extra: list = []
    if user_email:
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            {label_col},
            u.email,
            u.name,
            p.name                                                              AS printer_name,
            n.name                                                              AS site_name,
            COUNT(DISTINCT jc.id)                                               AS total_copy_jobs,
            ISNULL(SUM(jcd.page_count), 0)                                      AS total_pages,
            ISNULL(SUM(CASE WHEN jcd.color = 1 THEN jcd.page_count ELSE 0 END), 0) AS color_pages,
            ISNULL(SUM(CASE WHEN jcd.color = 0 THEN jcd.page_count ELSE 0 END), 0) AS bw_pages,
            ISNULL(SUM(CASE WHEN jcd.duplex = 1 THEN jcd.page_count ELSE 0 END), 0) AS duplex_pages
        FROM {_V('jobs_copy')} jc
        LEFT JOIN {_V('jobs_copy_details')} jcd ON jcd.job_id = jc.id
        LEFT JOIN {_V('users')}    u ON u.id = jc.tenant_user_id AND u.tenant_id = jc.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = jc.printer_id     AND p.tenant_id = jc.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id      AND n.tenant_id = jc.tenant_id
        WHERE jc.tenant_id = ?
          AND jc.copy_time >= ?
          AND jc.copy_time <  DATEADD(day, 1, CAST(? AS DATE))
          {where_extra}
        GROUP BY {group_expr}, u.email, u.name, p.name, n.name
        ORDER BY {group_expr}, total_pages DESC
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (user_copy_detail), using demo-only: %s", e)
        sql_results = []

    # v4.4.14: Demo-Copy-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_copy_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if user_email:
                filtered = [r for r in filtered if r.get("user_email") == user_email]
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if filtered:
                groups: dict[tuple, list[dict]] = defaultdict(list)
                for r in filtered:
                    ct = str(r.get("copy_time", ""))
                    d = _parse_demo_date(ct)
                    pk = str(d) if group_by == "day" else str(_demo_week_start(d)) if group_by == "week" else str(_demo_month_start(d))
                    ue = r.get("user_email", "")
                    pn = r.get("printer_name", "")
                    groups[(pk, ue, pn)].append(r)
                for (period, ue, pn), rows in groups.items():
                    job_ids = set()
                    tp = cp = bp = dp = 0
                    name = site = ""
                    for r in rows:
                        job_ids.add(r.get("id", ""))
                        pc = int(r.get("page_count") or 0)
                        tp += pc
                        if bool(int(r.get("color") or 0)): cp += pc
                        else: bp += pc
                        if bool(int(r.get("duplex") or 0)): dp += pc
                        name = name or r.get("user_name", "")
                        site = site or r.get("network_name", "")
                    sql_results.append({
                        "period": period, "email": ue, "name": name,
                        "printer_name": pn, "site_name": site,
                        "total_copy_jobs": len(job_ids), "total_pages": tp,
                        "color_pages": cp, "bw_pages": bp, "duplex_pages": dp,
                    })

    return sql_results


# ─── 13. Scan-Jobs pro User ───────────────────────────────────────────────────

def query_user_scan_detail(
    start_date: str,
    end_date: str,
    user_email: Optional[str] = None,
    group_by: str = "month",
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Scan-Jobs (jobs_scan) pro Benutzer über Zeit.
    """
    tenant_id = get_tenant_id()

    group_expr = {
        "day":   "CAST(js.scan_time AS DATE)",
        "week":  "DATEADD(day, -(DATEPART(weekday, js.scan_time) - 1), CAST(js.scan_time AS DATE))",
        "month": "DATEFROMPARTS(YEAR(js.scan_time), MONTH(js.scan_time), 1)",
    }.get(group_by, "DATEFROMPARTS(YEAR(js.scan_time), MONTH(js.scan_time), 1)")

    label_col = {
        "day":   "CAST(js.scan_time AS DATE) AS period",
        "week":  "DATEADD(day, -(DATEPART(weekday, js.scan_time) - 1), CAST(js.scan_time AS DATE)) AS period",
        "month": "DATEFROMPARTS(YEAR(js.scan_time), MONTH(js.scan_time), 1) AS period",
    }.get(group_by, "DATEFROMPARTS(YEAR(js.scan_time), MONTH(js.scan_time), 1) AS period")

    where_extra = ""
    params_extra: list = []
    if user_email:
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            {label_col},
            u.email,
            u.name,
            p.name                                                              AS printer_name,
            n.name                                                              AS site_name,
            COUNT(DISTINCT js.id)                                               AS total_scan_jobs,
            SUM(js.page_count)                                                  AS total_pages,
            SUM(CASE WHEN js.color = 1 THEN js.page_count ELSE 0 END)          AS color_pages,
            SUM(CASE WHEN js.color = 0 THEN js.page_count ELSE 0 END)          AS bw_pages
        FROM {_V('jobs_scan')} js
        LEFT JOIN {_V('users')}    u ON u.id = js.tenant_user_id AND u.tenant_id = js.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = js.printer_id     AND p.tenant_id = js.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id      AND n.tenant_id = js.tenant_id
        WHERE js.tenant_id = ?
          AND js.scan_time >= ?
          AND js.scan_time <  DATEADD(day, 1, CAST(? AS DATE))
          {where_extra}
        GROUP BY {group_expr}, u.email, u.name, p.name, n.name
        ORDER BY {group_expr}, total_pages DESC
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (user_scan_detail), using demo-only: %s", e)
        sql_results = []

    # v4.4.14: Demo-Scan-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_scan_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if user_email:
                filtered = [r for r in filtered if r.get("user_email") == user_email]
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if filtered:
                groups: dict[tuple, list[dict]] = defaultdict(list)
                for r in filtered:
                    st = str(r.get("scan_time", ""))
                    d = _parse_demo_date(st)
                    pk = str(d) if group_by == "day" else str(_demo_week_start(d)) if group_by == "week" else str(_demo_month_start(d))
                    ue = r.get("user_email", "")
                    pn = r.get("printer_name", "")
                    groups[(pk, ue, pn)].append(r)
                for (period, ue, pn), rows in groups.items():
                    job_ids = set()
                    tp = cp = bp = 0
                    name = site = ""
                    for r in rows:
                        job_ids.add(r.get("id", ""))
                        pc = int(r.get("page_count") or 0)
                        tp += pc
                        if bool(int(r.get("color") or 0)): cp += pc
                        else: bp += pc
                        name = name or r.get("user_name", "")
                        site = site or r.get("network_name", "")
                    sql_results.append({
                        "period": period, "email": ue, "name": name,
                        "printer_name": pn, "site_name": site,
                        "total_scan_jobs": len(job_ids), "total_pages": tp,
                        "color_pages": cp, "bw_pages": bp,
                    })

    return sql_results


# ─── 14. Workstation-Übersicht ────────────────────────────────────────────────

def query_workstation_overview(
    start_date: str,
    end_date: str,
    site_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Workstation-Statistik. Erfordert dbo.workstations (optional in Printix BI Schema).
    Gibt leere Liste zurück wenn Tabelle nicht vorhanden.

    v4.6.11: Vollständig dynamische Spalten-Erkennung — fragt INFORMATION_SCHEMA.COLUMNS
    ab, um die tatsächlich vorhandenen Spalten der workstations-Tabelle zu ermitteln.
    Keine harten Spalten-Annahmen mehr (os_type, network_id, etc.).
    """
    tenant_id = get_tenant_id()
    from .sql_client import query_fetchone

    try:
        tbl_check = query_fetchone(
            "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA IN ('dbo','reporting') AND TABLE_NAME IN ('workstations','v_workstations')"
        )
        if not (tbl_check or {}).get("cnt"):
            if _has_active_demo():
                return [{"info": "Workstation-Daten sind in Demo-Daten nicht enthalten. "
                         "Dieser Report erfordert eine Azure SQL Datenbank mit dbo.workstations-Tabelle."}]
            return [{"info": "workstations-Tabelle nicht in diesem Schema vorhanden"}]
    except Exception:
        if _has_active_demo():
            return [{"info": "Workstation-Daten sind in Demo-Daten nicht enthalten. "
                     "Dieser Report erfordert eine Azure SQL Datenbank mit dbo.workstations-Tabelle."}]
        return [{"info": "workstations-Tabelle nicht in diesem Schema vorhanden"}]

    # v4.6.11: Dynamisch die vorhandenen Spalten der workstations-Tabelle ermitteln
    ws_cols: set[str] = set()
    try:
        col_rows = query_fetchall(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME IN ('workstations','v_workstations')"
        )
        ws_cols = {r.get("COLUMN_NAME", "").lower() for r in col_rows}
        logger.debug("workstations columns: %s", ws_cols)
    except Exception:
        pass

    # Verfügbare optionale Spalten für SELECT zusammenbauen
    select_extra = []
    group_extra = []
    if "os_type" in ws_cols:
        select_extra.append("w.os_type")
        group_extra.append("w.os_type")
    if "os_version" in ws_cols:
        select_extra.append("w.os_version")
        group_extra.append("w.os_version")
    if "last_seen" in ws_cols:
        select_extra.append("w.last_seen")
        group_extra.append("w.last_seen")
    if "hostname" in ws_cols:
        select_extra.append("w.hostname")
        group_extra.append("w.hostname")
    if "ip_address" in ws_cols:
        select_extra.append("w.ip_address")
        group_extra.append("w.ip_address")

    extra_select_sql = (", " + ", ".join(select_extra)) if select_extra else ""
    extra_group_sql = (", " + ", ".join(group_extra)) if group_extra else ""

    # Prüfe ob dbo.jobs eine workstation_id Spalte hat (für Job-Statistiken)
    has_ws_fk = False
    try:
        col_check = query_fetchone(
            "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'jobs' AND COLUMN_NAME = 'workstation_id'"
        )
        has_ws_fk = bool((col_check or {}).get("cnt"))
    except Exception:
        pass

    if has_ws_fk:
        where_extra = ""
        params_extra: list = []
        if site_id:
            where_extra += " AND p.network_id = ?"
            params_extra.append(site_id)

        sql = f"""
            SELECT
                w.id                     AS workstation_id,
                w.name                   AS workstation_name
                {extra_select_sql},
                n.name                   AS site_name,
                COUNT(DISTINCT j.id)     AS total_jobs,
                SUM(j.page_count)        AS total_pages
            FROM {_V('workstations')} w
            LEFT JOIN {_V('jobs')} j      ON j.workstation_id = w.id AND j.tenant_id = w.tenant_id
                                         AND j.submit_time >= ?
                                         AND j.submit_time < DATEADD(day, 1, CAST(? AS DATE))
            LEFT JOIN {_V('printers')} p  ON p.id = j.printer_id AND p.tenant_id = w.tenant_id
            LEFT JOIN {_V('networks')} n  ON n.id = p.network_id AND n.tenant_id = w.tenant_id
            WHERE w.tenant_id = ?
              {where_extra}
            GROUP BY w.id, w.name{extra_group_sql}, n.name
            ORDER BY total_pages DESC
        """
        params = (_fmt_date(start_date), _fmt_date(end_date), tenant_id) + tuple(params_extra)
    else:
        logger.info("dbo.jobs has no workstation_id column — listing workstations without job stats")
        sql = f"""
            SELECT
                w.id                     AS workstation_id,
                w.name                   AS workstation_name
                {extra_select_sql}
            FROM {_V('workstations')} w
            WHERE w.tenant_id = ?
            ORDER BY w.name
        """
        params = (tenant_id,)

    try:
        return query_fetchall(sql, params)
    except Exception as exc:
        return [{"error": str(exc)[:200]}]


# ─── 15. Workstation-Detail ───────────────────────────────────────────────────

def query_workstation_detail(
    start_date: str,
    end_date: str,
    workstation_id: str = "",
    group_by: str = "month",
) -> list[dict[str, Any]]:
    """
    Druckverlauf einer einzelnen Workstation über Zeit.
    Gibt leere Liste zurück wenn workstations-Tabelle nicht vorhanden.

    v4.6.10: workstation_id ist jetzt optional (Default "").
    Gibt Hinweis wenn nicht gesetzt oder wenn dbo.jobs kein workstation_id hat.
    """
    if not workstation_id:
        return [{"info": "Bitte eine Workstation-ID angeben. "
                 "Diese finden Sie im Report 'Workstation-Übersicht'."}]

    tenant_id = get_tenant_id()
    from .sql_client import query_fetchone

    try:
        tbl_check = query_fetchone(
            "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA IN ('dbo','reporting') AND TABLE_NAME IN ('workstations','v_workstations')"
        )
        if not (tbl_check or {}).get("cnt"):
            if _has_active_demo():
                return [{"info": "Workstation-Daten sind in Demo-Daten nicht enthalten. "
                         "Dieser Report erfordert eine Azure SQL Datenbank mit dbo.workstations-Tabelle."}]
            return [{"info": "workstations-Tabelle nicht in diesem Schema vorhanden"}]
    except Exception:
        if _has_active_demo():
            return [{"info": "Workstation-Daten sind in Demo-Daten nicht enthalten. "
                     "Dieser Report erfordert eine Azure SQL Datenbank mit dbo.workstations-Tabelle."}]
        return [{"info": "workstations-Tabelle nicht in diesem Schema vorhanden"}]

    # v4.6.10: Prüfe ob dbo.jobs eine workstation_id Spalte hat
    try:
        col_check = query_fetchone(
            "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = 'jobs' AND COLUMN_NAME = 'workstation_id'"
        )
        if not (col_check or {}).get("cnt"):
            return [{"info": "Workstation-Detail nicht verfügbar — dbo.jobs enthält keine "
                     "workstation_id Spalte in diesem Schema. "
                     "Bitte nutzen Sie den Report 'Workstation-Übersicht' für Stammdaten."}]
    except Exception:
        pass

    group_expr = {
        "day":   "CAST(j.submit_time AS DATE)",
        "week":  "DATEADD(day, -(DATEPART(weekday, j.submit_time) - 1), CAST(j.submit_time AS DATE))",
        "month": "DATEFROMPARTS(YEAR(j.submit_time), MONTH(j.submit_time), 1)",
    }.get(group_by, "DATEFROMPARTS(YEAR(j.submit_time), MONTH(j.submit_time), 1)")

    label_col = {
        "day":   "CAST(j.submit_time AS DATE) AS period",
        "week":  "DATEADD(day, -(DATEPART(weekday, j.submit_time) - 1), CAST(j.submit_time AS DATE)) AS period",
        "month": "DATEFROMPARTS(YEAR(j.submit_time), MONTH(j.submit_time), 1) AS period",
    }.get(group_by, "DATEFROMPARTS(YEAR(j.submit_time), MONTH(j.submit_time), 1) AS period")

    sql = f"""
        SELECT
            {label_col},
            COUNT(DISTINCT j.id)                                                AS total_jobs,
            SUM(j.page_count)                                                   AS total_pages,
            SUM(CASE WHEN j.color = 1 THEN j.page_count ELSE 0 END)            AS color_pages,
            SUM(CASE WHEN j.color = 0 THEN j.page_count ELSE 0 END)            AS bw_pages,
            SUM(CASE WHEN j.duplex = 1 THEN j.page_count ELSE 0 END)           AS duplex_pages
        FROM {_V('jobs')} j
        WHERE j.tenant_id = ?
          AND j.workstation_id = ?
          AND j.submit_time >= ?
          AND j.submit_time <  DATEADD(day, 1, CAST(? AS DATE))
        GROUP BY {group_expr}
        ORDER BY {group_expr}
    """
    params = (tenant_id, workstation_id, _fmt_date(start_date), _fmt_date(end_date))
    try:
        return query_fetchall(sql, params)
    except Exception as exc:
        return [{"error": str(exc)[:200]}]


# ─── 16. Tree-Meter / Nachhaltigkeit ──────────────────────────────────────────

def query_tree_meter(
    start_date: str,
    end_date: str,
    sheets_per_tree: int = 8333,
    site_id: Optional[str] = None,
) -> dict[str, Any]:
    """
    Nachhaltigkeits-Kennzahlen: eingesparte Blätter durch Duplex,
    umgerechnet in Bäume (Standard: 1 Baum = 8333 A4-Blätter).

    Formel:
      saved_sheets = page_count - CEILING(page_count/2) für Duplex-Jobs
      trees_saved  = saved_sheets / sheets_per_tree
    """
    tenant_id = get_tenant_id()

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)

    sql = f"""
        SELECT
            SUM(td.page_count)                                                  AS total_pages,
            SUM(CASE WHEN td.duplex = 1
                     THEN CEILING(CAST(td.page_count AS FLOAT) / 2)
                     ELSE td.page_count END)                                    AS total_sheets_used,
            SUM(CASE WHEN td.duplex = 1
                     THEN td.page_count - CEILING(CAST(td.page_count AS FLOAT) / 2)
                     ELSE 0 END)                                                AS saved_sheets_duplex,
            SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END)         AS duplex_pages,
            SUM(CASE WHEN td.duplex = 0 THEN td.page_count ELSE 0 END)         AS simplex_pages,
            CAST(SUM(CASE WHEN td.duplex = 1 THEN td.page_count ELSE 0 END) * 100.0
                 / NULLIF(SUM(td.page_count), 0) AS DECIMAL(5,1))              AS duplex_pct
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)

    from .sql_client import query_fetchone
    try:
        row = query_fetchone(sql, params) or {}
    except Exception as e:
        logger.warning("SQL query failed (tree_meter), using demo-only: %s", e)
        row = {}

    total_pages = int(row.get("total_pages") or 0)
    total_sheets = int(row.get("total_sheets_used") or 0)
    saved = int(row.get("saved_sheets_duplex") or 0)
    duplex_pages = int(row.get("duplex_pages") or 0)
    simplex_pages = int(row.get("simplex_pages") or 0)

    # v4.4.14: Demo-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            for r in filtered:
                pc = int(r.get("page_count") or 0)
                is_duplex = bool(int(r.get("duplex") or 0))
                total_pages += pc
                if is_duplex:
                    sheets = math.ceil(pc / 2)
                    total_sheets += sheets
                    saved += pc - sheets
                    duplex_pages += pc
                else:
                    total_sheets += pc
                    simplex_pages += pc

    trees = round(saved / sheets_per_tree, 4) if sheets_per_tree else 0
    duplex_pct = round(duplex_pages * 100.0 / total_pages, 1) if total_pages else 0

    return {
        "start_date":           start_date,
        "end_date":             end_date,
        "total_pages":          total_pages,
        "total_sheets_used":    total_sheets,
        "saved_sheets_duplex":  saved,
        "trees_saved":          trees,
        "duplex_pages":         duplex_pages,
        "simplex_pages":        simplex_pages,
        "duplex_pct":           duplex_pct,
        "sheets_per_tree":      sheets_per_tree,
    }


# ─── 17. Service Desk / Fehlgeschlagene Jobs ──────────────────────────────────

def query_service_desk(
    start_date: str,
    end_date: str,
    site_id: Optional[str] = None,
    user_email: Optional[str] = None,
    group_by: str = "status",   # status | day | printer | user
) -> list[dict[str, Any]]:
    """
    Fehlgeschlagene und abgebrochene Druckaufträge für Service-Desk-Analysen.
    group_by: 'status' — nach Fehlertyp aggregiert
              'day'    — zeitlicher Verlauf
              'printer'— nach Drucker
              'user'   — nach Benutzer
    """
    tenant_id = get_tenant_id()

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)
    if user_email:
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)

    select_group = {
        "status":  "td.print_job_status AS group_key, td.print_job_status",
        "day":     "CAST(td.print_time AS DATE) AS group_key, td.print_job_status",
        "printer": "p.name AS group_key, td.print_job_status",
        "user":    "u.email AS group_key, td.print_job_status",
    }.get(group_by, "td.print_job_status AS group_key, td.print_job_status")

    group_by_clause = {
        "status":  "td.print_job_status",
        "day":     "CAST(td.print_time AS DATE), td.print_job_status",
        "printer": "p.name, td.print_job_status",
        "user":    "u.email, td.print_job_status",
    }.get(group_by, "td.print_job_status")

    sql = f"""
        SELECT
            {select_group},
            COUNT(DISTINCT td.job_id)                                           AS total_jobs,
            SUM(td.page_count)                                                  AS total_pages,
            MAX(td.print_time)                                                  AS last_occurrence
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('jobs')}     j ON j.id = td.job_id          AND j.tenant_id = td.tenant_id
        LEFT JOIN {_V('users')}    u ON u.id = j.tenant_user_id   AND u.tenant_id = td.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id      AND p.tenant_id = td.tenant_id
        LEFT JOIN {_V('networks')} n ON n.id = p.network_id       AND n.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status <> 'PRINT_OK'
          {where_extra}
        GROUP BY {group_by_clause}
        ORDER BY total_jobs DESC
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (service_desk), using demo-only: %s", e)
        sql_results = []

    # v4.4.15: Demo-Daten key-based merge (group_key + status)
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            failed = [r for r in demo_rows if r.get("print_job_status") != "PRINT_OK"]
            if site_id:
                failed = [r for r in failed if r.get("network_id") == site_id]
            if user_email:
                failed = [r for r in failed if r.get("user_email") == user_email]
            if failed:
                demo_groups: dict[tuple, list[dict]] = defaultdict(list)
                for r in failed:
                    status = r.get("print_job_status", "UNKNOWN")
                    if group_by == "status":
                        gk = status
                    elif group_by == "day":
                        gk = str(_parse_demo_date(str(r.get("print_time", ""))))
                    elif group_by == "printer":
                        gk = r.get("printer_name", "Unknown")
                    elif group_by == "user":
                        gk = r.get("user_email", "Unknown")
                    else:
                        gk = status
                    demo_groups[(gk, status)].append(r)
                # Build index of existing SQL rows by (group_key, status)
                sql_index: dict[tuple, dict] = {}
                for r in sql_results:
                    k = (str(r.get("group_key", "")), str(r.get("print_job_status", "")))
                    sql_index[k] = r
                for (gk, status), rows in demo_groups.items():
                    job_ids = set()
                    tp = 0
                    last = ""
                    for r in rows:
                        job_ids.add(r.get("job_id", ""))
                        tp += int(r.get("page_count") or 0)
                        pt = str(r.get("print_time", ""))
                        if pt > last: last = pt
                    key = (gk, status)
                    if key in sql_index:
                        e = sql_index[key]
                        e["total_jobs"] = (e.get("total_jobs") or 0) + len(job_ids)
                        e["total_pages"] = (e.get("total_pages") or 0) + tp
                        el = str(e.get("last_occurrence", ""))
                        if last > el:
                            e["last_occurrence"] = last
                    else:
                        sql_results.append({
                            "group_key": gk, "print_job_status": status,
                            "total_jobs": len(job_ids), "total_pages": tp,
                            "last_occurrence": last,
                        })

    return sql_results


# ─── Universeller Dispatcher (v3.7.0) ────────────────────────────────────────

def _translate_trend_kwargs(kwargs: dict) -> dict:
    """v3.7.10: Wenn nur start_date/end_date geliefert werden (z.B. aus Preset-Templates),
    berechne period1 = [start_date..end_date] und period2 = vorangehendes gleich langes Fenster.
    Akzeptiert auch explizit gesetzte period1_*/period2_* (durchreichen unverändert)."""
    if "period1_start" in kwargs and "period2_start" in kwargs:
        # Alle 4 period-Parameter vorhanden — nur irrelevante Felder strippen
        allowed = {"period1_start", "period1_end", "period2_start", "period2_end",
                   "cost_per_sheet", "cost_per_mono", "cost_per_color"}
        return {k: v for k, v in kwargs.items() if k in allowed}
    start = kwargs.get("start_date")
    end   = kwargs.get("end_date")
    if not (start and end):
        raise ValueError("trend benötigt entweder period1_*/period2_* oder start_date/end_date")
    from datetime import date, timedelta
    def _parse(d):
        if isinstance(d, date):
            return d
        return date.fromisoformat(str(d)[:10])
    d1s = _parse(start)
    d1e = _parse(end)
    span_days = (d1e - d1s).days + 1
    d2e = d1s - timedelta(days=1)
    d2s = d2e - timedelta(days=span_days - 1)
    translated = {
        "period1_start": d1s.isoformat(),
        "period1_end":   d1e.isoformat(),
        "period2_start": d2s.isoformat(),
        "period2_end":   d2e.isoformat(),
    }
    for k in ("cost_per_sheet", "cost_per_mono", "cost_per_color"):
        if k in kwargs and kwargs[k] is not None:
            translated[k] = kwargs[k]
    return translated


# ─── 18. Sensible Dokumente (v3.8.0) ──────────────────────────────────────────

# Vordefinierte Keyword-Sets — key entspricht i18n-Key `sens_kw_set_<key>`.
# Die Listen enthalten Substring-Matches (CI) auf dem filename/job-name.
# Bewusst kurz gehalten, damit nicht alle Worte "Finanz" etc. auslösen —
# es sind typische, geschäftssensible Begriffe.
SENSITIVE_KEYWORD_SETS: dict[str, list[str]] = {
    "hr": [
        "Gehalt", "Lohn", "Lohnabrechnung", "Gehaltsabrechnung",
        "Kündigung", "Arbeitsvertrag", "Urlaubsantrag", "Abmahnung",
        "Personalakte", "Bewerbung", "Zeugnis",
    ],
    "finance": [
        "Kreditkarte", "Kontoauszug", "Rechnung", "Budget",
        "Bilanz", "Umsatz", "Mahnung", "Zahlungseingang",
        "Steuer", "Kalkulation",
    ],
    "confidential": [
        "Vertraulich", "Geheim", "Confidential", "Secret",
        "NDA", "Intern", "Entwurf", "Draft",
    ],
    "health": [
        "Arztbrief", "Diagnose", "Krankmeldung", "Attest",
        "Medikation", "Befund", "AU-Bescheinigung",
    ],
    "legal": [
        "Vertrag", "Klage", "Mahnbescheid", "Anwalt",
        "Gerichtsurteil", "Vollmacht", "Gutachten",
    ],
    "pii": [
        "Personalausweis", "Reisepass", "Passport",
        "Geburtsurkunde", "Sozialversicherung", "Meldebescheinigung",
    ],
}


def _resolve_sensitive_keywords(
    keyword_sets: Optional[list[str]] = None,
    custom_keywords: Optional[list[str]] = None,
) -> list[str]:
    """
    Baut die finale Keyword-Liste aus (optionalen) Preset-Sets +
    benutzerdefinierten Keywords. Whitespace wird getrimmt, Duplikate
    Case-Insensitive entfernt, Reihenfolge bleibt stabil.
    """
    seen_lower: set[str] = set()
    out: list[str] = []

    def _push(term: str) -> None:
        t = (term or "").strip()
        if not t:
            return
        low = t.lower()
        if low in seen_lower:
            return
        seen_lower.add(low)
        out.append(t)

    for key in (keyword_sets or []):
        for term in SENSITIVE_KEYWORD_SETS.get(key, []):
            _push(term)
    for term in (custom_keywords or []):
        _push(term)
    return out


def query_sensitive_documents(
    start_date: str,
    end_date: str,
    keyword_sets: Optional[list[str]] = None,
    custom_keywords: Optional[list[str]] = None,
    site_id: Optional[str] = None,
    user_email: Optional[str] = None,
    include_scans: bool = True,
    page: int = 0,
    page_size: int = 500,
) -> list[dict[str, Any]]:
    """
    v3.8.0 — Scannt Print- und Scan-Jobs auf sensible Keywords im filename.

    Liefert pro Treffer:
      document_type (print|scan), print_time, user_email, user_name,
      printer_name, site_name, filename, matched_keyword, page_count, color

    Die Keyword-Suche verwendet case-insensitive LIKE '%term%' — jede
    Kombination aus `keyword_sets` (Preset-Keys) und `custom_keywords`
    (freie Liste) wird OR-verknüpft. Fehlt jedes Keyword, werden als
    Fallback alle Preset-Sets verwendet.
    """
    tenant_id = get_tenant_id()

    terms = _resolve_sensitive_keywords(keyword_sets, custom_keywords)
    if not terms:
        # Fallback: wenn nichts angegeben, alle Sets zusammen nehmen
        terms = _resolve_sensitive_keywords(list(SENSITIVE_KEYWORD_SETS.keys()), None)
    # Safety-Cap: max. 40 Terms, sonst wird das SQL-Pattern monströs
    terms = terms[:40]

    # v3.8.0-fix (verstärkt in v3.8.1):
    # Spalten-Alias + Quell-Tabelle dynamisch auflösen.
    #
    # Reporting-Views können in zwei Zuständen existieren:
    #   a) v3.7.x-Definition → KEIN `filename`-Feld (stale)
    #   b) v3.8.0+-Definition → mit `filename`-Alias auf dbo.jobs.name
    #
    # Wenn die View stale ist (oder gar nicht existiert), gehen wir direkt an
    # `dbo.jobs` / `dbo.jobs_scan` ran und verwenden das reale Spalten-Namen
    # (`name` für Print, KEIN filename für Scan → Scan-Zweig deaktiviert).
    _jobs_tbl = _V('jobs')
    _jobs_scan_tbl = _V('jobs_scan')

    # Prüfe ob die View (falls vorhanden) tatsächlich eine filename-Spalte hat.
    from .sql_client import query_fetchone as _qfo
    def _view_has_column(fq_view: str, col: str) -> bool:
        try:
            r = _qfo(
                "SELECT COUNT(*) AS cnt FROM sys.columns "
                "WHERE object_id = OBJECT_ID(?) AND name = ?",
                (fq_view, col),
            )
            return bool((r or {}).get("cnt", 0) > 0)
        except Exception:
            return False

    # Entscheide Print-Quelle
    if _jobs_tbl.startswith("reporting.") and _view_has_column(_jobs_tbl, "filename"):
        _print_src = _jobs_tbl
        _print_filename_expr = "j.filename"
    else:
        # View hat kein filename-Feld (stale View, Schema-Setup nicht gelaufen).
        # Fallback auf dbo.jobs mit `name`-Spalte (Printix-Dokumentenname).
        # Hinweis: Demo-Daten (demo.jobs) fehlen in diesem Pfad — Benutzer
        # sollte "Schema einrichten" auf der Demo-Seite ausführen, damit die
        # reporting.v_jobs-View erstellt wird und beide Quellen vereint.
        _print_src = "dbo.jobs"
        _print_filename_expr = "j.name"

    # v4.4.15: Entscheide Scan-Quelle — demo.jobs_scan existiert NICHT in
    # Azure SQL (Demo-Daten liegen in lokaler SQLite seit v4.4.0).
    # Nur die reporting.v_jobs_scan-View hat ein filename-Feld.
    # Wenn die View nicht vorhanden ist, wird der Scan-SQL-Zweig deaktiviert
    # und Demo-Scan-Daten werden weiter unten per Python gemerged.
    if include_scans:
        if _jobs_scan_tbl.startswith("reporting.") and _view_has_column(_jobs_scan_tbl, "filename"):
            _scan_src = _jobs_scan_tbl
        else:
            # View stale oder nicht vorhanden — SQL-Scan-Branch deaktivieren,
            # Demo-Merge weiter unten liefert Scan-Daten aus SQLite
            _scan_src = None
    else:
        _scan_src = None

    # Dynamisches WHERE-OR über alle Keyword-LIKE-Klauseln.
    # pymssql/pyodbc: wir verwenden Parameterisierung, um SQL-Injection
    # vollständig zu vermeiden. Jedes term wird zu '%term%' gewrappt.
    like_clauses = []
    like_params: list = []
    for t in terms:
        like_clauses.append("LOWER(q.filename) LIKE LOWER(?)")
        like_params.append(f"%{t}%")
    keyword_where = "(" + " OR ".join(like_clauses) + ")"

    where_extra = ""
    extra_params: list = []
    if site_id:
        where_extra += " AND q.site_id = ?"
        extra_params.append(site_id)
    if user_email:
        where_extra += " AND q.user_email = ?"
        extra_params.append(user_email)

    offset = max(0, int(page)) * max(1, int(page_size))
    fetch  = max(1, min(int(page_size), 2000))

    # Wir fassen Print- und Scan-Quellen in einer Subquery zusammen, damit
    # wir nur einmal filtern und paginieren. Für Print-Jobs kommt filename
    # aus v_jobs (dort als `name`/`filename` aliased); Scan-Jobs brauchen
    # ebenfalls einen `filename`-Alias im View `reporting.v_jobs_scan`.
    scan_union = ""
    if _scan_src:  # v4.4.15: nur wenn View tatsaechlich vorhanden
        scan_union = f"""
            UNION ALL
            SELECT
                'scan'                AS document_type,
                js.scan_time          AS event_time,
                u.email               AS user_email,
                u.name                AS user_name,
                p.name                AS printer_name,
                n.name                AS site_name,
                n.id                  AS site_id,
                js.filename           AS filename,
                js.page_count         AS page_count,
                js.color              AS color
            FROM {_scan_src} js
            LEFT JOIN {_V('users')}    u ON u.id = js.tenant_user_id AND u.tenant_id = js.tenant_id
            LEFT JOIN {_V('printers')} p ON p.id = js.printer_id     AND p.tenant_id = js.tenant_id
            LEFT JOIN {_V('networks')} n ON n.id = p.network_id      AND n.tenant_id = js.tenant_id
            WHERE js.tenant_id = ?
              AND js.scan_time >= ?
              AND js.scan_time <  DATEADD(day, 1, CAST(? AS DATE))
              AND js.filename IS NOT NULL
              AND js.filename <> ''
        """

    sql = f"""
        SELECT TOP ({offset + fetch})
               document_type, event_time, user_email, user_name,
               printer_name, site_name, filename, page_count, color
        FROM (
            SELECT * FROM (
                SELECT
                    'print'               AS document_type,
                    j.submit_time         AS event_time,
                    u.email               AS user_email,
                    u.name                AS user_name,
                    p.name                AS printer_name,
                    n.name                AS site_name,
                    n.id                  AS site_id,
                    CAST({_print_filename_expr} AS NVARCHAR(500)) AS filename,
                    j.page_count          AS page_count,
                    j.color               AS color
                FROM {_print_src} j
                LEFT JOIN {_V('users')}    u ON u.id = j.tenant_user_id AND u.tenant_id = j.tenant_id
                LEFT JOIN {_V('printers')} p ON p.id = j.printer_id     AND p.tenant_id = j.tenant_id
                LEFT JOIN {_V('networks')} n ON n.id = p.network_id     AND n.tenant_id = j.tenant_id
                WHERE j.tenant_id = ?
                  AND j.submit_time >= ?
                  AND j.submit_time <  DATEADD(day, 1, CAST(? AS DATE))
                  AND {_print_filename_expr} IS NOT NULL
                  AND {_print_filename_expr} <> ''
                {scan_union}
            ) q
            WHERE {keyword_where}
            {where_extra}
        ) qq
        ORDER BY event_time DESC
    """
    # Params-Reihenfolge:
    # 1) print branch: tenant_id, start, end
    # 2) scan branch:  tenant_id, start, end (nur wenn include_scans)
    # 3) keyword like_params (einmal, auf aggregiertem q)
    # 4) extra filters (site_id, user_email)
    params: list = [tenant_id, _fmt_date(start_date), _fmt_date(end_date)]
    if _scan_src:  # v4.4.15: nur wenn SQL-Scan-Branch aktiv
        params.extend([tenant_id, _fmt_date(start_date), _fmt_date(end_date)])
    params.extend(like_params)
    params.extend(extra_params)

    rows = query_fetchall(sql, tuple(params))

    # Post-processing: matched_keyword pro Zeile annotieren (erster Treffer)
    lowered_terms = [(t.lower(), t) for t in terms]
    for r in rows:
        fn = (r.get("filename") or "").lower()
        matched = ""
        for low, orig in lowered_terms:
            if low in fn:
                matched = orig
                break
        r["matched_keyword"] = matched
    # Letzter Schliff: Paging client-side (TOP offset+fetch liefert N rows,
    # wir liefern die letzten `fetch` ab offset).
    if offset > 0:
        rows = rows[offset:]
    sql_results = rows[:fetch]

    # v4.4.14: Demo-Daten mergen (Print + Scan Jobs mit filename-Keyword-Match)
    if _has_active_demo():
        lowered = [(t.lower(), t) for t in terms]
        # Demo print jobs
        demo_jobs = _get_demo_job_rows(start_date, end_date)
        for r in demo_jobs:
            fn = r.get("filename", "") or ""
            if not fn:
                continue
            fn_lower = fn.lower()
            matched = ""
            for low, orig in lowered:
                if low in fn_lower:
                    matched = orig
                    break
            if not matched:
                continue
            if site_id and r.get("network_id") != site_id:
                continue
            if user_email and r.get("user_email") != user_email:
                continue
            sql_results.append({
                "document_type": "print",
                "event_time": r.get("submit_time", ""),
                "user_email": r.get("user_email", ""),
                "user_name": r.get("user_name", ""),
                "printer_name": r.get("printer_name", ""),
                "site_name": r.get("network_name", ""),
                "filename": fn,
                "page_count": int(r.get("page_count") or 0),
                "color": int(r.get("color") or 0),
                "matched_keyword": matched,
            })
        # Demo scan jobs
        if include_scans:
            demo_scans = _get_demo_scan_rows(start_date, end_date)
            for r in demo_scans:
                fn = r.get("filename", "") or ""
                if not fn:
                    continue
                fn_lower = fn.lower()
                matched = ""
                for low, orig in lowered:
                    if low in fn_lower:
                        matched = orig
                        break
                if not matched:
                    continue
                if site_id and r.get("network_id") != site_id:
                    continue
                if user_email and r.get("user_email") != user_email:
                    continue
                sql_results.append({
                    "document_type": "scan",
                    "event_time": r.get("scan_time", ""),
                    "user_email": r.get("user_email", ""),
                    "user_name": r.get("user_name", ""),
                    "printer_name": r.get("printer_name", ""),
                    "site_name": r.get("network_name", ""),
                    "filename": fn,
                    "page_count": int(r.get("page_count") or 0),
                    "color": int(r.get("color") or 0),
                    "matched_keyword": matched,
                })
        # v6.7.111: event_time kann im gemischten Ergebnis sowohl
        # datetime.datetime (aus SQL Server via pymssql/pyodbc) als auch str
        # (aus den Demo-Rows oben) sein. Python 3 vergleicht diese Typen
        # nicht miteinander → TypeError. Deshalb normalisieren wir zum
        # Sort-Key auf eine ISO-String-Repraesentation.
        def _etime_sort_key(r):
            t = r.get("event_time", "")
            if hasattr(t, "isoformat"):
                try:
                    return t.isoformat()
                except Exception:
                    return str(t)
            return str(t or "")
        sql_results.sort(key=_etime_sort_key, reverse=True)
        sql_results = sql_results[:fetch]

    return sql_results


# ─── 19. Stunde × Wochentag Heatmap (v3.8.1) ──────────────────────────────────
# Aggregiert Druckvolumen nach Stunde (0..23) × Wochentag (1=Sonntag..7=Samstag)
# für die SVG-Heatmap in report_engine. Gibt eine flache Row-Liste zurück;
# fehlende Zellen werden im Engine-Layer mit 0 aufgefüllt.
def query_hour_dow_heatmap(
    start_date: str,
    end_date: str,
    site_id: Optional[str] = None,
    user_email: Optional[str] = None,
    printer_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    """
    Nutzungs-Heatmap: Stunde × Wochentag.

    Spalten pro Row:
      hour       — 0..23 (DATEPART hour)
      dow        — 1..7  (1 = Sonntag, 7 = Samstag; SQL Server DATEPART weekday
                          ist ab SET DATEFIRST abhängig, hier normalisiert)
      total_jobs — Anzahl eindeutiger Jobs
      total_pages— Summe Seiten
    """
    tenant_id = get_tenant_id()

    where_extra = ""
    params_extra: list = []
    if site_id:
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)
    if user_email:
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)
    if printer_id:
        where_extra += " AND td.printer_id = ?"
        params_extra.append(printer_id)

    # SET DATEFIRST 7 stellt sicher, dass 1=Sonntag .. 7=Samstag
    sql = f"""
        SET DATEFIRST 7;
        SELECT
            DATEPART(hour,    td.print_time) AS hour,
            DATEPART(weekday, td.print_time) AS dow,
            COUNT(DISTINCT td.job_id)        AS total_jobs,
            SUM(td.page_count)               AS total_pages
        FROM {_V('tracking_data')} td
        LEFT JOIN {_V('jobs')}     j ON j.id = td.job_id AND j.tenant_id = td.tenant_id
        LEFT JOIN {_V('users')}    u ON u.id = j.tenant_user_id AND u.tenant_id = td.tenant_id
        LEFT JOIN {_V('printers')} p ON p.id = td.printer_id AND p.tenant_id = td.tenant_id
        WHERE td.tenant_id = ?
          AND td.print_time >= ?
          AND td.print_time <  DATEADD(day, 1, CAST(? AS DATE))
          AND td.print_job_status = 'PRINT_OK'
          {where_extra}
        GROUP BY DATEPART(hour, td.print_time), DATEPART(weekday, td.print_time)
        ORDER BY dow, hour
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as e:
        logger.warning("SQL query failed (hour_dow_heatmap), using demo-only: %s", e)
        sql_results = []

    # v4.4.14: Demo-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if user_email:
                filtered = [r for r in filtered if r.get("user_email") == user_email]
            if printer_id:
                filtered = [r for r in filtered if r.get("printer_id") == printer_id]
            if filtered:
                heatmap: dict[tuple, dict] = {}
                for r in filtered:
                    pt = str(r.get("print_time", ""))
                    try:
                        dt = datetime.strptime(pt[:19], "%Y-%m-%d %H:%M:%S") if len(pt) >= 19 else datetime.strptime(pt[:10], "%Y-%m-%d")
                    except ValueError:
                        continue
                    hour = dt.hour
                    # SQL Server DATEFIRST 7: 1=Sun..7=Sat; Python weekday(): 0=Mon..6=Sun
                    dow = (dt.weekday() + 2) % 7  # Convert: Mon=2,Tue=3,...,Sat=7,Sun=1
                    if dow == 0:
                        dow = 7
                    key = (hour, dow)
                    if key not in heatmap:
                        heatmap[key] = {"hour": hour, "dow": dow, "job_ids": set(), "total_pages": 0}
                    heatmap[key]["job_ids"].add(r.get("job_id", ""))
                    heatmap[key]["total_pages"] += int(r.get("page_count") or 0)

                # Merge with SQL
                existing = {(r.get("hour"), r.get("dow")): r for r in sql_results}
                for key, v in heatmap.items():
                    if key in existing:
                        e = existing[key]
                        e["total_jobs"] = (e.get("total_jobs") or 0) + len(v["job_ids"])
                        e["total_pages"] = (e.get("total_pages") or 0) + v["total_pages"]
                    else:
                        sql_results.append({
                            "hour": key[0], "dow": key[1],
                            "total_jobs": len(v["job_ids"]),
                            "total_pages": v["total_pages"],
                        })

    return sql_results


# ─── v3.9.0: Admin-Audit-Trail (SQLite, kein MSSQL) ──────────────────────────

def query_audit_log(
    start_date=None,
    end_date=None,
    tenant_id: str = "",
    action_prefix: str = "",
    limit: int = 1000,
    **_ignored,
):
    """Liest Admin-Audit-Log-Einträge aus der lokalen SQLite-DB.

    Kein Zugriff auf MSSQL — der Audit-Trail ist applikations-weit und
    in /data/printix_multi.db gespeichert. Der Report-Engine behandelt
    diesen Query-Typ über den generischen Stufe-2-Tabellen-Fallback.
    """
    try:
        # Verzögerter Import um Zirkularitäten zu vermeiden
        from db import query_audit_log_range  # type: ignore
    except Exception:
        # Fallback — db-Modul nicht importierbar
        return []
    rows = query_audit_log_range(
        start_date=start_date,
        end_date=end_date,
        tenant_id=tenant_id or "",
        action_prefix=action_prefix or "",
        limit=int(limit or 1000),
    )
    # Normalisiere Feldnamen → Display-freundlich für den Tabellen-Fallback
    out = []
    for r in rows:
        out.append({
            "timestamp": r.get("timestamp", ""),
            "actor": r.get("actor") or r.get("user_id") or "",
            "action": r.get("action", ""),
            "object_type": r.get("object_type", ""),
            "object_id": r.get("object_id", ""),
            "details": r.get("details", ""),
            "tenant_id": r.get("tenant_id", ""),
        })
    return out


# ─── v3.9.0: Druck außerhalb der Geschäftszeiten ─────────────────────────────

def query_off_hours_print(
    start_date=None,
    end_date=None,
    site_id: str = "",
    user_email: str = "",
    business_start_hour: int = 7,
    business_end_hour: int = 18,
    include_weekends_as_off_hours: bool = True,
    **_ignored,
):
    """Aggregiert Druckaktivität außerhalb der regulären Arbeitszeit.

    Default: Geschäftszeiten Mo–Fr 07:00–18:00. Alles andere gilt als Off-Hours.
    Liefert eine Zeitreihe mit Tages-Summe der Off-Hours-Jobs sowie ein
    Gesamt-Split (in-hours vs off-hours).
    """
    tenant_id = get_tenant_id()
    _jobs_tbl = _V('jobs')

    # Off-hours condition: hour outside business window OR weekend (DOW 1=Sun,7=Sat with DATEFIRST 7)
    weekend_clause = ""
    if include_weekends_as_off_hours:
        weekend_clause = " OR DATEPART(weekday, j.submit_time) IN (1, 7)"
    off_cond = (
        f"(DATEPART(hour, j.submit_time) < {int(business_start_hour)} "
        f"OR DATEPART(hour, j.submit_time) >= {int(business_end_hour)}"
        f"{weekend_clause})"
    )

    join_extra = ""
    where_extra = ""
    params_extra: list = []
    if site_id:
        join_extra += f" INNER JOIN {_V('printers')} p ON p.id = j.printer_id AND p.tenant_id = j.tenant_id"
        where_extra += " AND p.network_id = ?"
        params_extra.append(site_id)
    if user_email:
        join_extra += f" INNER JOIN {_V('users')} u ON u.id = j.tenant_user_id AND u.tenant_id = j.tenant_id"
        where_extra += " AND u.email = ?"
        params_extra.append(user_email)

    sql = f"""
        SET DATEFIRST 7;
        SELECT
            CONVERT(date, j.submit_time)                     AS day,
            SUM(CASE WHEN {off_cond} THEN 1 ELSE 0 END)      AS off_hours_jobs,
            SUM(CASE WHEN {off_cond} THEN 0 ELSE 1 END)      AS in_hours_jobs,
            COUNT(*)                                         AS total_jobs
        FROM {_jobs_tbl} j
        {join_extra}
        WHERE j.tenant_id = ?
          AND j.submit_time >= ?
          AND j.submit_time <  DATEADD(day, 1, CAST(? AS DATE))
          {where_extra}
        GROUP BY CONVERT(date, j.submit_time)
        ORDER BY day
    """
    params = (tenant_id, _fmt_date(start_date), _fmt_date(end_date)) + tuple(params_extra)
    try:
        sql_results = query_fetchall(sql, params)
    except Exception as exc:
        logger.warning("SQL query failed (off_hours_print): %s", exc)
        sql_results = []

    # v4.4.14: Demo-Daten mergen
    if _has_active_demo():
        demo_rows = _get_demo_job_rows(start_date, end_date)
        if demo_rows:
            filtered = demo_rows
            if site_id:
                filtered = [r for r in filtered if r.get("network_id") == site_id]
            if user_email:
                filtered = [r for r in filtered if r.get("user_email") == user_email]
            if filtered:
                daily: dict[str, dict] = {}
                for r in filtered:
                    st = str(r.get("submit_time", ""))
                    d = str(_parse_demo_date(st))
                    try:
                        dt = datetime.strptime(st[:19], "%Y-%m-%d %H:%M:%S") if len(st) >= 19 else datetime.strptime(st[:10], "%Y-%m-%d")
                    except ValueError:
                        continue
                    hour = dt.hour
                    wd = dt.weekday()  # 0=Mon..6=Sun
                    is_weekend = wd >= 5  # Sat/Sun
                    is_off = hour < business_start_hour or hour >= business_end_hour
                    if include_weekends_as_off_hours and is_weekend:
                        is_off = True

                    if d not in daily:
                        daily[d] = {"off": 0, "in": 0, "total": 0}
                    daily[d]["total"] += 1
                    if is_off:
                        daily[d]["off"] += 1
                    else:
                        daily[d]["in"] += 1

                # Merge with SQL results
                existing = {str(r.get("day", ""))[:10]: r for r in sql_results}
                for d, v in daily.items():
                    if d in existing:
                        e = existing[d]
                        e["off_hours_jobs"] = (e.get("off_hours_jobs") or 0) + v["off"]
                        e["in_hours_jobs"] = (e.get("in_hours_jobs") or 0) + v["in"]
                        e["total_jobs"] = (e.get("total_jobs") or 0) + v["total"]
                    else:
                        sql_results.append({
                            "day": d,
                            "off_hours_jobs": v["off"],
                            "in_hours_jobs": v["in"],
                            "total_jobs": v["total"],
                        })
                sql_results.sort(key=lambda x: str(x.get("day", "")))

    return sql_results


def _filter_kwargs_to_sig(fn, kwargs: dict) -> dict:
    """
    v3.7.11: Filter kwargs to only those accepted by the target function.
    Schützt run_query-Dispatcher vor Stufe-2-Presets, die zusätzliche
    Layout-Keys (group_by, order_by, preset_id, …) in query_params ablegen.
    Unerwünschte Keys werden verworfen statt einen TypeError auszulösen.
    """
    import inspect as _insp
    try:
        sig = _insp.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    params = sig.parameters
    if any(p.kind == _insp.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    allowed = {
        name for name, p in params.items()
        if p.kind in (_insp.Parameter.POSITIONAL_OR_KEYWORD, _insp.Parameter.KEYWORD_ONLY)
    }
    dropped = [k for k in kwargs if k not in allowed]
    if dropped:
        try:
            import logging as _log
            _log.getLogger("reporting.query_tools").debug(
                "run_query: %s() — dropped unsupported kwargs: %s",
                getattr(fn, "__name__", "?"), dropped
            )
        except Exception:
            pass
    return {k: v for k, v in kwargs.items() if k in allowed}


def run_query(query_type: str, tenant_id: str = "", **kwargs):
    """
    Dispatcher für alle Report-Query-Typen (Stufe 1 + 2).
    tenant_id wird ignoriert — der Caller ruft set_config_from_tenant() vorher.

    Stufe-1-Typen (Original):
      print_stats, cost_report, top_users, top_printers, trend, anomalies

    Stufe-2-Typen (neu):
      printer_history, device_readings, job_history, queue_stats,
      user_detail, user_copy_detail, user_scan_detail,
      workstation_overview, workstation_detail,
      tree_meter, service_desk
    """
    # ── Stufe 1 ──────────────────────────────────────────────────────────────
    if query_type == "print_stats":
        return query_print_stats(**_filter_kwargs_to_sig(query_print_stats, kwargs))
    elif query_type == "cost_report":
        return query_cost_report(**_filter_kwargs_to_sig(query_cost_report, kwargs))
    elif query_type == "top_users":
        return query_top_users(**_filter_kwargs_to_sig(query_top_users, kwargs))
    elif query_type == "top_printers":
        return query_top_printers(**_filter_kwargs_to_sig(query_top_printers, kwargs))
    elif query_type == "trend":
        # v3.7.10: start_date/end_date → period1/period2 übersetzen, damit
        # Preset-Templates und Scheduler nicht an "unexpected keyword" scheitern.
        return query_trend(**_filter_kwargs_to_sig(query_trend, _translate_trend_kwargs(kwargs)))
    elif query_type == "anomalies":
        return query_anomalies(**_filter_kwargs_to_sig(query_anomalies, kwargs))
    # ── Stufe 2 ──────────────────────────────────────────────────────────────
    elif query_type == "printer_history":
        return query_printer_history(**_filter_kwargs_to_sig(query_printer_history, kwargs))
    elif query_type == "device_readings":
        return query_device_readings(**_filter_kwargs_to_sig(query_device_readings, kwargs))
    elif query_type == "job_history":
        return query_job_history(**_filter_kwargs_to_sig(query_job_history, kwargs))
    elif query_type == "queue_stats":
        return query_queue_stats(**_filter_kwargs_to_sig(query_queue_stats, kwargs))
    elif query_type == "user_detail":
        return query_user_detail(**_filter_kwargs_to_sig(query_user_detail, kwargs))
    elif query_type == "user_copy_detail":
        return query_user_copy_detail(**_filter_kwargs_to_sig(query_user_copy_detail, kwargs))
    elif query_type == "user_scan_detail":
        return query_user_scan_detail(**_filter_kwargs_to_sig(query_user_scan_detail, kwargs))
    elif query_type == "workstation_overview":
        return query_workstation_overview(**_filter_kwargs_to_sig(query_workstation_overview, kwargs))
    elif query_type == "workstation_detail":
        return query_workstation_detail(**_filter_kwargs_to_sig(query_workstation_detail, kwargs))
    elif query_type == "tree_meter":
        return query_tree_meter(**_filter_kwargs_to_sig(query_tree_meter, kwargs))
    elif query_type == "service_desk":
        return query_service_desk(**_filter_kwargs_to_sig(query_service_desk, kwargs))
    # ── Stufe 2 (v3.8.0) ─────────────────────────────────────────────────────
    elif query_type == "sensitive_documents":
        return query_sensitive_documents(
            **_filter_kwargs_to_sig(query_sensitive_documents, kwargs)
        )
    # ── Stufe 2 (v3.8.1) ─────────────────────────────────────────────────────
    elif query_type == "hour_dow_heatmap":
        return query_hour_dow_heatmap(
            **_filter_kwargs_to_sig(query_hour_dow_heatmap, kwargs)
        )
    # ── Stufe 2 (v3.9.0) ─────────────────────────────────────────────────────
    elif query_type == "audit_log":
        return query_audit_log(
            **_filter_kwargs_to_sig(query_audit_log, kwargs)
        )
    elif query_type == "off_hours_print":
        return query_off_hours_print(
            **_filter_kwargs_to_sig(query_off_hours_print, kwargs)
        )
    elif query_type == "forecast":
        return query_forecast(**_filter_kwargs_to_sig(query_forecast, kwargs))
    else:
        raise ValueError(f"Unbekannter query_type: {query_type!r}")


# ─── Forecast / Prognose (v4.3.3) ───────────────────────────────────────────

def query_forecast(
    start_date: str,
    end_date: str,
    group_by: str = "month",      # day | week | month
    forecast_periods: int = 1,
    cost_per_sheet: float = 0.01,
    cost_per_mono: float  = 0.02,
    cost_per_color: float = 0.08,
) -> dict[str, Any]:
    """
    Historische Druckdaten + lineare Regression für Prognose.

    Gibt historische Datenpunkte und projizierte Werte zurück.
    Reine Python-Implementierung (kein numpy nötig).
    """
    # Historische Daten über query_print_stats holen
    historical = query_print_stats(
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
    )

    if not historical:
        return {
            "historical": [],
            "forecast": [],
            "slope": 0, "intercept": 0, "r_squared": 0,
            "prediction_text": "",
        }

    # Datenpunkte für Regression: x = Index, y = total_pages
    n = len(historical)
    xs = list(range(n))
    ys = [float(r.get("total_pages") or 0) for r in historical]

    slope, intercept, r_sq = _linear_regression(xs, ys)

    # Prognose-Punkte generieren
    forecasted = []
    for i in range(1, forecast_periods + 1):
        x_new = n - 1 + i
        y_pred = max(slope * x_new + intercept, 0)  # Nicht negativ
        forecasted.append({
            "period_index": x_new,
            "total_pages": round(y_pred),
            "is_forecast": True,
        })

    # Trend-Text
    if forecasted:
        next_val = forecasted[0]["total_pages"]
        last_val = ys[-1] if ys else 0
        if last_val > 0:
            change_pct = round((next_val - last_val) / last_val * 100, 1)
        else:
            change_pct = 0

        if change_pct > 5:
            trend = "up"
        elif change_pct < -5:
            trend = "down"
        else:
            trend = "stable"

        prediction_text = f"~{next_val:,.0f}"
    else:
        trend = "stable"
        prediction_text = ""
        change_pct = 0

    return {
        "historical": historical,
        "forecast": forecasted,
        "slope": round(slope, 2),
        "intercept": round(intercept, 2),
        "r_squared": round(r_sq, 3),
        "trend": trend,
        "change_pct": change_pct,
        "prediction_text": prediction_text,
    }


def _linear_regression(xs: list, ys: list) -> tuple[float, float, float]:
    """
    Einfache lineare Regression (Least Squares).
    Gibt (slope, intercept, r_squared) zurück.
    """
    n = len(xs)
    if n < 2:
        return (0.0, ys[0] if ys else 0.0, 0.0)

    sum_x  = sum(xs)
    sum_y  = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)
    sum_y2 = sum(y * y for y in ys)

    denom = n * sum_x2 - sum_x * sum_x
    if denom == 0:
        return (0.0, sum_y / n, 0.0)

    slope = (n * sum_xy - sum_x * sum_y) / denom
    intercept = (sum_y - slope * sum_x) / n

    # R² berechnen
    ss_res = sum((y - (slope * x + intercept)) ** 2 for x, y in zip(xs, ys))
    mean_y = sum_y / n
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0.0

    return (slope, intercept, max(r_squared, 0.0))
