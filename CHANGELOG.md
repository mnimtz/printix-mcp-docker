# Changelog

This project follows [Semantic Versioning](https://semver.org/).

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
