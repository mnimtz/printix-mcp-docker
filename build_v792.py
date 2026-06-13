#!/usr/bin/env python3
"""v7.9.2 Build Script — Alignment Fix + Navigation Cleanup + Sub-Page Modernization

Changes:
1. Fix sidebar top: 56px → 0 (no top bar for logged-in users)
2. Fix main-area min-height: calc(100vh - 56px) → 100vh
3. Fix sidebar-overlay top: 56px → 0
4. Reduce container top margin
5. Remove /settings from sidebar-bottom (duplicate — already under Admin → System)
6. Modernize sub-page CSS (tables, badges, alerts, buttons, search)
7. Bump VERSION, update CHANGELOG
"""
import re

BASE_HTML = "/tmp/printix-mcp-docker/src/web/templates/base.html"

with open(BASE_HTML) as fh:
    html = fh.read()

changes = 0

# --- 1. Fix sidebar top alignment ---
old = "position: fixed; top: 56px; left: 0; bottom: 0;"
new = "position: fixed; top: 0; left: 0; bottom: 0;"
if old in html:
    html = html.replace(old, new)
    changes += 1
    print("  OK Sidebar top: 56px -> 0")
else:
    print("  WARN Sidebar top:56px not found")

# --- 2. Fix main-area min-height ---
old = "min-height: calc(100vh - 56px);"
new = "min-height: 100vh;"
if old in html:
    html = html.replace(old, new)
    changes += 1
    print("  OK main-area min-height -> 100vh")
else:
    print("  WARN main-area calc not found")

# --- 3. Fix sidebar-overlay top ---
old = "display: none; position: fixed; inset: 0; top: 56px;"
new = "display: none; position: fixed; inset: 0; top: 0;"
if old in html:
    html = html.replace(old, new)
    changes += 1
    print("  OK sidebar-overlay top -> 0")
else:
    print("  WARN sidebar-overlay top:56px not found")

# --- 4. Reduce container top margin ---
old = "margin: 28px auto;"
new = "margin: 16px auto;"
if old in html:
    html = html.replace(old, new)
    changes += 1
    print("  OK Container margin 28px -> 16px")
else:
    print("  WARN Container margin:28px not found")

# --- 5. Remove /settings from sidebar-bottom (Profil) ---
old_profil = '      <a href="/settings"         class="sb-bottom-link {% if active_page == \'settings\' %}active{% endif %}"><span class="sb-icon">\xf0\x9f\x91\xa4</span>{{ _(\'nav_profile\') }}</a>'
if old_profil in html:
    html = html.replace(old_profil + '\n', '')
    changes += 1
    print("  OK Removed /settings (Profil) from sidebar-bottom")
else:
    # Try line-by-line removal
    lines = html.split('\n')
    new_lines = []
    removed = False
    for l in lines:
        if '/settings"' in l and 'sb-bottom-link' in l and 'nav_profile' in l:
            removed = True
            continue
        new_lines.append(l)
    if removed:
        html = '\n'.join(new_lines)
        changes += 1
        print("  OK Removed /settings Profil line (fallback)")
    else:
        print("  WARN /settings Profil link not found")

# --- 6. Modernize sub-page CSS ---
modern_css = """
    /* -- v7.9.2: Modern Sub-Page Styling -- */

    /* Page header with Tungsten branding */
    .section-title {
      font-size: 1.3em;
      font-weight: 800;
      color: var(--ta-navy);
      letter-spacing: -.01em;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .section-sub {
      color: var(--ta-gray-500);
      font-size: .88em;
      margin-top: 2px;
    }

    /* Modern table styling */
    table {
      width: 100%;
      border-collapse: separate;
      border-spacing: 0;
    }
    table thead th {
      background: linear-gradient(180deg, #f8fafc 0%, #f1f5f9 100%);
      font-size: .76em;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: .06em;
      color: var(--ta-gray-500);
      padding: 10px 14px;
      border-bottom: 2px solid var(--ta-gray-300);
      text-align: left;
      position: sticky;
      top: 0;
      z-index: 1;
    }
    table thead th:first-child { border-radius: 10px 0 0 0; }
    table thead th:last-child { border-radius: 0 10px 0 0; }
    table tbody tr {
      transition: all .15s ease;
    }
    table tbody tr:hover {
      background: rgba(0, 160, 251, .04);
    }
    table tbody td {
      padding: 12px 14px;
      border-bottom: 1px solid #f1f5f9;
      font-size: .9em;
      color: #334155;
      vertical-align: middle;
    }
    table tbody tr:last-child td {
      border-bottom: none;
    }
    table tbody td a {
      color: var(--ta-cyan-dark);
      font-weight: 600;
      text-decoration: none;
      transition: color .15s;
    }
    table tbody td a:hover {
      color: var(--ta-cyan);
      text-decoration: underline;
    }

    /* Modern search bar */
    .search-bar, .card input[type="text"][placeholder*="earch"],
    .card input[type="text"][placeholder*="uche"],
    .card input[type="text"][placeholder*="ilter"] {
      background: #f8fafc;
      border: 1.5px solid var(--ta-gray-300);
      border-radius: 12px;
      padding: 12px 18px;
      font-size: .92em;
      transition: all .2s;
    }
    .search-bar:focus, .card input[type="text"]:focus {
      background: #fff;
      border-color: var(--ta-cyan);
      box-shadow: 0 0 0 4px rgba(0,160,251,.1);
    }

    /* Better card with backdrop blur */
    .card {
      backdrop-filter: blur(8px);
    }

    /* Enhanced badges */
    .badge, .role-badge, [class*="badge"] {
      font-size: .72em;
      font-weight: 700;
      padding: 4px 10px;
      border-radius: 999px;
      letter-spacing: .02em;
      white-space: nowrap;
    }

    /* Queue pills / tags */
    .tag, .queue-tag, .pill {
      display: inline-block;
      padding: 3px 10px;
      border-radius: 8px;
      font-size: .78em;
      font-weight: 600;
      background: rgba(0,160,251,.08);
      color: var(--ta-cyan-dark);
      margin: 2px 3px 2px 0;
      transition: all .15s;
    }
    .tag:hover, .queue-tag:hover, .pill:hover {
      background: rgba(0,160,251,.14);
    }

    /* Divider gradient */
    .divider, hr {
      border: none;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--ta-gray-300), transparent);
      margin: 20px 0;
    }

    /* Modernized alerts */
    .alert {
      border-radius: 12px;
      padding: 14px 18px;
      font-size: .88em;
      display: flex;
      align-items: flex-start;
      gap: 10px;
      border: none;
    }
    .alert-success {
      background: linear-gradient(135deg, #dcfce7, #bbf7d0);
      color: #14532d;
    }
    .alert-error, .alert-danger {
      background: linear-gradient(135deg, #fef2f2, #fecaca);
      color: #991b1b;
    }
    .alert-info {
      background: linear-gradient(135deg, #eff6ff, #dbeafe);
      color: #1e40af;
    }
    .alert-warning {
      background: linear-gradient(135deg, #fffbeb, #fef3c7);
      color: #92400e;
    }

    /* Filter buttons as pills */
    .filter-btn, .card .btn-sm {
      font-size: .82em;
      padding: 6px 14px;
      border-radius: 999px;
      border: 1.5px solid var(--ta-gray-300);
      background: #fff;
      color: #64748b;
      font-weight: 600;
      cursor: pointer;
      transition: all .15s;
    }
    .filter-btn:hover, .card .btn-sm:hover {
      border-color: var(--ta-cyan);
      color: var(--ta-cyan-dark);
      background: rgba(0,160,251,.04);
    }
    .filter-btn.active, .card .btn-sm.active {
      background: var(--ta-navy);
      color: #fff;
      border-color: var(--ta-navy);
    }

    /* Primary button with Gold gradient */
    .btn-primary {
      background: linear-gradient(135deg, var(--ta-gold) 0%, #e6b200 100%);
      box-shadow: 0 2px 8px rgba(255,198,0,.25);
    }
    .btn-primary:hover {
      background: linear-gradient(135deg, #e6b200 0%, #cc9e00 100%);
      box-shadow: 0 4px 16px rgba(255,198,0,.35);
      transform: translateY(-1px);
    }

    /* Better scrollbar for main area */
    .main-area {
      scrollbar-width: thin;
      scrollbar-color: var(--ta-gray-300) transparent;
    }

"""

# Remove old section-title definitions
old_section = '    /* -- Section header'
# Use a safer approach: find and replace old CSS blocks
old_section_block = """    /* -- Section header"""
# Find the section-title line
old_st = "    .section-title { font-size: 1.05em; font-weight: 700; color: var(--ta-navy); margin-bottom: 6px; }"
if old_st in html:
    html = html.replace(old_st, "    /* section-title replaced by v7.9.2 */")
    print("  OK Replaced old section-title")
old_ss = "    .section-sub   { color: #6b7280; font-size: .88em; margin-bottom: 20px; }"
if old_ss in html:
    html = html.replace(old_ss, "    /* section-sub replaced by v7.9.2 */")
    print("  OK Replaced old section-sub")

# Remove old table CSS
old_t1 = "    table { width: 100%; border-collapse: collapse; font-size: .9em; }"
if old_t1 in html:
    html = html.replace(old_t1, "    /* table base replaced by v7.9.2 */")
    print("  OK Replaced old table base")

old_th = """    th {
      text-align: left; padding: 10px 14px; background: #f8fafc; color: #6b7280;
      font-size: .82em; font-weight: 600; text-transform: uppercase;
      letter-spacing: .05em; border-bottom: 1px solid #e5e7eb;
    }"""
if old_th in html:
    html = html.replace(old_th, "    /* th replaced by v7.9.2 */")
    print("  OK Replaced old th")

old_td = "    td { padding: 10px 14px; border-bottom: 1px solid #f1f5f9; vertical-align: middle; }"
if old_td in html:
    html = html.replace(old_td, "    /* td replaced by v7.9.2 */")
    print("  OK Replaced old td")

old_trlast = "    tr:last-child td { border-bottom: none; }"
if old_trlast in html:
    html = html.replace(old_trlast, "    /* tr:last-child replaced by v7.9.2 */")

old_trhover = "    tr:hover td { background: #f8fafc; }"
if old_trhover in html:
    html = html.replace(old_trhover, "    /* tr:hover replaced by v7.9.2 */")

# Remove old alert CSS
old_alert_base = "    .alert { padding: 12px 16px; border-radius: 8px; margin-bottom: 20px; font-size: .9em; }"
if old_alert_base in html:
    html = html.replace(old_alert_base, "    /* alert base replaced by v7.9.2 */")
    print("  OK Replaced old alert base")

old_alert_err = "    .alert-error   { background: #fee2e2; border: 1px solid #fca5a5; color: #991b1b; }"
if old_alert_err in html:
    html = html.replace(old_alert_err, "    /* alert-error replaced by v7.9.2 */")
old_alert_suc = "    .alert-success { background: #dcfce7; border: 1px solid #86efac; color: #166534; }"
if old_alert_suc in html:
    html = html.replace(old_alert_suc, "    /* alert-success replaced by v7.9.2 */")
old_alert_inf = "    .alert-info    { background: rgba(0,160,251,.10); border: 1px solid var(--ta-cyan-light); color: var(--ta-navy); }"
if old_alert_inf in html:
    html = html.replace(old_alert_inf, "    /* alert-info replaced by v7.9.2 */")
old_alert_warn = "    .alert-warning { background: #fef9c3; border: 1px solid #fde047; color: #854d0e; }"
if old_alert_warn in html:
    html = html.replace(old_alert_warn, "    /* alert-warning replaced by v7.9.2 */")

# Remove old badge base
old_badge = """    .badge {
      display: inline-block; padding: 2px 8px; border-radius: 99px;
      font-size: .78em; font-weight: 600;
    }"""
if old_badge in html:
    html = html.replace(old_badge, "    /* badge base replaced by v7.9.2 */")
    print("  OK Replaced old badge")

# Remove old divider
old_div = "    .divider { height: 1px; background: #e5e7eb; margin: 24px 0; }"
if old_div in html:
    html = html.replace(old_div, "    /* divider replaced by v7.9.2 */")
    print("  OK Replaced old divider")

# Insert modern CSS before Forms section
forms_marker = "    /* -- Forms"
if forms_marker not in html:
    forms_marker = "    /* ── Forms"
if forms_marker in html:
    html = html.replace(forms_marker, modern_css + forms_marker)
    changes += 1
    print("  OK Inserted modern sub-page CSS block")
else:
    print("  WARN Forms marker not found")

# --- Save ---
with open(BASE_HTML, 'w') as fh:
    fh.write(html)
print(f"\n  OK Saved base.html ({len(html.splitlines())} lines)")

# --- VERSION ---
with open("/tmp/printix-mcp-docker/VERSION", 'w') as fh:
    fh.write("7.9.2\n")
print("  OK VERSION -> 7.9.2")

# --- CHANGELOG ---
cl_path = "/tmp/printix-mcp-docker/CHANGELOG.md"
with open(cl_path) as fh:
    cl = fh.read()

entry = """## [7.9.2] -- 2026-06-13

### Fixed
- Sidebar und Content starten jetzt buendig ab Oberkante (top: 0 statt 56px)
- Container-Margin reduziert fuer besseres Alignment
- /settings (Tenant-Konfiguration) aus Sidebar-Bottom Profil entfernt (ist bereits unter Administration -> System)

### Changed
- Modernisiertes Sub-Seiten-Design: Tabellen mit Hover-Effekten, sticky Headers, abgerundete Ecken
- Bessere Suchleisten mit Focus-Effekt und Cyan-Glow
- Alert-Boxen mit Gradient-Backgrounds
- Buttons mit Gold-Gradient und Hover-Animation
- Filter-Buttons als Pills mit Cyan-Akzent
- Badges mit groesserem Padding und voller Abrundung
- Divider mit Gradient-Styling
- Karten mit Backdrop-Blur-Effekt

"""

cl = cl.replace("## [7.9.1]", entry + "## [7.9.1]")
with open(cl_path, 'w') as fh:
    fh.write(cl)
print("  OK CHANGELOG updated")

print(f"\nDONE v7.9.2 -- {changes} core changes applied!")
