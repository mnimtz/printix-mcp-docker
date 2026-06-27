# Legal Pages — v7.9.4 Implementation Report

Date: 2026-06-27

## New public routes (all login-free, return `Cache-Control: public, max-age=3600`)

- `GET /privacy` — Privacy Policy (default language)
- `GET /datenschutz` — Privacy Policy, forces DE for first-time visitors
- `GET /imprint` — Imprint (TMG-conformant when country=Germany, else generic)
- `GET /impressum` — Imprint, forces DE for first-time visitors
- `GET /legal` — landing page linking the two

Plus the admin-only POST handler:

- `POST /admin/settings/legal/save` — persists 9 operator settings + audit log

## New templates

- `src/web/templates/legal_index.html`
- `src/web/templates/legal_privacy.html`
- `src/web/templates/legal_imprint.html`

Visual style matches the existing Tungsten/Printix sidebar pages (cyan/navy/gold tokens, card-based layout, generous whitespace).

## i18n keys

- DE: 60 new keys
- EN: 60 new keys
- All other languages (fr, it, es, nl, no, sv, bar, hessisch, oesterreichisch, schwiizerdütsch, cockney, us_south) fall back to EN automatically via the existing `make_translator` chain.

## DB settings added (all plain text — they are intentionally public)

- `legal_operator_name`
- `legal_operator_address`
- `legal_operator_email`
- `legal_operator_phone`
- `legal_operator_country`
- `legal_operator_vat_id`
- `legal_data_protection_officer`
- `legal_hosting_provider`
- `legal_supervisory_authority`

If `legal_operator_country` is unset the helper defaults to "Germany" so a fresh install still renders the TMG-style imprint.

## Files touched

```
M  CHANGELOG.md                              (+~80 lines)
M  VERSION                                   (7.9.3 -> 7.9.4)
M  src/web/app.py                            (+155 lines: 5 GET routes, 1 POST route, 4 helpers, legal block added to _admin_settings_ctx)
M  src/web/i18n.py                           (+~330 lines: DE+EN keys)
M  src/web/templates/admin_settings.html     (+~95 lines: new card after the timezone card)
M  src/web/templates/base.html               (+~20 lines: global footer block)
A  src/web/templates/legal_index.html        (44 lines)
A  src/web/templates/legal_privacy.html      (90 lines)
A  src/web/templates/legal_imprint.html      (105 lines)
A  LEGAL_PAGES_REPORT.md                     (this file)
```

## Validation performed

- `ast.parse` on `src/web/app.py` and `src/web/i18n.py` → OK
- Jinja render test with empty operator settings → all three templates render in DE + EN, no exceptions. Sizes: ~46–52 KB each (most of that is base.html shared CSS).
- Verified the "operator must configure this" warning banner appears on `/privacy`, `/imprint`, `/legal` and inside the admin card when name/address/email are empty.
- Verified the German imprint sections (§ 5 TMG intro) appear only when `operator_country` matches Germany/DE/Deutschland; other countries get the generic intro.
- Verified the footer appears on logged-in pages (via `base.html` after the main content block) AND on `/login`, `/register*` and the new `/legal*` pages because they all extend `base.html`.

## Commit + tag

Commit, tag `v7.9.4`, push to `origin/main` and push the tag — see the script the operator runs after reviewing this report. The commit message is in the task spec.

## Known minor items the user might want to polish later

1. **Translation coverage** — only DE + EN are populated; the other 12 languages silently fall back to EN. The fallback works because of `make_translator`, but if MySecurePrint ships in non-DE App Store regions a localised privacy policy is usually preferred. Add FR/IT/ES/NL/NO/SV when there's appetite.
2. **`legal_last_updated` is auto-derived from app.py mtime** — works for "code-driven" tracking but won't show the right date when only the i18n text changes without touching app.py. If you want a stricter "last *legal text* update" stamp, hard-code a date constant near `_legal_last_updated()` and bump it deliberately when the policy text changes.
3. **No machine-readable App Privacy Manifest output yet** — the iOS app needs `PrivacyInfo.xcprivacy` filed with the App Store entry separately; the server side is now ready but the iOS PrivacyInfo bundle still needs to be authored in the `printix-mcp-linux` sibling project that owns the Xcode workspace.
