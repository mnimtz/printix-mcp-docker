#!/bin/bash
# =============================================================================
# Printix MCP — Docker Entrypoint
# =============================================================================
# Startet bis zu drei Python-Services:
#   1. Web-Verwaltungsoberfläche  (WEB_PORT, default 8080)
#   2. MCP-Server (SSE + HTTP)    (MCP_PORT, default 8765)
#   3. Capture-Server (optional)  (CAPTURE_PORT, default 8775)
#
# Alle Secrets + SQLite-DB liegen in /data (muss als Volume gemountet sein).
# Konfiguration 100% via Environment-Variablen — siehe .env.example.
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------------
# Logging-Helper (Docker liest stdout/stderr → journalctl-kompatibel)
# -----------------------------------------------------------------------------
log_info()    { printf '[%s] [INFO]  %s\n'  "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*"; }
log_warn()    { printf '[%s] [WARN]  %s\n'  "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }
log_error()   { printf '[%s] [ERROR] %s\n'  "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$*" >&2; }

# -----------------------------------------------------------------------------
# Version / Banner
# -----------------------------------------------------------------------------
APP_VERSION="$(cat /app/VERSION 2>/dev/null || echo '0.0.0')"
export APP_VERSION

# -----------------------------------------------------------------------------
# /data muss beschreibbar sein
# -----------------------------------------------------------------------------
if [ ! -w /data ]; then
    log_error "/data ist nicht beschreibbar — bitte Volume korrekt mounten und Ownership prüfen (chown 1000:1000)."
    exit 1
fi

# -----------------------------------------------------------------------------
# Fernet-Key für DB-Feldverschlüsselung (wird einmalig beim ersten Start erzeugt)
# -----------------------------------------------------------------------------
if [ ! -f /data/fernet.key ]; then
    log_info "Erster Start erkannt — generiere Fernet-Key für DB-Verschlüsselung..."
    python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" > /data/fernet.key
    chmod 600 /data/fernet.key
fi
export FERNET_KEY
FERNET_KEY="$(cat /data/fernet.key)"

# -----------------------------------------------------------------------------
# Env-Var-Defaults (falls Docker/compose sie nicht setzt)
# -----------------------------------------------------------------------------
export MCP_HOST="${MCP_HOST:-0.0.0.0}"
export WEB_HOST="${WEB_HOST:-0.0.0.0}"
export CAPTURE_HOST="${CAPTURE_HOST:-0.0.0.0}"
export MCP_PORT="${MCP_PORT:-8765}"
export WEB_PORT="${WEB_PORT:-8080}"
export CAPTURE_PORT="${CAPTURE_PORT:-8775}"
export IPP_PORT="${IPP_PORT:-0}"
export CAPTURE_ENABLED="${CAPTURE_ENABLED:-false}"
export MCP_LOG_LEVEL="${MCP_LOG_LEVEL:-info}"
export MCP_PUBLIC_URL="${MCP_PUBLIC_URL:-}"
# Trailing-Slash wegnormalisieren
MCP_PUBLIC_URL="${MCP_PUBLIC_URL%/}"
export CAPTURE_PUBLIC_URL="${CAPTURE_PUBLIC_URL:-}"
CAPTURE_PUBLIC_URL="${CAPTURE_PUBLIC_URL%/}"

if [ -n "${MCP_PUBLIC_URL}" ]; then
    BASE="${MCP_PUBLIC_URL}"
else
    BASE="http://<host>:${MCP_PORT}"
fi

# -----------------------------------------------------------------------------
# Banner
# -----------------------------------------------------------------------------
cat <<BANNER
╔══════════════════════════════════════════════════════════════════════════╗
║        PRINTIX MCP SERVER v${APP_VERSION} — MULTI-TENANT (Docker)                  ║
╠══════════════════════════════════════════════════════════════════════════╣
║  Web-Verwaltung:   http://<host>:${WEB_PORT}
║    → Erstkonfiguration / Benutzer registrieren
║
║  MCP-Endpunkte:
║    claude.ai  →  ${BASE}/mcp
║    ChatGPT    →  ${BASE}/sse
║    Health     →  ${BASE}/health
║    OAuth      →  ${BASE}/oauth/authorize
BANNER

if [ -n "${IPP_PORT}" ] && [ "${IPP_PORT}" != "0" ]; then
    echo "║  IPP/IPPS Listener: Port ${IPP_PORT} (TLS via IPPS_CERTFILE/IPPS_KEYFILE)"
else
    echo "║  IPP/IPPS Listener: nur auf WEB-Port ${WEB_PORT} (IPP_PORT=0)"
fi

if [ "${CAPTURE_ENABLED}" = "true" ]; then
    CAPTURE_BASE="${CAPTURE_PUBLIC_URL:-http://<host>:${CAPTURE_PORT}}"
    echo "║  Capture-Server (separat): ${CAPTURE_BASE}/capture/webhook/<profile_id>"
else
    echo "║  Capture (via MCP):        ${BASE}/capture/webhook/<profile_id>"
fi
echo "╚══════════════════════════════════════════════════════════════════════════╝"

# -----------------------------------------------------------------------------
# Child-Prozesse sauber terminieren (SIGTERM von Docker → alle Kinder killen)
# -----------------------------------------------------------------------------
WEB_PID=""
CAPTURE_PID=""

shutdown() {
    log_info "SIGTERM erhalten — beende Services..."
    [ -n "${WEB_PID}" ] && kill -TERM "${WEB_PID}" 2>/dev/null || true
    [ -n "${CAPTURE_PID}" ] && kill -TERM "${CAPTURE_PID}" 2>/dev/null || true
    wait "${WEB_PID}" "${CAPTURE_PID}" 2>/dev/null || true
    exit 0
}
trap shutdown SIGTERM SIGINT

# -----------------------------------------------------------------------------
# Web-UI starten (Hintergrund)
# -----------------------------------------------------------------------------
log_info "Starte Web-UI auf ${WEB_HOST}:${WEB_PORT}..."
python3 /app/web/run.py &
WEB_PID=$!
log_info "Web-UI läuft (PID: ${WEB_PID})"

# -----------------------------------------------------------------------------
# Capture-Server starten (optional, Hintergrund)
# -----------------------------------------------------------------------------
if [ "${CAPTURE_ENABLED}" = "true" ]; then
    if [ ! -f /app/capture_server.py ]; then
        log_error "/app/capture_server.py nicht gefunden — Capture-Server kann nicht starten."
    else
        log_info "Starte Capture-Server auf ${CAPTURE_HOST}:${CAPTURE_PORT}..."
        python3 /app/capture_server.py &
        CAPTURE_PID=$!
        sleep 2
        if kill -0 "${CAPTURE_PID}" 2>/dev/null; then
            log_info "Capture-Server läuft (PID: ${CAPTURE_PID})"
        else
            log_error "Capture-Server ist sofort beendet — prüfe Logs oberhalb."
        fi
    fi
else
    log_info "Capture-Server deaktiviert (CAPTURE_ENABLED=${CAPTURE_ENABLED}) — Webhooks via MCP-Port."
fi

# -----------------------------------------------------------------------------
# MCP-Server starten (Vordergrund → wird PID des Containers)
# -----------------------------------------------------------------------------
log_info "Starte MCP-Server auf ${MCP_HOST}:${MCP_PORT}..."
exec python3 /app/server.py
