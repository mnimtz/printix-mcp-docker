# Changelog

This project follows [Semantic Versioning](https://semver.org/).

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
