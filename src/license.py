"""
Pro-Feature Activation (v7.2.39).

Stufe-A Hash-basierte Aktivierungscodes. Jedes Pro-Feature hat einen
eindeutigen 12-Zeichen-Code; das Geheimnis ist im Code eingebrannt.
Master-Code schaltet alle Features auf einmal frei.

Aktivierungs-Workflow:
  1. Marcus generiert die Codes offline mit `bin/generate-license-codes.py`
     (oder kopiert sie aus den Server-Logs beim ersten Boot).
  2. Marcus mailt dem Kunden den passenden Code.
  3. Kunde fügt ihn unter /admin/license ein → Server validiert →
     `pro_features` Setting in der DB wird aktualisiert.
  4. Web-UI Routen + Nav-Links checken `is_feature_enabled(...)` zur
     Render-Zeit.

Hinweis zur Sicherheit (Stufe A — bewusst):
  Quellcode ist offen, also kann ein technisch versierter User die
  Checks umgehen. Der Code dient als Schwelle gegen "casual misuse"
  und um eine klare Sales-Konversation zu ermöglichen ("hier ist dein
  Code"), nicht als kryptografisch unbrechbarer Schutz. Für solche
  Garantien wäre Stufe B nötig (signierte Tokens mit Public-Key).
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger("printix.license")

# Geheimnis — Änderung invalidiert alle bisher ausgegebenen Codes.
# In einem zukünftigen Build kann dies via Env-Var / Build-Argument
# überschrieben werden, dann hat jede Tungsten-Distribution eigene
# Codes. Aktuell: ein Geheimnis für alle Installationen.
_LICENSE_SECRET = "TUNGSTEN-MCP-PRO-2026"

# Pro-Features-Katalog. Jedes Feature kriegt seinen eigenen Code,
# zusätzlich gibt es einen Master-Code für alle (`*all*`).
PRO_FEATURES: dict[str, dict] = {
    "capture_store": {
        "icon":           "📥",
        "label_de":       "Capture Store",
        "label_en":       "Capture Store",
        "label_no":       "Capture Store",
        "description_de": "Dokumenten-Erfassung mit Webhook-Profilen, automatischer Indexierung und Routing zu Paperless-NGX, SharePoint oder beliebigen Drittsystemen.",
        "description_en": "Document capture with webhook profiles, automatic indexing, and routing to Paperless-NGX, SharePoint, or any third-party system.",
        "description_no": "Dokumentfangst med webhook-profiler, automatisk indeksering, og ruting til Paperless-NGX, SharePoint eller hvilken som helst tredjeparts-system.",
        "url_path":       "/capture",
    },
    "guest_print": {
        "icon":           "📮",
        "label_de":       "Guest-Print",
        "label_en":       "Guest Print",
        "label_no":       "Gjeste-utskrift",
        "description_de": "E-Mail-basierte Gastdruck-Mailboxes für externe Nutzer ohne Printix-Account. Konfiguration pro Mailbox: Approval-Workflow, Site-Routing, Storage-Behandlung.",
        "description_en": "Email-based guest-print mailboxes for external users without a Printix account. Per-mailbox configuration of approval workflow, site routing, and storage behaviour.",
        "description_no": "E-postbaserte gjesteutskrift-postkasser for eksterne brukere uten Printix-konto. Per-postkasse-konfigurasjon av godkjenningsflyt, områderuting og lagringshåndtering.",
        "url_path":       "/guestprint",
    },
    "print_job_mgmt": {
        "icon":           "🖨️",
        "label_de":       "Print-Job-Management",
        "label_en":       "Print Job Management",
        "label_no":       "Utskriftsjobb-administrasjon",
        "description_de": "Erweiterte Admin-UI für Bulk-Aktionen über Print-Jobs hinweg: Re-Assignment, Bulk-Cancel, Audit-Trail mit Replay, Anomalie-Detection. Aktuell vorbereitet für künftige Releases — Lizenz-Slot wird hier reserviert.",
        "description_en": "Extended admin UI for bulk actions across print jobs: reassignment, bulk-cancel, audit trail with replay, anomaly detection. Currently scaffolded for upcoming releases — license slot reserved here.",
        "description_no": "Utvidet admin-UI for bulk-handlinger på tvers av utskriftsjobber: omfordeling, bulk-kansellering, revisjonsspor med avspilling, avviksdeteksjon. Foreløpig forberedt for kommende utgivelser — lisensplass reservert her.",
        "url_path":       "",  # noch keine eigene Route — Reserve-Slot
    },
}


# ─── Hashing ─────────────────────────────────────────────────────────────────

def _normalize(code: str) -> str:
    """Normalisiert eine Code-Eingabe: Großbuchstaben, ohne Whitespace,
    ohne Bindestriche. Für vergleichsfähige Validierung."""
    return (code or "").strip().upper().replace("-", "").replace(" ", "")


def _hash_for(feature: str) -> str:
    """Erzeugt den 12-Zeichen-Code für ein Feature (oder '*all*' für Master)."""
    raw = f"{_LICENSE_SECRET}|{feature}".encode()
    return hashlib.sha256(raw).hexdigest()[:12].upper()


def all_codes() -> dict[str, str]:
    """Liefert eine Übersicht: Feature → Code. Nur für Generator-Tools
    und Debug-Logs gedacht."""
    out = {f: _hash_for(f) for f in PRO_FEATURES}
    out["*all*"] = _hash_for("*all*")
    return out


# ─── Validation ──────────────────────────────────────────────────────────────

def validate_code(code: str) -> set[str]:
    """Prüft einen Code und gibt die Menge der Features zurück, die er
    freischaltet. Leere Menge bei ungültigem Code.
    """
    nc = _normalize(code)
    if not nc:
        return set()
    # Master-Code?
    if nc == _hash_for("*all*"):
        return set(PRO_FEATURES.keys())
    # Einzelfeature-Code?
    for feature in PRO_FEATURES:
        if nc == _hash_for(feature):
            return {feature}
    return set()


# ─── Persistence ─────────────────────────────────────────────────────────────

def get_active_features() -> set[str]:
    """Liest die aktuell aktivierten Features aus dem Settings-Store."""
    try:
        import db
        raw = db.get_setting("pro_features", "") or ""
        if not raw:
            return set()
        v = json.loads(raw)
        return set(v) if isinstance(v, list) else set()
    except Exception as e:
        logger.warning("get_active_features: %s", e)
        return set()


def set_active_features(features: set[str]) -> None:
    """Persistiert die aktive Feature-Menge."""
    try:
        import db
        # Nur bekannte Features speichern, falls jemand Müll reinschreibt
        clean = sorted(f for f in features if f in PRO_FEATURES)
        db.set_setting("pro_features", json.dumps(clean))
    except Exception as e:
        logger.error("set_active_features: %s", e)


def is_feature_enabled(feature: str) -> bool:
    """Convenience für Templates und Routen-Gates."""
    if feature not in PRO_FEATURES:
        return True   # unbekanntes Feature wird nicht gesperrt
    return feature in get_active_features()


# ─── Activation actions ──────────────────────────────────────────────────────

def activate_code(code: str) -> dict:
    """Aktiviert einen Code. Liefert Status + neu freigeschaltete Features."""
    new = validate_code(code)
    if not new:
        return {"ok": False, "error": "invalid_code"}
    current = get_active_features()
    set_active_features(current | new)
    return {
        "ok": True,
        "newly_unlocked": sorted(new - current),
        "all_active":     sorted(current | new),
    }


def deactivate_feature(feature: str) -> bool:
    """Schaltet ein Feature manuell ab (z.B. Lizenz abgelaufen)."""
    if feature not in PRO_FEATURES:
        return False
    current = get_active_features()
    if feature not in current:
        return False
    set_active_features(current - {feature})
    return True
