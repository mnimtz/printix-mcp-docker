"""
Design Presets — Themes, Fonts, Layouts für Report-Templates (v3.8.0)
======================================================================
Zentrale Quelle der Wahrheit für alle Design-Optionen, die ein Report-Template
verwenden kann. Wird von:
  - reports_form.html      (Dropdowns, Theme-Picker)
  - report_engine.py        (HTML/PDF/XLSX-Rendering)
  - server.py MCP-Tools    (printix_report_list_themes, …)
  - reports_routes.py       (AI-Design-Chat — Parsing von Natural Language)
referenziert. Neue Themes/Fonts hier ergänzen → werden automatisch überall verfügbar.

Ein Layout-Dict hat nach v3.8.0 folgende Felder (alle optional, werden über
DEFAULT_LAYOUT aufgefüllt):

    # Branding
    company_name, footer_text, logo_base64, logo_url (legacy), logo_mime

    # Theme (entweder theme_id ODER explizite Farben — explizit gewinnt)
    theme_id, primary_color, accent_color, background_color, text_color,
    muted_color, table_header_bg, table_alt_bg

    # Typography
    font_family, font_size_base, font_size_h1, font_size_h2

    # Layout
    header_variant ("left" | "center" | "banner" | "minimal")
    density         ("compact" | "normal" | "airy")
    logo_position   ("left" | "right" | "center")

    # Charts
    charts_enabled (bool)
    chart_style    ("bars" | "minimal" | "none")

    # Analytics
    show_period_comparison (bool)
    show_env_impact        (bool)
    currency               ("EUR" | "USD" | "GBP" | "CHF")
"""

from typing import Any

# ── Themes (Farbpaletten) ────────────────────────────────────────────────────

# Jedes Theme hat ein `description_key` statt eines Literals — der Key wird
# zur Render-Zeit mit `t(key)` gegen die aktive UI-Sprache aufgelöst. So sind
# die Theme-Beschreibungen in der Form und in allen MCP-Tool-Outputs in der
# Sprache des Benutzers, nicht hardcoded Deutsch oder Englisch.

THEMES: dict[str, dict[str, Any]] = {
    "corporate_blue": {
        "name":             "Corporate Blue",
        "description_key":  "rpt_eng_theme_corporate_blue_desc",
        "primary_color":    "#0078D4",
        "accent_color":     "#005A9E",
        "background_color": "#FFFFFF",
        "text_color":       "#333333",
        "muted_color":      "#888888",
        "table_header_bg":  "#0078D4",
        "table_alt_bg":     "#F5F9FD",
    },
    "tungsten_red": {
        "name":             "Tungsten Red",
        "description_key":  "rpt_eng_theme_tungsten_red_desc",
        "primary_color":    "#C8102E",
        "accent_color":     "#8B0000",
        "background_color": "#FFFFFF",
        "text_color":       "#2B2B2B",
        "muted_color":      "#7A7A7A",
        "table_header_bg":  "#C8102E",
        "table_alt_bg":     "#FDF4F5",
    },
    "printix_green": {
        "name":             "Printix Green",
        "description_key":  "rpt_eng_theme_printix_green_desc",
        "primary_color":    "#1BA17D",
        "accent_color":     "#0F6E52",
        "background_color": "#FFFFFF",
        "text_color":       "#2E2E2E",
        "muted_color":      "#7E7E7E",
        "table_header_bg":  "#1BA17D",
        "table_alt_bg":     "#F3FAF7",
    },
    "executive_slate": {
        "name":             "Executive Slate",
        "description_key":  "rpt_eng_theme_executive_slate_desc",
        "primary_color":    "#3B4A5A",
        "accent_color":     "#1F2933",
        "background_color": "#FFFFFF",
        "text_color":       "#1F2933",
        "muted_color":      "#6B7280",
        "table_header_bg":  "#3B4A5A",
        "table_alt_bg":     "#F4F5F7",
    },
    "dark_mode": {
        "name":             "Dark Mode",
        "description_key":  "rpt_eng_theme_dark_mode_desc",
        "primary_color":    "#4FC3F7",
        "accent_color":     "#29B6F6",
        "background_color": "#1E1E2E",
        "text_color":       "#E4E4E4",
        "muted_color":      "#9A9A9A",
        "table_header_bg":  "#29B6F6",
        "table_alt_bg":     "#2A2A3C",
    },
    "sunrise_orange": {
        "name":             "Sunrise Orange",
        "description_key":  "rpt_eng_theme_sunrise_orange_desc",
        "primary_color":    "#FF7043",
        "accent_color":     "#E64A19",
        "background_color": "#FFFFFF",
        "text_color":       "#333333",
        "muted_color":      "#8A8A8A",
        "table_header_bg":  "#FF7043",
        "table_alt_bg":     "#FFF5F0",
    },
    "royal_purple": {
        "name":             "Royal Purple",
        "description_key":  "rpt_eng_theme_royal_purple_desc",
        "primary_color":    "#6A1B9A",
        "accent_color":     "#4A148C",
        "background_color": "#FFFFFF",
        "text_color":       "#2B2B2B",
        "muted_color":      "#7A7A7A",
        "table_header_bg":  "#6A1B9A",
        "table_alt_bg":     "#F8F4FB",
    },
    "minimal_mono": {
        "name":             "Minimal Mono",
        "description_key":  "rpt_eng_theme_minimal_mono_desc",
        "primary_color":    "#212121",
        "accent_color":     "#424242",
        "background_color": "#FFFFFF",
        "text_color":       "#212121",
        "muted_color":      "#757575",
        "table_header_bg":  "#212121",
        "table_alt_bg":     "#F5F5F5",
    },
}

# ── Fonts (Web-safe + PDF-kompatibel) ────────────────────────────────────────
#
# Wichtig: fpdf2 unterstützt nativ nur Courier / Helvetica / Times.
# Alle anderen Fonts fallen im PDF auf Helvetica zurück. Im HTML- und XLSX-
# Output funktionieren sie voll.

FONTS: list[dict[str, str]] = [
    {
        "key":             "arial",
        "name":            "Arial",
        "css":             "Arial, Helvetica, sans-serif",
        "pdf_family":      "Helvetica",
        "description_key": "rpt_eng_font_arial_desc",
    },
    {
        "key":             "helvetica",
        "name":            "Helvetica",
        "css":             "Helvetica, Arial, sans-serif",
        "pdf_family":      "Helvetica",
        "description_key": "rpt_eng_font_helvetica_desc",
    },
    {
        "key":             "inter",
        "name":            "Inter",
        "css":             "'Inter', 'Segoe UI', system-ui, sans-serif",
        "pdf_family":      "Helvetica",
        "description_key": "rpt_eng_font_inter_desc",
    },
    {
        "key":             "roboto",
        "name":            "Roboto",
        "css":             "'Roboto', 'Segoe UI', sans-serif",
        "pdf_family":      "Helvetica",
        "description_key": "rpt_eng_font_roboto_desc",
    },
    {
        "key":             "segoe",
        "name":            "Segoe UI",
        "css":             "'Segoe UI', system-ui, sans-serif",
        "pdf_family":      "Helvetica",
        "description_key": "rpt_eng_font_segoe_desc",
    },
    {
        "key":             "georgia",
        "name":            "Georgia (Serif)",
        "css":             "Georgia, 'Times New Roman', serif",
        "pdf_family":      "Times",
        "description_key": "rpt_eng_font_georgia_desc",
    },
    {
        "key":             "courier",
        "name":            "Courier (Mono)",
        "css":             "'Courier New', Courier, monospace",
        "pdf_family":      "Courier",
        "description_key": "rpt_eng_font_courier_desc",
    },
]

FONTS_BY_KEY = {f["key"]: f for f in FONTS}


# ── Layout-Varianten ─────────────────────────────────────────────────────────

# Layout-Varianten — label_key/description_key werden zur Render-Zeit von der
# UI übersetzt (`t(key)`). Siehe reports_form.html / list_*_summary().

HEADER_VARIANTS = [
    {"key": "left",    "label_key": "rpt_eng_header_left_label",    "description_key": "rpt_eng_header_left_desc"},
    {"key": "center",  "label_key": "rpt_eng_header_center_label",  "description_key": "rpt_eng_header_center_desc"},
    {"key": "banner",  "label_key": "rpt_eng_header_banner_label",  "description_key": "rpt_eng_header_banner_desc"},
    {"key": "minimal", "label_key": "rpt_eng_header_minimal_label", "description_key": "rpt_eng_header_minimal_desc"},
]

DENSITY_VARIANTS = [
    {"key": "compact", "label_key": "rpt_eng_density_compact_label", "description_key": "rpt_eng_density_compact_desc"},
    {"key": "normal",  "label_key": "rpt_eng_density_normal_label",  "description_key": "rpt_eng_density_normal_desc"},
    {"key": "airy",    "label_key": "rpt_eng_density_airy_label",    "description_key": "rpt_eng_density_airy_desc"},
]

LOGO_POSITIONS = [
    {"key": "left",   "label_key": "rpt_eng_logo_left"},
    {"key": "right",  "label_key": "rpt_eng_logo_right"},
    {"key": "center", "label_key": "rpt_eng_logo_center"},
]

CHART_STYLES = [
    {"key": "bars",    "label_key": "rpt_eng_chart_bars_label",    "description_key": "rpt_eng_chart_bars_desc"},
    {"key": "minimal", "label_key": "rpt_eng_chart_minimal_label", "description_key": "rpt_eng_chart_minimal_desc"},
    {"key": "none",    "label_key": "rpt_eng_chart_none_label",    "description_key": "rpt_eng_chart_none_desc"},
]

# Currencies are displayed with a locale-independent symbol and an i18n label.
CURRENCIES = [
    {"key": "EUR", "symbol": "€",   "label_key": "rpt_eng_currency_eur"},
    {"key": "USD", "symbol": "$",   "label_key": "rpt_eng_currency_usd"},
    {"key": "GBP", "symbol": "£",   "label_key": "rpt_eng_currency_gbp"},
    {"key": "CHF", "symbol": "CHF", "label_key": "rpt_eng_currency_chf"},
]

CURRENCIES_BY_KEY = {c["key"]: c for c in CURRENCIES}


# ── Defaults und Merge-Hilfen ────────────────────────────────────────────────

DEFAULT_LAYOUT: dict[str, Any] = {
    # Branding
    "company_name":     "",
    "footer_text":      "",
    "logo_base64":      "",
    "logo_url":         "",         # Legacy — wird beim Laden migriert
    "logo_mime":        "image/png",

    # Theme (Corporate Blue als Default)
    "theme_id":         "corporate_blue",
    "primary_color":    "#0078D4",
    "accent_color":     "#005A9E",
    "background_color": "#FFFFFF",
    "text_color":       "#333333",
    "muted_color":      "#888888",
    "table_header_bg":  "#0078D4",
    "table_alt_bg":     "#F5F9FD",

    # Typography
    "font_family":      "arial",
    "font_size_base":   13,
    "font_size_h1":     22,
    "font_size_h2":     15,

    # Layout
    "header_variant":   "left",
    "density":          "normal",
    "logo_position":    "right",

    # Charts
    "charts_enabled":   True,
    "chart_style":      "bars",

    # Analytics
    "show_period_comparison": True,
    "show_env_impact":        False,
    "currency":               "EUR",
}


def apply_theme(layout: dict, theme_id: str) -> dict:
    """
    Überträgt die Farben eines Themes in ein Layout-Dict.
    Gibt ein NEUES Dict zurück (mutiert das Original nicht).
    Unbekannte theme_ids werden ignoriert.
    """
    result = dict(layout)
    theme = THEMES.get(theme_id)
    if not theme:
        return result
    result["theme_id"] = theme_id
    for key in ("primary_color", "accent_color", "background_color",
                "text_color", "muted_color", "table_header_bg", "table_alt_bg"):
        result[key] = theme[key]
    return result


def normalize_layout(layout: dict | None) -> dict:
    """
    Füllt ein Layout-Dict mit Defaults auf und sorgt für Rückwärtskompatibilität.
    Wandelt alte Templates (nur primary_color, logo_url) in das neue Schema um.

    Wichtig: mutiert `layout` NICHT — liefert ein neues Dict.
    """
    result = dict(DEFAULT_LAYOUT)
    if layout:
        result.update({k: v for k, v in layout.items() if v is not None})

    # Wenn theme_id gesetzt ist, aber keine expliziten Farben → Farben aus Theme
    # Wenn eine einzelne Farbe abweicht → explizite Farben wurden überschrieben, theme_id ist nur noch Label
    if result.get("theme_id") in THEMES:
        theme = THEMES[result["theme_id"]]
        # Wenn ALLE Themefarben noch Default-Werte sind → auffüllen
        # (das deckt den Fall ab: Altes Template hatte nur primary_color)
        for k in ("accent_color", "background_color", "table_header_bg", "table_alt_bg"):
            if result.get(k) in (None, "", DEFAULT_LAYOUT[k]):
                result[k] = theme[k]

    # Legacy: wenn nur primary_color gesetzt war, table_header_bg synchronisieren
    if result.get("table_header_bg") == DEFAULT_LAYOUT["table_header_bg"] and \
       result.get("primary_color") != DEFAULT_LAYOUT["primary_color"]:
        result["table_header_bg"] = result["primary_color"]

    return result


def list_themes_summary(t=None) -> list[dict[str, Any]]:
    """
    Kompakte Theme-Liste für UI und MCP-Tools.

    `t` ist eine Übersetzer-Callable `t(key) -> str`. Ohne sie wird der
    Übersetzungs-Key selbst zurückgegeben — der Aufrufer kann dann im Template
    nachträglich übersetzen oder den Key direkt anzeigen.
    """
    def tr(key: str) -> str:
        return t(key) if t else key
    return [
        {
            "id":              tid,
            "name":            theme["name"],
            "description":     tr(theme["description_key"]),
            "description_key": theme["description_key"],
            "primary_color":   theme["primary_color"],
            "accent_color":    theme["accent_color"],
        }
        for tid, theme in THEMES.items()
    ]


def list_fonts_summary(t=None) -> list[dict[str, str]]:
    """
    Kompakte Font-Liste für UI und MCP-Tools.

    `t` ist eine Übersetzer-Callable `t(key) -> str`.
    """
    def tr(key: str) -> str:
        return t(key) if t else key
    return [
        {
            "key":             f["key"],
            "name":            f["name"],
            "description":     tr(f["description_key"]),
            "description_key": f["description_key"],
        }
        for f in FONTS
    ]
