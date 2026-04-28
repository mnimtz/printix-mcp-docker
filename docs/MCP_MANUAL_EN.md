# Printix MCP — User Handbook

> **Version:** 6.8.10 · **Tool inventory:** 127 tools · **As of:** April 2026
> **Audience:** Administrators, helpdesk and power users who interact with the Printix MCP Server through an AI assistant (claude.ai, ChatGPT, Claude Desktop, Cursor, etc.).
> **Language:** English · German version see `MCP_MANUAL_DE.pdf`

---

## ⚠️ Important: keep your AI assistant's tool list up to date

When the MCP server gets a new release, **new tools** are usually added. To make sure your AI assistant actually picks them up, the tool list has to be reloaded. **A server upgrade alone is not enough** — the client caches tool definitions.

| Client | How to refresh the tool list |
|--------|------------------------------|
| **claude.ai (web)** | *Settings → Connectors → Printix MCP → disconnect → reconnect*. Or just start a **new conversation** — the tool list is pulled on the first message. |
| **ChatGPT (custom connector)** | In the *Custom GPT editor*, click *Disconnect* on the MCP server, then *Connect*. Closing and re-opening the tab usually works too. |
| **Claude Desktop** | **Full app restart** (`Cmd+Q` then re-launch — *not* just close the window). Tools are loaded on startup. |
| **Cursor / Continue / others** | Toggle the connector off and on, or use the client's `/mcp reload` (varies by client). |

**Quick test** to see if a new tool has arrived: ask your assistant *"Which Printix tools do you have?"*. If you see e.g. `printix_print_self` or `printix_welcome_user` in the list, you're current. If not, do the refresh above.

---

## What is Printix MCP?

The Printix MCP Server bridges modern AI assistants and the Printix Cloud Print API. It exposes **127 tools** that let you drive Printix in natural language — from the simple *"Which printers do we have in Düsseldorf?"* to complex workflows like *"Send me this weekly report as a PDF to my printer and archive a copy in Paperless tagged 'Q1-report'."*

You **don't have to memorize tool names**. The assistant picks the right tool for your question. This handbook shows you *what's possible* so you can ask focused questions.

---

## How to read this handbook

Each category has a short intro, a tool table with purpose descriptions and several **example dialogues** with concrete prompts and tool calls. You can use the prompts verbatim or as inspiration.

🆕 marks tools added in v6.8.0–v6.8.10 (April 2026).

---

## Table of Contents

1. [System & Self-Diagnosis](#1-system--self-diagnosis)
2. [Printers, Sites & Networks](#2-printers-sites--networks)
3. [Print Jobs & Cloud Print](#3-print-jobs--cloud-print)
4. [Users, Groups & Workstations](#4-users-groups--workstations)
5. [Cards & Card Profiles](#5-cards--card-profiles)
6. [Reports & Analytics](#6-reports--analytics)
7. [Report Templates & Scheduling](#7-report-templates--scheduling)
8. [Capture & Document Workflow](#8-capture--document-workflow)
9. [Onboarding, Time-Bombs & Entra Sync 🆕](#9-onboarding-time-bombs--entra-sync-)
10. [Operations, Maintenance & Audit](#10-operations-maintenance--audit)
11. [Tips for productive AI dialogues](#11-tips-for-productive-ai-dialogues)

---

## 1. System & Self-Diagnosis

Meta questions: *Who am I? Is everything running? What's my role? What should I do next?* Great as a session opener — or when something doesn't work and you need to know **why**.

| Tool | Purpose |
|------|---------|
| `printix_status` | Health check: server up, tenant reachable, configured credential scopes. |
| `printix_whoami` | Current tenant + own Printix user + admin status. |
| `printix_tenant_summary` | Snapshot: printers, users, sites, cards, open jobs counts. |
| `printix_explain_error` | Translates a Printix error code or message to plain English + remedy hints. |
| `printix_suggest_next_action` | Suggests a sensible next step based on a context string. |
| `printix_natural_query` | Takes a natural-language question and proposes the matching reports tool. |

### Example dialogues

**Prompt:** *"Is Printix up?"*
→ `printix_status` reports API connection, tenant ID and configured credential scopes.

**Prompt:** *"Who am I logged in as in Printix?"*
→ `printix_whoami` returns tenant, your email and admin flag.

**Prompt:** *"Give me an overview of my tenant."*
→ `printix_tenant_summary` returns all key counts in one block.

**Prompt:** *"What does error 'AADSTS700025' mean?"*
→ `printix_explain_error("AADSTS700025")` explains it (public client / no client_secret with PKCE) and suggests fixes.

**Prompt:** *"What should I do next? I just installed a new printer."*
→ `printix_suggest_next_action("new printer installed")` suggests SNMP config check, test print, health report.

---

## 2. Printers, Sites & Networks

Physical and logical infrastructure: printers, queues, sites, networks, SNMP configs. Read and write operations. The `*_context` tools return aggregated views (queue + printer + recent jobs in one call).

| Tool | Purpose |
|------|---------|
| `printix_list_printers` | All printers (optional search). |
| `printix_get_printer` | Details + capabilities of a specific printer. |
| `printix_resolve_printer` | Fuzzy match (name + location + model + site). |
| `printix_network_printers` | All printers in a network or site. |
| `printix_get_queue_context` | Queue + printer object + recent jobs in one call. |
| `printix_printer_health_report` | Status grouped: online / offline / error states. |
| `printix_top_printers` | Top-N printers by volume. |
| `printix_list_sites` / `printix_get_site` | Site listing / details. |
| `printix_create_site` / `printix_update_site` / `printix_delete_site` | Site management. |
| `printix_site_summary` | Site + networks + printers in one aggregated block. |
| `printix_list_networks` / `printix_get_network` | Network listing / details. |
| `printix_create_network` / `printix_update_network` / `printix_delete_network` | Network management. |
| `printix_get_network_context` | Network + site + printers in one block. |
| `printix_list_snmp_configs` / `printix_get_snmp_config` | SNMP configs. |
| `printix_create_snmp_config` / `printix_delete_snmp_config` | SNMP config create/delete. |
| `printix_get_snmp_context` | SNMP config + affected printers + network. |

### Example dialogues

**Prompt:** *"Which Brother printers are in Düsseldorf?"*
→ `printix_resolve_printer("Brother Düsseldorf")` token-fuzzy match across name/location/vendor/site.

**Prompt:** *"Show me all printers in network 9cfa4bf0."*
→ `printix_network_printers(network_id="9cfa4bf0")` resolves the site (when no direct network→printer mapping exists) and returns the relevant printers.

**Prompt:** *"Give me a complete summary of the DACH site."*
→ `printix_site_summary(site_id=…)` — site meta + networks + all printers.

**Prompt:** *"Which printers are currently offline?"*
→ `printix_printer_health_report` groups by status, problems first.

**Prompt:** *"Top 5 printers by page count last week?"*
→ `printix_top_printers(days=7, limit=5, metric="pages")`.

**Prompt:** *"Create a new site 'Hamburg' at Mönckebergstraße 7."*
→ `printix_create_site(name="Hamburg", address="Mönckebergstraße 7", ...)`.

---

## 3. Print Jobs & Cloud Print

View, submit and delegate print jobs. **🆕 v6.8.x**: three new high-level tools (`print_self`, `print_to_recipients`, `session_print`) for AI workflows with native PDF/PCL conversion.

> 🆕 **Auto PDL conversion (v6.8.8+)**: All print tools auto-convert PDF/PostScript/Text to PCL XL via Ghostscript before submitting to the printer queue. Without this, printers without a PDF RIP would print hieroglyphs (raw PDF source as ASCII). Default param `pdl="auto"` (= PCL XL color). Use `pdl="passthrough"` to send the file unchanged.

| Tool | Purpose |
|------|---------|
| `printix_list_jobs` | All jobs, optional queue filter. |
| `printix_get_job` | Job details. |
| `printix_submit_job` | Low-level job submit (stage 1 of the 5-stage flow). |
| `printix_complete_upload` | Finish upload. |
| `printix_delete_job` | Cancel job. |
| `printix_change_job_owner` | Delegate job to another user. |
| `printix_jobs_stuck` | Jobs hanging more than N minutes. |
| `printix_quick_print` | Single-shot: URL + recipient → done. |
| `printix_send_to_user` | Send document (URL or base64) to user X. v6.8.8+: with auto-conversion. |
| 🆕 `printix_print_self` | Print to **own** secure-print queue (AI generates PDF inline). |
| 🆕 `printix_print_to_recipients` | Multi-recipient: one PDF to many recipients (also via `group:Name` or `entra:OID`). |
| 🆕 `printix_resolve_recipients` | Diagnostic: shows which recipients would resolve before `print_to_recipients`. |
| 🆕 `printix_session_print` | Job with time-bomb (auto-expire after N hours). |

### Example dialogues

**Prompt:** *"Send this PDF as Secure Print to marcus@firma.de."*
→ `printix_send_to_user(user_email="marcus@firma.de", file_content_b64=..., filename="contract.pdf")` — server converts PDF→PCL XL and queues the job.

**Prompt:** *"Which jobs have been stuck for more than 30 minutes?"*
→ `printix_jobs_stuck(minutes=30)`.

**Prompt:** *"Hand over job 4711 to marcus@firma.de — I'm going on vacation."*
→ `printix_change_job_owner(job_id="4711", new_owner_email="marcus@firma.de")`.

🆕 **Prompt:** *"Build me an A4 print template with the quarterly numbers and queue it for me to pick up at the printer."*
→ AI generates PDF inline → `printix_print_self(file_b64=..., filename="Q1.pdf")` puts the job in your own queue. Pick it up at the printer with card/code.

🆕 **Prompt:** *"Send this memo to everyone in the Marketing group as Secure Print."*
→ `printix_print_to_recipients(recipients_csv="group:Marketing", file_b64=..., filename="memo.pdf")` — one job per member.

🆕 **Prompt:** *"Before I send: who exactly is on the list?"*
→ `printix_resolve_recipients("group:Marketing, alice@firma.de, entra:abc-uuid")` shows the resolved recipients without printing. Ambiguities + not-found inputs are listed separately.

🆕 **Prompt:** *"Send this draft contract to externer-gast@partner.de — should auto-expire after 4 hours."*
→ `printix_session_print(user_email="externer-gast@partner.de", file_b64=..., filename="contract.pdf", expires_in_hours=4)` — submits the job and arms a cleanup time-bomb.

🆕 **Prompt:** *"Print mono only — no need for color."*
→ Set `color=False` on any print tool (default `True`).

🆕 **Prompt:** *"My printer does the PDF RIP itself — just pass the file through."*
→ Set `pdl="passthrough"` to disable server-side conversion.

---

## 4. Users, Groups & Workstations

Full lifecycle: create, edit, deactivate, diagnose. The `user_360` and `diagnose_user` tools are helpdesk all-rounders. **🆕 v6.8.x**: detailed group membership + print pattern analysis + quota guard.

| Tool | Purpose |
|------|---------|
| `printix_list_users` | All users, with pagination + role filter. |
| `printix_get_user` | User details. |
| `printix_find_user` | Fuzzy search by email or name. |
| `printix_user_360` | 360° view: user + cards + groups + workstations + recent jobs. |
| `printix_diagnose_user` | Helpdesk diagnosis: what works, what doesn't, why. |
| `printix_create_user` / `printix_delete_user` | User management. |
| `printix_generate_id_code` | New ID code for a user. |
| `printix_onboard_user` / `printix_offboard_user` | Guided on/offboarding (multi-step in one call). |
| `printix_list_admins` | All admins. |
| `printix_permission_matrix` | Matrix: user × permissions. |
| `printix_inactive_users` | Users idle for N days. |
| `printix_sso_status` | Check SSO mapping. |
| `printix_list_groups` / `printix_get_group` | Group listing / details. |
| `printix_create_group` / `printix_delete_group` | Group management. |
| 🆕 `printix_get_group_members` | Members of a group by UUID or name (ambiguity-safe). |
| 🆕 `printix_get_user_groups` | Reverse lookup: which groups is user X in? |
| 🆕 `printix_describe_user_print_pattern` | Print profile of a user: top printers, color quote, page count. |
| 🆕 `printix_quota_guard` | Pre-flight burst check before submit (verdict allow/throttle/block). |
| 🆕 `printix_print_history_natural` | History with natural-language windows ("today", "last week", "Q1", "7d"). |
| `printix_list_workstations` / `printix_get_workstation` | Workstations listing / details. |

### Example dialogues

**Prompt:** *"Tell me everything you know about marcus@firma.de."*
→ `printix_user_360(query="marcus@firma.de")` returns the full 360° view.

**Prompt:** *"Why can't Anna print anymore?"*
→ `printix_diagnose_user(email="anna@firma.de")` checks status, SSO, cards, groups, blockers — and suggests fixes.

**Prompt:** *"Which users have been inactive for 180 days?"*
→ `printix_inactive_users(days=180)` — offboarding candidate list.

**Prompt:** *"Onboard a new employee: peter@firma.de, Peter Meier, group 'Finance'."*
→ `printix_onboard_user(...)` runs all steps in order.

🆕 **Prompt:** *"Who's in the Marketing group?"*
→ `printix_get_group_members("Marketing")` — for ambiguous names you get a candidate list with UUIDs.

🆕 **Prompt:** *"Which groups is Anna in?"*
→ `printix_get_user_groups("anna@firma.de")` — checks user object first, falls back to group scan.

🆕 **Prompt:** *"What did Marcus print today?"*
→ `printix_print_history_natural(user_email="marcus@firma.de", when="today")`.

🆕 **Prompt:** *"Marcus's print pattern over the last 30 days?"*
→ `printix_describe_user_print_pattern(user_email="marcus@firma.de", days=30)` — top printers, color quote, average pages.

🆕 **Prompt:** *"Before I send another PDF: has the user not sent too many in the last 5 minutes?"*
→ `printix_quota_guard(user_email="marcus@firma.de", window_minutes=5, max_jobs=10)` — verdict `allow`/`throttle`/`block`.

---

## 5. Cards & Card Profiles

Everything around RFID/Mifare/HID cards: registration, mapping, profiles, bulk import. **🆕 v6.8.x**: AI wrapper `card_enrol_assist` that does UID + profile-transform + register in one tool.

| Tool | Purpose |
|------|---------|
| `printix_list_cards` | Cards of a user. |
| `printix_list_cards_by_tenant` | All cards in tenant (filter: `all`/`registered`/`orphaned`). |
| `printix_search_card` | Search card by ID/number. |
| `printix_register_card` | Assign card to user (low-level). |
| `printix_delete_card` | Remove card assignment. |
| `printix_get_card_details` | Card + local mapping + owner in one block. |
| `printix_decode_card_value` | Decode raw card value (Base64/Hex/YSoft/Konica variants). |
| `printix_transform_card_value` | Run value through transformation pipeline (hex↔dec, reverse, prefix/suffix …). |
| `printix_get_user_card_context` | User + all cards + profiles in one block. |
| `printix_list_card_profiles` / `printix_get_card_profile` | Profile listing/details. |
| `printix_search_card_mappings` | Search local mapping DB. |
| `printix_bulk_import_cards` | CSV bulk import (with profile + dry run). |
| `printix_suggest_profile` | Suggest profile from sample UID (top-10). |
| `printix_card_audit` | Audit trail of all card changes for a user. |
| `printix_find_orphaned_mappings` | Local mappings without matching Printix user. |
| 🆕 `printix_card_enrol_assist` | AI onboarding: UID + profile transform + register in one call. |

### Example dialogues

**Prompt:** *"Which cards does Marcus have?"*
→ `printix_get_user_card_context(email="marcus@firma.de")` — user + all cards + profiles.

**Prompt:** *"What is card UID `04:5F:F0:02:AB:3C`?"*
→ `printix_decode_card_value(card_value="04:5F:F0:02:AB:3C")` recognizes hex with separators, returns decoded bytes + profile hint.

**Prompt:** *"Import 500 cards from this CSV — but first as a dry run."*
→ `printix_bulk_import_cards(csv=..., profile=..., dry_run=True)` validates each row and shows previews without writing.

**Prompt:** *"For UID `045FF002` — which profile fits?"*
→ `printix_suggest_profile(sample_uid="045FF002")` — top-10 with score + `best_match`.

🆕 **Prompt:** *"Marcus tapped his ID card on the iPhone. UID is `04A1B2C3D4E5F6`. Please register it for him."*
→ `printix_card_enrol_assist(user_email="marcus@firma.de", card_uid_raw="04A1B2C3D4E5F6")` — runs through the default card profile and registers it via `register_card`.

---

## 6. Reports & Analytics

Reports run against the separate SQL Server warehouse. You get key figures, trends, anomalies and ad-hoc queries through a unified interface. `query_any` is the universal entry point; the specialized tools are quicker shortcuts for common questions.

| Tool | Purpose |
|------|---------|
| `printix_reporting_status` | Reports engine status (DB connection, last nightlies, preset count). |
| `printix_query_any` | Universal: preset + filters → table. |
| `printix_query_print_stats` | Print volume by dimension. |
| `printix_query_cost_report` | Costs, optionally by department/user. |
| `printix_query_top_users` / `printix_query_top_printers` | Top-N with time window. |
| `printix_query_anomalies` | Anomaly detection. |
| `printix_query_trend` | Trend lines over time. |
| `printix_query_audit_log` | Structured audit trail of the MCP server (actions, objects, actor). |
| `printix_top_printers` / `printix_top_users` | Short-form wrappers. |
| `printix_print_trends` | Trend by day/week/month. |
| `printix_cost_by_department` | Costs aggregated per department. |
| `printix_compare_periods` | Period A vs period B. |

### Example dialogues

**Prompt:** *"Who printed the most last month?"*
→ `printix_top_users(days=30, limit=10, metric="pages")`.

**Prompt:** *"Show the print trend for the last 90 days, monthly."*
→ `printix_print_trends(group_by="month", days=90)`.

**Prompt:** *"Compare the last 30 days with the 30 days before — what changed?"*
→ `printix_compare_periods(days_a=30, days_b=30)` returns delta KPIs.

**Prompt:** *"Which department has the highest print costs?"*
→ `printix_cost_by_department(days=30)`.

**Prompt:** *"What did user X do in the MCP on April 15?"*
→ `printix_query_audit_log(start_date="2026-04-15", end_date="2026-04-15", actor_email="x@firma.de")`.

**Prompt:** *"Are there any anomalies in print behavior the last 14 days?"*
→ `printix_query_anomalies(days=14)` — e.g. sudden volume spikes or unusual printer usage patterns.

---

## 7. Report Templates & Scheduling

For analyses you need regularly: save as template, schedule recurring delivery, ship by email. Design options via `list_design_options`; `preview_report` renders a PDF preview without sending.

| Tool | Purpose |
|------|---------|
| `printix_save_report_template` | Save query + design as template. |
| `printix_list_report_templates` | All saved templates. |
| `printix_get_report_template` | Template details. |
| `printix_delete_report_template` | Delete template. |
| `printix_run_report_now` | Run template once, deliver. |
| `printix_send_test_email` | Test email (SMTP check). |
| `printix_schedule_report` | Schedule template as cron job. |
| `printix_list_schedules` | All active schedules. |
| `printix_update_schedule` / `printix_delete_schedule` | Schedule modify/delete. |
| `printix_list_design_options` | Available color schemes, logos, layouts. |
| `printix_preview_report` | PDF preview of a report without sending. |

### Example dialogues

**Prompt:** *"Save the current top-10 user report as template 'Monthly Print Top10'."*
→ `printix_save_report_template(...)`.

**Prompt:** *"Send this template on the 1st working day of every month to management@firma.de."*
→ `printix_schedule_report(report_id=…, cron="0 8 1 * *", recipients=["management@firma.de"])`.

**Prompt:** *"Show me a PDF preview of template XY."*
→ `printix_preview_report(report_id=…)`.

**Prompt:** *"Which color schemes can I use for reports?"*
→ `printix_list_design_options()`.

---

## 8. Capture & Document Workflow

Capture connects scanned or AI-generated documents to target systems (Paperless-ngx, SharePoint, DMS …) via plugins. **🆕 v6.8.x**: two new tools that let the AI feed files directly into a capture workflow — not only via the classic webhook path.

| Tool | Purpose |
|------|---------|
| `printix_list_capture_profiles` | All capture profiles in tenant. |
| `printix_capture_status` | Status: server port, webhook base URL, available plugins, configured profiles. |
| 🆕 `printix_describe_capture_profile` | Plugin schema of a profile: which metadata fields are accepted, plus current config (secrets masked). |
| 🆕 `printix_send_to_capture` | Feed file directly into capture workflow — same code path as a webhook, no printer/blob detour. |

### Example dialogues

**Prompt:** *"Is capture active and which plugins are installed?"*
→ `printix_capture_status` — plugin list (e.g. paperless_ngx) + count of configured profiles.

**Prompt:** *"Which capture profiles do I have?"*
→ `printix_list_capture_profiles` — list with target system, webhook URL and recent runs.

🆕 **Prompt:** *"What metadata does the Paperless profile accept?"*
→ `printix_describe_capture_profile("Paperless (Marcus)")` — plugin schema (`tags`, `correspondent`, `document_type` etc.) plus current default values.

🆕 **Prompt:** *"Save this AI-generated contract to Paperless with tags 'Q1', 'Contract' and correspondent 'Acme Corp'."*
→ `printix_send_to_capture(profile="Paperless (Marcus)", file_b64=..., filename="contract_acme.pdf", metadata_json='{"tags":["Q1","Contract"], "correspondent":"Acme Corp", "document_type":"Contract"}')`.

🆕 **Prompt:** *"My weekly report is done — file it in Paperless and send a print copy to my printer too."*
→ Two calls: `printix_send_to_capture(...)` + `printix_print_self(...)` — the assistant chains both automatically.

---

## 9. Onboarding, Time-Bombs & Entra Sync 🆕

Everything in this section is **new in v6.8.x**. It's about automated user lifecycle workflows: a freshly created user receives a welcome PDF, reminder time-bombs are scheduled, and 7 days later we follow up automatically if no action has been taken. Plus: Entra/AD group sync with Printix groups.

| Tool | Purpose |
|------|---------|
| 🆕 `printix_welcome_user` | Onboarding companion: welcome PDF + time-bombs for `card_enrol`, `first_print_reminder`. |
| 🆕 `printix_list_timebombs` | View active/past time-bombs in tenant. |
| 🆕 `printix_defuse_timebomb` | Manually defuse a time-bomb (with audit reason). |
| 🆕 `printix_sync_entra_group_to_printix` | Diff Entra/AD group members vs Printix group members (default `report_only`). |

### Example dialogues

🆕 **Prompt:** *"Onboard new employee peter@firma.de and run the welcome workflow with reminders."*
→ `printix_onboard_user(...)` (for DB/account creation) + `printix_welcome_user(user_email="peter@firma.de")` puts a welcome PDF in his secure-print queue AND arms two time-bombs: 3-day reminder if no first print, 7-day reminder if no card enrolled.

🆕 **Prompt:** *"Which onboarding reminders are currently active?"*
→ `printix_list_timebombs(status="pending")`.

🆕 **Prompt:** *"Peter just told me he's on holiday until next week. Defuse his reminders."*
→ `printix_list_timebombs(user_email="peter@firma.de")` shows his bombs with IDs, then `printix_defuse_timebomb(bomb_id=42, reason="on holiday until 2026-05-12")`.

🆕 **Prompt:** *"Sync Entra group `Marketing-DACH` (OID `abc-123`) with our Printix group — but report only, no writes."*
→ `printix_sync_entra_group_to_printix(entra_group_oid="abc-123", printix_group_id="def-456", sync_mode="report_only")` returns diff: `to_add` (in Entra but not Printix) and `to_remove` (in Printix but not Entra).

> ℹ️ **Prerequisite for `sync_entra_group_to_printix`**: the MCP server's Entra app registration needs the application permission `Group.Read.All` (Application, not Delegated) + admin consent. Add it in the Azure portal.

### Time-bomb concept

Time-bombs are **conditional, deferred actions**. A cron job runs hourly (`minute=7`) and for every pending bomb checks:

1. Is the condition still met? (e.g. *"user has no card enrolled yet"*)
2. If yes → run the action (queue a reminder PDF, log entry, etc.).
3. If no → bomb is auto-marked `defused`.

So the `first_print_reminder_3d` bomb stays pending after 3 days — if the user actually printed in between, it auto-defuses. Only if the user really did nothing, the reminder fires.

---

## 10. Operations, Maintenance & Audit

Backups, demo data, feature tracking. Mix of operations and meta.

| Tool | Purpose |
|------|---------|
| `printix_list_backups` | All existing backups. |
| `printix_create_backup` | New backup (DB + config + metadata). |
| `printix_demo_setup_schema` | Create demo schema in reports DB. |
| `printix_demo_generate` | Generate synthetic demo data. |
| `printix_demo_rollback` | Remove demo data (by demo tag). |
| `printix_demo_status` | Which demo sets are active? |
| `printix_list_feature_requests` / `printix_get_feature_request` | Ticket system for feature requests. |

### Example dialogues

**Prompt:** *"Make a backup before I change anything."*
→ `printix_create_backup`.

**Prompt:** *"Set up a demo environment with 50 users and 500 jobs."*
→ `printix_demo_setup_schema` (once) + `printix_demo_generate(users=50, jobs=500)`.

**Prompt:** *"Show me all open feature requests."*
→ `printix_list_feature_requests(status="open")`.

---

## 11. Tips for productive AI dialogues

1. **Think in goals, not tools.** *"Who prints too much?"* beats *"call query_top_users with days=30"*. The assistant picks the right tool.
2. **Provide context.** *"Marcus from finance"* is clearer than just *"Marcus"*.
3. **Use the 360° tools.** `printix_user_360`, `printix_get_queue_context`, `printix_site_summary` save you follow-up questions.
4. **Ask "why" on errors.** *"Why did this fail?"* triggers `printix_explain_error` or `printix_diagnose_user`.
5. **Dry run before bulk operations.** `printix_bulk_import_cards(dry_run=True)`, `printix_resolve_recipients` (before `print_to_recipients`), `printix_sync_entra_group_to_printix(sync_mode="report_only")`.
6. **🆕 Multi-step workflows in a single prompt.** The assistant can chain tool calls: *"Generate a weekly report PDF, archive it in Paperless, and print a copy for me."*
7. **🆕 Trust auto-conversion in `print_*` tools.** Default `pdl="auto"` does the right thing 99% of the time (PCL XL color). Only override when your printer queue does conversion server-side (`passthrough`) or you explicitly need PostScript.
8. **🆕 Refresh the tool list regularly** (see notice at the top). Otherwise the assistant uses outdated tool definitions.

---

*Document generated from Printix MCP Server v6.8.10 · April 2026 · 127 tools · [Repository](https://github.com/mnimtz/Printix-MCP)*
