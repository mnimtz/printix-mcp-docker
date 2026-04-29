"""
Printix MCP — Web-Verwaltungsoberfläche Entry Point
====================================================
Startet den FastAPI-Web-Server auf dem konfigurierten WEB_PORT.

Wird von run.sh im Hintergrund gestartet (vor dem MCP-Server).
"""

import os
import sys
import secrets
import logging

# Logging zentral einrichten BEVOR irgendein Modul Logger holt — sonst landen
# printix.web Logs (inkl. Demo-Job-Lifecycle) im Vakuum, weil run.py sonst
# keinen StreamHandler an stdout setzt und _WebTenantDBHandler nur in die SQL
# tenant_logs-Tabelle schreibt (was bei SQL-Hangs ebenfalls haengt).
logging.basicConfig(
    level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)

logger = logging.getLogger("printix.web")

SESSION_KEY_FILE = "/data/web_session_key"

def _get_or_create_session_key() -> str:
    """Erzeugt oder liest den Session-Signierungsschlüssel."""
    if os.path.exists(SESSION_KEY_FILE):
        with open(SESSION_KEY_FILE) as f:
            key = f.read().strip()
        if key:
            return key
    key = secrets.token_hex(32)
    os.makedirs("/data", exist_ok=True)
    with open(SESSION_KEY_FILE, "w") as f:
        f.write(key)
    return key


if __name__ == "__main__":
    import uvicorn

    # sys.path so anpassen dass /app Module gefunden werden
    app_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)

    from web.app import create_app

    host     = os.environ.get("WEB_HOST", "0.0.0.0")
    port     = int(os.environ.get("WEB_PORT", "8080"))
    log_lvl  = os.environ.get("MCP_LOG_LEVEL", "info").lower()
    session_secret = _get_or_create_session_key()

    app = create_app(session_secret=session_secret)

    # v7.2.35: Wenn der Admin im Web-UI ein eigenes TLS-Cert importiert
    # hat, läuft die Web-UI direkt auf HTTPS — ohne Cloudflare oder
    # Reverse-Proxy-Sidecar. Cert + Key liegen unter /data/tls/{cert,key}.pem.
    # Der `tls_enabled` Settings-Flag entscheidet, ob aktiv.
    ssl_kwargs: dict = {}
    try:
        import sys as _tls_sys
        _tls_sys.path.insert(0, "/app")
        from db import init_db as _tls_init_db, get_setting as _tls_get_setting
        # init_db ist idempotent; web/app.py ruft es ohnehin beim Import
        # auf, aber wir lesen Settings VOR FastAPI-Import (anderer Pfad)
        _tls_init_db()
        if _tls_get_setting("tls_enabled", "0") == "1":
            cert_path = "/data/tls/cert.pem"
            key_path  = "/data/tls/key.pem"
            if os.path.isfile(cert_path) and os.path.isfile(key_path):
                ssl_kwargs = {
                    "ssl_certfile": cert_path,
                    "ssl_keyfile":  key_path,
                }
                logger.info("TLS aktiviert — uvicorn startet auf HTTPS mit %s", cert_path)
            else:
                logger.warning(
                    "tls_enabled=1, aber Cert/Key fehlen unter /data/tls/. "
                    "Falle auf HTTP zurück."
                )
    except Exception as e:
        logger.warning("TLS-Check fehlgeschlagen, falle auf HTTP zurück: %s", e)

    proto = "https" if ssl_kwargs else "http"
    logger.info("Starte Web-Verwaltungsoberfläche auf %s://%s:%d", proto, host, port)
    # proxy_headers=True + forwarded_allow_ips="*" lässt Uvicorn die
    # X-Forwarded-Proto/-Host/-For Header auswerten. Ohne das liefert
    # request.base_url nur das interne Schema (http) und den internen
    # Host, was z.B. den Mobile-App-QR mit "http://..." statt
    # "https://..." bestückt, wenn man via Cloudflare Tunnel / Reverse
    # Proxy kommt. "*" ist hier ok, weil wir im Container hinter HA
    # laufen und externer Traffic immer proxied reinkommt.
    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_lvl,
        proxy_headers=True,
        forwarded_allow_ips="*",
        **ssl_kwargs,
    )
