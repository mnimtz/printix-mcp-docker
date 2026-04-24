"""
Printix Capture Server — Standalone Webhook Endpoint
=============================================================
Optionaler dedizierter Server nur fuer Capture Webhooks.
Laeuft IMMER auf Container-Port 8775 (fest, passend zu config.yaml ports).

Wird von run.sh gestartet wenn capture_enabled=true konfiguriert ist.

Endpunkte:
  POST /capture/webhook/{profile_id}  -> Printix Capture Webhook
  GET  /capture/webhook/{profile_id}  -> Health-Check
  ALL  /capture/debug                 -> Debug-Endpoint
  ALL  /capture/debug/{path}          -> Debug mit Subpath
  GET  /health                        -> Server Health-Check
"""

import os
import sys
import json
import logging
import traceback
from app_version import APP_VERSION

# v4.6.7: Sofort loggen — noch vor allen Imports die fehlschlagen koennten
print(f"[capture_server] Starting... (PID={os.getpid()}, "
      f"CAPTURE_PORT={os.environ.get('CAPTURE_PORT', '?')}, "
      f"CAPTURE_HOST={os.environ.get('CAPTURE_HOST', '?')})",
      flush=True)

# Logging einrichten
logging.basicConfig(
    level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)

logger = logging.getLogger("printix.capture.server")

# sys.path so anpassen dass /app Module gefunden werden
app_dir = os.path.dirname(os.path.abspath(__file__))
if app_dir not in sys.path:
    sys.path.insert(0, app_dir)

try:
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse
except ImportError as e:
    logger.error("FATAL: FastAPI Import fehlgeschlagen: %s", e)
    logger.error("Installierte Pakete pruefen: pip3 list | grep fastapi")
    sys.exit(1)


def create_capture_app() -> FastAPI:
    """Erstellt die FastAPI-App fuer den Capture-Server."""
    app = FastAPI(
        title="Printix Capture Server",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # ── Health-Check ─────────────────────────────────────────────────────────

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "capture", "version": APP_VERSION}

    # ── Capture Webhook ──────────────────────────────────────────────────────

    @app.api_route("/capture/webhook/{profile_id}", methods=["GET", "POST"])
    async def capture_webhook(request: Request, profile_id: str):
        from capture.webhook_handler import handle_webhook

        method = request.method
        headers = {k: v for k, v in request.headers.items()}
        body_bytes = await request.body()

        logger.info("▶ CAPTURE REQUEST [capture-server]: %s /capture/webhook/%s",
                     method, profile_id[:8])

        try:
            status, data = await handle_webhook(
                profile_id=profile_id,
                method=method,
                headers=headers,
                body_bytes=body_bytes,
                source="capture",
            )
        except Exception as e:
            logger.error("Capture handler error: %s", e, exc_info=True)
            status, data = 500, {"error": str(e)}

        return JSONResponse(content=data, status_code=status)

    # ── Debug Endpoint ───────────────────────────────────────────────────────

    @app.api_route("/capture/debug", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    @app.api_route("/capture/debug/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def capture_debug(request: Request, path: str = ""):
        from capture.webhook_handler import handle_webhook

        method = request.method
        headers = {k: v for k, v in request.headers.items()}
        body_bytes = await request.body()

        logger.info("▶ CAPTURE DEBUG [capture-server]: %s /capture/debug/%s",
                     method, path)

        debug_profile_id = "00000000-0000-0000-0000-000000000000"
        try:
            status, data = await handle_webhook(
                profile_id=debug_profile_id,
                method=method,
                headers=headers,
                body_bytes=body_bytes,
                source="capture",
            )
        except Exception as e:
            logger.error("Capture debug error: %s", e, exc_info=True)
            status, data = 500, {"error": str(e)}

        return JSONResponse(content=data, status_code=status)

    return app


# ── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        import uvicorn
    except ImportError as e:
        logger.error("FATAL: uvicorn Import fehlgeschlagen: %s", e)
        sys.exit(1)

    host = os.environ.get("CAPTURE_HOST", "0.0.0.0")
    port_str = os.environ.get("CAPTURE_PORT", "8775")
    log_level = os.environ.get("MCP_LOG_LEVEL", "info").lower()

    # v4.6.7: Port robust parsen
    try:
        port = int(port_str)
    except (ValueError, TypeError):
        logger.error("FATAL: CAPTURE_PORT='%s' ist keine gueltige Portnummer!", port_str)
        sys.exit(1)

    if port <= 0 or port > 65535:
        logger.error("FATAL: CAPTURE_PORT=%d liegt ausserhalb des gueltigen Bereichs (1-65535)!", port)
        sys.exit(1)

    capture_public_url = os.environ.get("CAPTURE_PUBLIC_URL", "").rstrip("/")
    base = capture_public_url or f"http://{host}:{port}"

    logger.info("Erstelle Capture-App...")
    try:
        app = create_capture_app()
    except Exception as e:
        logger.error("FATAL: Capture-App konnte nicht erstellt werden: %s", e, exc_info=True)
        sys.exit(1)

    logger.info("╔══════════════════════════════════════════════════════════════╗")
    logger.info("║        PRINTIX CAPTURE SERVER v%s — STANDALONE           ║", APP_VERSION)
    logger.info("╠══════════════════════════════════════════════════════════════╣")
    logger.info("║  Host:     %s:%d", host, port)
    logger.info("║  Webhook:  %s/capture/webhook/<profile_id>", base)
    logger.info("║  Debug:    %s/capture/debug", base)
    logger.info("║  Health:   %s/health", base)
    logger.info("╚══════════════════════════════════════════════════════════════╝")

    try:
        uvicorn.run(app, host=host, port=port, log_level=log_level)
    except Exception as e:
        logger.error("FATAL: Capture-Server konnte nicht starten: %s", e, exc_info=True)
        sys.exit(1)
