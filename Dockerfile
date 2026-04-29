# syntax=docker/dockerfile:1.7
# =============================================================================
# Printix MCP — Standalone Docker Image (Multi-Tenant MCP Server + Web-UI + IPPS)
# =============================================================================
# Multi-Stage Build:
#   1. builder  — kompiliert Python-Wheels (pyodbc/pymssql brauchen dev-Header)
#   2. runtime  — schlankes Debian-Slim mit nur Runtime-Deps (FreeTDS, LibreOffice-core)
#
# Ziel-Plattformen: linux/amd64, linux/arm64 (gebaut via buildx in CI)
# Für armv7-Support zusätzlich libssl-dev libkrb5-dev libffi-dev zlib1g-dev
# libjpeg-dev in den builder-Stage packen — deaktiviert, da wenig Nachfrage.
# =============================================================================

ARG PYTHON_VERSION=3.13

# -----------------------------------------------------------------------------
# Stage 1: Builder — Python-Wheels kompilieren
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

# Build-Tools + Entwickler-Header für pyodbc/pymssql/cryptography/bcrypt
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        gcc \
        unixodbc-dev \
        freetds-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /wheels
COPY src/requirements.txt .
RUN pip wheel --wheel-dir=/wheels -r requirements.txt

# -----------------------------------------------------------------------------
# Stage 2: Runtime — nur Laufzeit-Deps
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Defaults: werden vom entrypoint.sh / docker-compose gesetzt
    MCP_HOST=0.0.0.0 \
    WEB_HOST=0.0.0.0 \
    CAPTURE_HOST=0.0.0.0 \
    MCP_PORT=8765 \
    WEB_PORT=8080 \
    CAPTURE_PORT=8775 \
    IPP_PORT=0 \
    CAPTURE_ENABLED=false \
    MCP_LOG_LEVEL=info \
    DB_PATH=/data/printix_multi.db \
    IPP_SPOOL_DIR=/data/ipp-spool \
    DEMO_DB_PATH=/data/demo_data.db \
    TEMPLATES_PATH=/data/report_templates.json

RUN apt-get update && apt-get install -y --no-install-recommends \
        # FreeTDS ODBC (Azure SQL)
        unixodbc \
        tdsodbc \
        freetds-bin \
        # LibreOffice-core für Web-Upload-Konvertierung (docx/xlsx/pptx/odt → PDF)
        # ~230 MB — nur die Core-Pakete, kein Java/GUI
        libreoffice-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-dejavu \
        # v7.2.8: Ghostscript fuer PDF→PCL/PostScript-Konvertierung in den
        # Print-Workflow-Tools (print_self, print_to_recipients,
        # send_to_user etc.). Default-Target = PCL XL (pxlcolor) — sonst
        # drucken Drucker ohne PDF-RIP rohe PDF-Bytes als Klartext.
        ghostscript \
        # Healthcheck braucht curl (schlanker als netcat und python-urllib)
        curl \
        # tini = sauberer PID 1 (weiterleitet Signale an Python-Prozesse,
        # reapt Zombies — wichtig weil wir mehrere Hintergrund-Prozesse haben)
        tini \
    && rm -rf /var/lib/apt/lists/*

# FreeTDS-Treiber in ODBC-Registry eintragen (tdsodbc macht das meist, wir ergänzen als Safety-Net)
RUN if ! grep -q "\[FreeTDS\]" /etc/odbcinst.ini 2>/dev/null; then \
        DRIVER=$(find /usr/lib -name "libtdsodbc.so*" 2>/dev/null | head -1); \
        if [ -n "$DRIVER" ]; then \
            printf "[FreeTDS]\nDescription=FreeTDS ODBC Driver\nDriver=%s\nSetup=%s\nFileUsage=1\n" \
                   "$DRIVER" "$DRIVER" >> /etc/odbcinst.ini; \
        fi; \
    fi

# Python-Wheels aus dem Builder-Stage kopieren und installieren
COPY --from=builder /wheels /wheels
COPY src/requirements.txt /tmp/requirements.txt
RUN pip install --no-index --find-links=/wheels -r /tmp/requirements.txt \
    && rm -rf /wheels /tmp/requirements.txt

# v7.2.36: Certbot für /admin/auto-tls (1-Click Let's Encrypt mit sslip.io).
# Erlaubt Usern mit fester Public IP aber ohne eigene Domain ein
# vollautomatisches HTTPS-Setup ohne Cloudflare-Konto, ohne Tailscale-
# VPN, ohne manuelle CLI-Schritte. ACME HTTP-01 Challenge läuft via
# port 80 standalone (~30 Sekunden während der Anforderung).
RUN apt-get update && apt-get install -y --no-install-recommends \
        certbot \
    && rm -rf /var/lib/apt/lists/*

# v7.2.32: Cloudflare Tunnel Binary (für /admin/tunnel In-App-Setup).
# Ein-Klick-HTTPS für Endkunden: Quick Tunnel (anonym, *.trycloudflare.com)
# oder Named Tunnel mit eigenem CF-Token.
# Multi-Arch: BuildKit setzt TARGETARCH auf amd64 / arm64 / armv7 etc.
ARG TARGETARCH
RUN set -eux; \
    case "${TARGETARCH:-amd64}" in \
        amd64)  CF_ARCH="amd64" ;; \
        arm64)  CF_ARCH="arm64" ;; \
        arm)    CF_ARCH="arm" ;; \
        386)    CF_ARCH="386" ;; \
        *)      echo "Unsupported arch: ${TARGETARCH}"; exit 1 ;; \
    esac; \
    curl -fsSL "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF_ARCH}" \
         -o /usr/local/bin/cloudflared; \
    chmod +x /usr/local/bin/cloudflared; \
    /usr/local/bin/cloudflared --version

# Anwendungs-Code
WORKDIR /app
COPY src/ /app/
COPY VERSION /app/VERSION
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# Non-root User — wichtig auch wenn User im Compose nicht gemapped wird
RUN groupadd --system --gid 1000 printix \
    && useradd --system --uid 1000 --gid printix --home-dir /app --shell /bin/bash printix \
    && mkdir -p /data \
    && chown -R printix:printix /data /app

USER printix

VOLUME ["/data"]

EXPOSE 8080 8765 8775 631

# Healthcheck: MCP-Server muss auf /health antworten
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${MCP_PORT}/health" > /dev/null || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/app/entrypoint.sh"]
