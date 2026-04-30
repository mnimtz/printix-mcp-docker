"""
Backup / Restore helpers for the Printix Management Console.

Creates full local backups of the persistent add-on state so a later restore can
bring back users, password hashes, tenant settings, SQL/card mappings, demo
data, templates and the encryption key.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


DATA_DIR = Path(os.environ.get("PERSISTENT_DATA_DIR", "/data"))
# v7.6.5: Default-Pfad korrigiert. Vorher zeigte BACKUP_DIR auf
# /backup/printix-mcp — das ist ein HA-Addon-Pfad (HA mountet
# /backup aus dem Supervisor-Volume) und existiert im Docker-
# Container schlicht nicht → Permission denied beim mkdir(). Default
# ist jetzt /data/backups, was im persistenten Volume liegt das
# der Container ohnehin schreibend mountet. Override weiterhin über
# `BACKUP_DIR=/eigener/pfad` möglich.
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", str(DATA_DIR / "backups")))

# v7.6.5: Größen-Limit für Restore-Upload (DoS-Schutz). 200 MB ist
# großzügig — eine echte SQLite-DB wird selten so groß; gleichzeitig
# verhindert es 5-GB-Müll-Upload der den Container fluten könnte.
MAX_RESTORE_SIZE_BYTES = int(os.environ.get("MAX_RESTORE_SIZE_BYTES", str(200 * 1024 * 1024)))


MANAGED_FILES = {
    "printix_multi.db": {"kind": "sqlite", "required": True},
    "demo_data.db": {"kind": "sqlite", "required": False},
    "fernet.key": {"kind": "plain", "required": True},
    "report_templates.json": {"kind": "plain", "required": False},
    "mcp_secrets.json": {"kind": "plain", "required": False},
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _version() -> str:
    try:
        return Path(__file__).with_name("VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "unknown"


def _ensure_backup_dir() -> None:
    """v7.6.5: bessere Fehlermeldung — vorher kam ein nichtssagendes
    Permission-denied auf den falschen Default-Pfad."""
    try:
        BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    except PermissionError as e:
        raise PermissionError(
            f"Backup-Verzeichnis '{BACKUP_DIR}' nicht erstellbar "
            f"({e}). Default ist /data/backups — wenn /data im Container "
            f"nicht schreibbar ist, prüfe das Volume-Mount oder setze "
            f"BACKUP_DIR=/eigener/pfad in docker-compose.yml."
        ) from e
    if not os.access(str(BACKUP_DIR), os.W_OK):
        raise PermissionError(
            f"Backup-Verzeichnis '{BACKUP_DIR}' existiert, ist aber "
            f"nicht beschreibbar. Volume-Mount-Permissions prüfen "
            f"(im Container: chown -R 1000:1000 /data)."
        )


def _managed_source(name: str) -> Path:
    return DATA_DIR / name


def _backup_zip_name() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return f"printix-mcp-backup-{stamp}.zip"


def _sqlite_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source_conn:
        with sqlite3.connect(dst) as target_conn:
            source_conn.backup(target_conn)


def _copy_managed_file(name: str, temp_dir: Path) -> dict | None:
    meta = MANAGED_FILES[name]
    src = _managed_source(name)
    if not src.exists():
        if meta["required"]:
            raise FileNotFoundError(f"Required backup source missing: {src}")
        return None
    rel_path = f"data/{name}"
    target = temp_dir / rel_path
    if meta["kind"] == "sqlite":
        _sqlite_backup(src, target)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
    return {
        "kind": meta["kind"],
        "required": bool(meta["required"]),
        "archive_path": rel_path,
        "size": target.stat().st_size,
    }


def create_backup() -> dict:
    """
    Create a full backup ZIP inside BACKUP_DIR.

    The backup contains the local application DB, local demo DB, encryption key
    and other persistent local data files used by the add-on.
    """
    _ensure_backup_dir()
    created_at = _utc_now()
    backup_name = _backup_zip_name()
    backup_path = BACKUP_DIR / backup_name

    with tempfile.TemporaryDirectory(prefix="printix-backup-") as tmp_root:
        temp_dir = Path(tmp_root)
        files: dict[str, dict] = {}
        for name in MANAGED_FILES:
            entry = _copy_managed_file(name, temp_dir)
            if entry:
                files[name] = entry

        manifest = {
            "format": "printix-mcp-backup-v1",
            "created_at": created_at,
            "version": _version(),
            "data_dir": str(DATA_DIR),
            "files": files,
        }
        (temp_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        with zipfile.ZipFile(backup_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for root, _, filenames in os.walk(temp_dir):
                for filename in filenames:
                    abs_path = Path(root) / filename
                    arcname = abs_path.relative_to(temp_dir).as_posix()
                    zf.write(abs_path, arcname)

    return {
        "filename": backup_name,
        "path": str(backup_path),
        "created_at": created_at,
        "size": backup_path.stat().st_size,
    }


def list_backups() -> list[dict]:
    _ensure_backup_dir()
    rows = []
    for item in sorted(BACKUP_DIR.glob("printix-mcp-backup-*.zip"), reverse=True):
        rows.append({
            "filename": item.name,
            "path": str(item),
            "size": item.stat().st_size,
            "modified_at": datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc).replace(microsecond=0).isoformat(),
        })
    return rows


def resolve_backup_path(filename: str) -> Path:
    if not filename or "/" in filename or "\\" in filename:
        raise ValueError("Invalid backup filename")
    path = BACKUP_DIR / filename
    if not path.exists() or not path.is_file():
        raise FileNotFoundError(filename)
    return path


def _remove_sqlite_sidecars(target: Path) -> None:
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{target}{suffix}")
        if sidecar.exists():
            sidecar.unlink()


def _restore_to_target(extracted_file: Path, target: Path, kind: str) -> None:
    """v7.2.40: copy + atomic-replace via temp-file in target dir.

    Previously used os.replace(extracted_file, target) which fails with
    EXDEV (Errno 18, "Invalid cross-device link") in Docker because
    /tmp (tmpfs/overlay) and /data (mounted volume) are different file
    systems and rename() can only operate within one fs.

    Approach now:
      1. Copy from /tmp into the same directory as the target
         (= same filesystem).
      2. os.replace() the temp file onto the target — atomic on the
         target filesystem.
    Atomicity matters for SQLite (no half-replaced DB visible to a
    concurrent reader).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "sqlite":
        _remove_sqlite_sidecars(target)

    staging = target.with_suffix(target.suffix + ".restore-staging")
    try:
        if staging.exists():
            staging.unlink()
        shutil.copy2(extracted_file, staging)
        os.replace(staging, target)
    except Exception:
        if staging.exists():
            try:
                staging.unlink()
            except Exception:
                pass
        raise


def verify_backup(backup_zip_path: str) -> dict:
    """v7.6.5: Validiert ein Backup-ZIP ohne es zu restoren.

    Prüft:
      1. Datei existiert und ist <= MAX_RESTORE_SIZE_BYTES
      2. Ist ein gültiges ZIP
      3. Enthält manifest.json mit korrektem Format
      4. Alle in manifest.json referenzierten Dateien sind im Archiv vorhanden
      5. Alle SQLite-Dateien sind als gültige SQLite-DB öffenbar
      6. Required-Files (printix_multi.db, fernet.key) sind anwesend

    Returns:
        {"ok": bool, "errors": [...], "warnings": [...], "manifest": {...}}
    """
    errors: list[str] = []
    warnings: list[str] = []
    manifest: dict | None = None

    try:
        size = os.path.getsize(backup_zip_path)
    except OSError as e:
        return {"ok": False, "errors": [f"Datei nicht lesbar: {e}"], "warnings": [], "manifest": None}
    if size > MAX_RESTORE_SIZE_BYTES:
        errors.append(f"Backup zu groß: {size} bytes > Limit {MAX_RESTORE_SIZE_BYTES}")
        return {"ok": False, "errors": errors, "warnings": warnings, "manifest": None}
    if size == 0:
        errors.append("Backup ist leer (0 bytes)")
        return {"ok": False, "errors": errors, "warnings": warnings, "manifest": None}

    try:
        zf = zipfile.ZipFile(backup_zip_path, "r")
    except zipfile.BadZipFile as e:
        return {"ok": False, "errors": [f"Kein gültiges ZIP: {e}"], "warnings": [], "manifest": None}

    with zf:
        names = set(zf.namelist())
        if "manifest.json" not in names:
            errors.append("manifest.json fehlt im Backup")
            return {"ok": False, "errors": errors, "warnings": warnings, "manifest": None}
        try:
            manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        except Exception as e:
            errors.append(f"manifest.json nicht parsbar: {e}")
            return {"ok": False, "errors": errors, "warnings": warnings, "manifest": None}

        if manifest.get("format") != "printix-mcp-backup-v1":
            errors.append(f"Format-Version unbekannt: {manifest.get('format')!r}")

        files = manifest.get("files") or {}
        for name, meta in MANAGED_FILES.items():
            if meta["required"] and name not in files:
                errors.append(f"Required file missing in manifest: {name}")
        for name, entry in files.items():
            arc = entry.get("archive_path", "")
            if arc not in names:
                errors.append(f"manifest referenziert {arc!r}, aber Datei nicht im ZIP")
            if entry.get("kind") == "sqlite" and arc in names:
                # SQLite-Header check (erste 16 bytes = "SQLite format 3\x00")
                with zf.open(arc) as fh:
                    head = fh.read(16)
                if not head.startswith(b"SQLite format 3"):
                    errors.append(f"{arc}: kein gültiger SQLite-Header")

    return {
        "ok":       len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
        "manifest": manifest,
        "size":     size,
    }


def restore_backup(uploaded_zip_path: str) -> dict:
    """
    Restore a previously created backup ZIP into DATA_DIR.

    Returns metadata and requires an application restart afterwards so the
    running process reloads the restored encryption key and state.
    """
    # v7.6.5: Pre-flight verify — verhindert dass ein halbgültiges
    # Archive teilweise extracted wird und die laufende Installation
    # in einen inkonsistenten Zustand bringt.
    verdict = verify_backup(uploaded_zip_path)
    if not verdict["ok"]:
        raise RuntimeError("Backup-Validierung fehlgeschlagen: " +
                           "; ".join(verdict["errors"]))

    with tempfile.TemporaryDirectory(prefix="printix-restore-") as tmp_root:
        temp_dir = Path(tmp_root)
        with zipfile.ZipFile(uploaded_zip_path, "r") as zf:
            zf.extractall(temp_dir)

        manifest_path = temp_dir / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Backup manifest missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") != "printix-mcp-backup-v1":
            raise RuntimeError("Unsupported backup format")

        files = manifest.get("files") or {}
        for required_name, meta in MANAGED_FILES.items():
            if meta["required"] and required_name not in files:
                raise RuntimeError(f"Required backup entry missing: {required_name}")

        restored_files: list[str] = []
        for name, meta in MANAGED_FILES.items():
            entry = files.get(name)
            target = _managed_source(name)
            if not entry:
                if target.exists():
                    if meta["kind"] == "sqlite":
                        _remove_sqlite_sidecars(target)
                    target.unlink()
                continue
            archive_path = entry.get("archive_path", "")
            extracted_file = temp_dir / archive_path
            if not extracted_file.exists():
                raise RuntimeError(f"Backup payload missing: {archive_path}")
            _restore_to_target(extracted_file, target, entry.get("kind", meta["kind"]))
            restored_files.append(name)

    return {
        "restored_at": _utc_now(),
        "backup_version": manifest.get("version", ""),
        "backup_created_at": manifest.get("created_at", ""),
        "restored_files": restored_files,
        "restart_required": True,
    }
