"""GitHub-Releases-basierter Update-Check fuer das Docker-Image.

Schaut periodisch nach, ob auf
  https://api.github.com/repos/mnimtz/printix-mcp-docker/releases/latest
ein neuerer Tag liegt als die laufende `APP_VERSION`. Wenn ja, zeigt
die Web-UI ein dezentes Banner neben der Versionsanzeige.

Cache: 1h in-memory + lazy background-fetch — der erste Dashboard-
Render wartet NIEMALS auf GitHub (Time-To-First-Byte ist hier wichtiger
als Aktualitaet). Wenn der Cache leer/abgelaufen ist, kickt
`get_update_info()` einen Thread, der den Wert holt; der Aufrufer
bekommt den letzten bekannten Wert zurueck.

Opt-out per ENV-Var:
    UPDATE_CHECK_ENABLED=false

Failures (Netzwerk, GitHub-Rate-Limit, JSON-Schema-Drift, …) werden
schweigend geschluckt — der Check darf den Render nie kippen.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger("printix.update_check")

_REPO = "mnimtz/printix-mcp-docker"
_TTL_SECONDS = 3600          # 1h
_TIMEOUT_SECONDS = 4

_lock = threading.Lock()
_cache = {
    "checked_at": 0.0,   # Zeit des letzten Versuchs (auch bei Fehler)
    "latest":     "",    # zuletzt erkannte Versions-Tag, ohne fuehrendes 'v'
    "url":        "",    # Release-Notes-URL
    "ok":         False, # True, wenn der letzte Fetch eine Version geliefert hat
}


def _enabled() -> bool:
    val = (os.environ.get("UPDATE_CHECK_ENABLED", "true") or "true").strip().lower()
    return val not in ("false", "0", "no", "off")


def _parse_semver(v: str) -> tuple:
    """Tolerant: '7.7.3', 'v7.7.3', '7.7.3-rc1' -> (7, 7, 3)."""
    s = (v or "").lstrip("v").split("-", 1)[0].split("+", 1)[0]
    parts = s.split(".")
    out = []
    for p in parts[:3]:
        try:
            out.append(int(p))
        except Exception:
            out.append(0)
    while len(out) < 3:
        out.append(0)
    return tuple(out)


def _fetch_latest() -> Tuple[str, str]:
    """Holt das hoechste Tag aus dem Repo. Returns (tag_ohne_v, html_url)
    oder ('','').

    Wir bevorzugen `/releases/latest` (liefert ein veroeffentlichtes
    Release inkl. Notes-URL), fallen aber auf `/tags` zurueck — z.B.
    wenn der Maintainer nur Tags pusht und keine GitHub-Releases anlegt.
    Bei `/tags` zeigt die URL auf `releases/tag/<tag>`; GitHub rendert
    dort automatisch den Tag mit oder ohne offizielle Release-Notes.
    """
    headers = {
        "Accept":     "application/vnd.github+json",
        "User-Agent": "printix-mcp-update-check",
    }

    # 1) /releases/latest — bevorzugt
    try:
        r = requests.get(
            f"https://api.github.com/repos/{_REPO}/releases/latest",
            timeout=_TIMEOUT_SECONDS, headers=headers,
        )
        if r.status_code == 200 and r.content:
            data = r.json()
            tag = (data.get("tag_name") or "").lstrip("v").strip()
            if tag:
                return tag, (data.get("html_url") or "")
        elif r.status_code != 404:
            logger.debug("Update-Check: /releases/latest -> %s", r.status_code)
    except Exception as e:
        logger.debug("Update-Check: /releases/latest fehlgeschlagen: %s", e)

    # 2) /tags — Fallback. Liefert eine Liste, wir nehmen das Top-Element
    #    nach Semver (GitHub sortiert nicht garantiert, deshalb selber).
    try:
        r = requests.get(
            f"https://api.github.com/repos/{_REPO}/tags?per_page=30",
            timeout=_TIMEOUT_SECONDS, headers=headers,
        )
        if r.status_code != 200 or not r.content:
            logger.debug("Update-Check: /tags -> %s", r.status_code)
            return "", ""
        items = r.json() or []
        candidates = []
        for it in items:
            name = (it.get("name") or "").lstrip("v").strip()
            if not name:
                continue
            sv = _parse_semver(name)
            if sv == (0, 0, 0):
                continue
            candidates.append((sv, name))
        if not candidates:
            return "", ""
        candidates.sort(reverse=True)
        _, top = candidates[0]
        return top, f"https://github.com/{_REPO}/releases/tag/v{top}"
    except Exception as e:
        logger.debug("Update-Check: /tags fehlgeschlagen: %s", e)
        return "", ""


def _refresh_in_background() -> None:
    def _job():
        try:
            latest, url = _fetch_latest()
            with _lock:
                _cache["checked_at"] = time.time()
                if latest:
                    _cache["latest"] = latest
                    _cache["url"]    = url
                    _cache["ok"]     = True
                # Wenn _fetch_latest leer zurueckkommt (Rate-Limit etc.):
                # alten Wert behalten, ok=ok bleibt wie es war.
            if latest:
                logger.info("Update-Check: latest release = %s", latest)
        except Exception as e:
            logger.debug("Update-Check Background-Fetch fehlgeschlagen: %s", e)
            with _lock:
                # Trotz Fehler Zeit setzen, damit wir nicht im Sekundentakt retry-en.
                _cache["checked_at"] = time.time()

    t = threading.Thread(target=_job, name="update-check", daemon=True)
    t.start()


def get_update_info(local_version: str) -> dict:
    """Liefert {available, latest, url, enabled} fuer das Dashboard.

    Erster Aufruf hat leeren Cache → triggert Background-Fetch und liefert
    `available=False`. Subsequent Calls bekommen das Ergebnis.
    """
    if not _enabled():
        return {"available": False, "latest": "", "url": "", "enabled": False}

    now = time.time()
    refresh = False
    with _lock:
        if (now - _cache["checked_at"]) >= _TTL_SECONDS:
            refresh = True
            # _cache["checked_at"] wird vom Background-Job gesetzt
        latest = _cache["latest"]
        url    = _cache["url"]
        ok     = _cache["ok"]

    if refresh:
        _refresh_in_background()

    if not ok or not latest:
        return {"available": False, "latest": "", "url": "", "enabled": True}

    available = _parse_semver(latest) > _parse_semver(local_version)
    return {
        "available": available,
        "latest":    latest,
        "url":       url,
        "enabled":   True,
    }


def warm_up() -> None:
    """Beim App-Start aufrufen, damit der erste Dashboard-Render schon
    eine Antwort hat. Ist nur ein Thread-Spawn — sofort zurueck."""
    if _enabled():
        _refresh_in_background()
