# Printix MCP Server — Docker

**Multi-Tenant MCP Server** für die [Printix](https://printix.net) Cloud Print API — mit Web-Verwaltungsoberfläche, Cloud-Print-Gateway und optionalem Capture-Server.

Läuft plattformunabhängig als Docker-Container (Linux / macOS / Windows / Synology NAS / TrueNAS / Unraid / …).

> Das hier ist die **Docker-Variante** als eigenständiges Projekt. Die ursprüngliche **Home Assistant Add-on**-Variante lebt separat weiter in [`printix-mcp-addon`](https://github.com/mnimtz/printix-mcp-addon).

---

## Schnell-Installation

```bash
# 1. Projekt-Ordner anlegen
mkdir printix-mcp && cd printix-mcp

# 2. Compose-Datei + Beispiel-Config holen
curl -O https://raw.githubusercontent.com/mnimtz/printix-mcp-docker/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/mnimtz/printix-mcp-docker/main/.env.example
mv .env.example .env

# 3. Konfiguration anpassen (mindestens MCP_PUBLIC_URL wenn hinter Tunnel/Proxy)
nano .env

# 4. Starten
docker compose up -d

# 5. Browser öffnen für Erstkonfiguration
open http://localhost:8080
```

Fertig. Im Web-UI registrierst du einen ersten Admin-User und hinterlegst deine Printix-API-Credentials.

---

## Was läuft da drin?

Der Container startet drei Python-Services (kein separater Reverse Proxy nötig):

| Port (Default) | Service | Zweck |
|---|---|---|
| **8080** | Web-Verwaltungsoberfläche | Registrierung, Admin-UI, Mobile-App-Onboarding |
| **8765** | MCP Endpoint | `claude.ai` (Streamable HTTP) + `ChatGPT` (SSE) + OAuth |
| **8775** | Capture Webhook *(optional)* | Papercut-Style Follow-Me-Print Trigger |
| **631** | IPP/IPPS Listener *(optional)* | Cloud Print Eingang für Druckertreiber |

Alle Ports und ihre Host-Mappings sind in [`docker-compose.yml`](docker-compose.yml) + [`.env`](.env.example) konfigurierbar.

---

## Persistenz & Daten

Alle Daten liegen im Docker-Volume `printix-data` (gemountet nach `/data` im Container):

| Pfad | Inhalt |
|---|---|
| `/data/printix_multi.db` | SQLite — User, Tenants, Jobs, Reports, Audit-Log |
| `/data/fernet.key` | Symmetrischer Schlüssel für DB-Feldverschlüsselung *(automatisch generiert beim ersten Start)* |
| `/data/web_session_key` | Session-Signierungs-Schlüssel der Web-UI |
| `/data/demo_data.db` | Lokale Demo-/Playground-Daten *(nur wenn Demo-Modus genutzt)* |
| `/data/report_templates.json` | Gespeicherte Report-Templates |
| `/data/ipp-spool/` | IPP-Spool (nur wenn Cloud-Print-Listener aktiv) |

**⚠️ Backup-Empfehlung**: das komplette `/data`-Volume sichern reicht — alles Sensible ist mit dem Fernet-Key (der auch dort liegt) verschlüsselt.

### Bind-Mount statt Named Volume

Wenn du die Daten auf dem Host sichtbar haben willst, in `docker-compose.yml` das Volume ändern:

```yaml
volumes:
  - ./data:/data          # statt: printix-data:/data
```

und das `printix-data:`-Named-Volume unten entfernen. **Wichtig**: Bind-Mount-Ownership setzen, sonst kann der Container nicht schreiben:

```bash
mkdir -p ./data
sudo chown -R 1000:1000 ./data
```

Der Container läuft als non-root User `printix` (UID 1000, GID 1000).

---

## Konfiguration

Alle Einstellungen laufen über Environment-Variablen in der [`.env`](.env.example)-Datei. Die wichtigsten:

| Variable | Default | Zweck |
|---|---|---|
| `MCP_PUBLIC_URL` | *(leer)* | Öffentliche URL wenn hinter Tunnel/Proxy (z.B. `https://mcp.example.com`) |
| `MCP_LOG_LEVEL` | `info` | `debug` \| `info` \| `warning` \| `error` \| `critical` |
| `HOST_WEB_PORT` | `8080` | Host-Port-Mapping für die Web-UI |
| `HOST_MCP_PORT` | `8765` | Host-Port-Mapping für den MCP-Endpoint |
| `CAPTURE_ENABLED` | `false` | Separater Capture-Server auf Port 8775 statt via MCP-Port |
| `IPP_PORT` | `0` | IPP-Listener-Port *(0 = deaktiviert, 631 = Standard)* |
| `IPPS_CERTFILE` / `IPPS_KEYFILE` | *(leer)* | TLS-Zertifikat für IPPS *(wenn IPP_PORT gesetzt)* |

Siehe `.env.example` für die vollständige, kommentierte Liste.

---

## Updates

```bash
# Neueste Version ziehen und neu starten
docker compose pull
docker compose up -d

# Oder explizit auf einen Tag pinnen in .env:
#   PRINTIX_TAG=6.7.118
```

Alle verfügbaren Tags: <https://github.com/mnimtz/printix-mcp-docker/pkgs/container/printix-mcp-docker>

Updates sind safe — alle persistenten Daten im `/data`-Volume überleben den Container-Austausch. DB-Migrationen laufen automatisch beim Start.

---

## Reverse Proxy / Cloudflare Tunnel

Standard-Deployment im Internet: Reverse Proxy terminiert TLS, der Container hört nur auf `127.0.0.1`.

**Traefik-Beispiel** (Labels in `docker-compose.yml` ergänzen):

```yaml
services:
  printix-mcp:
    # ...
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.printix.rule=Host(`mcp.example.com`)"
      - "traefik.http.routers.printix.entrypoints=websecure"
      - "traefik.http.routers.printix.tls.certresolver=le"
      - "traefik.http.services.printix.loadbalancer.server.port=8765"
```

**Cloudflare Tunnel**: einfach `cloudflared` nebenan laufen lassen und den Tunnel auf `http://printix-mcp:8765` zeigen lassen (bei Bedarf zweiter Hostname → `:8080` für die Web-UI).

In beiden Fällen: `MCP_PUBLIC_URL` in der `.env` auf die öffentliche URL setzen — sonst stimmen OAuth-Redirects und QR-Code-Links nicht.

---

## AI-Assistant-Anbindung

Nach Erstkonfiguration im Web-UI:

- **claude.ai** → *Settings → Integrations → Add MCP Server* → `<MCP_PUBLIC_URL>/mcp`
- **ChatGPT** → MCP via SSE → `<MCP_PUBLIC_URL>/sse`
- **Claude Code (CLI)** → `claude mcp add printix <MCP_PUBLIC_URL>/mcp`

Die OAuth-Endpunkte (`/oauth/authorize`, `/oauth/token`) werden von den AI-Clients automatisch genutzt — keine manuelle Token-Verwaltung nötig.

---

## Troubleshooting

```bash
# Logs anschauen
docker compose logs -f printix-mcp

# Container-Status + Health
docker compose ps

# In den Container reinkommen
docker compose exec printix-mcp bash

# SQLite direkt anschauen
docker compose exec printix-mcp sqlite3 /data/printix_multi.db '.tables'

# Container komplett zurücksetzen (Achtung: ALLE Daten weg)
docker compose down -v
```

**„Permission denied“ bei Bind-Mount**: siehe [Bind-Mount statt Named Volume](#bind-mount-statt-named-volume) — Ownership auf 1000:1000.

**Web-UI zeigt 502/Timeout hinter Cloudflare**: `MCP_PUBLIC_URL` muss gesetzt sein, damit interne Redirects das richtige Schema/Host benutzen.

**Container neu gestartet, aber Login geht nicht**: Check ob `/data/fernet.key` und `/data/web_session_key` noch da sind — wenn das Volume versehentlich geleert wurde, werden sie neu generiert und alle bestehenden verschlüsselten Secrets sind unlesbar.

---

## Lokales Bauen (Entwickler)

```bash
# Clone + Build
git clone https://github.com/mnimtz/printix-mcp-docker.git
cd printix-mcp-docker

# In docker-compose.yml: image: → build: . tauschen, dann:
docker compose up --build

# Oder direkt:
docker build -t printix-mcp-docker:dev .
docker run --rm -p 8080:8080 -p 8765:8765 -v printix-data:/data printix-mcp-docker:dev
```

Multi-Arch-Build (amd64 + arm64 + armv7) wird in CI via GitHub Actions erledigt — siehe [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml).

---

## Lizenz & Herkunft

Lizenziert unter der [**Apache License 2.0**](LICENSE) — Copyright © 2026 Marcus Nimtz.

Fork der HA-Addon-Code-Base ([`printix-mcp-addon`](https://github.com/mnimtz/printix-mcp-addon)), ent-HA-ified und als Standalone-Docker-Distribution paketiert. Beide Projekte entwickeln sich parallel weiter — Änderungen im HA-Addon-Core werden bei Bedarf portiert.

Maintainer: Marcus Nimtz · `marcus@nimtz.email`
