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
    """Schreibt eine kleine Test-DB + Dummy-Fernet-Key + JSON-Datei.
    v7.6.7: + web_session_key + tls/ + letsencrypt/ Subdirs."""
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = data_dir / "printix_multi.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE smoke (k TEXT PRIMARY KEY, v TEXT)")
        conn.execute("INSERT INTO smoke VALUES ('marker','printix-mcp-backup-test')")
        conn.commit()
    (data_dir / "fernet.key").write_text("dummy-fernet-key-for-test\n")
    (data_dir / "report_templates.json").write_text(json.dumps({"hello": "world"}))
    (data_dir / "web_session_key").write_text("dummy-session-signing-key\n")

    # tls/ + letsencrypt/ als realistische Sub-Trees
    (data_dir / "tls").mkdir(exist_ok=True)
    (data_dir / "tls" / "cert.pem").write_text("---FAKE CERT---\n")
    (data_dir / "tls" / "key.pem").write_text("---FAKE KEY---\n")
    le_live = data_dir / "letsencrypt" / "live" / "example.com"
    le_live.mkdir(parents=True, exist_ok=True)
    (le_live / "fullchain.pem").write_text("---FAKE LE FULLCHAIN---\n")
    (data_dir / "letsencrypt" / "renewal" / "example.com.conf").parent.mkdir(parents=True, exist_ok=True)
    (data_dir / "letsencrypt" / "renewal" / "example.com.conf").write_text("[renewalparams]\n")


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

        print(f"[6/6] verify managed files + dirs all present in restore target")
        for f in ("printix_multi.db", "fernet.key", "report_templates.json",
                  "web_session_key"):
            p = restore_data / f
            assert p.exists() and p.stat().st_size > 0, p
            print(f"      {f}: {p.stat().st_size} bytes ✓")
        # Verzeichnisse
        for rel in ("tls/cert.pem", "tls/key.pem",
                    "letsencrypt/live/example.com/fullchain.pem",
                    "letsencrypt/renewal/example.com.conf"):
            p = restore_data / rel
            assert p.exists() and p.stat().st_size > 0, p
            print(f"      {rel}: {p.stat().st_size} bytes ✓")

    # ── v7.6.6: Encrypted-Backup-Roundtrip ──────────────────────────────
    with tempfile.TemporaryDirectory(prefix="bm-orig2-") as orig2_root, \
         tempfile.TemporaryDirectory(prefix="bm-bak2-") as bak2_root, \
         tempfile.TemporaryDirectory(prefix="bm-restore2-") as restore2_root:

        orig2 = Path(orig2_root) / "data"
        backup2_dir = Path(bak2_root) / "backups"
        restore2 = Path(restore2_root) / "data"

        os.environ["PERSISTENT_DATA_DIR"] = str(orig2)
        os.environ["BACKUP_DIR"] = str(backup2_dir)

        if "backup_manager" in sys.modules:
            del sys.modules["backup_manager"]
        bm3 = importlib.import_module("backup_manager")

        print(f"\n[ENC 1/5] Encrypted backup — seed source")
        _seed_data_dir(orig2)

        passphrase = "correct horse battery staple"
        print(f"[ENC 2/5] create_backup(passphrase=…)")
        r = bm3.create_backup(passphrase=passphrase)
        assert r["encrypted"], r
        print(f"          encrypted={r['encrypted']} size={r['size']} ✓")

        print(f"[ENC 3/5] verify_backup() — encrypted format")
        v = bm3.verify_backup(r["path"])
        assert v["ok"], f"verify failed: {v}"
        assert v["manifest"]["format"] == "printix-mcp-backup-v1-encrypted", v["manifest"]["format"]
        print(f"          format={v['manifest']['format']} ✓")

        print(f"[ENC 4/5] restore with WRONG passphrase — must fail")
        os.environ["PERSISTENT_DATA_DIR"] = str(restore2)
        del sys.modules["backup_manager"]
        bm4 = importlib.import_module("backup_manager")
        try:
            bm4.restore_backup(r["path"], passphrase="WRONG")
            print(f"          ✗ should have raised")
            return 1
        except RuntimeError as e:
            assert "Passphrase" in str(e) or "passphrase" in str(e).lower(), str(e)
            print(f"          rejected: {e!r} ✓")

        print(f"[ENC 5/5] restore with CORRECT passphrase")
        bm4.restore_backup(r["path"], passphrase=passphrase)
        marker_restored = _read_marker(restore2)
        assert marker_restored == "printix-mcp-backup-test", marker_restored
        print(f"          DB marker = {marker_restored!r} ✓")
        # Encrypted-Roundtrip muss auch dirs durchhalten
        assert (restore2 / "tls" / "cert.pem").read_text().startswith("---FAKE CERT---"), \
            "tls/cert.pem decryption mismatch"
        assert (restore2 / "letsencrypt" / "live" / "example.com" / "fullchain.pem")\
            .read_text().startswith("---FAKE LE FULLCHAIN---"), "le fullchain mismatch"
        print(f"          dirs restored & decrypted (tls/, letsencrypt/) ✓")

    print("\nALL TESTS PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(main())
