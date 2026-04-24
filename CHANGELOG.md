# Changelog

This project follows [Semantic Versioning](https://semver.org/).

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

- Printer- and queue-IDs in the guest-print forms are free-text for the MVP.
  Look them up under **Tenant -> Queues** and paste in; a dropdown resolver
  is follow-up work.
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
