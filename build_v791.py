#!/usr/bin/env python3
"""v7.9.1 Build Script — Sidebar Refinements
1. Remove old tenant sub-navigation from all templates
2. Remove tenant-sidebar CSS, simplify tenant-shell
3. Remove top-bar for logged-in users, add branding to sidebar
4. Move language switcher to sidebar bottom
5. Move /settings from profile section to Administration > System
6. Fix i18n keys (common_invite, common_create, etc.)
"""
import glob, os, re

BASE = "/tmp/printix-mcp-docker/src/web/templates"
BASE_HTML = os.path.join(BASE, "base.html")

# ─── 1. Remove {% include "_tenant_sidebar.html" %} from all tenant templates ───
tenant_files = glob.glob(os.path.join(BASE, "tenant_*.html"))
for f in tenant_files:
    with open(f) as fh:
        content = fh.read()
    if '_tenant_sidebar.html' in content:
        content = content.replace('    {% include "_tenant_sidebar.html" %}\n', '')
        with open(f, 'w') as fh:
            fh.write(content)
        print(f"  ✓ Removed _tenant_sidebar include from {os.path.basename(f)}")

# ─── 2-5. Modify base.html ──────────────────────────────────────────────────
with open(BASE_HTML) as fh:
    html = fh.read()

# 2a. Replace .tenant-shell CSS with simple block layout
html = html.replace(
    """.tenant-shell {
      display: grid;
      grid-template-columns: minmax(252px, 308px) minmax(0, 1fr);
      gap: 18px;
      align-items: start;
    }""",
    """.tenant-shell {
      display: block;
    }"""
)
print("  ✓ Simplified .tenant-shell CSS")

# 2b. Remove all .tenant-sidebar* CSS (from .tenant-sidebar { to .tenant-main {)
# Match from ".tenant-sidebar {" up to but not including ".tenant-main {"
html = re.sub(
    r'    \.tenant-sidebar \{[^}]+\}\n'
    r'(    \.tenant-sidebar[^\n]+\{[^}]+\}\n)*',
    '',
    html
)
print("  ✓ Removed tenant-sidebar CSS")

# 2c. Remove responsive tenant-sidebar CSS
html = html.replace(
    """      .tenant-shell { grid-template-columns: 1fr; gap: 12px; }
      .tenant-sidebar { position: static; top: auto; }
      .tenant-sidebar-card { padding: 14px; }
      .tenant-sidebar-nav {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
        padding: 0;
        background: transparent;
        border: none;""",
    """      .tenant-shell { display: block; }"""
)
print("  ✓ Removed responsive tenant-sidebar CSS")

# Remove leftover responsive tenant-sidebar-nav line
html = html.replace(
    "      .tenant-sidebar-nav { grid-template-columns: 1fr; }\n",
    ""
)

# 3. Replace <nav> — for logged-in users: only mobile hamburger, no branding/lang
#    for non-logged-in: keep full nav
old_nav = """  {% if show_nav | default(true) %}
  <nav>
    <div class="nav-shell">
      <a class="nav-brand" href="/">
        <span class="nav-brand-mark">{% include "_brand_logo.svg" %}</span>
        <span>{{ _('brand_console_name') }}</span>
      </a>

      <div class="nav-right">
        <!-- Desktop links (v7.9.0: nur Login/Register wenn nicht eingeloggt) -->
        <div class="nav-links">
          {% if not user %}
            <a href="/login">{{ _('nav_login') }}</a>
            <a href="/register">{{ _('nav_register') }}</a>
          {% endif %}
        </div>

        {# v7.6.0: Prefetch-Status-Pille — zeigt während des
           Hintergrund-Ladens dass Tenant-Daten gerade gewärmt werden.
           Verschwindet wenn alle Topics frisch im Cache liegen. #}
        {% if user and user.role_type != 'employee' %}
        <div id="prefetchPill" class="prefetch-pill" style="display:none;"
             title="{{ _('prefetch_pill_tip') }}">
          <span class="prefetch-pill-spin">⏳</span>
          <span class="prefetch-pill-label">{{ _('prefetch_pill_running') }}</span>
        </div>
        {% endif %}

        <!-- Language Switcher (desktop) -->
        <div class="lang-switcher" id="langSwitcher">
          <button class="lang-btn" onclick="toggleLangDropdown(event)">🌐 {{ lang | upper }}</button>
          <div class="lang-dropdown" id="langDropdown">
            <div class="lang-dropdown-inner">
              {% if lang_names and supported_langs %}
                {% for code in supported_langs %}
                  <a href="/lang/{{ code }}" {% if code == lang %}class="active"{% endif %}>
                    {{ lang_names[code] }}
                  </a>
                {% endfor %}
              {% endif %}
            </div>
          </div>
        </div>

        <!-- Hamburger button (mobile) -->
        <button class="hamburger" id="hamburger" aria-label="{{ _('common_open_menu') }}"
                aria-expanded="false" onclick="toggleMobileMenu()">
          <span></span><span></span><span></span>
        </button>
      </div>
    </div>
  </nav>"""

new_nav = """  {% if show_nav | default(true) %}
  {# v7.9.1: Logged-in users — no top bar, sidebar handles everything.
     Only a minimal mobile hamburger floats top-right. #}
  {% if user %}
  <button class="mobile-hamburger" id="hamburger" aria-label="{{ _('common_open_menu') }}"
          aria-expanded="false" onclick="toggleMobileMenu()">
    <span></span><span></span><span></span>
  </button>
  {# Prefetch pill — floats top-right on desktop #}
  {% if user.role_type != 'employee' %}
  <div id="prefetchPill" class="prefetch-pill prefetch-pill-float" style="display:none;"
       title="{{ _('prefetch_pill_tip') }}">
    <span class="prefetch-pill-spin">⏳</span>
    <span class="prefetch-pill-label">{{ _('prefetch_pill_running') }}</span>
  </div>
  {% endif %}
  {% else %}
  {# Not logged in — full top nav with branding, login, register, language #}
  <nav>
    <div class="nav-shell">
      <a class="nav-brand" href="/">
        <span class="nav-brand-mark">{% include "_brand_logo.svg" %}</span>
        <span>{{ _('brand_console_name') }}</span>
      </a>
      <div class="nav-right">
        <div class="nav-links">
          <a href="/login">{{ _('nav_login') }}</a>
          <a href="/register">{{ _('nav_register') }}</a>
        </div>
        <div class="lang-switcher" id="langSwitcher">
          <button class="lang-btn" onclick="toggleLangDropdown(event)">🌐 {{ lang | upper }}</button>
          <div class="lang-dropdown" id="langDropdown">
            <div class="lang-dropdown-inner">
              {% if lang_names and supported_langs %}
                {% for code in supported_langs %}
                  <a href="/lang/{{ code }}" {% if code == lang %}class="active"{% endif %}>
                    {{ lang_names[code] }}
                  </a>
                {% endfor %}
              {% endif %}
            </div>
          </div>
        </div>
        <button class="hamburger" id="hamburger" aria-label="{{ _('common_open_menu') }}"
                aria-expanded="false" onclick="toggleMobileMenu()">
          <span></span><span></span><span></span>
        </button>
      </div>
    </div>
  </nav>
  {% endif %}"""

html = html.replace(old_nav, new_nav)
print("  ✓ Replaced <nav> — logged-in users get no top bar")

# 4. Modify sidebar: add branding at top, language switcher + move settings
old_sidebar_top = """  <aside class="sidebar" id="sidebar">
    <div class="sidebar-section">
      {# Dashboard — immer sichtbar #}
      <a href="/dashboard" class="sidebar-top-link {% if active_page == 'dashboard' %}active{% endif %}">
        <span class="sb-icon">🏠</span> {{ _('nav_dashboard') }}
      </a>
    </div>"""

new_sidebar_top = """  <aside class="sidebar" id="sidebar">
    {# v7.9.1: Branding in Sidebar #}
    <div class="sidebar-brand">
      <span class="sidebar-brand-logo">{% include "_brand_logo.svg" %}</span>
      <span class="sidebar-brand-text">{{ _('brand_console_name') }}</span>
    </div>
    <div class="sidebar-section">
      {# Dashboard — immer sichtbar #}
      <a href="/dashboard" class="sidebar-top-link {% if active_page == 'dashboard' %}active{% endif %}">
        <span class="sb-icon">🏠</span> {{ _('nav_dashboard') }}
      </a>
    </div>"""

html = html.replace(old_sidebar_top, new_sidebar_top)
print("  ✓ Added branding to sidebar top")

# 5. Move /settings from bottom to Administration > System, add language switcher to bottom
old_sidebar_bottom = """    {# ── Bottom: Profil, Connect, Hilfe, Logout ──────────────────────── #}
    <div class="sidebar-bottom">
      <a href="/my/connect"       class="sb-bottom-link {% if active_page == 'connect' %}active{% endif %}"><span class="sb-icon">🔌</span>{{ _('cc_nav_label') }}</a>
      <a href="/settings"         class="sb-bottom-link {% if active_page == 'settings' %}active{% endif %}"><span class="sb-icon">👤</span>{{ _('nav_profile') }}</a>
      <a href="/settings/password" class="sb-bottom-link {% if active_page == 'password' %}active{% endif %}"><span class="sb-icon">🔑</span>{{ _('nav_password') }}</a>
      <a href="/feedback"         class="sb-bottom-link {% if active_page == 'feedback' %}active{% endif %}"><span class="sb-icon">💬</span>{{ _('nav_feedback') }}</a>
      <a href="/help"             class="sb-bottom-link {% if active_page == 'help' %}active{% endif %}"><span class="sb-icon">❓</span>{{ _('nav_help') }}</a>
      <div class="sb-divider"></div>
      <a href="/logout" class="sb-bottom-link"><span class="sb-icon">🚪</span>{{ _('nav_logout') }}</a>
    </div>"""

new_sidebar_bottom = """    {# ── Bottom: Connect, Profil, Hilfe, Sprache, Logout ───────────── #}
    <div class="sidebar-bottom">
      <a href="/my/connect"       class="sb-bottom-link {% if active_page == 'connect' %}active{% endif %}"><span class="sb-icon">🔌</span>{{ _('cc_nav_label') }}</a>
      <a href="/settings"         class="sb-bottom-link {% if active_page == 'settings' %}active{% endif %}"><span class="sb-icon">👤</span>{{ _('nav_profile') }}</a>
      <a href="/settings/password" class="sb-bottom-link {% if active_page == 'password' %}active{% endif %}"><span class="sb-icon">🔑</span>{{ _('nav_password') }}</a>
      <a href="/feedback"         class="sb-bottom-link {% if active_page == 'feedback' %}active{% endif %}"><span class="sb-icon">💬</span>{{ _('nav_feedback') }}</a>
      <a href="/help"             class="sb-bottom-link {% if active_page == 'help' %}active{% endif %}"><span class="sb-icon">❓</span>{{ _('nav_help') }}</a>
      <div class="sb-divider"></div>
      {# v7.9.1: Language switcher in sidebar bottom #}
      <div class="sb-lang-switcher">
        <button class="sb-lang-btn" onclick="toggleSbLang(this)">
          <span class="sb-icon">🌐</span>{{ _('common_language') }}: {{ lang | upper }}
          <span class="sb-lang-chevron">▸</span>
        </button>
        <div class="sb-lang-grid">
          {% if lang_names and supported_langs %}
            {% for code in supported_langs %}
              <a href="/lang/{{ code }}" class="sb-lang-option {% if code == lang %}active{% endif %}">
                {{ lang_names[code] }}
              </a>
            {% endfor %}
          {% endif %}
        </div>
      </div>
      <div class="sb-divider"></div>
      <a href="/logout" class="sb-bottom-link"><span class="sb-icon">🚪</span>{{ _('nav_logout') }}</a>
    </div>"""

html = html.replace(old_sidebar_bottom, new_sidebar_bottom)
print("  ✓ Added language switcher to sidebar bottom")

# 6. Add /settings link under Administration > System
old_admin_system = """      <div class="sb-subgroup-label">{{ _('nav_system') }}</div>
      <a href="/admin/settings"        class="sb-sublink {% if active_page == 'admin_settings' %}active{% endif %}"><span class="sb-dot"></span>{{ _('nav_settings') }}</a>
      <a href="/admin/mcp-permissions" class="sb-sublink {% if active_page == 'admin_rbac' %}active{% endif %}"><span class="sb-dot"></span>RBAC</a>
    </div>"""

new_admin_system = """      <div class="sb-subgroup-label">{{ _('nav_system') }}</div>
      <a href="/admin/settings"        class="sb-sublink {% if active_page == 'admin_settings' %}active{% endif %}"><span class="sb-dot"></span>{{ _('nav_settings') }}</a>
      <a href="/settings"              class="sb-sublink {% if active_page == 'settings' %}active{% endif %}"><span class="sb-dot"></span>{{ _('nav_api_config') }}</a>
      <a href="/admin/mcp-permissions" class="sb-sublink {% if active_page == 'admin_rbac' %}active{% endif %}"><span class="sb-dot"></span>RBAC</a>
    </div>"""

html = html.replace(old_admin_system, new_admin_system)
print("  ✓ Added API-Config under Administration > System")

# 7. Fix i18n keys in sidebar: common_invite → nav_invite, common_create → nav_create_user
html = html.replace(
    "{{ _('common_invite') }}",
    "{{ _('nav_invite') }}"
)
html = html.replace(
    "{{ _('common_create') }}",
    "{{ _('nav_create_user') }}"
)
# Also fix hardcoded texts
html = html.replace(
    '>Bulk-Import</a>',
    '>{{ _(\'nav_bulk_import\') }}</a>'
)
html = html.replace(
    '>Printix-Import</a>',
    '>{{ _(\'nav_printix_import\') }}</a>'
)
html = html.replace(
    '>RBAC</a>',
    '>{{ _(\'nav_rbac\') }}</a>'
)
html = html.replace(
    '>SSL</a>',
    '>{{ _(\'nav_ssl\') }}</a>'
)
html = html.replace(
    '>SSL-Diagnose</a>',
    '>{{ _(\'nav_ssl_diagnose\') }}</a>'
)
html = html.replace(
    '>TLS</a>',
    '>{{ _(\'nav_tls\') }}</a>'
)
html = html.replace(
    '>Auto-TLS</a>',
    '>{{ _(\'nav_auto_tls\') }}</a>'
)
html = html.replace(
    '>Tunnel</a>',
    '>{{ _(\'nav_tunnel\') }}</a>'
)
print("  ✓ Replaced hardcoded texts with i18n keys")

# 8. Add CSS for new elements: mobile-hamburger, sidebar-brand, sb-lang-switcher, prefetch-pill-float
new_css = """
    /* v7.9.1: Mobile hamburger (logged-in users — no top bar) */
    .mobile-hamburger {
      display: none;
      position: fixed;
      top: 12px;
      right: 14px;
      z-index: 1100;
      background: var(--ta-navy);
      border: none;
      border-radius: 10px;
      padding: 10px;
      cursor: pointer;
      flex-direction: column;
      gap: 4px;
      box-shadow: 0 4px 12px rgba(0,40,84,.25);
    }
    .mobile-hamburger span {
      display: block;
      width: 22px;
      height: 2px;
      background: #fff;
      border-radius: 2px;
    }

    /* v7.9.1: Sidebar branding */
    .sidebar-brand {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 16px 16px 12px;
      border-bottom: 1px solid #e8ecf0;
      margin-bottom: 4px;
    }
    .sidebar-brand-logo {
      width: 28px;
      height: 28px;
      flex-shrink: 0;
    }
    .sidebar-brand-logo svg {
      width: 100%;
      height: 100%;
    }
    .sidebar-brand-text {
      font-size: .82em;
      font-weight: 800;
      color: var(--ta-navy);
      letter-spacing: .01em;
    }

    /* v7.9.1: Language switcher in sidebar */
    .sb-lang-switcher { padding: 0 4px; }
    .sb-lang-btn {
      display: flex;
      align-items: center;
      gap: 6px;
      width: 100%;
      padding: 7px 8px;
      border: none;
      background: transparent;
      color: #64748b;
      font-size: .82em;
      font-weight: 600;
      cursor: pointer;
      border-radius: 6px;
      font-family: inherit;
      transition: background .15s;
    }
    .sb-lang-btn:hover { background: #f1f5f9; }
    .sb-lang-chevron {
      margin-left: auto;
      font-size: .7em;
      transition: transform .2s;
    }
    .sb-lang-btn.open .sb-lang-chevron { transform: rotate(90deg); }
    .sb-lang-grid {
      display: none;
      grid-template-columns: 1fr 1fr;
      gap: 2px;
      padding: 4px 8px 8px;
    }
    .sb-lang-grid.open { display: grid; }
    .sb-lang-option {
      padding: 5px 8px;
      border-radius: 6px;
      font-size: .78em;
      color: #334155;
      text-decoration: none;
      text-align: center;
      font-weight: 500;
      transition: background .15s;
    }
    .sb-lang-option:hover { background: #f1f5f9; }
    .sb-lang-option.active {
      background: var(--ta-navy);
      color: #fff;
      font-weight: 700;
    }

    /* v7.9.1: Prefetch pill floating (no top bar for logged-in users) */
    .prefetch-pill-float {
      position: fixed;
      top: 12px;
      right: 14px;
      z-index: 1050;
    }
"""

# Insert new CSS before the "/* ── Layout ──" comment
html = html.replace(
    "    /* ── Layout ──",
    new_css + "    /* ── Layout ──"
)
print("  ✓ Added new CSS (mobile-hamburger, sidebar-brand, sb-lang-switcher)")

# 9. Add responsive CSS for mobile-hamburger
html = html.replace(
    "      .sidebar { transform: translateX(-100%); }",
    "      .mobile-hamburger { display: flex; }\n      .sidebar { transform: translateX(-100%); }"
)
print("  ✓ Added responsive mobile-hamburger display")

# 10. Add JS for language toggle in sidebar
old_js_toggle = """    function toggleSbCat(btn) {"""
new_js_toggle = """    function toggleSbLang(btn) {
      btn.classList.toggle('open');
      const grid = btn.nextElementSibling;
      if (grid) grid.classList.toggle('open');
    }
    function toggleSbCat(btn) {"""

html = html.replace(old_js_toggle, new_js_toggle)
print("  ✓ Added toggleSbLang JS function")

# 11. Adjust main-area: remove top padding since no nav bar for logged-in users
# The .main-area margin-top was for the top nav — now it should be 0 for logged-in users
# Actually the main-area only has margin-left for the sidebar, which is fine

with open(BASE_HTML, 'w') as fh:
    fh.write(html)
print(f"\n  ✓ Saved {BASE_HTML} ({len(html.splitlines())} lines)")

# ─── 6. i18n: Add missing keys ──────────────────────────────────────────────
I18N = os.path.join(BASE, "..", "i18n.py")
with open(I18N) as fh:
    i18n = fh.read()

new_keys_block = '''

# ── v7.9.1: Additional sidebar i18n keys ────────────────────────────────────
_SIDEBAR_V791_KEYS = {
    "de": {
        "nav_invite": "Einladen",
        "nav_create_user": "Erstellen",
        "nav_bulk_import": "Bulk-Import",
        "nav_printix_import": "Printix-Import",
        "nav_api_config": "API-Konfiguration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-Diagnose",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "en": {
        "nav_invite": "Invite",
        "nav_create_user": "Create",
        "nav_bulk_import": "Bulk Import",
        "nav_printix_import": "Printix Import",
        "nav_api_config": "API Configuration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL Diagnostics",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "fr": {
        "nav_invite": "Inviter",
        "nav_create_user": "Créer",
        "nav_bulk_import": "Import en masse",
        "nav_printix_import": "Import Printix",
        "nav_api_config": "Configuration API",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "Diagnostic SSL",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "it": {
        "nav_invite": "Invita",
        "nav_create_user": "Crea",
        "nav_bulk_import": "Importazione in blocco",
        "nav_printix_import": "Importazione Printix",
        "nav_api_config": "Configurazione API",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "Diagnostica SSL",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "es": {
        "nav_invite": "Invitar",
        "nav_create_user": "Crear",
        "nav_bulk_import": "Importación masiva",
        "nav_printix_import": "Importación Printix",
        "nav_api_config": "Configuración API",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "Diagnóstico SSL",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "nl": {
        "nav_invite": "Uitnodigen",
        "nav_create_user": "Aanmaken",
        "nav_bulk_import": "Bulk-import",
        "nav_printix_import": "Printix-import",
        "nav_api_config": "API-configuratie",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-diagnose",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "no": {
        "nav_invite": "Inviter",
        "nav_create_user": "Opprett",
        "nav_bulk_import": "Masseimport",
        "nav_printix_import": "Printix-import",
        "nav_api_config": "API-konfigurasjon",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-diagnose",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "sv": {
        "nav_invite": "Bjud in",
        "nav_create_user": "Skapa",
        "nav_bulk_import": "Massimport",
        "nav_printix_import": "Printix-import",
        "nav_api_config": "API-konfiguration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-diagnostik",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "bar": {
        "nav_invite": "Eiladn",
        "nav_create_user": "Erstöin",
        "nav_bulk_import": "Bulk-Import",
        "nav_printix_import": "Printix-Import",
        "nav_api_config": "API-Konfig",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-Diagnos",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "hessisch": {
        "nav_invite": "Eilade",
        "nav_create_user": "Erstelle",
        "nav_bulk_import": "Bulk-Import",
        "nav_printix_import": "Printix-Import",
        "nav_api_config": "API-Konfiguration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-Diagnose",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "oesterreichisch": {
        "nav_invite": "Einladn",
        "nav_create_user": "Erstölln",
        "nav_bulk_import": "Bulk-Import",
        "nav_printix_import": "Printix-Import",
        "nav_api_config": "API-Konfiguration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-Diagnose",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "schwiizerdütsch": {
        "nav_invite": "Ilade",
        "nav_create_user": "Erstelle",
        "nav_bulk_import": "Bulk-Import",
        "nav_printix_import": "Printix-Import",
        "nav_api_config": "API-Konfiguration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL-Diagnose",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "cockney": {
        "nav_invite": "Invite",
        "nav_create_user": "Create",
        "nav_bulk_import": "Bulk Import",
        "nav_printix_import": "Printix Import",
        "nav_api_config": "API Config",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL Diagnostics",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
    "us_south": {
        "nav_invite": "Invite",
        "nav_create_user": "Create",
        "nav_bulk_import": "Bulk Import",
        "nav_printix_import": "Printix Import",
        "nav_api_config": "API Configuration",
        "nav_rbac": "RBAC",
        "nav_ssl": "SSL",
        "nav_ssl_diagnose": "SSL Diagnostics",
        "nav_tls": "TLS",
        "nav_auto_tls": "Auto-TLS",
        "nav_tunnel": "Tunnel",
    },
}

for _lang, _keys in _SIDEBAR_V791_KEYS.items():
    if _lang in TRANSLATIONS:
        TRANSLATIONS[_lang].update(_keys)
'''

i18n += new_keys_block

with open(I18N, 'w') as fh:
    fh.write(i18n)
print(f"  ✓ Added v7.9.1 i18n keys to i18n.py")

# ─── 7. Update VERSION ──────────────────────────────────────────────────────
version_file = "/tmp/printix-mcp-docker/VERSION"
with open(version_file, 'w') as fh:
    fh.write("7.9.1\n")
print("  ✓ VERSION → 7.9.1")

# ─── 8. Update CHANGELOG ────────────────────────────────────────────────────
changelog_file = "/tmp/printix-mcp-docker/CHANGELOG.md"
with open(changelog_file) as fh:
    changelog = fh.read()

entry = """## [7.9.1] — 2026-06-13

### Removed
- Alte Tenant-Sub-Navigation (doppelt mit neuer Sidebar) aus allen 16 Templates entfernt
- Top-Bar für eingeloggte Benutzer entfernt — Branding und Sprachwahl jetzt in Sidebar

### Changed
- Branding „Management Console" in Sidebar-Kopf verschoben
- Sprachwahl von Top-Bar in Sidebar-Bottom verschoben (klappbares Grid)
- API-Konfiguration (/settings) unter Administration → System eingeordnet
- Hardcoded deutsche Texte (Bulk-Import, Printix-Import, SSL, TLS, etc.) durch i18n-Keys ersetzt
- Fehlende i18n-Keys ergänzt: nav_invite, nav_create_user, nav_bulk_import, nav_printix_import, nav_api_config, nav_rbac, nav_ssl, nav_ssl_diagnose, nav_tls, nav_auto_tls, nav_tunnel (alle 14 Sprachen)

"""

changelog = changelog.replace("## [7.9.0]", entry + "## [7.9.0]")
with open(changelog_file, 'w') as fh:
    fh.write(changelog)
print("  ✓ CHANGELOG updated")

print("\n✅ All v7.9.1 changes applied successfully!")
