# Printix MCP — Permission Matrix

**Comprehensive list of MCP tools and which roles can invoke them**

- Total tools tagged: **127**
- Generated for Printix MCP v7.2.26
- Source of truth: `src/permissions.py` (TOOL_SCOPES, ROLE_SCOPES)

## Role-to-scope summary

| Role | mcp:self | mcp:read | mcp:audit | mcp:write | mcp:system |
|------|:--------:|:--------:|:---------:|:---------:|:----------:|
| **End User** | ✓ | — | — | — | — |
| **Helpdesk** | ✓ | ✓ | — | — | — |
| **Admin** | ✓ | ✓ | ✓ | ✓ | ✓ |
| **Auditor** | — | ✓ | ✓ | — | — |
| **Service Acct.** | — | — | — | — | — |

---

## Scope `mcp:self` — Own data only

Tools that act on the caller's own data — print own jobs, look up own status, ask self-introspection questions.

**Allowed roles:** End User, Helpdesk, Admin

**Tool count:** 8

| # | Tool |
|---|------|
| 1 | `printix_explain_error` |
| 2 | `printix_generate_id_code` |
| 3 | `printix_my_role` |
| 4 | `printix_print_self` |
| 5 | `printix_session_print` |
| 6 | `printix_status` |
| 7 | `printix_suggest_next_action` |
| 8 | `printix_whoami` |

---

## Scope `mcp:read` — Read-only across the tenant

Tools that list, get, query, search, describe, resolve, or report — no mutations.

**Allowed roles:** Helpdesk, Admin, Auditor

**Tool count:** 77

| # | Tool |
|---|------|
| 1 | `printix_capture_status` |
| 2 | `printix_card_audit` |
| 3 | `printix_compare_periods` |
| 4 | `printix_cost_by_department` |
| 5 | `printix_decode_card_value` |
| 6 | `printix_describe_capture_profile` |
| 7 | `printix_describe_user_print_pattern` |
| 8 | `printix_diagnose_user` |
| 9 | `printix_find_orphaned_mappings` |
| 10 | `printix_find_user` |
| 11 | `printix_get_card_details` |
| 12 | `printix_get_card_profile` |
| 13 | `printix_get_feature_request` |
| 14 | `printix_get_group` |
| 15 | `printix_get_group_members` |
| 16 | `printix_get_job` |
| 17 | `printix_get_network` |
| 18 | `printix_get_network_context` |
| 19 | `printix_get_printer` |
| 20 | `printix_get_queue_context` |
| 21 | `printix_get_report_template` |
| 22 | `printix_get_site` |
| 23 | `printix_get_snmp_config` |
| 24 | `printix_get_snmp_context` |
| 25 | `printix_get_user` |
| 26 | `printix_get_user_card_context` |
| 27 | `printix_get_user_groups` |
| 28 | `printix_get_workstation` |
| 29 | `printix_inactive_users` |
| 30 | `printix_jobs_stuck` |
| 31 | `printix_list_admins` |
| 32 | `printix_list_backups` |
| 33 | `printix_list_capture_profiles` |
| 34 | `printix_list_card_profiles` |
| 35 | `printix_list_cards` |
| 36 | `printix_list_cards_by_tenant` |
| 37 | `printix_list_design_options` |
| 38 | `printix_list_feature_requests` |
| 39 | `printix_list_groups` |
| 40 | `printix_list_jobs` |
| 41 | `printix_list_networks` |
| 42 | `printix_list_printers` |
| 43 | `printix_list_report_templates` |
| 44 | `printix_list_schedules` |
| 45 | `printix_list_sites` |
| 46 | `printix_list_snmp_configs` |
| 47 | `printix_list_timebombs` |
| 48 | `printix_list_users` |
| 49 | `printix_list_workstations` |
| 50 | `printix_natural_query` |
| 51 | `printix_network_printers` |
| 52 | `printix_permission_matrix` |
| 53 | `printix_preview_report` |
| 54 | `printix_print_history_natural` |
| 55 | `printix_print_trends` |
| 56 | `printix_printer_health_report` |
| 57 | `printix_query_anomalies` |
| 58 | `printix_query_any` |
| 59 | `printix_query_cost_report` |
| 60 | `printix_query_print_stats` |
| 61 | `printix_query_top_printers` |
| 62 | `printix_query_top_users` |
| 63 | `printix_query_trend` |
| 64 | `printix_quota_guard` |
| 65 | `printix_reporting_status` |
| 66 | `printix_resolve_printer` |
| 67 | `printix_resolve_recipients` |
| 68 | `printix_search_card` |
| 69 | `printix_search_card_mappings` |
| 70 | `printix_site_summary` |
| 71 | `printix_sso_status` |
| 72 | `printix_suggest_profile` |
| 73 | `printix_tenant_summary` |
| 74 | `printix_top_printers` |
| 75 | `printix_top_users` |
| 76 | `printix_transform_card_value` |
| 77 | `printix_user_360` |

---

## Scope `mcp:audit` — Audit log

Read access to the structured audit trail (Art. 30).

**Allowed roles:** Admin, Auditor

**Tool count:** 1

| # | Tool |
|---|------|
| 1 | `printix_query_audit_log` |

---

## Scope `mcp:write` — Mutations

Create / update / delete operations on Printix entities and configurations.

**Allowed roles:** Admin

**Tool count:** 35

| # | Tool |
|---|------|
| 1 | `printix_bulk_import_cards` |
| 2 | `printix_card_enrol_assist` |
| 3 | `printix_change_job_owner` |
| 4 | `printix_complete_upload` |
| 5 | `printix_create_group` |
| 6 | `printix_create_network` |
| 7 | `printix_create_site` |
| 8 | `printix_create_snmp_config` |
| 9 | `printix_create_user` |
| 10 | `printix_delete_card` |
| 11 | `printix_delete_group` |
| 12 | `printix_delete_job` |
| 13 | `printix_delete_network` |
| 14 | `printix_delete_report_template` |
| 15 | `printix_delete_schedule` |
| 16 | `printix_delete_site` |
| 17 | `printix_delete_snmp_config` |
| 18 | `printix_delete_user` |
| 19 | `printix_offboard_user` |
| 20 | `printix_onboard_user` |
| 21 | `printix_print_to_recipients` |
| 22 | `printix_quick_print` |
| 23 | `printix_register_card` |
| 24 | `printix_run_report_now` |
| 25 | `printix_save_report_template` |
| 26 | `printix_schedule_report` |
| 27 | `printix_send_test_email` |
| 28 | `printix_send_to_capture` |
| 29 | `printix_send_to_user` |
| 30 | `printix_submit_job` |
| 31 | `printix_sync_entra_group_to_printix` |
| 32 | `printix_update_network` |
| 33 | `printix_update_schedule` |
| 34 | `printix_update_site` |
| 35 | `printix_welcome_user` |

---

## Scope `mcp:system` — System administration

Backups, demo data, time-bomb engine, sensitive system commands.

**Allowed roles:** Admin

**Tool count:** 6

| # | Tool |
|---|------|
| 1 | `printix_create_backup` |
| 2 | `printix_defuse_timebomb` |
| 3 | `printix_demo_generate` |
| 4 | `printix_demo_rollback` |
| 5 | `printix_demo_setup_schema` |
| 6 | `printix_demo_status` |

---

## Notes

- Tools not listed in this document fall back to `mcp:write` by default. This is intentional safe-by-default behaviour: any tool added to the server without an explicit scope tag remains admin-only until an operator categorises it.
- The Service Account role has no implicit scopes; permissions for non-human tokens are whitelisted explicitly per token.
- The MCP introspection tool `printix_my_role` is always available regardless of role — every user can ask their AI assistant *"what can I do?"* and get a structured answer back.
- Denied calls are recorded with `action='mcp_permission_denied'` in the `audit_log` table for compliance review.
