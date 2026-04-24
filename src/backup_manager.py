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
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", "/backup/printix-mcp"))


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
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)


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
    target.parent.mkdir(parents=True, exist_ok=True)
    if kind == "sqlite":
        _remove_sqlite_sidecars(target)
    os.replace(extracted_file, target)


def restore_backup(uploaded_zip_path: str) -> dict:
    """
    Restore a previously created backup ZIP into DATA_DIR.

    Returns metadata and requires an application restart afterwards so the
    running process reloads the restored encryption key and state.
    """
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
