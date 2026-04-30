"""
Tenant Cache — In-Memory Caching für Printix-Daten (v6.1.0)
===========================================================
Pattern: Lazy-Fill with TTL + Manual Refresh.

  - Erster get() für (tenant_id, topic) → Loader ausführen und cachen.
  - Weitere gets innerhalb TTL → direkt aus Cache.
  - invalidate() → nächster get() lädt frisch.
  - clear_tenant() beim Logout.

Thread-safe via threading.RLock. Process-lokal (bei Home-Assistant-Addon
ein einziger Uvicorn-Worker — kein Shared-State-Problem).

Aufruf-Pattern aus Request-Handlern:

    from cache import tenant_cache
    users = tenant_cache.get(
        tenant_id, "users",
        loader=lambda: client.list_all_users(page_size=200),
    )

Bei Create/Delete/Update einer Ressource:

    tenant_cache.invalidate(tenant_id, "users")

Oder alles invalidieren (z.B. bei Credentials-Änderung):

    tenant_cache.invalidate(tenant_id)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("printix.cache")


# ─── Default TTLs pro Topic ──────────────────────────────────────────────────
# Basiert auf typischer Änderungsfrequenz dieser Daten in Printix.
# Einzelne Topics können beim get() via ttl= overridden werden.

DEFAULT_TTLS: dict[str, int] = {
    # Stammdaten — ändern sich selten
    "users":         600,   # 10 min
    "printers":      600,   # 10 min
    "queues":        600,   # 10 min
    "sites":         1800,  # 30 min
    "networks":      1800,  # 30 min
    "snmp":          1800,  # 30 min
    "groups":        600,   # 10 min

    # Dynamische Daten — kurzer TTL
    "workstations":  120,   # 2 min (Online-Status ändert sich)

    # Pro User
    "cards_per_user": 900,  # 15 min
    "user_detail":    600,  # 10 min
}

FALLBACK_TTL = 600


class _TenantCache:
    """Thread-safe In-Memory-Cache mit TTL pro (tenant_id, topic)."""

    def __init__(self) -> None:
        # Hauptspeicher: key → (loaded_at_ts, data)
        self._data: dict[tuple[str, str], tuple[float, Any]] = {}
        # Sekundärer Sub-Key-Speicher (z.B. cards pro user_id):
        # key_with_subkey → (loaded_at_ts, data)
        self._sub: dict[tuple[str, str, str], tuple[float, Any]] = {}
        self._lock = threading.RLock()

    # ── Main API ──────────────────────────────────────────────────────────

    def get(self, tenant_id: str, topic: str,
            loader: Callable[[], Any],
            ttl: Optional[int] = None) -> Any:
        """Cache-First lookup. Bei Miss den Loader ausführen und cachen."""
        if not tenant_id or not topic:
            return loader()
        eff_ttl = ttl if ttl is not None else DEFAULT_TTLS.get(topic, FALLBACK_TTL)
        key = (tenant_id, topic)
        now = time.time()

        with self._lock:
            hit = self._data.get(key)
            if hit and (now - hit[0]) < eff_ttl:
                return hit[1]

        # Miss — loader außerhalb des Locks ausführen (kann Sekunden dauern)
        try:
            data = loader()
        except Exception:
            raise
        with self._lock:
            self._data[key] = (time.time(), data)
        return data

    def get_sub(self, tenant_id: str, topic: str, subkey: str,
                loader: Callable[[], Any],
                ttl: Optional[int] = None) -> Any:
        """Wie get() aber mit einem zusätzlichen Sub-Key (z.B. user_id).

        Ideal für "Karten pro User" oder "Detail pro ID", wo man nicht
        alle Sub-Entitäten in einem Rutsch lädt.
        """
        if not tenant_id or not topic or subkey is None:
            return loader()
        eff_ttl = ttl if ttl is not None else DEFAULT_TTLS.get(topic, FALLBACK_TTL)
        key = (tenant_id, topic, str(subkey))
        now = time.time()

        with self._lock:
            hit = self._sub.get(key)
            if hit and (now - hit[0]) < eff_ttl:
                return hit[1]

        data = loader()
        with self._lock:
            self._sub[key] = (time.time(), data)
        return data

    def set(self, tenant_id: str, topic: str, data: Any) -> None:
        """Manueller Cache-Write (z.B. nach Create um das neue Item
        direkt im Cache zu haben ohne Reload)."""
        if not tenant_id or not topic:
            return
        with self._lock:
            self._data[(tenant_id, topic)] = (time.time(), data)

    # ── Invalidierung ────────────────────────────────────────────────────

    def invalidate(self, tenant_id: str, topic: Optional[str] = None) -> int:
        """Entfernt Cache-Einträge. Gibt Anzahl entfernter Einträge zurück.

        topic=None    → alle Topics dieses Tenants
        topic="users" → nur dieses Topic (inkl. sub-keys mit gleichem topic)
        """
        if not tenant_id:
            return 0
        n = 0
        with self._lock:
            if topic:
                key = (tenant_id, topic)
                if key in self._data:
                    del self._data[key]
                    n += 1
                for k in [k for k in self._sub if k[0] == tenant_id and k[1] == topic]:
                    del self._sub[k]
                    n += 1
            else:
                for k in [k for k in self._data if k[0] == tenant_id]:
                    del self._data[k]
                    n += 1
                for k in [k for k in self._sub if k[0] == tenant_id]:
                    del self._sub[k]
                    n += 1
        if n:
            logger.debug("Cache: %d Eintrag/Einträge invalidiert (tenant=%s, topic=%s)",
                         n, tenant_id, topic or "*")
        return n

    def invalidate_sub(self, tenant_id: str, topic: str, subkey: str) -> bool:
        """Entfernt nur einen Sub-Cache-Eintrag (z.B. cards_per_user/UID)."""
        if not tenant_id or not topic or subkey is None:
            return False
        key = (tenant_id, topic, str(subkey))
        with self._lock:
            if key in self._sub:
                del self._sub[key]
                return True
        return False

    def clear_tenant(self, tenant_id: str) -> int:
        """Komplett-Clear aller Daten eines Tenants (z.B. beim Logout)."""
        return self.invalidate(tenant_id)

    def clear_all(self) -> int:
        with self._lock:
            n = len(self._data) + len(self._sub)
            self._data.clear()
            self._sub.clear()
        logger.info("Cache: alle Einträge gelöscht (%d)", n)
        return n

    # ── Metadaten ────────────────────────────────────────────────────────

    def last_refreshed(self, tenant_id: str, topic: str) -> Optional[float]:
        """Unix-Timestamp wann das Topic zuletzt geladen wurde, oder None."""
        with self._lock:
            hit = self._data.get((tenant_id, topic))
            return hit[0] if hit else None

    def age_seconds(self, tenant_id: str, topic: str) -> Optional[float]:
        ts = self.last_refreshed(tenant_id, topic)
        return (time.time() - ts) if ts else None

    def is_fresh(self, tenant_id: str, topic: str,
                 ttl: Optional[int] = None) -> bool:
        age = self.age_seconds(tenant_id, topic)
        if age is None:
            return False
        eff_ttl = ttl if ttl is not None else DEFAULT_TTLS.get(topic, FALLBACK_TTL)
        return age < eff_ttl

    def stats(self, tenant_id: Optional[str] = None) -> dict:
        """Debug-Info: was steckt drin?"""
        with self._lock:
            now = time.time()
            if tenant_id:
                out = {}
                for (tid, topic), (ts, data) in self._data.items():
                    if tid == tenant_id:
                        out[topic] = {
                            "age_s": round(now - ts, 1),
                            "items": len(data) if hasattr(data, "__len__") else 1,
                        }
                sub_count = sum(1 for k in self._sub if k[0] == tenant_id)
                if sub_count:
                    out["_sub_entries"] = sub_count
                return out
            return {
                "tenants": len({k[0] for k in self._data} | {k[0] for k in self._sub}),
                "main_entries": len(self._data),
                "sub_entries":  len(self._sub),
            }


# ─── Singleton ────────────────────────────────────────────────────────────
tenant_cache = _TenantCache()


# ─── Formatter für UI ─────────────────────────────────────────────────────

def format_age(age_s: Optional[float]) -> str:
    """Menschenlesbare Alter-Angabe: 'gerade eben' / 'vor 45s' / 'vor 3 Min.' ...

    Wird im Template genutzt um 'Stand: vor X' anzuzeigen.
    """
    if age_s is None:
        return ""
    if age_s < 10:
        return "gerade eben"
    if age_s < 60:
        return f"vor {int(age_s)} s"
    if age_s < 3600:
        return f"vor {int(age_s // 60)} Min."
    if age_s < 86400:
        return f"vor {int(age_s // 3600)} Std."
    return f"vor {int(age_s // 86400)} Tagen"


# ─── Login-Prefetch (v6.2.0) ─────────────────────────────────────────────
#
# Nach erfolgreichem Login werden die wichtigsten Tenant-Daten im
# Hintergrund geladen und im Cache abgelegt. Der Login-Flow blockiert
# NICHT darauf — der User wird sofort weitergeleitet, während das
# Prefetch parallel läuft.
#
# Wenn der User dann auf /tenant/users klickt, greift der Cache-Hit und
# die Seite rendert sofort. Bei langsamer Azure-SQL oder Printix-API
# fühlt sich das Addon damit deutlich responsiver an.


# Prefetch-Status (pro Tenant): "idle" | "running" | "done" | "error"
_prefetch_status: dict[str, str] = {}
_prefetch_status_lock = threading.Lock()


def prefetch_status(tenant_id: str) -> str:
    """Aktueller Status des Background-Prefetch für einen Tenant."""
    with _prefetch_status_lock:
        return _prefetch_status.get(tenant_id, "idle")


def _set_prefetch_status(tenant_id: str, status: str) -> None:
    with _prefetch_status_lock:
        _prefetch_status[tenant_id] = status


def count_user_cards_robust(client, user_id: str) -> int:
    """v7.6.0: Single-Source-of-Truth für die Karten-Zählung pro User.

    Die Printix-API liefert /users/{id}/cards in vier verschiedenen
    Formen zurück (variiert pro Tenant-Konfiguration):

      1. ``{"cards":   [...]}``
      2. ``{"content": [...]}``
      3. ``{"items":   [...]}``
      4. ``[...]``  (Top-Level-Liste)

    Vor v7.2.47 wurde nur Form 1+2 abgedeckt — Tenants mit Form 3+4
    sahen 0 Karten für jeden User. Diese Helper-Funktion deckt alle
    vier ab und filtert Strings/None aus dem Listen-Inhalt heraus
    (nur Dict-Einträge zählen als echte Karte).

    Wird sowohl vom Bulk-Prefetch als auch vom on-demand
    ``_load_card_counts_parallel`` aufgerufen — dadurch zeigt
    /tenant/users immer den gleichen Zähler wie der Cache.
    """
    try:
        data = client.list_user_cards(user_id)
    except Exception:
        return 0
    if isinstance(data, list):
        raw = data
    elif isinstance(data, dict):
        raw = (data.get("cards")
               or data.get("content")
               or data.get("items")
               or [])
    else:
        return 0
    return sum(1 for c in raw if isinstance(c, dict))


async def _bulk_prefetch_card_counts(tenant_id: str, client, users) -> int:
    """Lädt Karten-Zähler für alle übergebenen User parallel und
    schreibt sie in den Sub-Cache (gleicher Slot den
    ``_load_card_counts_parallel`` in app.py liest)."""
    import asyncio as _aio
    if not users:
        return 0
    user_ids: list[str] = []
    for u in users:
        if isinstance(u, dict):
            uid = u.get("id") or u.get("userId") or u.get("user_id") or ""
            if uid:
                user_ids.append(uid)
    if not user_ids:
        return 0

    async def _one(uid: str) -> tuple[str, int]:
        try:
            n = await _aio.to_thread(count_user_cards_robust, client, uid)
        except Exception:
            n = 0
        return uid, n

    pairs = await _aio.gather(*(_one(uid) for uid in user_ids))
    now = time.time()
    for uid, n in pairs:
        tenant_cache._sub[(tenant_id, "cards_per_user", uid)] = (now, n)
    return len(pairs)


async def prefetch_tenant(tenant: dict, client) -> dict:
    """Lädt parallel die wichtigsten Tenant-Topics in den Cache.

    Fehler bei einzelnen Topics werden geschluckt — ein defektes Topic
    (z.B. fehlende ws_client_id) soll die anderen nicht stoppen.

    Args:
        tenant: Tenant-Dict mit 'id' und Credentials-Flags
        client: Bereits konfigurierter PrintixClient (läuft mit den
                Credentials des Tenants)

    Returns:
        Dict mit {topic: "ok" | "skip" | "error:<msg>"}
    """
    import asyncio as _aio

    tenant_id = tenant.get("id", "")
    if not tenant_id:
        return {}

    _set_prefetch_status(tenant_id, "running")
    logger.info("Prefetch gestartet für Tenant %s", tenant_id[:8] + "…")

    has_print_creds = bool(tenant.get("print_client_id") or tenant.get("shared_client_id"))
    has_card_creds  = bool(tenant.get("card_client_id")  or tenant.get("shared_client_id")
                            or tenant.get("um_client_id"))
    has_ws_creds    = bool(tenant.get("ws_client_id")    or tenant.get("shared_client_id"))

    async def _load(topic: str, loader_fn, required: bool = True) -> str:
        if not required:
            return "skip"
        try:
            # Loader in thread ausführen (blocking HTTP requests)
            data = await _aio.to_thread(loader_fn)
            tenant_cache.set(tenant_id, topic, data)
            n = len(data) if hasattr(data, "__len__") else 1
            logger.debug("Prefetch %s[%s]: ok (%d items)", tenant_id[:8], topic, n)
            return "ok"
        except Exception as e:
            logger.debug("Prefetch %s[%s]: error %s", tenant_id[:8], topic, e)
            return f"error:{str(e)[:100]}"

    # Topics in prioritisierter Reihenfolge — die häufigsten zuerst, damit
    # User diese auch dann schnell sehen wenn spätere Prefetches hängen.
    tasks = []
    topic_names = []

    if has_card_creds:
        topic_names.append("users")
        tasks.append(_load(
            "users",
            lambda: client.list_all_users(page_size=200),
            required=True,
        ))
    if has_print_creds:
        topic_names.append("printers")
        tasks.append(_load(
            "printers",
            lambda: client.list_printers(size=200),
            required=True,
        ))
    if has_ws_creds:
        topic_names.append("workstations")
        tasks.append(_load(
            "workstations",
            lambda: client.list_workstations(size=200),
            required=True,
        ))
    if has_print_creds:
        topic_names.append("sites")
        tasks.append(_load(
            "sites",
            lambda: client.list_sites(size=200),
            required=True,
        ))
        topic_names.append("networks")
        tasks.append(_load(
            "networks",
            lambda: client.list_networks(size=200),
            required=True,
        ))
        topic_names.append("groups")
        tasks.append(_load(
            "groups",
            lambda: client.list_groups(size=200),
            required=True,
        ))
        # v7.6.0: SNMP-Configs gehören zu den Stammdaten — selten geändert,
        # 30 min TTL. Vorher wurden sie erst beim ersten /tenant/snmp-Hit
        # geladen → erste Navigation langsam.
        topic_names.append("snmp")
        tasks.append(_load(
            "snmp",
            lambda: client.list_snmp_configs(size=200),
            required=True,
        ))

    if not tasks:
        _set_prefetch_status(tenant_id, "done")
        logger.info("Prefetch: keine Credentials vorhanden, nichts zu laden")
        return {}

    results = await _aio.gather(*tasks, return_exceptions=True)
    summary: dict[str, str] = {}
    for topic, res in zip(topic_names, results):
        if isinstance(res, Exception):
            summary[topic] = f"error:{str(res)[:100]}"
        else:
            summary[topic] = res

    # v7.6.0: Bulk-Cards-Prefetch — nachdem die User-Liste im Cache ist,
    # parallel pro User /users/{id}/cards holen und Zähler in den
    # cards_per_user-Sub-Cache schreiben. /tenant/users zeigt die Zahlen
    # dann ohne weiteren API-Round-Trip. Best-effort: schluckt Fehler
    # einzelner User damit ein 403 für einen User nicht alle anderen
    # ausbremst.
    if has_card_creds and summary.get("users") == "ok":
        try:
            users_data = tenant_cache.get(tenant_id, "users", loader=lambda: [])
            users_list = []
            if isinstance(users_data, list):
                users_list = users_data
            elif isinstance(users_data, dict):
                users_list = (users_data.get("users")
                              or users_data.get("content")
                              or users_data.get("items")
                              or [])
            n = await _bulk_prefetch_card_counts(tenant_id, client, users_list)
            summary["cards_per_user"] = "ok" if n > 0 else "skip"
            logger.info("Prefetch cards: %d user warmed", n)
        except Exception as ce:
            summary["cards_per_user"] = f"error:{str(ce)[:80]}"
            logger.debug("Bulk-Cards-Prefetch fehlgeschlagen: %s", ce)

    ok_count = sum(1 for v in summary.values() if v == "ok")
    logger.info("Prefetch abgeschlossen für Tenant %s: %d/%d topics ok",
                tenant_id[:8], ok_count, len(summary))
    _set_prefetch_status(tenant_id, "done" if ok_count > 0 else "error")
    return summary


def schedule_prefetch(tenant: dict, client_factory) -> None:
    """Startet einen Background-Prefetch-Task via asyncio.create_task.

    Wird aus Request-Handlern aufgerufen — der Handler kann sofort
    zurückkehren, der Prefetch läuft im Event-Loop weiter. Fehler
    werden geloggt, landen aber nie beim User.

    Args:
        tenant: Tenant-Dict mit Credentials
        client_factory: Callable ohne Args, das einen PrintixClient liefert.
                        (Lazy damit wir den Client nur erstellen wenn Prefetch
                        tatsächlich startet.)
    """
    import asyncio as _aio

    tenant_id = tenant.get("id", "")
    if not tenant_id:
        return

    # Wenn bereits ein Prefetch läuft, nicht nochmal starten
    if prefetch_status(tenant_id) == "running":
        logger.debug("Prefetch für %s läuft bereits, skip", tenant_id[:8])
        return

    # Wenn der Cache schon frisch ist (< 2 min), auch skippen
    fresh_age = tenant_cache.age_seconds(tenant_id, "users")
    if fresh_age is not None and fresh_age < 120:
        logger.debug("Prefetch für %s übersprungen — Cache ist frisch (%ds)",
                     tenant_id[:8], int(fresh_age))
        return

    async def _run():
        try:
            client = client_factory()
            await prefetch_tenant(tenant, client)
        except Exception as e:
            tenant_id2 = tenant.get("id", "")[:8]
            logger.warning("Prefetch-Task für %s fehlgeschlagen: %s", tenant_id2, e)
            _set_prefetch_status(tenant.get("id", ""), "error")

    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        logger.debug("schedule_prefetch: kein Event-Loop — skip")
        return
    loop.create_task(_run())


# ─── v7.6.0: Periodic Background Refresher ───────────────────────────────────
# Jede Minute: schaut für jeden Tenant der schonmal einen Prefetch hatte
# nach, ob ein Topic in <60 Sek. ablaufen würde. Wenn ja → re-prefetch in
# diesem Tick. Effekt: Cache läuft nie kalt während ein Tab offen ist.

_REFRESHER_TASK = None  # type: ignore
_TENANT_PROVIDERS: dict[str, Callable] = {}  # tenant_id → factory


def register_tenant_for_refresh(tenant: dict, client_factory: Callable) -> None:
    """Registriert (tenant, client_factory) für den Periodic Refresher.

    Muss nach jedem Login aufgerufen werden — das Callable bekommt eine
    aktuelle Client-Instanz, weil Tokens irgendwann ablaufen.
    """
    tid = tenant.get("id", "")
    if not tid:
        return
    _TENANT_PROVIDERS[tid] = client_factory


def _topic_age(tenant_id: str, topic: str) -> Optional[float]:
    return tenant_cache.age_seconds(tenant_id, topic)


def _topic_needs_refresh(tenant_id: str, topic: str, lead_time: int = 60) -> bool:
    """True wenn Topic ABLÄUFT in <= lead_time Sekunden (oder schon ist)."""
    age = _topic_age(tenant_id, topic)
    if age is None:
        return False  # noch nie geladen — nicht hier neu starten, das macht der Login-Path
    ttl = DEFAULT_TTLS.get(topic, FALLBACK_TTL)
    return (ttl - age) <= lead_time


async def _refresher_loop(interval: int = 60) -> None:
    """Hintergrund-Schleife — startet bei Bedarf einen erneuten Prefetch."""
    import asyncio as _aio
    logger.info("Periodic cache refresher started (interval=%ds)", interval)
    while True:
        try:
            await _aio.sleep(interval)
            for tid, factory in list(_TENANT_PROVIDERS.items()):
                # Prüf-Topics — die teuersten/häufigsten zuerst
                topics_to_check = ("users", "printers", "workstations",
                                   "sites", "networks", "groups", "snmp")
                if not any(_topic_needs_refresh(tid, t) for t in topics_to_check):
                    continue
                if prefetch_status(tid) == "running":
                    continue
                try:
                    client = factory()
                    # Tenant-Dict rekonstruieren ist nicht ideal — wir
                    # speichern nur die ID hier, aber prefetch_tenant
                    # braucht das Credentials-Flags-Dict. Lösung: die
                    # Factory gibt ein Tuple (tenant, client) zurück,
                    # falls nötig. Vorerst: die Factory liefert direkt
                    # einen Client; wir bauen ein Minimal-Tenant-Dict
                    # aus dem Tenant-Cache rückwärts. Ist nicht perfekt,
                    # aber tut's für den Refresh weil prefetch_tenant
                    # bereits has_*_creds aus dem Tenant-Dict bestimmt.
                    # → besser: Factory liefert (tenant, client).
                    if isinstance(client, tuple) and len(client) == 2:
                        tenant_dict, real_client = client
                    else:
                        # Backwards-compat: alter Code lieferte nur
                        # Client. Wir füllen Tenant-Dict minimal aus.
                        tenant_dict = {"id": tid,
                                       "shared_client_id": "1",  # Optimistisch
                                       "print_client_id": "1",
                                       "card_client_id": "1",
                                       "ws_client_id": "1"}
                        real_client = client
                    await prefetch_tenant(tenant_dict, real_client)
                except Exception as e:
                    logger.warning("Refresher: prefetch %s failed: %s",
                                   tid[:8], e)
        except Exception as e:
            # Refresher selber soll niemals sterben — alles fangen
            logger.warning("Refresher loop hiccup: %s", e)


def start_background_refresher() -> None:
    """Startet den Refresher einmal im aktuellen Event-Loop. Idempotent."""
    global _REFRESHER_TASK
    import asyncio as _aio
    if _REFRESHER_TASK is not None and not _REFRESHER_TASK.done():
        return
    try:
        loop = _aio.get_running_loop()
    except RuntimeError:
        logger.debug("start_background_refresher: no running loop")
        return
    _REFRESHER_TASK = loop.create_task(_refresher_loop())
