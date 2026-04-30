# Changelog

This project follows [Semantic Versioning](https://semver.org/).

## 7.6.2 (2026-04-30) — Diagnose Test-Button HTML-Quote-Fix

In v7.6.0 habe ich die Test-Buttons auf URL-Mode umgestellt. Der
`onclick="ssldTestUrl({{ url | tojson }}, this)"` enthielt aber
Doppel-Quotes vom JSON-Filter inside einem doppelt-gequoteten
HTML-Attribut → Attribut wurde frühzeitig terminiert, der Click-
Handler war nie wirklich registriert. Klick → nichts passiert.

Fix: Single-Quotes fürs onclick-Attribut. JSON-Doppel-Quotes drinnen
sind dann valide.

---

## 7.6.1 (2026-04-30) — Suche-Hotfixes + Diagnose 500-Fix

Drei Bugs aus v7.6.0 die direkt aufgefallen sind:

### 1. Karten-Suche fand Karten nie

`/tenant/users` Suche nach Kartennummer ergab IMMER 0 Treffer — auch
direkt nach dem Speichern. Ursache: `save_mapping` schreibt mit
`tenant.get("id")` (DB-interne UUID), die Suche las aber mit
`tenant.get("printix_tenant_id")` (Printix-seitige UUID). Zwei
verschiedene Felder → garantierter Miss seit v5.20.0. Jetzt
konsistent auf `tenant.get("id")`.

Plus erweitert: die Suche scannt jetzt zusätzlich durch die im
Bulk-Prefetch gecachten Karten-Listen — findet damit auch Karten die
nur in Printix existieren (über die Printix-UI angelegt, nicht über
unseren Mapping-Flow).

### 2. Teilsuche bei Usernamen klappte nicht

„cus" fand kein „Marcus", „mar" fand nichts. Ursache: der Printix-
API-`query`-Parameter macht Server-side Prefix-/Wort-Matching und
match keine Substrings in der Mitte eines Namens. Lösung: Cache lädt
jetzt immer die Vollliste (ein Roundtrip alle 10 min, durch den
Prefetch eh schon warm), Filterung läuft lokal mit case-insensitive
Substring-Match auf alle relevanten Felder (fullName, email, sub,
telephone, displayName, first/last_name).

### 3. /admin/ssl/diagnose 500 Internal Server Error

Zwei Fehler:
- `from tunnel import _manager` — den Symbol gibt's nicht, korrekt
  ist `get_manager()`. Dadurch crashte die Diagnose-Route schon vor
  dem Render.
- Template nutzte `test_rows.append(...)` was Jinja's Sandbox als
  None-Call interpretiert. Liste wird jetzt mit `+`-Konkatenation
  aufgebaut.

### Bonus

Karten-Bulk-Prefetch speichert jetzt nicht nur den Count sondern die
volle Karten-Liste pro User. Damit kann die Live-Suche durch alle
Karten des Tenants scannen (siehe Punkt 1) — single round-trip pro
User beim Login warmt alles vor.

---

## 7.6.0 (2026-04-30) — Cache-Prefetch + dynamische Diagnose-Tests

### Hintergrund-Cache wesentlich aggressiver

Beim Login startet weiterhin der Background-Prefetch (existiert seit
v6.2.0), aber ab jetzt deutlich umfassender:

- **Bulk-Cards-Prefetch**: parallel zu allen anderen Topics zieht der
  Prefetch jetzt auch `/users/{id}/cards` für jeden User des Tenants
  (asyncio.gather, ein Round-Trip pro User). Damit zeigt
  `/tenant/users` die Karten-Anzahl-Spalte ohne weitere API-Calls —
  vorher war das eine n+1-Last beim ersten Aufruf.
  Die Zähl-Logik ist in `cache.count_user_cards_robust()`
  zentralisiert und unterstützt alle vier API-Response-Formen
  (`cards`/`content`/`items`/Top-Level-Liste) — gleiche Quelle wie
  der on-demand Pfad, also keine Drift zwischen den Werten
  (Bug-Fix-Erinnerung an v7.2.47).
- **SNMP-Topic** ist neu im Prefetch-Set + `/tenant/snmp` liest jetzt
  über den Cache statt jedesmal live zu pollen.

### Periodischer Refresher

Neuer `start_background_refresher()` Loop läuft alle 60 s im
Event-Loop. Für jeden Tenant der schon einen Login-Prefetch hatte
prüft er ob ein Topic in <60 s ablaufen würde — wenn ja, refresht er
es im Hintergrund. Effekt: nach dem ersten Login wird der Cache nie
mehr stale solange der Server läuft. Klick auf `/tenant/users` ist
jederzeit instant.

### Prefetch-Status-Pille

In der Top-Nav erscheint während des initialen Prefetches eine
kleine ⏳-Pille („Daten werden geladen…"). Sobald `prefetch_status ==
done` springt sie auf grün („Daten geladen") und blendet sich nach
2.4 s aus. User sieht: jetzt kurz warten, dann ist alles flott.

i18n-Keys `prefetch_pill_*` in de/en/no, andere Sprachen über die
EN-Fallback-Kette.

### Diagnose-Test-Buttons jetzt dynamisch

`/admin/ssl/diagnose` testet jetzt nicht mehr die nackte WAN-IP,
sondern in dieser Priorität:

1. **Tunnel-URL** wenn aktiv
2. **public_url-Setting** (Admin → Tenant-Einstellungen)
3. **Public-IP** als Fallback

Plus zwei neue Test-Zeilen:

- **Public-URL Home** — testet das was AI-Clients sehen
- **/health-Endpoint** — bestätigt dass die App selbst gesund antwortet

Port-spezifische Tests bleiben für ACME-/Listener-Diagnose über die
Public-IP. Backend prüft dass die getestete URL aus der Allowlist
(Tunnel-URL · public_url · detected public IP) kommt — Endpoint kann
nicht für SSRF missbraucht werden, auch wenn Admin kompromittiert
wäre.

### API

Neuer Endpoint `GET /api/prefetch-status` — liefert für den
eingeloggten User Status + Topic-Frische (für die Pille).

---

## 7.5.1 (2026-04-30) — Pro-Lock-Badge auf Dashboard-Tiles

Auf dem Admin-Dashboard zeigen jetzt die **Capture**- und **Delegate
Print**-Kacheln ein 🔒-Badge (oben rechts) wenn das Pro-Feature noch
nicht aktiviert ist — gleiche Optik-Sprache wie der Top-Nav-Lock.
Tile bleibt klickbar, der Klick führt wie bisher zur Seite mit dem
Aktivierungs-Hinweis. Hover bringt das Tile etwas zurück (visuelle
Bestätigung dass es interaktiv ist).

Implementation: `feature-card-locked`-Modifier-Klasse mit
Greyscale-Filter + reduzierter Opacity, Schloss-Badge per absolut
positionierter `.feature-lock`-Span. Verwendet `pro_capture_enabled`
und `pro_print_job_mgmt_enabled` aus `t_ctx` — kein neuer Code-Pfad.

## 7.5.0 (2026-04-30) — UX cleanup release

Mehrere zusammengehörige Verbesserungen, die einzeln zu klein für ein
eigenes Release wären, zusammen aber das Erlebnis spürbar aufräumen.

### Setup-Diagnose: Erklärspalte + 1-Klick-Tests

Auf `/admin/ssl/diagnose` hat jede Prüfungszeile jetzt eine zweite
Spalte „Was bedeutet das?" mit konkreten Implikationen je nach Status
(grün/gelb/rot) — kein Raten mehr was die Public-IP-Detection
fehlschlagen lassen würde, oder warum DNS auf eine andere IP zeigen
darf (Tunnel/Proxy davor).

Pro extern-zu-testendem Port (80/443/8080/8765) gibt's einen 🧪 **Test**-
Button neben dem Curl-Snippet. Der Server probiert per NAT-Loopback
gegen die eigene Public-IP an und liefert eine lokalisierte Erklärung
zurück — *Port offen, HTTP antwortet*, *Connection refused — niemand
lauscht*, *Timeout — Firewall oder NAT*, *TLS-Fehler*, etc. Mit
explizitem Caveat dass Heim-Router ohne Hairpin-NAT den Test
fehlschlagen lassen, obwohl der Port von außen erreichbar ist.

Listener-Enumeration nutzt jetzt `/proc/net/tcp(6)` statt `ss`
(Binary war im Slim-Image nicht da → vorher Warnung „No such file or
directory: 'ss'"). Funktioniert jetzt ohne extra Paket.

i18n-Keys in de/en/no, andere Sprachen fallen wie üblich auf EN
zurück.

### Top-Nav aufgeräumt

- „Einstellungen" aus dem Top-Level-Menü entfernt — sitzt jetzt als
  Kachel **„Tenant-Einstellungen"** unter Administration neben
  „Server-Einstellungen". Konsistenter mental model: User-Settings
  zur Tenant-Konfig, Server-Einstellungen für die Maschine. (14
  Sprachen.)
- „Karten & Codes" raus aus der Top-Nav — sitzt jetzt als Tab in der
  Tenant-Sidebar (vor „Demo-Daten"). Seite selbst bleibt unter
  `/cards`, Bookmarks weiterhin gültig.
- Reihenfolge der verbleibenden Top-Nav-Einträge: **Reports** vor
  „Printjob Management" — wer Berichte will, sieht sie zuerst.

### Container-Zeitzone

`docker-compose.yml`, `.env.example` und das README-Compose-Snippet
setzen jetzt `TZ=Europe/Berlin` als Default. Das betrifft `docker logs`
und alle Subprozess-Zeitstempel; die Web-UI-Einstellung „Display
Timezone" bleibt davon unabhängig (retaggt nur die gerenderten
Tabellen). Override via `.env` oder Stack-Env-Var (`TZ=UTC`,
`TZ=America/New_York`, …).

### Other

- Diagnose-Endpoint hardent: Test-Probe akzeptiert nur den im
  vorherigen Diagnose-Run erkannten Public-IP-Wert, nicht beliebige
  Hosts (Defence-in-Depth gegen SSRF-Missbrauch eines admin-gegateten
  Endpunkts).

---

## 7.2.49 (2026-04-30) — SSL & Domain hub + setup diagnostics

### Changed

The admin dashboard's three HTTPS-related buttons (🌐 HTTPS Tunnel,
🔒 TLS Certificate, 🌍 Free HTTPS) are consolidated into a single
**🌐 SSL & Domain** entry that opens an overview hub. Less visual
clutter on `/admin`, all related decisions in one place.

### Added — `/admin/ssl` overview hub

Three status tiles, side-by-side, each with:

- Icon + name + active/inactive indicator (green dot when running,
  grey when off)
- One-line description of the option
- Live data: tunnel URL when active, cert expiry + days remaining,
  sslip.io hostname when configured
- Direct deep-link to the detail config page

The hub also surfaces the **currently active public URL** as a green
banner at the top — admin sees at a glance whether HTTPS is up
without scanning each tile, and which strategy provides it.

A collapsible "Which option fits me?" decision-tree section explains
when each strategy makes sense.

### Added — `/admin/ssl/diagnose` setup diagnostics

A pre-flight check that examines the network conditions from inside
the container and recommends a concrete strategy based on what it
finds:

**Server-side checks**:
- Public IP detection (api.ipify.org)
- Suggested sslip.io hostname
- Outbound reachability to Cloudflare API
- Outbound reachability to Let's Encrypt ACME directory
- Container-internal listeners (ss -tlnH)
- DNS resolution of the configured public_url
- Whether the public_url DNS matches the public IP

**External-test recipes**: copy-paste curl commands for the admin to
run from a laptop/phone (not the container) to confirm whether ports
80, 443, 8080, 8765 are actually reachable from the internet — the
only way to catch Azure NSG / firewall gaps reliably.

**Recommendation**: based on the results, the page suggests one of
the three HTTPS strategies with a one-paragraph rationale and a
direct setup link. Examples:

- Public IP detected + Let's Encrypt reachable + outbound OK
  → recommend Auto-HTTPS (sslip.io)
- Outbound to Cloudflare OK but no public IP → recommend Tunnel
- Limited outbound → recommend manual cert import

### Implementation

- `src/web/app.py` — two new routes: `/admin/ssl` (status hub) and
  `/admin/ssl/diagnose` (live network checks). Both admin-only.
- `src/web/templates/admin_ssl.html` (new) — three-tile grid with
  hover lift + status indicators + decision helper.
- `src/web/templates/admin_ssl_diagnose.html` (new) — three-section
  layout (server-side checks / external-test recipes /
  recommendation), copy-buttons on every command.
- `src/web/templates/admin_dashboard.html` — three button rows
  collapsed into one.
- i18n: 24 new keys (`ssl_*` + `ssld_*`) per language in `de`,
  `en`, `no`.

## 7.2.48 (2026-04-29) — Display timezone configurable from the web UI

### Added

A new "🕐 Anzeige-Zeitzone / Display Timezone" card on
`/admin/settings#timezone`. Container internals stay in UTC (best
practice for storage), but the **display** of timestamps in the web
UI and the logs page is now per-installation configurable without a
container restart.

### How it works

- New DB setting `display_timezone` (default empty → falls back to env
  var `TZ`, then to `Europe/Berlin`).
- Central helpers `_resolve_display_tz_name()` /
  `_resolve_display_tz()` in `web/app.py` — used by the `/logs`
  route and the new Jinja filter.
- New Jinja filter `{{ ts | localtime }}` that converts UTC ISO
  strings (or datetime objects) to the configured display timezone
  with format `YYYY-MM-DD HH:MM:SS TZ`. Templates can now use
  `{{ entry.created_at | localtime }}` instead of repeating the
  conversion logic inline.
- POST handler validates the IANA zone via `zoneinfo.ZoneInfo(name)`
  and rejects unknown values with a clear error.
- After saving, calls `os.environ["TZ"] = name; time.tzset()` —
  affects subsequent `%(asctime)s` log records in the **web process**
  (port 8080) immediately. The MCP server process (port 8765) is
  separate and requires a container restart to pick up the change.

### UI

The card shows two side-by-side panels:
- **Server time (UTC)** — the absolute internal timestamp
- **Display time** — the same moment in the configured zone

A typeahead input with HTML5 `<datalist>` autocompletes ~40 common
zones (Europe/*, America/*, Asia/*, Australia/*, etc.). A "🌍
Browser erkennen" button reads
`Intl.DateTimeFormat().resolvedOptions().timeZone` and fills the
input with the user's local TZ. A yellow info panel below explains
the scope of the change (immediate for web, restart needed for the
MCP-server process, optional `TZ` env var for full effect from
startup).

### i18n
Twelve new keys (`tz_*`) per language in `de`, `en`, `no`.

### Files
- `src/web/app.py` — helpers + POST handler + ctx
- `src/web/templates/admin_settings.html` — new card + JS detection
- `src/web/i18n.py` — translations

## 7.2.47 (2026-04-29) — Hotfix: Cards column always shows 0 in Users & Cards list

### Fixed

The "CARDS" column on `/tenant/users` (Printix Management → Users & Cards)
showed 0 for every user even when cards existed in Printix. Root cause:
`_load_card_counts_parallel()` extracted the card list from
`list_user_cards()` response as:

```python
raw = data.get("cards", data.get("content", [])) if isinstance(data, dict) else []
```

This handled two response shapes (`{"cards": [...]}` and
`{"content": [...]}`), but the Printix API can return up to four
shapes depending on tenant/version:

1. `{"cards": [...]}` ✓
2. `{"content": [...]}` ✓
3. `{"items": [...]}` ✗ — fell through to `[]`
4. Top-level list `[{...}]` ✗ — `isinstance(data, dict)` was False, fell through to `[]`

The MCP server's `_card_items()` helper in `server.py` had handled all
four shapes for years. The web UI duplicated a stripped-down version
when card-count parallelisation was added in v6.0.0.

### Fix

`_count()` inside `_load_card_counts_parallel()` now mirrors the
`_card_items()` logic and counts only dict entries:

```python
if isinstance(data, list):
    raw = data
elif isinstance(data, dict):
    raw = data.get("cards") or data.get("content") or data.get("items") or []
else:
    raw = []
n = sum(1 for c in raw if isinstance(c, dict))
```

### After upgrade

The cached counts from the broken version may still display "0" for
about 15 minutes (TTL of the per-user cards cache). To force an
immediate refresh: click the **Refresh** button on
`/tenant/users` — that flushes the cards cache.

## 7.2.46 (2026-04-29) — README: network architecture + proxy bypass recipes

### Documentation

Added a "Network architecture (v7.2.43+)" section to README.md
covering:

- The two-listener architecture (8080 web + 8765 mcp) and what the
  proxy on 8080 routes internally to 8765
- A table of which deployment setups go through the proxy vs.
  bypass it (Cloudflare Quick Tunnel, Named Tunnel with path-routing,
  Auto-TLS sslip.io, manual TLS-Import, reverse proxy, direct 8765)
- Performance trade-off (~1–2 ms extra latency per call via proxy,
  below 1% for typical 50–500 ms MCP tool execution times)
- Three concrete bypass recipes:
  1. Cloudflare Named Tunnel with path-based routing (5 public-hostname
     entries to send /mcp, /sse, /oauth, /.well-known to 8765 and the
     rest to 8080)
  2. Traefik/nginx with PathPrefix rules
  3. External TLS terminator in front of port 8765

The previous "Reverse proxy / advanced setups" section is rewritten
as "Reverse proxy — manual setups" and refers back to the bypass
recipes for higher-throughput deployments. The simple single-backend
setup is still documented for users who don't need to optimise.

This release is documentation-only — no code changes.

## 7.2.45 (2026-04-29) — Server-Info: drop misleading "port 8765" direct URL

### Fixed UX

The Server-Info card on `/admin` previously listed two MCP URLs:

- "MCP URL via öffentliche URL" → tunnel-style `/mcp`
- "MCP Direkt (mit Port): 8765" → `https://host:8765/mcp`

The second one was misleading: port 8765 only speaks plain HTTP, and
most cloud NSGs / firewalls don't expose it. Customers tried it,
got `Connection refused` or `ERR_SSL_PROTOCOL_ERROR`, and assumed the
proxy was broken. Since v7.2.43 the proxy on the web port handles
`/mcp` internally, the "direct" row added noise without value.

Removed. The Server-Info card now shows just one clear path:

```
🤖 MCP server (for AI assistants)
   The built-in proxy forwards /mcp, /sse, /oauth, /.well-known
   internally to the MCP server port — you only need the web port
   reachable from outside.

   MCP URL:   <base>/mcp
   SSE URL:   <base>/sse
```

Cleaner, no misleading port-8765 references. New i18n keys
`admin_si_mcp_hint_v2` and `admin_si_mcp_url_hint_v2` in de/en/no.

## 7.2.44 (2026-04-29) — Auto-TLS public_url now includes the web port

### Fixed

When Auto-TLS (sslip.io + Let's Encrypt) finished successfully, it
saved the `public_url` setting as `https://<ip-dashed>.sslip.io`
without a port suffix. That URL implies port 443 — but uvicorn binds
the cert on port 8080 (the WEB_PORT). Result: every Connect-Center
link, every OAuth redirect, every MCP-tool URL pointed at port 443
which is typically blocked by Azure NSG / cloud firewall (only
required port 80 was opened earlier for the ACME challenge).

User-visible symptom: `https://20-52-1-199.sslip.io/health` → timeout,
but `https://20-52-1-199.sslip.io:8080/health` → works.

### Fix

`acme_auto.py:request_cert()` now appends the WEB_PORT to the saved
`public_url` whenever it isn't 443:

```python
web_port = os.environ.get("WEB_PORT", "8080")
suffix   = "" if web_port == "443" else f":{web_port}"
set_setting("public_url", f"https://{hostname}{suffix}")
```

After this fix, fresh Auto-TLS activations save `public_url` as
`https://<hash>.sslip.io:8080`, and every link in the UI lands on the
right port.

### Heal an already-broken install

If you already activated Auto-TLS on v7.2.36–7.2.43, your
`public_url` is wrong. Either re-activate (deactivate + re-activate
Auto-TLS in the UI) or fix it directly:

```bash
docker exec printix-mcp sqlite3 /data/printix_multi.db \
  "UPDATE settings SET value='https://20-52-1-199.sslip.io:8080' \
   WHERE key='public_url';"
```

Replace the hostname with your own. Then refresh `/my/connect` — the
copyable URLs should now include `:8080`.

## 7.2.43 (2026-04-29) — Hotfix: MCP-proxy was not streaming → "Empty reply from server"

### Fixed

The v7.2.42 proxy used a non-streaming HTTP client for `/mcp` and
`/oauth`, but the MCP Streamable-HTTP transport returns an SSE-style
event-stream response for `GET /mcp`. The proxy hung waiting for the
end of the stream (300 s default timeout) and either timed out or
closed the connection without forwarding any bytes — `curl` saw
"Empty reply from server".

### Fix

All four proxy families (`/mcp`, `/sse`, `/oauth`, `/.well-known`)
now use `httpx`'s streaming API:

```python
req  = client.build_request(method, target, ...)
resp = await client.send(req, stream=True)   # ← streaming
return StreamingResponse(_aiter_raw(resp), status_code=resp.status_code,
                         headers=resp.headers,
                         media_type=resp.headers["content-type"])
```

This works for both:
- Immediate request/response (POST /mcp with JSON body) — single
  chunk, streamed but small
- Long-lived SSE streams (GET /mcp, GET /sse) — chunks as they arrive

The upstream status code, headers and content-type are passed
through faithfully (previously SSE was hardcoded to 200 +
text/event-stream — wrong for OAuth responses).

### Verification

```bash
curl -v http://localhost:8080/mcp
# v7.2.42: "Empty reply from server" — bug
# v7.2.43: returns the MCP server's response (auth challenge, etc.)
```

Through Cloudflare Quick Tunnel:

```bash
curl -v https://*.trycloudflare.com/mcp
# v7.2.43: same response as direct access
```

claude.ai connector setup completes via the tunnel URL end-to-end.

## 7.2.42 (2026-04-29) — MCP-proxy on web port (Quick Tunnel single-URL fix)

### Fixed

**Cloudflare Quick Tunnel returned `{"detail":"Not Found"}` on `/mcp`.**
Quick Tunnel forwards all traffic to a single internal port — by
default the web UI on 8080 — but the MCP server runs on a separate
port (8765). When claude.ai or a browser hit
`https://….trycloudflare.com/mcp`, the request landed on the web UI's
FastAPI app, which has no `/mcp` route, hence 404.

### Fix

The web UI on port 8080 now proxies four families of paths internally
to the MCP server on port 8765:

- `/mcp` and `/mcp/*` — Streamable HTTP transport (claude.ai, Claude Code)
- `/sse` and `/sse/*` — Server-Sent Events (ChatGPT-style connectors)
- `/oauth/*` — OAuth Authorize, Token, callbacks
- `/.well-known/*` — RFC-compliant discovery (oauth-authorization-server, etc.)

The proxy preserves request method, headers (including the bearer
token), query parameters, and body. SSE traffic uses `httpx.stream`
+ `StreamingResponse` so long-lived event streams remain functional
through the proxy.

A single Cloudflare Tunnel URL (Quick or Named) now works end-to-end
for both the admin web UI and AI-assistant MCP traffic — no separate
hostnames or path-based routing required on the Cloudflare side.

### Implementation

- `src/web/app.py` — four proxy routes mounted near the bottom of
  `create_app()` (so they don't shadow more specific routes).
  Internal `_proxy_to_mcp()` helper wraps `httpx.AsyncClient`, reads
  `MCP_PORT` env var (default 8765), strips hop-by-hop headers, and
  passes everything else through.
- `requirements.txt` — `httpx>=0.25.0` added explicitly (was a
  transitive dep, now a direct one).
- Auth: no checks on port 8080 for the proxied paths — the existing
  `BearerAuthMiddleware` and `OAuthMiddleware` on port 8765 still
  apply, so security model is unchanged.

### Test

After the upgrade:

```bash
curl https://your-tunnel.trycloudflare.com/mcp
# Should now return MCP server's JSON-RPC error or 401, not 404
```

In Claude.ai → Settings → Connectors, paste the tunnel URL — the
OAuth-based handshake completes via the same URL.

## 7.2.41 (2026-04-29) — Pro feature gating extended: Print Job Management (/my)

### Fixed/Added

The Pro feature `print_job_mgmt` previously had no actual gate — it was a
"reserved license slot" but didn't lock anything. Customer expectation
is that the German UI label "Printjob Management" (which is what the
employee portal `/my` is called in `nav_my_portal`) IS the gated feature.

This release wires up the gate properly.

### Two-layer enforcement

**Layer 1 — Login gate.** Non-admin users (`is_admin=False`) cannot log
into the web UI when `print_job_mgmt` is locked. The login form rejects
their credentials with the explicit message:
> *"Anmeldung für normale Benutzer ist auf dieser Installation nicht
> aktiviert (Free Tier). Bitte deinen Administrator um Aktivierung des
> Pro-Features 'Print Job Management'."*

This matches the customer's mental model: in Free Tier, regular users
exist as **MCP-permission subjects** (RBAC roles, tool access), but the
web UI is admin-only. Pro Tier unlocks employee self-service.

**Layer 2 — Route gate.** If somehow an employee already has a session
(e.g. license was active and got revoked), the `/my` and `/my/jobs`
routes return the standard `feature_locked.html` page with the
"Printjob Management" badge and description.

**Nav-link masking.** The `/my` link in the desktop and mobile nav-bars
shows a 🔒 prefix and dimmed colour when the feature is inactive, same
pattern as `/capture` and `/guestprint`.

### Implementation

- `employee_routes.py` — new `_printjob_locked_response()` helper, gates
  `/my` and `/my/jobs`. (Sub-routes like `/my/upload`, `/my/delegation`
  are reachable only via /my, so gating the entry points covers them
  for normal navigation. Bookmark deep-links would still reach the
  raw routes — these can be gated incrementally if needed.)
- `web/app.py:login_post()` — pre-session check for non-admin login
  blocked when feature locked.
- `base.html` — nav-link masking like the other Pro features.
- New i18n key `login_employee_locked` in `de`, `en`, `no`.

## 7.2.40 (2026-04-29) — Hotfix: backup restore fails with EXDEV cross-device error

### Fixed

**Backup restore broke with `[Errno 18] Invalid cross-device link`** in
the Docker image. Root cause: `_restore_to_target()` used
`os.replace(extracted_file, target)` to move the extracted backup
artefacts from `/tmp/printix-restore-…` into `/data/`. In Docker `/tmp`
is a tmpfs/overlayfs and `/data` is a mounted volume — different
file systems, and the Linux kernel allows `rename()` only within one
filesystem. The bug went unnoticed in single-fs dev environments.

### Fix

Two-step copy via a staging file in the target directory:

1. `shutil.copy2(extracted_file, target.with_suffix(".restore-staging"))`
   — same filesystem as the target, no EXDEV.
2. `os.replace(staging, target)` — atomic rename on the target
   filesystem.

Atomic replace matters for SQLite restores, so concurrent readers
never see a half-written DB file. The staging file is cleaned up on
any error path.

### Verification

After the upgrade: open `/admin/settings → Backup & restore`,
upload a previously created backup ZIP, click Restore. The error
panel that previously showed "Errno 18 / Invalid cross-device link"
should now show success.

## 7.2.39 (2026-04-29) — Pro features with activation codes (Basic/Pro tier)

### Added

**Two-tier feature model** — Basic ships free for everyone, Pro features
gate three operational admin pages behind an activation code. Once a
code is entered under *Server settings → Pro features*, the unlocked
features stay unlocked for the lifetime of the installation.

The MCP tools (Claude/ChatGPT side) and Printix webhook endpoints
remain functional regardless of license — only the human-facing admin
pages are gated. This keeps the AI integration unrestricted while
giving customers a clear upgrade path for operational features.

### Pro features (gated)

| Feature | Web route | Why gated |
|---------|-----------|-----------|
| 📥 Capture Store | `/capture/*` | Document capture with webhook profiles, indexing, third-party routing |
| 📮 Guest-Print | `/guestprint/*` | Email-based guest-print mailboxes with approval workflow |
| 🖨️ Print Job Management | (reserved) | Bulk-action admin UI for upcoming releases |

Webhook endpoints (`/capture/webhook/{id}`, etc.) remain accessible —
gating these would break the data flow from Printix and create silent
data loss when a license expires.

### Implementation

- **`src/license.py`** (new, ~180 lines) — Stage-A hash-based codes:
  SHA-256 of `<SECRET>|<feature>` truncated to 12 hex chars, uppercase.
  A master code (`*all*`) unlocks everything at once. Persisted state
  lives in the `pro_features` setting (JSON list of active feature
  IDs).
- **`/admin/settings/license/activate`** + `/deactivate` POST handlers,
  flash messages on success/failure, audit log entries
  (`license_activated` / `license_deactivated`).
- **Per-route gates** in `capture_routes.py` and `guestprint_routes.py`
  that render `feature_locked.html` (with the per-feature icon, label
  and description in the user's language) instead of the normal page
  when the feature is inactive.
- **Nav-link masking** in `base.html` — locked features show a 🔒
  prefix and are dimmed; click still navigates to the lock page so
  the customer can read what's behind it.
- **`feature_locked.html`** template with a clear CTA pointing to the
  settings page, plus the standard "ask your contact person for the
  code" message.
- **`bin/generate-license-codes.py`** — CLI tool for the operator to
  re-derive the codes when needed (extracts the secret from
  `src/license.py`, prints the four codes).

### UI

The license card sits at the **top of `/admin/settings`**, before
all the existing settings, with a green border. It shows:

- A list of all three Pro features with their current state (✓ active
  / 🔒 locked) and a one-line description
- A single text field for the activation code
- "Aktivieren" button — green, prominent
- Per-feature "Deactivate" button when active (so the admin can
  manually revoke if a license expires)
- Success message: *"Code erfolgreich aktiviert — Neu freigeschaltet: …"*
- Failure message: *"Ungültiger Code. Fragen Sie Ihren Ansprechpartner
  nach dem Freischaltcode."*

### README

Feature overview rewritten with two clear sections:
- **🟢 Basic — included for everyone** — all the existing features
  (MCP tools, admin UI, dashboard, reports, RBAC, HTTPS options,
  etc.)
- **💎 Pro — activation code required** — Capture Store, Guest-Print,
  Print Job Management. Each with one-line description and a note
  that the MCP tools / webhooks remain functional regardless.

### i18n

Eighteen new keys (`lic_*`) per language for activation flow,
locked-feature page, status messages. `de` / `en` / `no`.

## 7.2.38 (2026-04-29) — RBAC UI toggle (no compose / restart needed) + comprehensive README

### Added

**Inline RBAC enable/disable button** on `/admin/mcp-permissions`.

The previous activation flow required editing `docker-compose.yml` to
set `MCP_RBAC_ENABLED=1` and restarting the container — which is
awkward for users running the container via Portainer / `docker run`
without a tracked compose file. The new toggle is right next to the
status banner: green "Enable" when off, red "Disable" when on.

Activation is now read live from a DB setting (`rbac_enabled`), so
flipping the toggle takes effect immediately on the next tool call —
no container restart, no compose edit. The env var
`MCP_RBAC_ENABLED` continues to work as the **initial default** for
fresh installations; the DB setting takes precedence once the admin
has used the UI toggle at least once.

The status banner now also shows the source of truth ("via env var"
vs "via UI toggle") so the admin understands why the value is what
it is and what they can change.

### Changed

- `_check_tool_permission()` in `server.py` now calls
  `_is_rbac_enabled()` per call (cheap DB lookup) rather than caching
  the env var at import time.
- `printix_my_role` reports the live state.
- New i18n keys for the toggle button, confirm dialog and source
  notice in `de` / `en` / `no`.

### Documentation

**README.md** rewritten with three new sections:

- **HTTPS — three built-in options** (Cloudflare Tunnel, Auto-HTTPS
  via sslip.io + Let's Encrypt, Bring-your-own-certificate) with a
  comparison table
- **Role-based access control** with the role/scope mapping, the two
  assignment paths, and the three activation methods
- **Bundled compliance documentation** (GDPR Compliance Guide +
  Permission Matrix PDFs)

The previous "Reverse proxy / Cloudflare Tunnel" section is renamed
to "Reverse proxy / advanced setups" and now positions itself as the
fallback for users who prefer Traefik/nginx/Caddy over the built-in
HTTPS options.

## 7.2.37 (2026-04-29) — Server-Info: OAuth credentials + MCP port URL on the dashboard

### Changed
The "Server Info" card on `/admin` now shows everything an admin needs
to wire up claude.ai or ChatGPT, without leaving the dashboard:

**Three logical blocks** (was previously one flat table):

- **🖥️ Web management UI** — admin URL + healthcheck endpoint
- **🤖 MCP server** — both the public-tunnel-style URL
  (`https://your-host/mcp`) AND the direct port URL
  (`https://your-host:8765/mcp`) with explanatory hints about which
  to use when. The previous version only showed the tunnel-style
  URL; users hitting Cloudflare Tunnel without /mcp path-routing
  would silently get 404s without realising the MCP server is on
  a separate port.
- **🔑 OAuth credentials** — Authorize/Token URLs plus the **OAuth
  Client ID and Client Secret** with the same reveal-toggle the
  Connect-Center already uses. Saves a click for the most common
  ChatGPT-connector setup flow.

**Tunnel status banner** — when a Cloudflare Tunnel is active, a
green banner at the top of the Server Info card surfaces the live
public URL. Eliminates "is my tunnel even running?" confusion.

### Implementation

- `admin_dashboard` route now also loads the owner-tenant record
  (with the `parent_user_id` fallback we already use elsewhere)
  and computes both the tunnel-style and direct-port MCP URLs.
- Template rewritten with three subsection headings, copy-buttons
  on every value, and the standard reveal-toggle on the secret.
- Single source of truth: same `cc_reveal` / `cc_hide` translation
  keys as the Connect-Center, same `toggleSecret()` JS pattern.

### i18n
Eighteen new keys per language for the new sections, hints, and
tunnel-active banner. Localised in `de` / `en` / `no`.

## 7.2.36 (2026-04-29) — 1-click free HTTPS for IP-only setups (sslip.io + Let's Encrypt)

### Added

**`/admin/auto-tls`** — fully automatic HTTPS for users with a fixed
public IP and no domain. One click, no account, no DNS configuration,
no manual certbot. Targets the Azure-VM-with-public-IP scenario where
neither Cloudflare Tunnel (no domain) nor manual cert generation is
appealing.

What happens behind the click:

1. Public IP detected via `api.ipify.org`
2. sslip.io hostname generated (`52-143-121-45.sslip.io`) — sslip.io
   is a free wildcard DNS service that maps any IPv4 to a hostname,
   no account or signup needed
3. `certbot certonly --standalone` runs ACME HTTP-01 challenge against
   that hostname (port 80 opens for ~30 s during the challenge)
4. Cert + key copied to `/data/tls/cert.pem` and `/data/tls/key.pem`
5. `tls_enabled=1` set, `public_url` updated
6. Container restart needed → uvicorn picks up the new cert and the
   web UI is on HTTPS

**Auto-renewal** runs as a daemon thread inside the web process,
waking daily and invoking `certbot renew`. Idempotent — only acts
when the cert has <30 days remaining. No cron setup required.

### UI

The `/admin/auto-tls` page is the prominent green "Free HTTPS"
option on the admin dashboard, alongside the existing tunnel and
manual TLS pages. It shows:

- Auto-detected public IP and the resulting sslip.io hostname
- A short pitch describing when this option is the right choice
  (fixed IP, no domain, no third-party-routing requirement)
- One email field + one big green "Set up free HTTPS" button
- After setup: status banner with cert details, days remaining,
  manual renewal trigger, and a "How does this work?" details panel
  documenting all five technical steps

### Implementation

- `src/acme_auto.py` (new, ~250 lines) — IP detection, hostname
  generation, certbot subprocess wrapper, renewal scheduler thread,
  status helper.
- Three new admin routes (status page, request, manual renew).
- New template `admin_auto_tls.html`.
- Bundled `certbot` in the Docker image (~30 MB additional).
- `docker-compose.yml` exposes port 80 by default with a comment
  explaining it's only required during the ~30 s challenge.
- Renewal scheduler started at web-app boot via daemon thread.
- Audit log records `auto_tls_acquired` and `auto_tls_renewed`.

### Three HTTPS options now coexist

The admin dashboard offers three independent paths to HTTPS:

| Option | Best for | Domain required | Auto-renew |
|--------|----------|-----------------|------------|
| 🌐 HTTPS Tunnel (Cloudflare) | most users | yes (free if existing) | yes |
| 🔒 TLS Certificate Import | own CA / commercial cert | yes | no (manual) |
| 🌍 Free HTTPS (sslip.io+LE) | fixed IP, no domain | **no** | **yes** |

### i18n
Full translations for all auto-TLS strings in `de` / `en` / `no`.

## 7.2.35 (2026-04-29) — Bring-your-own-cert: TLS import + tunnel-page wording fix

### Added
**`/admin/tls`** — second native HTTPS option alongside Cloudflare
Tunnel. Operators who already have a TLS certificate (commercial CA,
internal PKI, Let's Encrypt via certbot, …) can paste the PEM cert
chain plus matching private key directly into the admin UI. The web
UI then runs on HTTPS without any reverse proxy, sidecar container,
or third-party routing.

Features:

- **Cert validation on save** — both the certificate and the key are
  parsed via the `cryptography` library before persisting; mismatched
  cert/key pairs are rejected with a clear error message instead of
  failing silently when uvicorn restarts.
- **Live cert details panel** — subject, issuer, validity window,
  Subject Alternative Names, and days remaining are displayed.
  Three status banners: green (valid), amber (≤30 days remaining,
  renew soon), red (expired).
- **Stored under `/data/tls/`** — `cert.pem` (mode 0644) and
  `key.pem` (mode 0600). Lives in the same encrypted data volume
  as all other secrets.
- **Audit log** — `tls_cert_uploaded` and `tls_cert_disabled`
  actions with the cert subject for traceability.
- **Restart needed** to take effect — `web/run.py` reads the
  `tls_enabled` setting at start time and passes
  `ssl_certfile` / `ssl_keyfile` to uvicorn. Clear notice in the
  UI explaining this.

### Caveats panel (in-UI)
- Manual renewal — for auto-renewal, Cloudflare Tunnel or a
  reverse-proxy sidecar (Caddy, Traefik) is the better choice.
- Web UI port (8080) only — MCP server (8765) and IPP listener have
  their own TLS configs; Cloudflare Tunnel covers all in one go.

### Fixed
**Misleading currency mention in the Cloudflare wizard step 2.**
The previous wording made it look like the Cloudflare account costs
~10€/year, when in fact the account itself is free and only an
optional new domain purchase costs anything. Step 2 now leads with
"the Cloudflare service is free; you only pay for a domain name if
you don't have one" and reorders the three options so the cheapest
("subdomain CNAME of an existing domain") comes first.

### i18n
Full translations of the new TLS page and the corrected step 2
wording in `de` / `en` / `no`.

### Admin dashboard
New "🔒 TLS Certificate" button next to the "🌐 HTTPS Tunnel" button.

## 7.2.34 (2026-04-29) — Tunnel page: detailed in-line setup wizard

### Changed

The Step-by-step wizard on `/admin/tunnel` is now a complete walkthrough,
not a placeholder. Each of the six steps is rendered as its own
numbered card with a title and a body that includes:

- Step 1 — direct link to Cloudflare sign-up
- Step 2 — three concrete domain options (move existing, buy new at
  ~10€/year, or just delegate a subdomain via CNAME) so the operator
  knows what to do regardless of their starting point
- Step 3 — exact click sequence in the Zero Trust dashboard
- Step 4 — explicit warning that the cloudflared install commands
  shown by Cloudflare must NOT be executed (we handle that internally)
  but the embedded token must be copied
- Step 5 — a small configuration table with the four required field
  values (Subdomain, Domain, Service Type, URL) so the user can't
  mistype the localhost:8080 service URL
- Step 6 — explicit hand-off to the form below ("paste the token from
  step 4 and the subdomain from step 5"), highlighted in a
  contrasting green block to mark the end of the wizard

### Translations
Six new key pairs (`tn_step1_title` / `tn_step1_body` through
`tn_step6_title` / `tn_step6_body`) in `de` / `en` / `no`. Localised
domain price examples per region (10€ for de, $10 for en, 100kr for no).

### Visual
Each step is a flex-row with a green numbered avatar on the left and
the title/body on the right. The 6th step uses a darker green
background and a thicker border to mark the transition from "go to
Cloudflare" to "come back here and submit the form".

## 7.2.33 (2026-04-29) — Tunnel page UX: Named Tunnel as primary, Quick Tunnel as advanced

### Changed

The first cut of the tunnel page presented Quick Tunnel and Named
Tunnel as equal options, which mis-priced them. Quick Tunnel's URL
changes on every container restart, so it cannot be registered
permanently in claude.ai or ChatGPT — it is only useful for a
30-second smoke test. Named Tunnel is the path 99% of users want.

The page now reflects that:

- **Named Tunnel section is the prominent default**, green-bordered,
  marked with a **RECOMMENDED** badge.
- A built-in **5-step setup wizard** (collapsible, open by default
  on first visit) walks the admin from "create a Cloudflare account"
  through "paste the token here" with direct deep-links to:
  - Cloudflare sign-up
  - Cloudflare Zero Trust dashboard
- The Named Tunnel form fields are now numbered (1. token, 2. domain)
  to match the wizard.
- The submit button reads **"Connect"** instead of "Start Named
  Tunnel" — clearer in the context of an already-numbered flow.
- **Quick Tunnel is moved into a collapsible details section**
  near the bottom, summary line "30-second tests only — URL changes
  on every restart". Inside, a yellow warning panel reiterates that
  the URL is anonymous and changes on restart, so it cannot be
  registered permanently in claude.ai/ChatGPT.

### Translations
Five new keys added across `de` / `en` / `no`:
`tn_named_sub_v2`, `tn_named_badge_recommended`, `tn_named_wizard`,
`tn_named_step1` through `tn_named_step5`, `tn_status_off_sub_v2`,
`tn_quick_summary`, `tn_quick_warning_title`, `tn_quick_warning_body`.

### No backend changes
The tunnel manager itself (subprocess control, persistence, audit
log) is unchanged from v7.2.32. This release is pure UX.

## 7.2.32 (2026-04-29) — Built-in Cloudflare Tunnel manager: one-click HTTPS

### Added

A complete in-app tunnel manager so users with no domain, no Public IP,
or behind NAT can expose the MCP server to claude.ai / ChatGPT in
under a minute. Bundled `cloudflared` binary; managed as a subprocess
inside the existing container. No additional service required.

### Two modes

**Quick Tunnel** — for testing and demos.

- One click → cloudflared launches → random `*.trycloudflare.com`
  URL is captured from the cloudflared output → URL written into the
  `public_url` setting → Connect-Center automatically reflects the
  new URL → ready to paste into claude.ai.
- No Cloudflare account, no DNS, no token. Free.
- URL changes on container restart; not for production use.

**Named Tunnel** — for production.

- Admin enters a Cloudflare tunnel token (from the free Zero Trust
  dashboard) and the public hostname they configured there.
- Token is Fernet-encrypted before persisting (same pattern as
  Printix client secrets and OAuth secrets).
- Persistent URL with full Cloudflare DDoS/bot protection.
- Auto-restarts on container reboot from the persisted settings.

### Implementation

- New module `src/tunnel.py` — `TunnelManager` singleton manages the
  cloudflared subprocess, parses the trycloudflare.com URL out of
  stdout, ring-buffer of last 30 log lines for the admin UI,
  thread-safe with a single lock.
- New routes in `src/web/app.py`:
  - `GET /admin/tunnel` — full status page with Quick / Named forms
  - `GET /admin/tunnel/status` — JSON for live polling
  - `POST /admin/tunnel/start-quick` — anonymous Quick Tunnel
  - `POST /admin/tunnel/start-named` — token-based Named Tunnel
  - `POST /admin/tunnel/stop` — terminate cloudflared
- Auto-start at web-app boot via `auto_start_from_settings()` in a
  daemon thread (so a slow Cloudflare endpoint doesn't block the
  rest of the web UI from coming up).
- New template `admin_tunnel.html` with traffic-light status banner,
  copy-button on the live URL, collapsible cloudflared log pane,
  and auto-refresh while the URL is being detected.
- Admin dashboard gets a new "🌐 HTTPS Tunnel" button.
- Audit log records `tunnel_start_quick`, `tunnel_start_named`,
  and `tunnel_stop` with the calling admin and the resulting URL.

### Dockerfile

`cloudflared` is added in the runtime stage with multi-arch support
(amd64, arm64, arm, 386). The binary is downloaded from the official
GitHub release URL and verified by a `--version` invocation at build
time, so a broken release blocks the build instead of producing a
silently broken image. Adds approximately 30 MB to the image size.

### i18n

Full translation set (de / en / no) for all tunnel-related strings —
status banner, two mode descriptions, token input hint with a link to
Cloudflare's setup guide, action buttons, log expander label.

### README

New "One-click HTTPS via Cloudflare Tunnel" feature section pointing
operators at `/admin/tunnel` for VM deployments without DNS.

## 7.2.31 (2026-04-29) — Health & status endpoints on the web UI port

### Added

The MCP server has had a `/health` endpoint on port 8765 since the
beginning, but the web UI (port 8080) had no equivalent — Docker
healthcheck, Cloudflare Tunnel, Pingdom and similar uptime probes
that target the web port had nothing to hit. Two new endpoints close
the gap.

**`GET /health`** — JSON status for monitoring tools.

```json
{
  "status": "ok",
  "service": "printix-mcp-web",
  "version": "7.2.31",
  "checks": {
    "db": "ok",
    "tenant": "configured",
    "rbac_enabled": true
  },
  "timestamp": 1746104730.12
}
```

Returns HTTP 200 when all critical checks pass, HTTP 503 when the
SQLite database is unreachable. No login required so that uptime
probes don't need credentials.

**`GET /status`** — pretty HTML dashboard for browsers.

Renders a clean version-stamped panel with traffic-light style
indicators (✓/⚠/✕/ℹ) for:

- DB connection
- Printix tenant configuration
- MCP RBAC mode (active vs pass-through)

Also no login required, but deliberately shows no tenant data —
just system health.

### Use cases

- **Docker Compose healthcheck** can now also point at port 8080
  (currently it targets 8765/health):
  ```yaml
  test: ["CMD", "curl", "-fsS", "http://127.0.0.1:8080/health"]
  ```
- **Cloudflare Tunnel** /  reverse proxy can route a public
  `https://mcp.example.com/status` to the container without exposing
  any login or admin surface.
- **Operational checks** when something feels off: open `/status`
  in a browser to confirm the web UI process is alive and the DB is
  reachable, before drilling into the admin area.

### Internal
- Both endpoints are added in `src/web/app.py` next to the manual
  download routes; they share the same `current_app_version()`
  helper that the dashboard already uses.

## 7.2.30 (2026-04-29) — GDPR data subject rights: two new MCP tools

### Added

**`printix_personal_data_export` — GDPR Article 15 (right of access)**

Every authenticated user can ask their AI assistant *"What data do
you have about me?"* and receive a structured ZIP archive on the spot.
The tool gathers from the live tenant:

- Printix user profile (id, email, name, status, tenant)
- Group memberships (resolved via the existing reverse-lookup helper)
- All RFID/HID/Mifare cards mapped to the subject
- Last 500 audit-log entries authored by the subject
- Pending and resolved onboarding time-bombs
- Print statistics for the last 365 days (when SQL reporting is
  configured)
- The MCP role override, if set

Output is a ZIP with one JSON per category plus a README.txt
explaining each artefact. End users are restricted to their own
record by a self-check inside the tool body; Helpdesk and Admin
roles can export on behalf of any subject in support of formal
data-subject access requests.

Scope: `mcp:self` — every role can invoke; the tool body enforces
the self-or-elevated rule.

**`printix_personal_data_purge_request` — GDPR Article 17 (right to erasure)**

A non-destructive request channel. The tool does **not** delete
anything by itself — end users are not authorised to remove records.
Instead it:

1. Compiles the same data summary the export tool produces.
2. Records the request in the audit log with
   `action='gdpr_purge_requested'` and a unique `request_id`.
3. Sends a structured HTML email to the configured `alert_recipients`
   (tenant administrators) containing the summary, the requester's
   identity, the optional reason, and a checklist of next steps
   (review identity → execute deletion → notify subject within
   one month per Art. 12(3)).
4. Returns the request ID and the list of notified administrators.

The administrator reviews each request and executes the deletion
manually via `printix_offboard_user` (preserves audit trail) or
`printix_delete_user` (full erasure). This deliberate two-step
design protects against malicious self-purge, accidental data loss,
and ensures every deletion is traceable to a specific reviewer.

Scope: `mcp:self` — same self-or-elevated semantics as the export
tool. Helpdesk and Admin can file a request on behalf of another
user.

### Documentation refresh

- **GDPR Compliance Guide PDF** — added a new "Data Subject Rights"
  section describing both tools, plus rows for Art. 12(3) and
  Art. 15 in the article-coverage table. Re-rendered.
- **Permission Matrix PDF** — auto-regenerated with the two new
  tools listed under `mcp:self`. 129 production tools tagged.
- **README** — added a "GDPR data subject rights" feature section
  and updated the tool count from 125 to 129.

### Internal helpers
- `_resolve_data_subject(c, email_or_id)` — finds a Printix user by
  email or UUID; reuses `_collect_all_users`.
- `_gather_personal_data(c, target)` — single source for both
  export and purge_request, ensures the request summary in the
  admin notification matches what would actually be exported.
- `_build_personal_data_zip(data)` — one ZIP, one JSON per category,
  README.txt explaining the contents.
- `_caller_email()` / `_caller_is_admin_or_helpdesk()` — lightweight
  helpers used by both tools to enforce the self-or-elevated rule.

## 7.2.29 (2026-04-29) — `/logs` page: web-side tenant log handler + employee fallback

### Fixed
**`/logs` was always empty in the web UI**, even on tenants where the
MCP server clearly was active. Two layered bugs:

1. **No web-side log handler.** The MCP server process (port 8765)
   has a `_TenantDBHandler` that writes records into the
   `tenant_logs` table when an authenticated tool call is in flight.
   The web UI process (port 8080) is a separate Python process that
   never installed the equivalent handler — meaning every login,
   admin save, capture configuration change, and OAuth flow happened
   silently and produced zero log rows. The `/logs` page therefore
   showed nothing for users who interact only with the web interface.

2. **Employees fell through tenant lookup.** Same single-tenant model
   bug we fixed for the Connect-Center in v7.2.22: employees do not
   own a `tenants` row, so `get_tenant_by_user_id(employee.id)`
   returns `None` and the route returned an empty list even if logs
   existed.

### Fix

- New `_WebTenantDBHandler` in `web/app.py` is attached to the root
  logger at module import time. It writes web-side records into
  `tenant_logs` with the same category mapping the MCP-side handler
  uses (PRINTIX_API / SQL / AUTH / CAPTURE / SYSTEM). The owner
  tenant ID is resolved lazily on first emit and cached, with a 5 s
  retry cooldown to handle a fresh install where the tenant doesn't
  yet exist when the first request arrives.
- A thread-local reentrancy guard prevents `add_tenant_log` from
  triggering further emit calls (the DB module itself uses
  `logger.info` / `logger.warning` internally — without the guard
  the first failed write would loop).
- `/logs` route now applies the same parent-tenant fallback chain as
  the Connect-Center: own tenant → parent's tenant → first owner-
  admin tenant. Employees see the shared logs of the single tenant
  they belong to.

### What you'll see now

Activity that produces log rows after this update:

- All web UI requests at INFO and above (filterable in the page)
- Login / OAuth / Bearer auth events under the AUTH category
- Capture configuration changes (already partially logged in
  v7.2.x; now consistent)
- Settings saves, role changes (already logged via the audit table;
  the in-process logger output now also hits `/logs`)
- All MCP tool calls executed against the bearer token (these went
  through the MCP-side handler before and continue to do so)

The main page filter still defaults to DEBUG; flip to INFO if it
becomes too noisy under load.

## 7.2.28 (2026-04-29) — Hotfix: group member counts always 0

### Fixed
**Every Printix group on `/admin/mcp-permissions` showed 0 members**,
which combined with the active-only filter introduced in v7.2.20 hid
the entire group list from the operator. User confirmed real groups
do have members ("DACH Sales Team has 2 users in Printix").

Root cause: `client.list_groups()` returns lightweight metadata only —
no `memberCount`, no embedded `members` array. The richer view lives
behind `client.get_group(gid)` (the same endpoint
`printix_get_group_members` already uses internally), but the
permissions UI was only consuming the list response.

### Fix
For every group returned by `list_groups()` the route now:
1. Tries the cheap path first — checks the list-response for any
   count-shaped field (`memberCount`, `userCount`, `numMembers`,
   `numUsers`, `size`, `totalMembers`) or an embedded `members` list.
2. Falls back to `get_group(gid)` and inspects the same set of fields
   on the detail response, plus the embedded `members` / `users` /
   `memberUsers` arrays.
3. As a last resort: if the detail response carries an HAL
   `_links.users` href, treats the group as "has members, exact count
   unknown" and renders `?` in the table cell.

The detail calls run in parallel via `asyncio.gather` + `to_thread`,
so the per-page latency for ~30 groups stays around one second.

### UX
- Active-filter is now `member_count != 0` (not `> 0`), so groups in
  the "members exist but count unknown" bucket also remain visible
  with the default filter.
- Member-count column shows `—` for genuinely empty, the integer
  count when known, or `?` (with hover hint) when only the existence
  is detectable.

## 7.2.27 (2026-04-29) — Hotfix: user MCP-role override never re-displayed

### Fixed
**`/admin/mcp-permissions` would always show "no override"** for every
user after a save+refresh, even though the value was correctly written
to the database. Both the user override save (PR 1) and the new RBAC
enforcement (PR 2) appeared to be ignoring the data.

Root cause: `db._user_public()` filters the SELECT result down to a
"safe" public dict before returning it. The list of permitted fields
was authored before the `mcp_role` column existed and the new field
was being silently dropped on the read path. UPDATE worked; SELECT
came back clean.

Fix: include `mcp_role` in the `_user_public()` projection. This also
fixes RBAC role resolution at MCP-call time, which goes through the
same code path via `db.get_user_by_id()`.

Single-line change. Affects every page that reads user records —
admin lists, RBAC gate, audit display, employee dashboards. All now
see the persisted `mcp_role` value.

## 7.2.26 (2026-04-29) — Permission Matrix PDF + README update + RBAC default flip

### Added

**Permission Matrix PDF** — second compliance document downloadable
from `/admin/mcp-permissions`. Auto-generated from
`permissions.TOOL_SCOPES` and provides a complete, auditable list of:

- Role-to-scope summary matrix (✓ / — for every role × scope pair)
- All 127 production tools grouped by required scope (`mcp:self`,
  `mcp:read`, `mcp:audit`, `mcp:write`, `mcp:system`)
- Per-scope explanation and the list of allowed roles
- Default-fallback note: tools without an explicit scope tag default
  to `mcp:write` (admin-only — safe-by-default for unknown tools)

This is the document an admin or auditor pulls when they need to prove
exactly which roles can invoke which tools. Source markdown lives in
`docs/PERMISSION_MATRIX.md`; rendered PDF in
`src/web/assets/manuals/MCP_PERMISSION_MATRIX.pdf`. Served from
`/manuals/permission-matrix.pdf` (admin-only, login-gated).

### Changed

- **`docker-compose.yml` default for `MCP_RBAC_ENABLED` flipped from
  `0` to `1`.** Fresh installs now ship with role-based access control
  active by default; existing deployments are unaffected because their
  own compose file or `.env` overrides the upstream default.
- **README** — added a "GDPR-compliant role-based access control"
  feature section, a "Per-user Connect-Center" section, and a new
  `MCP_RBAC_ENABLED` row in the configuration table that links to the
  bundled GDPR Compliance Guide PDF.
- New i18n key `mp_matrix_pdf` in `de`, `en`, `no`.

### Internal

- The PDF is regenerated from `TOOL_SCOPES` via a one-shot script in
  the build step. To regenerate manually, re-run the embedded Python
  block under `docs/` and re-render with pandoc + headless Chrome.

## 7.2.25 (2026-04-29) — GDPR Compliance Guide (PDF download)

### Added
**Customer-facing English-language compliance document** linked from
the top of `/admin/mcp-permissions`. The administrator can hand this
PDF directly to a procurement reviewer, internal DPO, or external
auditor without extra preparation.

The guide covers:
- Customer-hosted deployment model and the Article 28 implication
  (Tungsten is not a processor of metadata that flows through MCP).
- The five-role catalogue with GDPR article mapping
  (End User → Art. 5/15-22, Helpdesk → Art. 32, Admin → Art. 24,
  Auditor → Art. 37-39, Service Account → Art. 28+32).
- Two assignment paths (per-user override + per-Printix-group default)
  with worked examples and the highest-role-wins rule.
- Permission-scope catalogue with the role-to-scope matrix.
- Audit-trail structure (`audit_log` schema + denied-call logging).
- GDPR Article-by-Article coverage (5(1)(c), 5(1)(f), 17, 24, 25,
  28, 30, 32, 37-39) plus EU AI Act Art. 50 transparency.
- Operational controls (Fernet, TLS, OAuth+PKCE, tool annotations).
- Verification checklist for ongoing compliance review.

PDF is bundled in the image at
`src/web/assets/manuals/MCP_GDPR_COMPLIANCE_GUIDE.pdf` and served
under `/manuals/gdpr-compliance.pdf` (login-gated, like the user
manuals shipped earlier).

### Changes
- New "📄 GDPR Compliance Guide (PDF)" button on
  `/admin/mcp-permissions` next to the "Back" button.
- i18n key `mp_gdpr_pdf` in `de`, `en`, `no`.

## 7.2.24 (2026-04-29) — MCP Permissions: RBAC status banner

### Added
A prominent status banner at the top of `/admin/mcp-permissions` shows
at a glance whether RBAC enforcement is currently active. The banner is
green with a lock icon when enforcement is on, amber with a warning icon
when off — and includes the literal `MCP_RBAC_ENABLED=0/1` value as a
chip so the admin can copy/paste it into a support ticket if needed.

The amber inactive-banner explains in one sentence what to do to enable
enforcement (set the env var and restart the container). Translations
in `de`, `en`, and `no`.

## 7.2.23 (2026-04-29) — MCP Permission Model — PR 2: enforcement

### Added

**Role-based access control is now enforced** at the MCP tool layer. The
foundation from PR 1 (v7.2.18) — five roles, scope catalogue, admin UI —
is wired up to actually block calls that exceed the caller's scope.

**Activation is opt-in.** A new env var `MCP_RBAC_ENABLED` controls
enforcement:

| Value | Behaviour |
|-------|-----------|
| `0` *(default)* | Pass-through. Every tool call runs as before. PR 1 admin UI continues to capture role assignments without consequence. |
| `1` | Enforced. Tools outside the caller's scope return a structured `permission_denied` JSON payload and an audit-log entry. |

This keeps the upgrade safe — operators turn enforcement on only when
they have populated the role assignments and verified them.

### Implementation

- **`server.py` — automatic gate on every tool registration.** A wrapper
  replaces `mcp.tool` at module load time so every subsequent
  `@mcp.tool(...)` decorator gets a permission-check layer
  transparently. No tool source code changed; the wrap is an
  infrastructure-level concern.
- **`_check_tool_permission(tool_name)`** resolves the caller's role from
  `current_tenant` (set by the bearer/OAuth middleware), looks up the
  tool's required scope in `permissions.TOOL_SCOPES`, and either passes
  through or returns a denial response.
- **Fail-closed semantics when RBAC is enabled**: missing tenant
  context, role-resolution errors, or unmapped tools all result in
  denial rather than accidental access. Disabled mode short-circuits
  the gate entirely.
- **Audit trail**: denied calls are recorded with
  `action='mcp_permission_denied'`, `object_type='mcp_tool'`,
  `object_id=<tool_name>` so DPO and compliance reviewers see every
  attempted-but-blocked call.

### New introspection tool

- **`printix_my_role`** — every user can ask their AI assistant
  *"What can I do?"* and get a structured answer back: their role,
  permitted scopes, count of allowed/denied tools, and ten-tool sample
  of each. Helps end-users and helpdesk understand denials without
  requiring admin access. Scope: `mcp:self` (always available).

### Tool scope catalogue

All 125 production tools are pre-tagged with one of five scopes:

| Scope | Allowed roles | Tool count (approx) |
|-------|---------------|---------------------|
| `mcp:self` | all roles | ~8 (own data only) |
| `mcp:read` | helpdesk, admin, auditor | ~70 (list/get/query) |
| `mcp:audit` | admin, auditor | 1 (`query_audit_log`) |
| `mcp:write` | admin only | ~40 (create/update/delete) |
| `mcp:system` | admin only | ~6 (backup, demo, defuse) |

Unmapped tools default to `mcp:write` (safe-by-default — only admins
can run a tool that wasn't categorised explicitly).

### Compatibility

- v7.2.22 deployments upgrade safely: env var defaults to off, behaviour
  identical.
- Bestehende User have `mcp_role='admin'` from the PR 1 backfill, so
  flipping the flag does not lock anyone out.
- `printix_my_role` works in both modes — always reports the role
  regardless of whether enforcement is active.

### Enabling enforcement

1. Open `/admin/mcp-permissions` and review the auto-populated roles.
   Confirm group assignments for "Helpdesk" and "End User" Printix
   groups, override individual users where needed, set Auditor and
   Service Account roles for the few people who need them.
2. Set `MCP_RBAC_ENABLED=1` in `.env` (or the docker-compose
   environment block).
3. Restart the container.
4. Verify with `printix_my_role` from a user account at each role
   level.
5. Watch the audit log (`/admin/audit`) for denied calls — they tell
   you whether the role assignments match the actual usage.

## 7.2.22 (2026-04-29) — i18n for new pages + manual download fix + employee tenant fallback

### Fixed
- **Manual download links 404** — Connect-Center pointed at
  `/static/MCP_MANUAL_*.pdf`, but the project has no `/static` mount and
  the PDFs were never copied into the container image. Manuals are now
  shipped under `src/web/assets/manuals/` and served from
  `/manuals/{lang}.pdf` with proper `application/pdf` headers and
  download filename.
- **Employees saw "no Printix tenant assigned"** in the Connect-Center
  even though every user shares the single-tenant configuration. Cause:
  `get_tenant_full_by_user_id` is keyed on the user's own `id`, but
  employees don't own a tenant row — they hang off the admin's tenant
  via `parent_user_id`. The route now falls back to the parent's tenant,
  and from there to any admin-owned tenant, so the page renders
  correctly for every user role.

### Added
- **i18n keys** for the Connect-Center and the MCP Permissions admin
  page in `de`, `en`, and `no`. The Connect-Center previously rendered
  in German only — every label, hint, step text, example prompt,
  reveal toggle and manual button is now translated. Same for the
  Permissions page (groups section, user override list, role legend,
  active/all filter, orphan cleanup confirmation).
- **Role labels are language-aware**: `ROLE_LABELS_EN` / `ROLE_LABELS_DE`
  picked at request time; non-DE locales default to English.
- Nav entry uses `cc_nav_label` so the Connect-Center label appears in
  the user's chosen language.

### Internal
- New helper route `/manuals/{lang}.pdf` (login-gated). Lang whitelist:
  `de`, `en`, `no`.
- Connect-Center now uses `get_tenant_full_by_user_id` chained with
  `parent_user_id` and `_find_tenant_owner_user_id` fallbacks.

## 7.2.21 (2026-04-29) — Connect-Center replaces Help page

### Added
**`/my/connect`** — a personal connection center for every WebUI user.
Replaces the old `/help` page (which now redirects here). One screen,
all the connection data, with copy buttons everywhere and a per-platform
walkthrough.

The page renders:

- **Personal greeting** with the logged-in user's name
- **Connection profile card** with copyable MCP Server URL, SSE
  endpoint, OAuth Authorize URL, Token URL, Client ID, and Client
  Secret. The Client Secret is masked by default with an explicit
  "Anzeigen / Verbergen" reveal toggle — secrets are never visible
  by default in the rendered HTML
- **Three platform cards** with step-by-step instructions:
  - **Claude.ai** (custom connector flow)
  - **ChatGPT** (OAuth-based connector)
  - **Claude Code** (CLI: `claude mcp add --transport http ...`)
- **"Was kann ich jetzt?" section** with five example prompts and
  direct links to the localised handbooks (DE/EN/NO)

### Changed
- Navigation entry "Hilfe" replaced with "🔌 Connect-Center" (desktop
  + mobile, both employee and admin role variants).
- `/help` returns a 302 redirect to `/my/connect` for backwards
  compatibility with existing bookmarks.
- The legacy help template remains accessible at `/_legacy/help` for
  reference until the next release.

### Why
Connection data was previously scattered across `/help`, `/settings`,
and `/admin/dashboard`. Users had to assemble OAuth ID + Secret +
URLs from three different pages and figure out which platform wanted
which value. The Connect-Center consolidates everything into one
self-explanatory page that doubles as the primary onboarding artefact.

## 7.2.20 (2026-04-29) — MCP Permissions UI: active-groups filter (default)

### Changed
- `/admin/mcp-permissions` now shows **only active Printix groups** by
  default — defined as groups with `memberCount > 0` OR groups that
  already carry an MCP-role assignment (so deliberate assignments
  remain visible even if members are temporarily missing). A toggle
  link at the top switches between "active only" and "all groups".
  URL parameter: `?show_all=1`.
- `member_count` is now extracted robustly: handles both `memberCount`
  (int) and `members` (int or list) variants returned by the Printix
  API. Stored as int internally; display falls back to "—" when 0.

### Why
The unfiltered list also showed historical / orphaned groups (typically
remnants of old AD imports) that distract from real role assignment.
"Active" matches the same semantic Printix uses elsewhere: groups
people are actually part of.

## 7.2.19 (2026-04-29) — Hotfix: `from_json` Jinja filter never registered in Docker repo

### Fixed
**`/admin/settings` returned 500 Internal Server Error** with
`TemplateRuntimeError: No filter named 'from_json' found.` since v7.2.15
(when settings.html started using the filter). The v7.2.17 CHANGELOG
described this fix, but the code change was only landed in the
HA-Addon sister repository — the Docker repo never received it.

The filter is now registered on the Jinja environment in
`web/app.py` right after `Jinja2Templates(...)`. None-tolerant: empty
string, `None`, already-a-list, and already-a-dict all pass through
without raising.

## 7.2.18 (2026-04-29) — MCP Permission Model — PR 1: Schema + Persistence + Admin UI

### Added
GDPR-compliant role-based access control foundation for MCP tool access.
This is **PR 1 of a three-part rollout**. PR 1 ships data structures and
the admin interface; enforcement (`@require_scope` decorator and
`tools/list` filter) follows in PR 2.

**Five roles** mapped to GDPR articles:

| Role | GDPR reference |
|------|----------------|
| `end_user` | Art. 15-22 (data subject rights) |
| `helpdesk` | Art. 32 (technical and organisational measures — separation of duties) |
| `admin` | Art. 24 (controller obligations) |
| `auditor` | Art. 37-39 (Data Protection Officer) |
| `service_account` | Art. 28 (processor) + Art. 32 (accountability) |

**Two assignment paths** — per user (explicit override) and per Printix
group (default for all members). When a user belongs to multiple groups
with different role assignments, the highest role wins. Auditor and
Service Account are explicit-only; they cannot be assigned via groups
because they are personal/system designations, not organisational scopes.

### Changes
- **`db.py`** — idempotent migration adds `users.mcp_role` column plus two
  new tables `mcp_group_roles` and `user_group_cache`. Existing users are
  backfilled to `mcp_role='admin'` so PR 2 enforcement does not lock
  anyone out at activation time.
- **`permissions.py`** (new, ~190 lines) — role catalogue, role rank,
  bilingual labels and descriptions, `resolve_mcp_role()` (PR 1: explicit
  override only; PR 2 will wire up group resolution against
  `printix_list_groups`).
- **`db.py` CRUD helpers** — `set_user_mcp_role`, `get_user_mcp_role`,
  `set_group_mcp_role`, `get_group_mcp_role`, `list_group_mcp_roles`,
  `delete_group_mcp_role`, `get_user_group_cache`, `set_user_group_cache`.
- **`web/app.py`** — three new admin routes: `GET /admin/mcp-permissions`
  (UI), `POST /admin/mcp-permissions/user-role`, `POST /admin/mcp-permissions/group-role`.
  All actions audited via the existing `audit_log` table.
- **`templates/admin_mcp_permissions.html`** (new) — two-section admin
  UI: live Printix groups with role dropdown (queried via
  `printix_client.list_groups`); user override list with full role
  catalogue.
- **`templates/admin_dashboard.html`** — new "🔐 MCP Permissions" entry
  in the admin actions row.

### Compatibility
PR 1 is **fully backwards-compatible**. No MCP-call behaviour changes;
the admin can populate roles now so they are ready when PR 2 activates
enforcement. Cache TTL for the group-membership lookup is 5 minutes,
deliberately conservative until live performance data exists.

## 7.2.17 (2026-04-28) — Toggle persistence bug: two more missing links

### Fixed
Save path was made correct in v7.2.14/15/16 (form fields received, DB column added, `update_tenant_credentials(notify_events=...)` writes the JSON). But the reload still shows the toggle as inactive. User repro confirmed.

Two more bugs along the read path:

1. **`get_tenant_full_by_user_id` doesn't return `notify_events`** — the column is read from the DB row but the field is missing from the final result dict. Template gets `tenant.notify_events = Undefined`, falls back to `["log_error"]` default.

2. **Jinja filter `from_json` is not registered.** Template uses `tenant.notify_events | from_json` — filter doesn't exist, returns Undefined or template error.

### Fix
- `db.get_tenant_full_by_user_id` now includes `notify_events` with fallback `'["log_error"]'`.
- `web/app.py` registers the `from_json` filter (None-tolerant: empty / None / already-a-list all handled). 

## 7.2.16 (2026-04-28) — Settings save: diagnostic log for toggle states

### Improved
- `settings_post` now logs the exact toggle state that arrived from the browser + what gets persisted as `notify_events`. Enables clear A/B distinction between "user didn't tick" vs "save didn't persist".

## 7.2.15 (2026-04-28) — DB migration: `tenants.notify_events` (was claimed in v7.2.14 but never created)

### Fixed
- **`no such column: notify_events`** when saving settings in v7.2.14. The previous CHANGELOG entry claimed the column already existed — it did not. First save crashed with sqlite3 OperationalError.
- **Fix**: idempotent ALTER migration in `init_db()`, in line with the existing alert_recipients / alert_min_level / mail_from_name migrations. Default value `'["log_error"]'` keeps existing tenants compatible with pre-v7.2.x code (`reporting/log_alert_handler` falls back to log_error when notify_events is empty).
- Runs once on next container start. No data loss.

## 7.2.14 (2026-04-28) — Settings save: notification toggles were never persisted

### Fixed
- **Classic half-finished feature**: the settings template renders 8 notification fields (`alert_recipients`, `alert_min_level`, plus 6 `notify_*` toggles) and the form posts them to `/settings`. But the `settings_post` handler in `web/app.py` declared **none** of them as `Form(...)` parameters — they were silently discarded.

  Symptom: user ticks the toggle, saves, reloads — toggle is gone. The `notify_events` column in the `tenants` table was never updated, so our `_notify_admins_of_user_registered` (v7.2.12+) couldn't fire either: `is_event_enabled(tenant, 'user_registered')` returned False because `notify_events` was empty.

- **Fix**: 8 form parameters added to `settings_post`. The 6 toggle booleans are turned into a JSON array and passed as `notify_events` to `update_tenant_credentials`. `update_tenant_credentials` itself gains a `notify_events` parameter — the DB column already existed.

### Effect
Together with the diagnostic logs from v7.2.13, the notification flow is now fully functional:
1. Settings toggle persists across save+reload ✅ *(new)*
2. Mail to admins on user_registered event actually fires ✅ *(new in v7.2.12)*
3. Misconfiguration is logged per-admin with the exact reason ✅ *(new in v7.2.13)*

## 7.2.13 (2026-04-28) — user_registered notify: per-admin diagnostic logs

### Improved
- **`_notify_admins_of_user_registered` now logs an INFO-level reason for every admin that did NOT receive a mail.** Previously only the summary "X mails sent" was logged — when X=0, the user couldn't tell which of the 5 prerequisites was missing.
- Per-admin diagnostic before the send call:
  - `'user_registered' NOT in notify_events` (Settings toggle)
  - `'alert_recipients' is empty` (recipient CSV)
  - `no mail credentials found in any of: tenant / global / env`
- Success case now also logs: `Mail an Admin '...' gesendet (Empfaenger: ..., Mail-Source: tenant|global|env)` so user can see which credential chain matched.
- `check_enabled=False` on the send call (we already checked above) — avoids a redundant DB hit and avoids silent skips inside the helper.

### Background
Bug-reporter's test registration produced `0 Mail(s) versendet` with no explanation. v7.2.13 makes the log transparent.

## 7.2.12 (2026-04-28) — Bugfix: admin notification on pending registration never fired

### Fixed
- **No mail to admins on a new pending registration** — the UI toggle (*Settings → Notifications → "🔔 New MCP user registered (admin)"*) was visible and saved, but no mail was sent.

  **Half-finished feature**: UI present (`settings.html:211`), setting persisted into `notify_events`, helper `send_event_notification(tenant, "user_registered", ...)` and HTML template `html_user_registered(...)` already existed in `reporting/notify_helper.py` — only the trigger call in the registration flow was missing. `register_step4_post` created the user + wrote an audit event but never called the notify helper.

- **Fix**: new helper `_notify_admins_of_user_registered(new_user)` right before the registration routes. Iterates all approved admins, loads each tenant via `get_tenant_full_by_user_id`, calls `send_event_notification(tenant, "user_registered", ...)`. The per-tenant Settings toggle is still honored (`check_enabled=True`).

- Best-effort: mail failures are logged but don't block registration. New user lands correctly in pending state regardless.

### Mail prerequisites
All 5 must be true per admin tenant for the mail to actually go out:
1. Admin has status=`approved` and is_admin=1
2. Tenant exists (via `get_tenant_full_by_user_id`)
3. `notify_events` contains `user_registered` (Settings toggle)
4. `alert_recipients` is non-empty (recipient CSV in Settings)
5. Mail credentials configured (tenant own OR global fallback OR env)

If v7.2.12 still doesn't deliver, walk through points 1–5. The log gives concrete hints.

## 7.2.11 (2026-04-28) — Tool annotations: 82 read-only tools marked (fewer permission prompts)

### Changed
- **All 126 MCP tools now have `ToolAnnotations` set** (previously bare = every tool call defensively permission-prompted by clients). Classification:

  | Category | Count | Annotation |
  |----------|-------|------------|
  | **Read-only** | **82** | `readOnlyHint=True, idempotentHint=True` |
  | **Destructive** (delete/offboard/demo_rollback) | **11** | `destructiveHint=True, idempotentHint=True` |
  | **Write idempotent** | **18** | `idempotentHint=True` |
  | **Write non-idempotent** (print/send/submit/welcome/generate_id_code/...) | **14** | defaults |
  | **Open-world** (all) | **126** | `openWorldHint=True` |

- Effect: MCP clients that respect annotations (Cursor, Claude Code, Continue, Anthropic Console) skip the permission prompt for read-only tools. Real destructive tools still prompt.

- claude.ai web UI: not all versions evaluate annotations consistently — for that case the eventual answer is the in-app chat (planned).

### Implementation
- Refactor script classifies into 4 buckets, rewrites `@mcp.tool()` -> `@mcp.tool(annotations=ToolAnnotations(...))`, adds `from mcp.types import ToolAnnotations` import. Function code unchanged.

## 7.2.10 (2026-04-28) — Tool-picking optimization phase 2: all 126 MCP tools with structured docstrings

### Changed
- **All 126 MCP tools now ship with consistently structured docstrings** in the new format (one-liner + when-to-use + when-NOT + returns + args). Batch 1 (16 v6.8.x workflow tools) shipped in v7.2.9 — this release covers the rest:

  - Batch 2 (79 tools): high-overlap families — print/send/submit/jobs, list/get/find for printers/sites/networks/snmp/users/groups/cards, query/reports/analytics, user lifecycle, status/whoami/explain
  - Batch 3 (31 tools): CRUD/reports-templates/schedules/capture-listing/backup/demo/feature-requests

- Format per tool (compact):
  ```
  [One-liner]
  Wann nutzen: "example prompt 1" • "prompt 2" • ...
  Wann NICHT — stattdessen: <case> → other_tool
  Returns: brief fields + follow-up tools
  Args: param value-example | description
  ```

- Effect on the AI assistant:
  - Clear **when/when-NOT rules** resolve overlaps (e.g. `print_self` vs `send_to_user` vs `quick_print`, `query_top_users` vs `top_users`, `list_users` vs `find_user` vs `user_360`)
  - **Concrete user prompts** in the description field act as trigger phrases the model matches directly
  - **Cross-references** (`stattdessen → printix_X`) help build multi-step plans
  - **Args with value examples** cut back-and-forth like *"in which format?"*

- Function code is fully unchanged — no behavior change. Pure metadata improvement.

### Affected
- 126 tools across both repos
- No DB migration needed
- Recommend: AI assistants should refresh their tool lists (see MCP_MANUAL_*.md) for the new description text to take effect during tool picking

## 7.2.9 (2026-04-28) — Tool-picking optimization: 16 v6.8.x workflow tools with structured docstrings

### Changed
- **All 16 v6.8.x workflow tools now have expanded docstrings** in a new format:
  - **One-liner** — single clear sentence on what the tool does
  - **When to use** — 4-6 typical user prompts (mixed DE + EN)
  - **When NOT** — negative disambiguation with cross-refs to related tools
  - **Returns** — what comes back plus follow-up tools (e.g. job_id → printix_get_job)
  - **Args** — value examples and accepted formats including defaults

  Effect: AI models (claude.ai, ChatGPT, etc.) pick the right tool more reliably; fewer *"which of the three print tools do you mean?"* clarification rounds; better multi-step plans because the model knows which tools chain together.

- Function code is UNCHANGED — only docstrings were reworked. No breaking changes.

### Background
AI tool picking is driven by tool name + description + parameter descriptions + conversation context. Concrete example prompts in the description work especially well because the model picks them up as trigger phrases. Negative disambiguation (*"NOT for X — use Y instead"*) resolves overlaps between similar tools (`print_self` vs `send_to_user` vs `print_to_recipients`).

### Affected Tools (all v6.8.x)
print_self, send_to_capture, describe_capture_profile, get_group_members, get_user_groups, resolve_recipients, print_to_recipients, welcome_user, list_timebombs, defuse_timebomb, sync_entra_group_to_printix, card_enrol_assist, describe_user_print_pattern, session_print, quota_guard, print_history_natural

## 7.2.8 (2026-04-28) — Auto-PDL-Conversion: hieroglyph print fix

### Fixed
- **Printers printed hieroglyphs instead of PDF content**: Printix API accepts no `PDF` PDL — only `PCL5 | PCLXL | POSTSCRIPT | UFRII | TEXT | XPS`. We were uploading raw PDF bytes, which printers without an internal PDF RIP interpreted as ASCII text. Submit worked, output was garbage.

### Added
- **New module `print_conversion.py`** with magic-byte detection, Ghostscript wrappers for PDF→PCL XL / PCL5 / PostScript, a PostScript text generator and a top-level `prepare_for_print(file_bytes, target="PCLXL", color=True)` function.
- **Ghostscript** is now installed in the container (Dockerfile +`ghostscript`, ~25 MB).
- **Four print tools extended** with `pdl: str = "auto"` + `color: bool = True`:
  - `printix_print_self`
  - `printix_print_to_recipients`
  - `printix_send_to_user` (legacy — finally with conversion)
  - `printix_session_print` (indirectly via send_to_user)

  Default `pdl="auto"` maps to **PCL XL** (`pxlcolor`) — most universal modern printer language, compatible with HP/Konica/Ricoh/Xerox/Canon/Brother. Values: `auto` | `PCLXL` | `PCL5` | `POSTSCRIPT` | `passthrough`.

- **Clear error messages**: `ConversionError` from Ghostscript is caught and returned as `{"error": "conversion failed: ...", "hint": "..."}` — no silent submit with broken bytes. `passthrough` mode warns in the log about the hieroglyph risk.

- **Tool response now reports `size_input` + `size_after_conversion` + `pdl`** so the user sees what happened (PDF 660 → PCLXL 14k, PDL=PCLXL).

### Notes

- For multi-recipient bursts (`print_to_recipients`) conversion runs **once** before the loop, not per recipient.
- `passthrough` is explicitly for debug or for tenants whose Cloud-Print-Gateway queue does conversion server-side. Default stays `auto`.

## 7.2.7 (2026-04-28) — Azure Blob Upload requires `x-ms-blob-type` header

### Fixed
- **After v7.2.6 the submit path crashed with HTTP 400 / `MissingRequiredHeader`** from Azure Blob Storage. The Printix API actually returns the required PUT headers in the `submit_print_job` response:
  ```
  uploadLinks: [{"url": "https://...blob...", "headers": {"x-ms-blob-type": "BlockBlob"}}]
  ```
  Azure requires this header for PUTs to a BlockBlob — without it the request fails. My code (and the old send_to_user code) never read or forwarded the headers.
- `_extract_job_id_and_upload(job)` now also returns an `upload_headers` dict (tuple grew from 2 → 3 elements). All three callers forward the headers to `c.upload_file_to_url(upload_url, file_bytes, extra_headers=upload_headers)`.

## 7.2.6 (2026-04-28) — upload_file_to_url: invalid `filename` parameter

### Fixed
- **Right after v7.2.5 the submit path crashed at the next step** with `PrintixClient.upload_file_to_url() got an unexpected keyword argument 'filename'`. Real signature: `upload_file_to_url(upload_url, file_bytes, content_type='application/pdf', extra_headers=None)`. Removed `filename=` kwarg from all three call sites (print_self, print_to_recipients, legacy send_to_user). `filename` is already passed to `submit_print_job(title=...)`.

## 7.2.5 (2026-04-28) — submit_print_job: nested response shape

### Fixed
- **All print tools (print_self / print_to_recipients / session_print + the legacy send_to_user) extracted job_id and upload_url from the wrong fields.** Live probe via `printix_submit_job` showed Printix returns
  ```
  {"job":{"id":"...","_links":{...}}, "uploadLinks":[{"url":"https://...blob..."}],
   "_links":{"uploadCompleted":{...}, "changeOwner":{...}}, "success":true}
  ```
  i.e. `job.id` is nested under `"job"` and the upload URL is in `uploadLinks[0].url` (list). The old code read `job.id` flat and `_links.upload.href` — both empty -> "no job_id/upload_url in response".
- New helper `_extract_job_id_and_upload(job)` with the nested path plus fallbacks for alternate shapes (future-proof). Wired into all 3 new tools + the legacy `send_to_user`.

## 7.2.4 (2026-04-28) — submit_print_job: invalid parameter `size_bytes`

### Fixed
- **`print_self`, `print_to_recipients`, `send_to_user` crashed on submit** with `PrintixClient.submit_print_job() got an unexpected keyword argument 'size_bytes'`. The real client signature has no `size_bytes` — the param was carried forward from an old code generation. Existing `send_to_user` had the same bug for a long time but apparently was never exercised. All three call-sites cleaned up.

## 7.2.3 (2026-04-28) — Workflow-Tools: send_to_capture asyncio.run error

### Fixed
- **`send_to_capture` failed with `asyncio.run() cannot be called from a running event loop`**: FastMCP runs in its own asyncio loop, `asyncio.run()` is not allowed there. Tool is now `async def` and uses `await plugin.ingest_bytes(...)` directly.

## 7.2.2 (2026-04-28) — Workflow-Tools: 3 Bugfixes nach Live-Test

### Fixed
- **`describe_capture_profile` / `send_to_capture` returned "plugin not found"**
  even when `plugin_id` was correct. Root cause: `capture/plugins/__init__.py`
  does auto-discovery on package import via `pkgutil.walk_packages`, but
  the tools only imported `capture.base_plugin`, not the `capture.plugins`
  package — the `_PLUGINS` registry stayed empty. Fix: explicit
  `import capture.plugins` before the `get_plugin_class()` call to trigger
  discovery.
- **Group resolvers couldn't find groups (`could not resolve group_id`,
  `id: null`)**. Printix API returns group-UUIDs only in
  `_links.self.href`, never as `id` in the body. New `_group_id(g)`
  helper falls back to `_extract_resource_id_from_href(...)`. Wired into
  `get_group_members`, `get_user_groups` (fallback path) and
  `_resolve_recipients_internal`. Duplicates now collapse on the real
  UUID instead of the always-`None` `id` field.
- **Self-user resolution** (`print_self`, `quota_guard`,
  `print_history_natural`) read `tenant.email` — but the tenant row
  doesn't carry that field. Now falls back to
  `db.get_user_by_id(tenant.user_id)` and reads `email` from the joined
  user row.

### Test status
- Phase A (read-only) fully exercised pre-fix; bugs reproduced.
- Phase A post-fix: plugin lookup, group resolution, self-user functional.
- Phase B (write paths) tested in the next round.

## 7.2.1 (2026-04-28) — Hotfix: NameError 'Any' on import

### Fixed
- **Container boot loop**: v7.2.0 used `Any | None` as a type hint in
  `_follow_hal_link` without importing `Any` from `typing`. On module
  load this raised `NameError: name 'Any' is not defined`, putting the
  Docker container into a restart loop. `Any` is now imported alongside
  `Optional`. No other code changes.

## 7.2.0 — 2026-04-27

Workflow-Tools layer ported from the HA-Addon side (v6.8.0). Tool
inventory grows from 111 → 127. No breaking changes; existing tools
and endpoints are untouched.

### Added — Phase 1: Native File Ingest

- `printix_print_self(file_b64, filename, ...)` — AI generates a PDF
  inline, the tool drops it into the calling MCP-user's own
  secure-print queue. Self-user resolved from `current_tenant.email`.
- `printix_send_to_capture(profile, file_b64, filename, metadata_json)`
  — file straight into a capture pipeline (same code path as a
  webhook), bypassing the Azure Blob round-trip. Calls
  `plugin.ingest_bytes()` directly.
- `printix_describe_capture_profile(profile)` — self-describing,
  returns the plugin's `config_schema`, current config (secrets
  masked), and which metadata fields are accepted.

### Added — Phase 2: Multi-Recipient Print

- `printix_get_group_members(group_id_or_name)` — follows
  HAL `_links.users`, falls back to direct fields.
- `printix_get_user_groups(user_email_or_id)` — reverse lookup.
- `printix_resolve_recipients(recipients_csv)` — diagnose tool,
  resolves `alice@firma.de`, `group:Marketing`, `entra:<oid>`,
  `upn:...` to a flat printix-user list.
- `printix_print_to_recipients(recipients_csv, file_b64, filename,
  ...)` — per-recipient secure-print jobs (`individual` mode).
  `shared_pickup` intentionally omitted.

### Added — Phase 3: Onboarding + Time-Bomb Engine

- `printix_welcome_user(user_email, ...)` — personalized welcome PDF
  + scheduled reminders (`card_enrol_7d`, `first_print_reminder_3d`,
  `card_enrol_30d`).
- New table `user_timebombs` (idempotent CREATE on first call) with
  hourly APScheduler tick (cron `minute=7`) that re-checks the
  condition and auto-defuses if it's no longer true.
- Embedded `_generate_reminder_pdf_b64` — dependency-free A4 mini-PDF
  (~700 bytes) for reminders.
- `printix_list_timebombs(user_email, status)` and
  `printix_defuse_timebomb(bomb_id, reason)` for admin-side control.
- `printix_sync_entra_group_to_printix(entra_group_oid, ...)` —
  pulls Entra group members via Graph (App permission
  `Group.Read.All`), shows diff to printix group. Default
  `sync_mode="report_only"`; additive/mirror are wired but write
  paths are `not implemented` until the Printix public API exposes
  an add-member endpoint.

### Added — Bonus

- `printix_card_enrol_assist(user_email, card_uid_raw, profile_id)`
  — runs UID through `apply_profile_transform` and registers via
  `register_card`.
- `printix_describe_user_print_pattern(user_email, days)` — top
  printers, color quote, average pages. SQL preset first, falls
  back to API job scan.
- `printix_session_print(user_email, file_b64, filename,
  expires_in_hours)` — submit + auto-expire timebomb.
- `printix_quota_guard(user_email, window_minutes, max_jobs)` —
  pre-flight burst-check, returns `allow|throttle|block` verdict.
- `printix_print_history_natural(user_email, when, limit)` —
  natural-language date windows: `today`, `yesterday`, `this_week`,
  `last_month`, `Q1`-`Q4`, `7d`.

### DB Migration

Idempotent: `user_timebombs` is created via `_ensure_timebomb_table()`
on first tool call. No existing tables touched.

### Scheduler

`reporting.scheduler._scheduler` gains a new cron job `timebomb_tick`
(every hour, minute 7) when running. Idempotent — registered once on
first timebomb-related tool call.

## 7.1.4 — 2026-04-27

iOS Entra login migrated from Device Code Flow to native Authorization
Code + PKCE (RFC 7636). The mobile app now opens an in-app
`ASWebAuthenticationSession` (Safari sheet), the user signs in directly
at Microsoft (Face ID / MFA / passkeys), the sheet auto-closes and the
app is signed in — no device code to type, no second device. macOS and
Windows desktop clients keep the Device Code Flow.

This release ports the four iterative fixes from the HA-Addon side
(v6.7.119 → v6.7.122) into a single coherent release.

### Added

- **`POST /desktop/auth/entra/authcode/start`** — accepts form fields
  `device_name` and `redirect_uri`. Generates a PKCE pair (verifier +
  SHA-256 challenge), a CSRF state token, persists everything in a new
  table `desktop_entra_authcode_pending`, builds the Microsoft auth URL
  and returns `{session_id, auth_url, state, expires_in}`. The
  `code_verifier` never leaves the server.
- **`POST /desktop/auth/entra/authcode/exchange`** — accepts
  `session_id`, `code`, `state`. Validates state (CSRF), exchanges code
  + verifier for a Microsoft access token, fetches profile via Graph
  `/me`, maps to MCP user via the existing `get_or_create_entra_user`,
  issues a desktop bearer token. Returns `{status, token, user}`.
- **`entra.py`** — new helpers `generate_pkce_pair()`,
  `build_authorize_url_pkce(...)`, `exchange_code_pkce(...)`. New
  constant `_SCOPES_GRAPH_USER_READ` =
  `"https://graph.microsoft.com/User.Read offline_access openid email profile"`.

### Critical implementation notes

- **No `client_secret` in the PKCE token exchange.** Microsoft classifies
  custom-URL-scheme redirects (`printixmobileprint://...`) as Public
  Client. With a Public Client, sending `client_secret` fails with
  `AADSTS700025: Client is public so neither 'client_assertion' nor
  'client_secret' should be presented`. PKCE replaces the secret as the
  security guarantee. The web auth-code flow
  (`exchange_code_for_user`) keeps the secret — it's confidential.
- **Scope must include `https://graph.microsoft.com/User.Read`.** The
  narrow `_SCOPES = "openid profile email"` are ID-token claims only —
  not Graph permissions. Without `User.Read` the access_token is
  rejected by Graph `/v1.0/me` with **403 Forbidden**. Helpers default
  to `_SCOPES_GRAPH_USER_READ`.
- **Form-encoded requests, not JSON.** Both endpoints use FastAPI
  `Form(...)`. Clients must send `application/x-www-form-urlencoded`.
- **State is verified before token exchange.** Mismatch → HTTP 400
  `code="state_mismatch"`. Stale rows in
  `desktop_entra_authcode_pending` are deleted on success.

### Setup (one-time, Azure Portal)

In the existing Entra app registration:

1. *Authentication* → *Add a platform* → **Mobile and desktop applications**
2. Custom redirect URI: `printixmobileprint://oauth/callback`
3. Save. *Allow public client flows* stays **No**.

Same `client_id` / `client_secret` / `tenant_id` continue to serve the
web login (`exchange_code_for_user`) and admin device-code flows. First
sign-in per user shows a one-time consent prompt for `User.Read`;
expected and persistent.

### Database

New table `desktop_entra_authcode_pending` (created idempotently in the
start endpoint, columns: `session_id PRIMARY KEY`, `code_verifier`,
`state`, `redirect_uri`, `device_name`, `created_at`, `expires_at`).
Rows TTL ≈ 10 minutes; row deleted on successful exchange.

### Files touched

- `src/entra.py` — `import hashlib`, `_SCOPES_GRAPH_USER_READ`,
  `generate_pkce_pair`, `build_authorize_url_pkce`,
  `exchange_code_pkce` (no `client_secret`)
- `src/web/desktop_routes.py` — two new endpoints with full logging
- `VERSION` — `7.1.3` → `7.1.4`

### Client side (informational)

The matching iOS client changes are in the `printix-MobilePrint` /
`PrintixSendCore` repo (Swift `EntraAuthCodeStartResponse` model,
`entraAuthCodeStart` / `entraAuthCodeExchange` methods,
`ASWebAuthenticationSession` integration in `LoginView`). macOS /
Windows desktop clients are not affected.

## 7.1.3 — 2026-04-24

Per-mailbox control of what happens to an incoming mail after Guest-Print
has successfully submitted the attachment(s) to the queue.

### Added

- **Mailbox setting `on_success`** — pickable in the create form and the
  detail-page "Settings" tab. Three options:
  - **`move`** (default, previous behaviour) — mail is moved into the
    configured Processed-folder (`folder_processed`).
  - **`keep`** — mail stays in the Inbox but is flagged as read
    (`PATCH /messages/{id}` with `isRead=true`), so the next poll won't pick
    it up again.
  - **`delete`** — mail is deleted via `DELETE /messages/{id}`, which Graph
    translates into a move to the well-known *Deleted Items* folder. Not a
    hard-purge.
- Graph client: added `mark_message_read()` and `delete_message()` wrappers;
  `_request` now also handles PATCH/DELETE.
- DB: added column `on_success` to `guestprint_mailbox` (default `'move'`);
  idempotent `ALTER TABLE ADD COLUMN` migration runs on startup for existing
  installs.

### Notes

- The "no printer configured" early-exit branch (guest allowlisted, but
  neither guest nor mailbox default has printer+queue) now also respects
  `on_success`. Previously it always moved to the Processed-folder to avoid
  infinite retry; now it follows the admin's choice.
- Behaviour is identical to v7.1.2 when the setting is left at `move`.

## 7.1.2 — 2026-04-24

Hotfix for the Guest-Print poll loop.

### Fixes

- **`list_unread_with_attachments` returned HTTP 400 from Graph.** The query
  combined `$filter` (with `and`) and `$orderby` on `/users/{upn}/mailFolders/
  inbox/messages`, which Exchange rejects as "too complex" unless the mailbox
  has a matching composite index — which is not the default. The server-side
  filter is now limited to `isRead eq false` (no `$orderby`); the
  `hasAttachments` check and chronological sort happen client-side on the page
  returned by Graph. Polling now works against a vanilla Exchange Online
  mailbox with no extra configuration.

## 7.1.1 — 2026-04-24

Same-day follow-up release with three polish items on top of v7.1.0's
Guest-Print feature. No breaking changes, no DB migrations.

### Added

- **Attachment conversion pipeline.** Guest-Print now accepts more than
  just PDF. It reuses the existing `upload_converter.py` (already part of
  the web-upload flow) to transform attachments before submit:
  - **PDF** — passthrough.
  - **Images** (`png`, `jpg`/`jpeg`, `gif`, `bmp`, `tif`/`tiff`) — rendered
    to PDF via Pillow at 150 dpi.
  - **Plain text** (`txt`) — rendered to PDF via Pillow (monospace,
    A4 @150 dpi, soft-wrapped lines).
  - **Office** (`docx`, `xlsx`, `pptx`, `odt`, `ods`, `odp`, `doc`, `xls`,
    `ppt`, `rtf`) — converted via `libreoffice --headless --convert-to pdf`
    (LibreOffice is already bundled in the runtime image).

  The Printix submit-PDL is always `application/pdf` after conversion.
  Attachments with unsupported types or conversion errors land as
  `skipped` in the per-mailbox history with a readable reason.
- **Device-code auto-setup wizard** for the Guest-Print Entra-App, analogous
  to the SSO auto-setup on `/admin/settings`. The `/guestprint/config` page
  now has a "🚀 Auto-Setup starten" button that runs the full flow:
  1. Admin signs in via `https://microsoft.com/devicelogin` with a short code
     (scopes: `Application.ReadWrite.All`, `AppRoleAssignment.ReadWrite.All`,
     `User.Read.All`, `Organization.Read.All`).
  2. Server registers a **single-tenant** Entra app named "Printix Guest-Print"
     with `Mail.ReadWrite` as an **Application Role** (not a Delegated Scope —
     the poller runs app-only).
  3. Server generates a client secret and creates the app's service principal.
  4. Server grants admin consent programmatically via
     `POST /servicePrincipals/{id}/appRoleAssignments` (if the signed-in
     admin has a Privileged Role). If not, the UI instructs the admin to
     click **Grant admin consent** in the portal manually.
  5. Credentials are saved to `guestprint_entra_*` settings (client secret
     Fernet-encrypted via the same `_enc()` used elsewhere).
  6. Using the delegated admin token still in the session, the server
     fetches the tenant's mailbox list via `/users`. The admin picks the
     mailbox to monitor from a dropdown, the `guestprint_mailbox` row is
     created, and we redirect to the new mailbox's detail page.

  The manual 3-field form remains as a fallback below the wizard.
- **Printer / queue dropdown** in the mailbox create/edit forms and the
  per-guest override forms. Populated from
  `PrintixClient.list_printers(size=200)` using the same href-parser as
  `/tenant/queues`. Picking an option fills the `default_printer_id` /
  `default_queue_id` (or `printer_id` / `queue_id`) inputs via JS — the
  inputs stay visible and editable as a fallback. If the tenant has no
  Print-API credentials configured (or the API call fails), the dropdown
  is hidden and the form degrades silently to free-text.

## 7.1.0 — 2026-04-24

New main navigation tab **Guest-Print**: a mail-driven secure-print flow for
external guests. A dedicated Entra-registered mailbox is polled for incoming
attachments; senders are matched against an admin-curated allowlist, auto-
provisioned as Printix `GUEST_USER` (with an optional "timebomb" expiration),
and the attachment is uploaded to a secure-print queue with ownership
transferred to the guest via `change_job_owner`.

### Added

- **`guestprint_*` DB tables** — `guestprint_mailbox`, `guestprint_guest`,
  `guestprint_job` (with CRUD helpers in `db.py`). Unique dedupe index on
  `(mailbox, message, attachment)` makes the poll loop crash-safe.
- **`src/guestprint/` package** —
  - `config.py`     separate Entra-App credentials (Fernet-encrypted secret)
  - `graph.py`      Microsoft Graph v1.0 mail wrapper (token cache, 429
                    retry, `list_unread_with_attachments`,
                    `download_attachment`, `move_message`,
                    `ensure_folder_path`, `test_connection`)
  - `printix.py`    `GUEST_USER` provisioning with `expirationTimestamp` +
                    idempotent lookup-by-email
  - `printer.py`    secure-print 4-step flow (submit
                    `release_immediately=False` -> upload -> complete ->
                    `change_job_owner`)
  - `poller.py`     orchestrator: match -> download -> print -> move to
                    Processed/Skipped folder + job log
  - `scheduler.py`  meta-tick registered on the existing APScheduler;
                    polls every 60s and fires per-mailbox based on each
                    mailbox's `poll_interval_sec` + `last_poll_at`
- **Admin UI (`/guestprint`)** — nav-tab for admins with three surfaces:
  - `/guestprint/config`        Entra-App credentials (tenant / client id /
                                 secret). Separate from the SSO Entra-App so
                                 customers can register a minimal-scope
                                 `Mail.ReadWrite` application.
  - `/guestprint/mailboxes`     list + inline create; per-mailbox
                                 test-connection (AJAX) and poll-now button
  - `/guestprint/mailboxes/:id` detail page with 3 tabs (Guests, History,
                                 Settings); guest rows expand in-place to
                                 edit; delete-guest may optionally also
                                 delete the Printix user
- i18n: `nav_guestprint` key added to all 14 language blocks.

### Operator notes
- The Graph Entra-App needs application permission **`Mail.ReadWrite`** with
  admin consent for the monitored mailbox. `User.Read.All` is **not**
  required — the code addresses mailboxes by UPN.
- Poll interval per mailbox is configurable (30s-3600s). The scheduler
  tick runs every 60s, so anything below that is effectively clamped to 60s.

## 7.0.2 — 2026-04-24

Maintenance release: CI hotfix, Docker tag cleanup and three small fixes in
the new Printix direct import introduced in v7.0.1. No breaking changes.

### Fixes

- **Printix direct import (`/admin/users/import-printix`):**
  - Page size aligned with the rest of the codebase (`200` instead of `500`
    per role request). Avoids potential upper-bound issues on large tenants.
  - Dead `update_user` import removed from the POST handler.
  - **Temp password recovery on mail failure.** If an admin ticked
    "send invitation" but the mail send raised (SMTP down, wrong API key,
    quota hit …), the account was still created — but the UI blanked the
    generated one-time password, leaving the admin no way to communicate it
    to the user. The temp password is now shown whenever the invitation mail
    was **not** successfully delivered (no invite requested, mail not
    configured, or send error).

### CI / Release

- **`:latest` and `:stable` now track semver tags**, not the rolling `main`
  branch. Previously the metadata-action rule
  `enable={{is_default_branch}}` evaluated to false on tag pushes (the
  ref is the tag, not a branch), so `ghcr.io/.../printix-mcp-docker:latest`
  was silently stuck at whatever the last `main` push produced.
- **GHA cache backend disabled** (already in v7.0.1 hotfix). GitHub's cache
  service v2 rollout in early 2026 caused sporadic `404 Not Found` on
  `FinalizeCacheEntryUpload`, which in combination with `build-push-action`
  v6's build-summary PNG rendering surfaced as unreadable
  "buildx failed with: <base64 blob>" errors. Cold builds take ~20 min but
  are reliable; see the note in `.github/workflows/docker-publish.yml` for
  the registry-cache alternative if we need the speed back.

## 7.0.1 — 2026-04-24

UX polish on top of v7.0.0 plus a switch to English-only GitHub-facing docs.

### New

- **Printix direct import** on `/admin/users` as the primary import path.
  Admins see a checkbox list of all users from the Printix cloud (`USER` +
  `GUEST_USER` roles, minus anyone already imported) and can pick some or
  all of them. Each selected user gets a local account in one step — an
  auto-generated temp password plus, optionally, an invitation mail.
- **Button row on `/admin/users` restructured.** Printix import is the
  primary action now; invite, manual create and CSV import stay available
  as secondary options.

### Changes

- **Roadmap navigation hidden.** The `/roadmap` routes still exist for
  direct access to legacy data, but the nav link is gone from both the
  desktop and mobile menus.
- **Repository language: English.** README, CHANGELOG and the comments in
  `docker-compose.yml` + `.env.example` are now English across the board.
  The web UI keeps its i18n system and the German translation is still the
  default; only the public GitHub-facing content changed.

## 7.0.0 — 2026-04-24

First release as a standalone Docker image
(`ghcr.io/mnimtz/printix-mcp-docker`). Up to v6.7.118 the MCP server only
shipped as a Home Assistant add-on.

### Breaking changes

- **Single-tenant model.** Previously every invited user got their own
  isolated tenant — which never made sense for a self-hosted deployment
  serving a single organisation. Starting with v7.0.0 there is exactly
  **one** tenant per installation; all users share it. Migration happens
  automatically on the first start (see below).
- **2-role model.** Roles reduced to `admin` and `employee`. The legacy
  `user` role is migrated to `employee` at startup.
- **HA-add-on path removed.** No `run.sh`, no `config.yaml` ports, no
  ingress integration. If you're still on the HA add-on, stay on the
  v6.7.x branch.
- **Config cleanup.** The `HOST_*_PORT` env vars are gone — port mapping
  happens only in `docker-compose.yml`. `CAPTURE_PUBLIC_URL` is gone too;
  the capture URL is derived from `capture_public_url` (DB) or the main
  URL.

### New features

- **Multiple equal admins per tenant** — any admin can manage users,
  rotate credentials and configure the Printix integration.
- **CSV bulk import** under `/admin/users/bulk-import`. Required column:
  `email`. Optional: `full_name`, `username`, `company`, `local_role`,
  `printix_role`. Checkbox options: send an invitation mail with a temp
  password and/or create the user in Printix (`USER` or `GUEST_USER`).
- **Last-admin safeguard.** The last remaining admin cannot be deleted,
  demoted or disabled — a new `LastAdminError` exception surfaces as a UI
  banner (`/admin/users?err=...`).
- **Tenant-owner protection.** The first admin (the tenant owner) cannot
  be deleted without an explicit transfer.

### Improvements

- **Unified port config.** `docker-compose.yml` is the single place for
  host-port mappings; `.env` now only holds runtime settings.
- **Simpler URL resolution (2 tiers).** `public_url` (DB, admin UI)
  overrides `MCP_PUBLIC_URL` (env). Fallback to the request host for LAN
  mode.
- **Admin settings page** shows the effective public URL plus its source
  ("DB setting" / "env" / "request host") — no more guessing.
- **Capture URL resolution** simplified from 5 tiers to 3
  (DB override ➜ main URL ➜ request).

### Migration (automatic on first start)

1. `role_type='user'` → `role_type='employee'`
2. `parent_user_id` is set to the oldest admin (the tenant owner) when it
   was empty
3. Empty / orphaned tenant records (left over from the old
   "one-tenant-per-user" model) are removed

Existing data is preserved. A backup before the upgrade is still
recommended (`/admin/settings` → Backup).

### Internal

- Deprecations removed: `_create_empty_tenant()` calls out of
  `create_user_admin`, `create_invited_user`, `get_or_create_entra_user`
- `get_parent_user_id` now resolves the tenant owner for **all** users,
  not just employees
- `<HA-IP>` hard-coded fallbacks removed from `app.py`, `server.py`,
  `capture_server.py`, `employee_routes.py`
