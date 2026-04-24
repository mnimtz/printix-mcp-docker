"""
SQL Client — Azure SQL Verbindung für Printix BI-Datenbank
==========================================================
Multi-Tenant v2.1.0: pymssql bevorzugt auf ARM64 (kein SIGSEGV),
pyodbc als Fallback auf x86_64 mit ODBC Driver 17/18.

Konfiguration via ContextVar current_sql_config,
gesetzt durch BearerAuthMiddleware pro Request.

Jeder Tenant hat seine eigenen SQL-Credentials in der SQLite-DB.
Die ContextVar enthält nach Authentifizierung:
  sql_server    — z.B. printix-bi-data-2.database.windows.net
  sql_database  — z.B. printix_bi_data_2_1
  sql_username  — SQL-Benutzername
  sql_password  — SQL-Passwort (entschlüsselt)
  tenant_id     — Printix Tenant-ID für Datenfilterung
"""

import logging
import platform
import time as _time
from typing import Any, Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Azure SQL Serverless transient-fault Fehlercodes (Auto-Pause / Skalierung)
_AZURE_SQL_TRANSIENT_STATES = {
    "40613",  # Database not currently available
    "40501",  # Service is currently busy
    "40540",  # Service has encountered an error
    "49918",  # Cannot process request — insufficient resources
    "49919",  # Cannot process create/update request
    "49920",  # Too many operations in progress
    "4060",   # Cannot open database
    "233",    # No process is on the other end of the pipe
    "10053",  # Transport-level error
    "10054",  # Existing connection was forcibly closed
    "10060",  # No connection could be made
}

def _is_transient_azure_sql_error(exc: Exception) -> bool:
    """Gibt True zurück wenn es sich um einen Azure SQL Transient Fault handelt."""
    msg = str(exc)
    for code in _AZURE_SQL_TRANSIENT_STATES:
        if code in msg:
            return True
    return "not currently available" in msg or "retry the connection" in msg.lower()


try:
    import pymssql
    PYMSSQL_AVAILABLE = True
    logger.debug("pymssql verfügbar: %s", pymssql.__version__)
except ImportError:
    PYMSSQL_AVAILABLE = False

try:
    import pyodbc
    PYODBC_AVAILABLE = True
except ImportError:
    PYODBC_AVAILABLE = False
    logger.warning("pyodbc nicht installiert — SQL-Reporting nicht verfügbar")

# ContextVar-Import für Multi-Tenant-Routing
try:
    from auth import current_sql_config as _current_sql_config
    _CONTEXTVAR_AVAILABLE = True
except ImportError:
    _current_sql_config = None  # type: ignore
    _CONTEXTVAR_AVAILABLE = False
    logger.warning("auth.py nicht gefunden — SQL-Config aus ContextVar nicht verfügbar")


# v6.2.2: Ergebnis von _prefer_pymssql() cachen — Arch + Driver ändern sich
# während der Laufzeit nicht. Vermeidet Log-Spam bei jeder SQL-Query.
_PYMSSQL_DECISION: Optional[bool] = None


def _prefer_pymssql() -> bool:
    """
    Bevorzuge pymssql auf ARM64 (aarch64/armv7l) oder wenn kein Microsoft ODBC Driver
    verfügbar ist. pyodbc + FreeTDS crasht mit SIGSEGV auf ARM64 (Azure SQL).

    Die Entscheidung wird beim ersten Aufruf gecacht — die Plattform-
    Architektur und installierte ODBC-Driver ändern sich nicht während
    der Laufzeit. So wird der Log-Output nicht bei jeder Query
    wiederholt ("ARM64 erkannt (aarch64) …").
    """
    global _PYMSSQL_DECISION
    if _PYMSSQL_DECISION is not None:
        return _PYMSSQL_DECISION

    if not PYMSSQL_AVAILABLE:
        _PYMSSQL_DECISION = False
        return False
    arch = platform.machine().lower()
    if arch in ("aarch64", "arm64", "armv7l", "armv7"):
        logger.info("ARM64 erkannt (%s) — verwende pymssql statt pyodbc/FreeTDS", arch)
        _PYMSSQL_DECISION = True
        return True
    # Auch auf x86_64: bevorzuge pymssql wenn kein Microsoft ODBC Driver 17/18 vorhanden
    if PYODBC_AVAILABLE:
        available = pyodbc.drivers()
        has_ms_driver = any(
            "ODBC Driver" in d and "SQL Server" in d
            for d in available
        )
        if not has_ms_driver:
            logger.info("Kein MS ODBC Driver — verwende pymssql")
            _PYMSSQL_DECISION = True
            return True
    _PYMSSQL_DECISION = False
    return False


def _adapt_sql(sql: str) -> str:
    """
    Konvertiert pyodbc-Platzhalter (?) zu pymssql-Platzhaltern (%s).
    Wird nur aufgerufen wenn pymssql bevorzugt wird.
    Einfaches replace — gilt nur für Parameter-Platzhalter außerhalb von Strings.
    """
    if _prefer_pymssql():
        return sql.replace("?", "%s")
    return sql


def _detect_driver() -> str:
    if not PYODBC_AVAILABLE:
        return "FreeTDS"

    available = pyodbc.drivers()
    logger.debug("Verfügbare ODBC-Treiber: %s", available)

    preferred = [
        "ODBC Driver 18 for SQL Server",
        "ODBC Driver 17 for SQL Server",
        "FreeTDS",
        "TDS",
    ]
    for d in preferred:
        if d in available:
            logger.debug("ODBC-Treiber gewählt: %s", d)
            return d

    import glob
    for pattern in (
        "/usr/lib/*/odbc/libtdsodbc.so",
        "/usr/lib/libtdsodbc.so*",
        "/usr/local/lib/libtdsodbc.so*",
    ):
        matches = glob.glob(pattern)
        if matches:
            logger.warning(
                "Kein ODBC-Treiber in odbcinst.ini — verwende direkten Pfad: %s.",
                matches[0],
            )
            return matches[0]

    logger.error("Kein ODBC-Treiber gefunden! Verfügbar: %s.", available)
    return "FreeTDS"


def _get_sql_config() -> dict:
    if _CONTEXTVAR_AVAILABLE and _current_sql_config is not None:
        cfg = _current_sql_config.get()
        if cfg:
            return cfg
    raise RuntimeError(
        "SQL-Konfiguration nicht im Request-Kontext. "
        "Bitte Bearer Token setzen — BearerAuthMiddleware muss den SQL-Kontext gesetzt haben."
    )


def _build_connection_string() -> str:
    cfg      = _get_sql_config()
    server   = cfg.get("server", "")
    database = cfg.get("database", "")
    username = cfg.get("username", "")
    password = cfg.get("password", "")
    driver   = _detect_driver()

    if "FreeTDS" in driver or driver.endswith(".so") or driver.endswith(".so.0"):
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={server},1433;"
            f"DATABASE={database};"
            f"UID={username};"
            f"PWD={password};"
            f"TDS_Version=7.4;"
            f"Connection Timeout=30;"
        )

    return (
        f"DRIVER={{{driver}}};"
        f"SERVER={server};"
        f"DATABASE={database};"
        f"UID={username};"
        f"PWD={password};"
        f"Encrypt=yes;"
        f"TrustServerCertificate=no;"
        f"Connection Timeout=30;"
    )


def get_tenant_id() -> str:
    if _CONTEXTVAR_AVAILABLE and _current_sql_config is not None:
        cfg = _current_sql_config.get()
        if cfg:
            return cfg.get("tenant_id", "")
    return ""


def get_current_db_key() -> tuple:
    if _CONTEXTVAR_AVAILABLE and _current_sql_config is not None:
        cfg = _current_sql_config.get()
        if cfg:
            return (cfg.get("server", ""), cfg.get("database", ""))
    return ("", "")


def set_config_from_tenant(tenant: dict) -> None:
    """
    Setzt die SQL-Konfiguration aus einem Tenant-Dict.
    tenant muss die Felder sql_server, sql_database, sql_username, sql_password,
    printix_tenant_id enthalten (alle bereits entschlüsselt).
    Fällt auf 'tenant_id' zurück falls 'printix_tenant_id' leer ist (CLI-Tests).
    """
    if not _CONTEXTVAR_AVAILABLE or _current_sql_config is None:
        raise RuntimeError(
            "current_sql_config ContextVar nicht verfügbar — auth.py fehlt?"
        )
    # printix_tenant_id ist die Printix Cloud Tenant-UUID (in dbo.*/demo.* als tenant_id gespeichert).
    # Fallback auf "tenant_id" erleichtert CLI-Tests und direkten Aufruf aus Workern.
    ptid = tenant.get("printix_tenant_id") or tenant.get("tenant_id", "")
    _current_sql_config.set({
        "server":    tenant.get("sql_server", ""),
        "database":  tenant.get("sql_database", ""),
        "username":  tenant.get("sql_username", ""),
        "password":  tenant.get("sql_password", ""),
        "tenant_id": ptid,
    })


def is_configured() -> bool:
    """Prüft ob alle SQL-Konfigurationsparameter im aktuellen Kontext gesetzt sind."""
    if not _CONTEXTVAR_AVAILABLE or _current_sql_config is None:
        return False
    cfg = _current_sql_config.get()
    if not cfg:
        return False
    return all([
        cfg.get("server"),
        cfg.get("database"),
        cfg.get("username"),
        cfg.get("password"),
    ])


@contextmanager
def get_connection():
    """
    Context Manager für eine Azure SQL-Verbindung.

    Bevorzugt pymssql auf ARM64 (kein SIGSEGV durch FreeTDS).
    Automatischer Retry (3×, 5s Pause) bei Azure SQL Serverless Auto-Pause.
    """
    if not is_configured():
        raise RuntimeError(
            "SQL nicht konfiguriert. Bitte SQL-Credentials für diesen Tenant in der Web-UI eintragen."
        )

    cfg = _get_sql_config()
    max_attempts = 3

    if _prefer_pymssql():
        # ── pymssql-Pfad (ARM64 / kein MS ODBC Driver) ──────────────────────
        server   = cfg.get("server", "")
        database = cfg.get("database", "")
        username = cfg.get("username", "")
        password = cfg.get("password", "")

        conn = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "Öffne Azure SQL Verbindung via pymssql zu %s/%s (Versuch %d/%d)",
                    server, database, attempt, max_attempts,
                )
                # WICHTIG: timeout MUSS gesetzt sein, sonst hängt pymssql ewig
                # bei TCP-Stalls (z.B. Azure SQL Serverless Wake-up). 300s = 5 Min
                # reicht für große BULK-INSERTs aus, verhindert aber Endlos-Hangs.
                conn = pymssql.connect(
                    server=server,
                    user=username,
                    password=password,
                    database=database,
                    port=1433,
                    login_timeout=30,
                    timeout=300,
                    charset="UTF-8",
                    as_dict=False,
                )
                break
            except Exception as e:
                if attempt < max_attempts and _is_transient_azure_sql_error(e):
                    logger.warning(
                        "Azure SQL transient fault via pymssql (Versuch %d/%d) — warte 5s: %s",
                        attempt, max_attempts, str(e)[:120],
                    )
                    _time.sleep(5)
                else:
                    logger.error("pymssql Verbindungsfehler: %s", e)
                    raise RuntimeError(f"Datenbankverbindung fehlgeschlagen (pymssql): {e}") from e
        try:
            yield conn
        finally:
            if conn:
                conn.close()

    else:
        # ── pyodbc-Pfad (x86_64 mit ODBC Driver 17/18) ──────────────────────
        if not PYODBC_AVAILABLE:
            raise RuntimeError(
                "pyodbc nicht installiert. Bitte 'pip install pyodbc' im Container ausführen."
            )

        conn_str = _build_connection_string()
        conn = None
        for attempt in range(1, max_attempts + 1):
            try:
                logger.debug(
                    "Öffne Azure SQL Verbindung via pyodbc zu %s (Versuch %d/%d)",
                    cfg.get("server", ""), attempt, max_attempts,
                )
                conn = pyodbc.connect(conn_str, timeout=30)
                break
            except pyodbc.Error as e:
                if attempt < max_attempts and _is_transient_azure_sql_error(e):
                    logger.warning(
                        "Azure SQL transient fault (Versuch %d/%d) — warte 5s: %s",
                        attempt, max_attempts, str(e)[:120],
                    )
                    _time.sleep(5)
                else:
                    logger.error("SQL-Verbindungsfehler: %s", e)
                    raise RuntimeError(f"Datenbankverbindung fehlgeschlagen: {e}") from e
        try:
            yield conn
        finally:
            if conn:
                conn.close()


def query_fetchall(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with get_connection() as conn:
        cursor = conn.cursor()
        sql = _adapt_sql(sql)
        logger.debug("SQL: %s | params: %s", sql[:200], params)
        cursor.execute(sql, params)
        columns = [col[0] for col in cursor.description]
        rows = cursor.fetchall()
        return [dict(zip(columns, row)) for row in rows]


def query_fetchone(sql: str, params: tuple = ()) -> Optional[dict[str, Any]]:
    results = query_fetchall(sql, params)
    return results[0] if results else None


def execute_write(sql: str, params: tuple = ()) -> int:
    with get_connection() as conn:
        cursor = conn.cursor()
        sql = _adapt_sql(sql)
        logger.debug("SQL-Write: %s | params: %s", sql[:200], params)
        cursor.execute(sql, params)
        conn.commit()
        return cursor.rowcount if cursor.rowcount >= 0 else 0


def execute_many(sql: str, params_list: list, batch_size: int = 500) -> int:
    """
    Bulk-Insert / Bulk-Update.

    Wichtig: pymssql hat KEIN fast_executemany. Naives cursor.executemany()
    macht in pymssql einen Round-Trip pro Zeile — bei 50.000 Demo-Datens\u00e4tzen
    \u00fcber Azure SQL Internet-Latenz dauert das Stunden und f\u00fchrt zu Hangs.

    pymssql-Pfad: baut Multi-Row VALUES Statements und sendet ~100 Zeilen
    pro Round-Trip. Aus 50.000 Round-Trips werden ~500 \u2192 ~100\u00d7 schneller.

    pyodbc-Pfad: fast_executemany (echtes BCP \u00fcber TDS) bleibt unver\u00e4ndert.
    """
    if not params_list:
        return 0
    sql = _adapt_sql(sql)

    if _prefer_pymssql():
        return _execute_many_multirow(sql, params_list)

    total = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        try:
            cursor.fast_executemany = True
        except AttributeError:
            pass
        for i in range(0, len(params_list), batch_size):
            batch = params_list[i:i + batch_size]
            cursor.executemany(sql, batch)
            total += len(batch)
        conn.commit()
    return total


def _execute_many_multirow(sql: str, params_list: list) -> int:
    """
    Pymssql-spezifischer Bulk-Insert: baut Multi-Row VALUES Statements,
    sodass viele Zeilen pro TDS-Round-Trip an Azure SQL gehen.

    SQL Server-Limits:
      - max. 2100 Parameter pro Statement
      - max. 1000 VALUES-Tupel pro Statement
    """
    import re as _re
    n_total = len(params_list)

    m = _re.search(r"VALUES\s*\(([^)]*)\)", sql, _re.IGNORECASE)
    if not m:
        # Kein erkennbares INSERT VALUES (?,?,?) Pattern \u2192 klassischer Fallback
        logger.warning("Bulk-Insert Fallback auf executemany (kein VALUES-Pattern in SQL)")
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.executemany(sql, params_list)
            conn.commit()
        return n_total

    placeholder_tuple = m.group(1)
    num_cols = placeholder_tuple.count("%s") or len(params_list[0])
    rows_per_stmt = max(1, min(1000, 2000 // max(1, num_cols)))

    head = sql[:m.start()] + "VALUES "
    one_tuple = "(" + ",".join(["%s"] * num_cols) + ")"

    logger.info(
        "Bulk-Insert (pymssql multirow): %d Zeilen \u00e0 %d Spalten in Batches \u00e0 %d Zeilen",
        n_total, num_cols, rows_per_stmt,
    )

    total = 0
    with get_connection() as conn:
        cursor = conn.cursor()
        for i in range(0, n_total, rows_per_stmt):
            batch = params_list[i:i + rows_per_stmt]
            stmt = head + ",".join([one_tuple] * len(batch))
            flat = tuple(v for row in batch for v in row)
            try:
                cursor.execute(stmt, flat)
            except Exception as exc:
                logger.error(
                    "Bulk-Insert fehlgeschlagen bei Batch Zeilen %d\u2013%d (%d Spalten): %s",
                    i, i + len(batch), num_cols, exc,
                )
                raise
            total += len(batch)
            if total == len(batch) or total == n_total or (i // rows_per_stmt) % 5 == 0:
                logger.info("Bulk-Insert: %d/%d Zeilen geschrieben", total, n_total)
        conn.commit()
    return total


def execute_script(statements: list[str]) -> None:
    with get_connection() as conn:
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            cursor = conn.cursor()
            stmt = _adapt_sql(stmt)
            logger.debug("SQL-Script: %s", stmt[:100])
            cursor.execute(stmt)
            try:
                conn.commit()
            except Exception:
                pass
