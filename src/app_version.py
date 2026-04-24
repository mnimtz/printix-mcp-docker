"""Single Source of Truth: Top-Level-VERSION-Datei.

v6.7.0: Vorher gab es ZWEI VERSION-Files (`/VERSION` und `/src/VERSION`),
die regelmäßig auseinanderlaufen sind — der Top-Level-File wurde via
`run.sh` für das HA-Addon-Banner gelesen, der src/-File hier für die
Python-Banner-Zeile in `server.py`. Resultat: das HA-Addon-Manifest und
das Stdout-Banner zeigten verschiedene Versionen.

Jetzt: wir suchen die Datei in mehreren plausiblen Pfaden und nehmen die
erste die wir finden. So ist `/VERSION` (Top-Level) maßgeblich, und der
alte Fallback bleibt erhalten falls jemand das Repo-Layout ändert.
"""
import os
from pathlib import Path


def _read_version() -> str:
    here = Path(__file__).resolve().parent
    candidates = [
        # 1. Im Container: /app/VERSION (vom Dockerfile dorthin kopiert)
        Path("/app/VERSION"),
        # 2. Repo Top-Level (parent von src/)
        here.parent / "VERSION",
        # 3. Direkt neben app_version.py — Legacy-Fallback
        here / "VERSION",
    ]
    # Optional: Override via Env-Var
    env_path = os.environ.get("APP_VERSION_FILE")
    if env_path:
        candidates.insert(0, Path(env_path))

    for path in candidates:
        try:
            if path.is_file():
                value = path.read_text(encoding="utf-8").strip()
                if value:
                    return value
        except Exception:
            continue
    return "0.0.0"


APP_VERSION = _read_version()
