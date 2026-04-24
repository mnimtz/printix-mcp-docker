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

    logger.info("Starte Web-Verwaltungsoberfläche auf %s:%d", host, port)
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
    )
