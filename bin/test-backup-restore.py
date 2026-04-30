#!/usr/bin/env python3
"""
v7.6.5 — End-to-End-Test für backup_manager (create → verify → restore).

Standalone-Test, kein pytest nötig. Legt einen temporären DATA_DIR und
BACKUP_DIR an, simuliert Container-State (DB + Fernet-Key), macht ein
Backup, validiert es, restored es in einen ZWEITEN DATA_DIR und prüft
dass die Inhalte identisch sind.

Aufruf:
  docker compose exec printix-mcp python3 /app/bin/test-backup-restore.py
oder lokal mit dem Repo:
  python3 bin/test-backup-restore.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path


def _seed_data_dir(data_dir: Path) -> None:
    """Schreibt eine kleine Test-DB + Dummy-Fernet-Key + JSON-Datei."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "printix_multi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE smoke (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO smoke VALUES ('marker','printix-mcp-backup-test')")
        conn.commit()
    (data_dir / "fernet.key").write_text("dummy-fernet-key-for-test\n")
    (data_dir / "report_templates.json").write_text(json.dumps({"hello": "world"}))


def _read_marker(data_dir: Path) -> str | None:
    db = data_dir / "printix_multi.db"
    if not db.exists():
        return None
    with sqlite3.connect(db) as conn:
        cur = conn.execute("SELECT v FROM smoke WHERE k='marker'")
        row = cur.fetchone()
        return row[0] if row else None


def main() -> int:
    repo_src = Path(__file__).resolve().parent.parent / "src"
    sys.path.insert(0, str(repo_src))

    with tempfile.TemporaryDirectory(prefix="bm-orig-") as orig_root, \
         tempfile.TemporaryDirectory(prefix="bm-bak-") as bak_root, \
         tempfile.TemporaryDirectory(prefix="bm-restore-") as restore_root:

        orig_data = Path(orig_root) / "data"
        backup_dir = Path(bak_root) / "backups"
        restore_data = Path(restore_root) / "data"

        os.environ["PERSISTENT_DATA_DIR"] = str(orig_data)
        os.environ["BACKUP_DIR"] = str(backup_dir)

        # Module nach dem env-set importieren — Modul liest os.environ
        # beim import (Modulglobale)
        import importlib
        if "backup_manager" in sys.modules:
            del sys.modules["backup_manager"]
        bm = importlib.import_module("backup_manager")

        print(f"[1/6] Seed source data dir: {orig_data}")
        _seed_data_dir(orig_data)
        marker_orig = _read_marker(orig_data)
        assert marker_orig == "printix-mcp-backup-test", marker_orig
        print(f"      DB marker = {marker_orig!r} ✓")

        print(f"[2/6] create_backup() -> {backup_dir}")
        result = bm.create_backup()
        zip_path = result["path"]
        assert os.path.exists(zip_path), zip_path
        print(f"      created {zip_path} ({result['size']} bytes) ✓")

        print(f"[3/6] verify_backup() pre-flight check")
        v = bm.verify_backup(zip_path)
        assert v["ok"], f"verify failed: {v}"
        print(f"      ok={v['ok']} errors={v['errors']} warnings={v['warnings']} ✓")

        print(f"[4/6] list_backups()")
        rows = bm.list_backups()
        assert len(rows) == 1, rows
        assert rows[0]["filename"].endswith(".zip"), rows[0]
        print(f"      {rows[0]['filename']} ✓")

        print(f"[5/6] swap PERSISTENT_DATA_DIR -> restore target & restore_backup()")
        os.environ["PERSISTENT_DATA_DIR"] = str(restore_data)
        del sys.modules["backup_manager"]
        bm2 = importlib.import_module("backup_manager")
        bm2.restore_backup(zip_path)
        marker_restored = _read_marker(restore_data)
        assert marker_restored == marker_orig, \
            f"marker mismatch: {marker_restored!r} != {marker_orig!r}"
        print(f"      restored DB marker = {marker_restored!r} ✓")

        print(f"[6/6] verify managed files all present in restore target")
        for f in ("printix_multi.db", "fernet.key", "report_templates.json"):
            p = restore_data / f
            assert p.exists() and p.stat().st_size > 0, p
            print(f"      {f}: {p.stat().st_size} bytes ✓")

    print("\nALL TESTS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
