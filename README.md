# Printix MCP Server — Docker

**Self-hosted MCP server** for the [Printix](https://printix.net) Cloud Print
API — with a web admin UI, AI-assistant integration (claude.ai / ChatGPT /
Claude Code), an optional cloud-print gateway and a capture webhook endpoint.

Runs as a cross-platform Docker container (Linux / macOS / Windows /
Synology NAS / TrueNAS / Unraid / …).

> This repo is the **Docker distribution** as a standalone project. The
> original **Home Assistant add-on** variant lives on separately in
> [`printix-mcp-addon`](https://github.com/mnimtz/printix-mcp-addon).

> **Current stable: v7.2.10** (April 2026) · **127 MCP tools** ·
> iOS mobile companion app · Auto-PDL conversion (PDF → PCL XL via
> Ghostscript) · Time-bomb-driven onboarding workflows · Multi-recipient
> secure print · Microsoft Entra ID + Authorization Code + PKCE flow.

> **v7.0.0 is single-tenant.** One installation hosts exactly *one* tenant;
> all users share it. The earlier "one-tenant-per-user" model from v6.7.x
> has been removed (see [CHANGELOG](CHANGELOG.md)).

---

## Feature overview

**AI-assistant integration — 129 MCP tools**
- MCP server for [claude.ai](https://claude.ai) (Streamable HTTP), ChatGPT (SSE) and Claude Code (CLI)
- Built-in OAuth 2.0 endpoints — no manual token juggling
- All tools ship with structured docstrings (when-to-use / when-NOT / returns / args + concrete example prompts) so the AI picks the right tool reliably
- See [`docs/MCP_MANUAL_EN.md`](docs/MCP_MANUAL_EN.md) / [`docs/MCP_MANUAL_DE.md`](docs/MCP_MANUAL_DE.md) for the complete tool catalogue

**GDPR-compliant role-based access control (v7.2.23+)**
- Five built-in roles mapped to GDPR articles: End User (Art. 15-22), Helpdesk (Art. 32 — separation of duties), Admin (Art. 24), Auditor / DPO (Art. 37-39), Service Account (Art. 28+32)
- Two assignment paths: per-user override **and** per-Printix-group default ("highest role wins" on multi-group membership)
- Live status banner on `/admin/mcp-permissions` shows whether enforcement is active
- Built-in compliance documentation: every customer-hosted instance ships with a GDPR Compliance Guide (PDF) and a Permission Matrix (PDF) downloadable from the admin UI
- Denied tool calls are recorded in the audit log for ongoing compliance review
- Activation is opt-in via `MCP_RBAC_ENABLED` (defaults to `1` in the bundled `docker-compose.yml`)

**GDPR data subject rights — built-in MCP tools (v7.2.30+)**
- `printix_personal_data_export` (GDPR Art. 15) — every user can ask their AI assistant *"What data do you have about me?"* and receive a structured ZIP with profile, group memberships, cards, audit-log entries, time-bombs, print statistics and MCP role override
- `printix_personal_data_purge_request` (GDPR Art. 17) — non-destructive deletion request: records the request in the audit log, sends a structured email to the configured tenant admins with the data summary and the requester's reason, returns a request ID. The admin reviews and executes the deletion via `printix_offboard_user` / `printix_delete_user` within the GDPR Art. 12(3) one-month deadline
- End users are restricted to their own data (self-check at the argument level); Helpdesk and Admin can act on any subject in support of formal access/deletion requests

**Per-user Connect-Center (v7.2.21+)**
- One-page personal connection profile at `/my/connect`
- All connection data (MCP URL, OAuth ID, Secret with reveal toggle, SSE endpoint, Authorize/Token URLs) in copy-buttoned cards
- Step-by-step instructions per platform (Claude.ai, ChatGPT, Claude Code CLI)
- Direct downloads for the localised user manuals (DE / EN / NO)
- Localised in DE / EN / NO; non-DE locales default to English

**Workflow tools (v6.8.x / v7.2.x — AI-driven workflows)**
- `printix_print_self` — AI generates a PDF inline and queues it on the caller's own secure-print queue (auto-PDL conversion to PCL XL)
- `printix_print_to_recipients` — multi-recipient secure print, accepts emails, `group:<Name>`, `entra:<group-OID>` mixed
- `printix_send_to_capture` — push files straight into the capture pipeline (e.g. Paperless-ngx) without the printer detour
- `printix_welcome_user` — onboarding workflow with conditional time-bombs (auto-reminder if user hasn't enrolled a card / printed yet after N days)
- `printix_session_print` — secure-print job that auto-expires after N hours
- `printix_card_enrol_assist` — register an NFC card UID with auto-transform via the user's profile
- `printix_describe_user_print_pattern`, `printix_quota_guard`, `printix_print_history_natural`, `printix_resolve_recipients` and more

**Mobile app (iOS) — *Printix MobilePrint***
- Native SwiftUI app for iPhone/iPad
- Microsoft sign-in via in-app Safari sheet (`ASWebAuthenticationSession` + Authorization Code Flow with PKCE — *no* device-code prompt)
- NFC card enrolment (tap an HID/Mifare/FeliCa/DESFire badge, UID is decoded via the profile transformer and registered to the Printix user)
- Share Extension: send any file from any iOS app via *Share → Printix → choose target*
- QR onboarding from the admin portal (`/my/setup-guide`) — no manual server URL entry
- Keychain-stored bearer token with Face ID / Touch ID unlock

**Web admin (`/admin`)**
- User management: create, invite, CSV bulk-import, **Printix direct import** (pull users straight from the Printix cloud into local accounts, optionally with an invitation mail)
- 2-role model (`admin` | `employee`) with last-admin safeguard
- Printix credentials management (Print / Card / WS / UM scopes)
- SMTP configuration for report and invitation mails
- Audit log with a searchable event history
- Backup / restore of the entire `/data` volume

**Self-service (`/my`)**
- View and delete jobs, delegate printing to other users
- Personal dashboard (own jobs, delegations, managed employees)
- QR code for iOS app pairing (`/my/setup-guide`)

**Reporting**
- Report templates with design options (colour, logo, chart type)
- Scheduled reports (daily / weekly / monthly) delivered by mail
- Live queries: top users, top printers, cost per department, trends, anomalies

**Cloud-print gateway** *(optional)*
- IPP/IPPS listener on port 631 — PCs can treat the container as a network printer
- Capture webhook endpoint (Papercut-style follow-me-print trigger)
- **Auto-PDL conversion** (PDF / PostScript / Text → PCL XL via Ghostscript) for every server-side print path — so printers without a built-in PDF RIP no longer print hieroglyphs

**Auth**
- Local accounts (username / password, PIN, ID code)
- Microsoft Entra ID / Azure AD SSO *(optional)*
  - Web SSO (Authorization Code + client_secret)
  - macOS / Windows desktop client (Device Code Flow)
  - iOS mobile app (Authorization Code + PKCE — public client)
  - Single Entra app registration covers all three flows
- OAuth for AI assistants

**i18n**
- Multi-language web UI (de / en / more), invitation mails localised

---

## Quick install

```bash
# 1. Create a project folder
mkdir printix-mcp && cd printix-mcp

# 2. Grab the compose file and sample config
curl -O https://raw.githubusercontent.com/mnimtz/printix-mcp-docker/main/docker-compose.yml
curl -O https://raw.githubusercontent.com/mnimtz/printix-mcp-docker/main/.env.example
mv .env.example .env

# 3. Adjust the config (at minimum set MCP_PUBLIC_URL if behind a tunnel/proxy)
nano .env

# 4. Start
docker compose up -d

# 5. Open the browser for the first-time setup
open http://localhost:8080
```

That's it. In the web UI you register the first admin user and store your Printix API credentials.

---

## Deployment via Portainer

Portainer offers three ways to create a stack — each one works with this project.

### Option A — Repository *(recommended, automatic updates)*

Stacks → *Add stack* → **Repository**

| Field | Value |
|---|---|
| Repository URL | `https://github.com/mnimtz/printix-mcp-docker` |
| Repository reference | `refs/heads/main` |
| Compose path | `docker-compose.yml` |
| Auto update | *(optional)* webhook or polling interval |

Under **Environment variables** set at least `MCP_PUBLIC_URL` (if you're behind a tunnel/proxy). Everything else is optional — see [.env.example](.env.example).

Benefit: Portainer pulls updates directly from the repo, no more manual `docker compose pull`.

### Option B — Web editor *(fastest, no Git needed)*

Stacks → *Add stack* → **Web editor** → paste the following compose snippet:

```yaml
services:
  printix-mcp:
    image: ghcr.io/mnimtz/printix-mcp-docker:latest
    container_name: printix-mcp
    restart: unless-stopped
    environment:
      MCP_PUBLIC_URL: ${MCP_PUBLIC_URL:-}
      MCP_LOG_LEVEL: ${MCP_LOG_LEVEL:-info}
      WEB_PORT: 8080
      MCP_PORT: 8765
      CAPTURE_PORT: 8775
      CAPTURE_ENABLED: ${CAPTURE_ENABLED:-false}
      IPP_PORT: ${IPP_PORT:-0}
    ports:
      - "8080:8080"
      - "8765:8765"
      - "8775:8775"
      # - "631:631"   # only when IPP_PORT is set
    volumes:
      - printix-data:/data
    healthcheck:
      test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8765/health"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s

volumes:
  printix-data:
    driver: local
```

In the **Environment variables** section below, set:
- `MCP_PUBLIC_URL=https://mcp.example.com` *(if behind a tunnel/proxy — leave empty otherwise)*
- `CAPTURE_ENABLED=false` *(set to `true` if capture webhooks should arrive from outside)*
- `IPP_PORT=0` *(set to `631` to enable the cloud-print gateway)*

Then *Deploy the stack* — done.

### Option C — Upload

Stacks → *Add stack* → **Upload** → upload the [`docker-compose.yml`](docker-compose.yml) from this repo, set env vars as above, deploy.

### After deploy

Portainer lists the container under `Containers`; logs are available directly in the Portainer UI. Open the web UI at `http://<portainer-host>:8080` and finish the first-time setup (create admin, store Printix credentials).

**Updating to a new version:** Portainer → Stack → *Pull and redeploy* (or automatic via webhook/polling with Option A).

---

## What's running inside

The container starts three Python services (no separate reverse proxy required):

| Port (default) | Service | Purpose |
|---|---|---|
| **8080** | Web admin UI | Registration, admin UI, mobile-app onboarding |
| **8765** | MCP endpoint | `claude.ai` (Streamable HTTP) + `ChatGPT` (SSE) + OAuth |
| **8775** | Capture webhook *(optional)* | Papercut-style follow-me-print trigger |
| **631** | IPP/IPPS listener *(optional)* | Cloud-print input for printer drivers |

All ports and their host mappings live in [`docker-compose.yml`](docker-compose.yml) and [`.env`](.env.example).

---

## Persistence & data

All data lives in the Docker volume `printix-data` (mounted at `/data` in the container):

| Path | Contents |
|---|---|
| `/data/printix_multi.db` | SQLite — users, tenants, jobs, reports, audit log |
| `/data/fernet.key` | Symmetric key for DB-field encryption *(auto-generated on first start)* |
| `/data/web_session_key` | Session-signing key for the web UI |
| `/data/demo_data.db` | Local demo / playground data *(only when demo mode is used)* |
| `/data/report_templates.json` | Saved report templates |
| `/data/ipp-spool/` | IPP spool (only when the cloud-print listener is active) |

**⚠️ Backup recommendation**: backing up the whole `/data` volume is enough — everything sensitive is encrypted with the Fernet key (which also lives there).

### Bind mount instead of named volume

If you want the data visible on the host, change the volume in `docker-compose.yml`:

```yaml
volumes:
  - ./data:/data          # instead of: printix-data:/data
```

and drop the `printix-data:` named volume at the bottom. **Important**: set the bind-mount ownership or the container cannot write:

```bash
mkdir -p ./data
sudo chown -R 1000:1000 ./data
```

The container runs as the non-root user `printix` (UID 1000, GID 1000).

---

## Configuration

Two places, two responsibilities (since v7.0.0):

1. **Ports** → only in [`docker-compose.yml`](docker-compose.yml) under `ports:`.
   To move to a different host port, change only the left-hand number:
   ```yaml
   ports:
     - "9000:8080"   # web UI now on host port 9000
   ```
2. **Runtime settings** → [`.env`](.env.example) (env defaults) **or** the admin UI
   at `/admin/settings` (overrides `.env`).

The most important environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `MCP_PUBLIC_URL` | *(empty)* | Public URL (tunnel/proxy, e.g. `https://mcp.example.com`). Can be overridden at runtime under `/admin/settings` — the DB setting takes precedence. |
| `MCP_LOG_LEVEL` | `info` | `debug` \| `info` \| `warning` \| `error` \| `critical` |
| `MCP_RBAC_ENABLED` | `1` *(in compose; `0` if compose is bypassed)* | **Role-based access control** for MCP tool calls. `1` enforces the roles configured at `/admin/mcp-permissions`; tools outside the caller's scope return a `permission_denied` payload and an audit-log entry. `0` is pass-through (anyone can call any tool). See the [GDPR Compliance Guide](src/web/assets/manuals/MCP_GDPR_COMPLIANCE_GUIDE.pdf) shipped in the image. |
| `CAPTURE_ENABLED` | `false` | Separate capture server on port 8775 instead of going through the MCP port |
| `IPP_PORT` | `0` | IPP listener port *(0 = disabled, 631 = default)* |
| `IPPS_CERTFILE` / `IPPS_KEYFILE` | *(empty)* | TLS certificate for IPPS *(when `IPP_PORT` is set)* |

See `.env.example` for the full, annotated list.

---

## Microsoft Entra ID / SSO

Lets users sign in with their work Microsoft account instead of a local password. Works for the web UI, the Windows / macOS desktop clients (Device Code Flow) **and** the iOS *Printix MobilePrint* app (Authorization Code + PKCE, native in-app Safari sheet — v7.1.4+).

### Step 1 — Auto-create the Entra App Registration

1. Open the web UI → log in as admin → *Settings* → *Microsoft Entra ID*.
2. Click **„Auto-Setup"**. The page shows a one-time device code.
3. Open `https://microsoft.com/devicelogin` on **any** device, paste the code, sign in with an account that has **Application Administrator** (or Global Admin) rights in the target Entra tenant, grant consent.
4. The server uses Microsoft Graph to create an App Registration named *„Printix Management Console"*, generates a client secret, and stores `tenant_id` / `client_id` / `client_secret` in the settings table. Done — web SSO works.

> Already done on a sister instance? You can skip the auto-setup and paste an existing `tenant_id` / `client_id` / `client_secret` triple manually — the same App Registration can serve multiple MCP server instances.

### Step 2 — Add a redirect URI for the iOS app *(only if you use the mobile app)*

The iOS app uses Authorization Code Flow with PKCE. Microsoft treats the custom URL scheme `printixmobileprint://` as a **public client**, so it needs an extra platform on the App Registration:

1. Open <https://portal.azure.com> → *Microsoft Entra ID* → *App registrations* → tab **All applications** → search for the Client-ID shown in the MCP web UI (or the name *„Printix Management Console"*).
2. Open the app → *Authentication* → click **+ Add a platform** → choose **Mobile and desktop applications**.
3. In *Custom redirect URIs* enter exactly:

   ```
   printixmobileprint://oauth/callback
   ```

   No `https://`, no trailing slash, no spaces.
4. *Configure* → *Save*. *Allow public client flows* stays **No**.

That's it. The same App Registration now serves all four flows:

| Flow | Used by | Redirect / Mode |
|------|---------|-----------------|
| Auth Code (confidential) | Web UI | `https://<your-host>/auth/entra/callback` |
| Device Code | macOS, Windows clients, admin auto-setup | none — code-based |
| Auth Code + PKCE (public) | iOS *Printix MobilePrint* | `printixmobileprint://oauth/callback` |
| Auth Code (confidential) | Guest-Print mailbox onboarding | `https://<your-host>/admin/guestprint/...` |

### Step 3 — Verify

```bash
# Web SSO: visit https://<your-host>/login → "Sign in with Microsoft"
# iOS: install the TestFlight build, open the app, tap
#      "Sign in with Microsoft" — an in-app Safari sheet should open
#      and return automatically after authentication.
# Server smoketest for the iOS-PKCE endpoint:
curl -sS -X POST https://<your-host>/desktop/auth/entra/authcode/start \
     -d 'device_name=test' \
     -d 'redirect_uri=printixmobileprint://oauth/callback' \
| python3 -m json.tool
# Expected: {session_id, auth_url, state, expires_in: 600}
```

### Common errors

| Symptom | Cause | Fix |
|---------|-------|-----|
| `AADSTS50011 redirect URI mismatch` | URI typo or missing platform entry | Re-check Step 2; URI must match byte-for-byte |
| `AADSTS700025 Client is public, no client_secret allowed` | Mobile redirect on a server that still sends the secret | Already handled in v7.1.4+. Make sure you're on at least 7.1.4 (`docker compose pull && docker compose up -d`) |
| Graph `/me` returns **403 Forbidden** | Access token without `User.Read` permission | Already handled in v7.1.4+. First sign-in per user shows a one-time consent prompt — accept it |
| iOS app shows *„the data couldn't be read"* on tap | Server endpoint not reachable or version too old | Server must be ≥ 7.1.4 *and* expose `/desktop/*` |
| Auto-Setup wizard fails with *„insufficient privileges"* | Signed-in user is not Application Administrator | Use a Global Admin account for the device-code login or have your tenant admin run the wizard once |

### Single tenant vs. multi-tenant App

The auto-setup wizard registers a **single-tenant** App by default (only users from the tenant where the app was created can log in). If you operate the MCP server for multiple Entra tenants, switch the App's *Supported account types* in the Azure Portal to *„Accounts in any organizational directory (Multitenant)"* and set `tenant_id = "common"` in the settings — the rest of the code already handles multi-tenant tokens.

---

## Updates

```bash
# Pull the latest version and restart
docker compose pull
docker compose up -d

# Or pin to a specific tag in .env:
#   PRINTIX_TAG=7.0.0
```

All available tags: <https://github.com/mnimtz/printix-mcp-docker/pkgs/container/printix-mcp-docker>

Updates are safe — all persistent data in the `/data` volume survives the container swap. DB migrations run automatically on startup.

---

## Reverse proxy / Cloudflare Tunnel

Typical internet deployment: a reverse proxy terminates TLS, the container only listens on `127.0.0.1`.

**Traefik example** (add these labels to `docker-compose.yml`):

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

**Cloudflare Tunnel**: run `cloudflared` alongside the container and point the tunnel at `http://printix-mcp:8765` (optionally add a second hostname → `:8080` for the web UI).

In both cases: set `MCP_PUBLIC_URL` in `.env` to the public URL — otherwise OAuth redirects and QR-code links won't line up.

---

## AI-assistant integration

After the first-time setup in the web UI:

- **claude.ai** → *Settings → Integrations → Add MCP Server* → `<MCP_PUBLIC_URL>/mcp`
- **ChatGPT** → MCP via SSE → `<MCP_PUBLIC_URL>/sse`
- **Claude Code (CLI)** → `claude mcp add printix <MCP_PUBLIC_URL>/mcp`

The OAuth endpoints (`/oauth/authorize`, `/oauth/token`) are used automatically by the AI clients — no manual token management needed.

> ⚠️ **After every server upgrade**: refresh the AI assistant's tool list, otherwise it keeps using stale tool definitions. **claude.ai**: start a new conversation or *Settings → Connectors → disconnect / reconnect*. **ChatGPT custom connector**: *Disconnect / Connect* in the Custom GPT editor. **Claude Desktop**: full app restart (`Cmd+Q`). **Cursor / Continue**: toggle the connector or use `/mcp reload`. See [`docs/MCP_MANUAL_EN.md`](docs/MCP_MANUAL_EN.md) for details + the full 127-tool reference.

---

## Troubleshooting

```bash
# Follow the logs
docker compose logs -f printix-mcp

# Container status + health
docker compose ps

# Shell into the container
docker compose exec printix-mcp bash

# Poke at the SQLite DB directly
docker compose exec printix-mcp sqlite3 /data/printix_multi.db '.tables'

# Reset the container completely (⚠️ ALL data is gone)
docker compose down -v
```

**"Permission denied" on bind mount**: see [Bind mount instead of named volume](#bind-mount-instead-of-named-volume) — ownership must be 1000:1000.

**Web UI returns 502 / timeout behind Cloudflare**: `MCP_PUBLIC_URL` must be set so internal redirects use the correct scheme/host.

**Container restarted but login fails**: check that `/data/fernet.key` and `/data/web_session_key` are still there — if the volume was accidentally emptied, they get regenerated and all previously-encrypted secrets become unreadable.

---

## Building locally (developers)

```bash
# Clone + build
git clone https://github.com/mnimtz/printix-mcp-docker.git
cd printix-mcp-docker

# In docker-compose.yml swap image: for build: ., then:
docker compose up --build

# Or directly:
docker build -t printix-mcp-docker:dev .
docker run --rm -p 8080:8080 -p 8765:8765 -v printix-data:/data printix-mcp-docker:dev
```

Multi-arch builds (amd64 + arm64) run in CI via GitHub Actions — see [`.github/workflows/docker-publish.yml`](.github/workflows/docker-publish.yml). armv7 / i386 are no longer built (no pre-built wheels for Python 3.13 on 32-bit ARM) — can be re-enabled in the workflow if anyone needs it.

---

## License & origin

Licensed under the [**Apache License 2.0**](LICENSE) — Copyright © 2026 Marcus Nimtz.

Fork of the HA-add-on code base ([`printix-mcp-addon`](https://github.com/mnimtz/printix-mcp-addon)), stripped of its HA scaffolding and repackaged as a standalone Docker distribution. Both projects keep evolving in parallel — changes in the HA add-on core are ported over when it makes sense.

Maintainer: Marcus Nimtz · `marcus@nimtz.email`
