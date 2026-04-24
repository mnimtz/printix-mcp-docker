"""
Demo Worker — Subprocess-Isolierung (v4.4.0: lokale SQLite)
===========================================================
Wird von app.py via subprocess.Popen gestartet. Läuft im eigenen Prozess,
damit ein etwaiger Crash den Web-Server nicht tötet.

v4.4.0: Schreibt nur noch in die lokale SQLite-DB — kein Azure SQL
Schreibzugriff mehr nötig. DEMO_TENANT_CONFIG wird weiterhin akzeptiert
(für Kompatibilität), aber nicht mehr zum Schreiben verwendet.

Eingabe:  Umgebungsvariablen DEMO_PARAMS (JSON)
          + DEMO_OUTPUT_FILE (Pfad zur Ergebnisdatei)
Ausgabe:  JSON-Datei an DEMO_OUTPUT_FILE mit {"session_id": ..., "error": ...}
"""

import json
import logging
import os
import sys
import traceback

# /app in den Suchpfad
sys.path.insert(0, "/app")

# Logging zentral konfigurieren BEVOR irgendein Modul Logger holt
logging.basicConfig(
    level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("printix.demo_worker")

def main():
    output_file = os.environ.get("DEMO_OUTPUT_FILE", "/tmp/demo_result.json")
    logger.info("demo_worker gestartet (output=%s, lokal SQLite)", output_file)

    try:
        # v4.4.0: Kein Azure SQL Config mehr nötig für Demo-Daten-Generierung.
        # Die lokale SQLite-DB wird automatisch von local_demo_db.py initialisiert.

        # Generation-Parameter
        params_str = os.environ.get("DEMO_PARAMS", "{}")
        params = json.loads(params_str)
        logger.info("Starte generate_demo_dataset(**%s)", params)

        from reporting.demo_generator import generate_demo_dataset
        result = generate_demo_dataset(**params)
        logger.info("generate_demo_dataset fertig: session_id=%s", result.get("session_id", "?"))

        # Ergebnis schreiben
        with open(output_file, "w") as f:
            json.dump(result, f)

        # Exit-Code 0 = Erfolg
        sys.exit(0)

    except Exception as exc:
        tb = traceback.format_exc()
        logger.error("demo_worker abgebrochen: %s\n%s", exc, tb)
        error_result = {
            "error": str(exc),
            "traceback": tb[:1000],
            "session_id": "",
        }
        try:
            with open(output_file, "w") as f:
                json.dump(error_result, f)
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()
