# Changelog

This project follows [Semantic Versioning](https://semver.org/).

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
