"""
Backup / Restore helpers for the Printix Management Console.

Creates full local backups of the persistent add-on state so a later restore can
bring back users, password hashes, tenant settings, SQL/card mappings, demo
data, templates and the encryption key.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import sqlite3
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
    "printix_multi.db":      {"kind": "sqlite", "required": True},
    "demo_data.db":          {"kind": "sqlite", "required": False},
    "fernet.key":            {"kind": "plain",  "required": True},
    "report_templates.json": {"kind": "plain",  "required": False},
    "mcp_secrets.json":      {"kind": "plain",  "required": False},
    # v7.6.7: Starlette-Session-Signing-Key. Ohne den werden ALLE
    # aktiven Browser-Sessions beim Restore invalidiert — kein
    # Datenverlust, aber jeder muss neu einloggen.
    "web_session_key":       {"kind": "plain",  "required": False},
}

# v7.6.7: Verzeichnisse die rekursiv mitgesichert werden. Zwei
# wichtige:
#   - tls/      → manuell importiertes oder Auto-TLS-Zertifikat
#                 (sonst muss man HTTPS nach Restore neu einrichten)
#   - letsencrypt/ → certbot-Account-Daten + Renewal-Konfiguration.
#                 OHNE die triggerst du bei Restore eine NEUE
#                 LE-Cert-Issuance — das Let's-Encrypt-Rate-Limit
#                 erlaubt nur 50 Certs/Domain/Woche, also lieber
#                 die Renewal-Historie behalten.
MANAGED_DIRS = {
    "tls":         {"required": False},
    "letsencrypt": {"required": False},
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


# ─── v7.6.6: Optional passphrase encryption ────────────────────────────────
# Wenn beim create_backup() eine Passphrase angegeben wird, wird jeder
# Managed-File-Inhalt vor dem Zippen mit Fernet (AES-128-CBC + HMAC-
# SHA256) verschlüsselt. Key kommt aus PBKDF2-HMAC-SHA256 mit 600 000
# Iterationen — derzeitige OWASP-Empfehlung für Passwort-derived-keys.
# Salt wird pro Backup neu generiert (16 Bytes) und im Manifest abgelegt.
# Manifest-Format kennt dann `encryption: {kdf, iterations, salt}`.
# Restore prüft Format, fordert Passphrase, derived denselben Key,
# entschlüsselt jede Datei vor dem Wegschreiben.
#
# Cloud-Storage-tauglich: ohne Passphrase ist die Datei wertlos. Selbst
# wenn jemand Zugriff aufs ZIP bekommt — kein Fernet-Key, keine
# Credentials.

PBKDF2_ITERATIONS = 600_000  # OWASP 2024 empfohlen für PBKDF2-SHA256
PBKDF2_SALT_BYTES = 16


def _derive_fernet_key(passphrase: str, salt: bytes,
                       iterations: int = PBKDF2_ITERATIONS) -> bytes:
    """PBKDF2-HMAC-SHA256(passphrase, salt) → 32 raw bytes → Fernet-Key
    (urlsafe-b64-encoded). Lazy-importiert `cryptography` damit das
    Modul auch ohne installierte cryptography-Lib geladen werden kann
    (z.B. für unverschlüsselte Backups)."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=iterations,
    )
    raw = kdf.derive(passphrase.encode("utf-8"))
    return base64.urlsafe_b64encode(raw)


def _encrypt_bytes(data: bytes, fernet_key: bytes) -> bytes:
    from cryptography.fernet import Fernet
    return Fernet(fernet_key).encrypt(data)


def _decrypt_bytes(data: bytes, fernet_key: bytes) -> bytes:
    from cryptography.fernet import Fernet, InvalidToken
    try:
        return Fernet(fernet_key).decrypt(data)
    except InvalidToken as e:
        raise RuntimeError(
            "Restore: Passphrase falsch oder Backup beschädigt"
        ) from e


def _sqlite_backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(src) as source_conn:
        with sqlite3.connect(dst) as target_conn:
            source_conn.backup(target_conn)


def _copy_managed_file(name: str, temp_dir: Path,
                        fernet_key: Optional[bytes] = None) -> dict | None:
    meta = MANAGED_FILES[name]
    src = _managed_source(name)
    if not src.exists():
        if meta["required"]:
            raise FileNotFoundError(f"Required backup source missing: {src}")
        return None
    rel_path = f"data/{name}"
    target = temp_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)

    # v7.6.6: Wenn Passphrase aktiv (fernet_key gesetzt), schreiben wir
    # NICHT die Originaldatei sondern die verschlüsselte Variante.
    # Dateiname bekommt `.enc`-Suffix damit beim Extrahieren ohne
    # Schlüssel klar ist dass das ein verschlüsseltes Blob ist.
    if fernet_key is None:
        if meta["kind"] == "sqlite":
            _sqlite_backup(src, target)
        else:
            shutil.copy2(src, target)
        archive_path = rel_path
    else:
        if meta["kind"] == "sqlite":
            # SQLite via .backup() in eine temp-Datei, dann Bytes
            # verschlüsseln. .backup() braucht eine echte Datei.
            with tempfile.NamedTemporaryFile(prefix="printix-bk-", suffix=".db",
                                              delete=False) as tf:
                staging_db = Path(tf.name)
            try:
                _sqlite_backup(src, staging_db)
                plain = staging_db.read_bytes()
            finally:
                if staging_db.exists():
                    staging_db.unlink()
        else:
            plain = src.read_bytes()
        encrypted = _encrypt_bytes(plain, fernet_key)
        archive_path = rel_path + ".enc"
        enc_target = temp_dir / archive_path
        enc_target.parent.mkdir(parents=True, exist_ok=True)
        enc_target.write_bytes(encrypted)
        target = enc_target

    return {
        "kind": meta["kind"],
        "required": bool(meta["required"]),
        "archive_path": archive_path,
        "size": target.stat().st_size,
    }


def _copy_managed_dir(name: str, temp_dir: Path,
                       fernet_key: Optional[bytes] = None) -> dict | None:
    """v7.6.7: Sichert ein ganzes Verzeichnis unter DATA_DIR rekursiv.

    Layout im ZIP:
      data/<name>/relpath/file       (unverschlüsselt)
      data/<name>/relpath/file.enc   (verschlüsselt)

    Symlinks werden gefolgt (shutil.copy2 default). Empty-Dirs werden
    NICHT als ZIP-Eintrag gespeichert — bei Restore ggf. fehlende
    Subdirs durch parent.mkdir(parents=True) wieder erzeugt.
    """
    meta = MANAGED_DIRS[name]
    src_root = DATA_DIR / name
    if not src_root.exists() or not src_root.is_dir():
        if meta["required"]:
            raise FileNotFoundError(f"Required backup dir missing: {src_root}")
        return None

    members: list[dict] = []
    for src_path in sorted(src_root.rglob("*")):
        if not src_path.is_file():
            continue
        rel = src_path.relative_to(src_root)
        if fernet_key is None:
            archive_rel = f"data/{name}/{rel.as_posix()}"
            tgt = temp_dir / archive_rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, tgt)
        else:
            plain = src_path.read_bytes()
            ciphertext = _encrypt_bytes(plain, fernet_key)
            archive_rel = f"data/{name}/{rel.as_posix()}.enc"
            tgt = temp_dir / archive_rel
            tgt.parent.mkdir(parents=True, exist_ok=True)
            tgt.write_bytes(ciphertext)
        members.append({
            "rel":          rel.as_posix(),
            "archive_path": archive_rel,
            "size":         tgt.stat().st_size,
        })

    return {
        "kind":     "dir",
        "required": bool(meta["required"]),
        "members":  members,
    }


def create_backup(passphrase: Optional[str] = None) -> dict:
    """
    Create a full backup ZIP inside BACKUP_DIR.

    The backup contains the local application DB, local demo DB, encryption key
    and other persistent local data files used by the add-on.

    v7.6.6: Wenn ``passphrase`` angegeben ist, wird jeder Inhalt mit
    Fernet (AES-128-CBC + HMAC) verschlüsselt — Key kommt aus
    PBKDF2-HMAC-SHA256(passphrase, random-salt, 600 000 iter). Salt
    landet im Manifest, Passphrase nicht. Restore braucht dieselbe
    Passphrase. Cloud-Sicherung dann sicher: ohne Passphrase ist das
    ZIP wertlos.
    """
    _ensure_backup_dir()
    created_at = _utc_now()
    backup_name = _backup_zip_name()
    backup_path = BACKUP_DIR / backup_name

    encryption_meta: dict | None = None
    fernet_key: Optional[bytes] = None
    if passphrase:
        salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
        fernet_key = _derive_fernet_key(passphrase, salt)
        encryption_meta = {
            "kdf": "pbkdf2-hmac-sha256",
            "iterations": PBKDF2_ITERATIONS,
            "salt": base64.b64encode(salt).decode("ascii"),
            "scheme": "fernet",
        }

    with tempfile.TemporaryDirectory(prefix="printix-backup-") as tmp_root:
        temp_dir = Path(tmp_root)
        files: dict[str, dict] = {}
        for name in MANAGED_FILES:
            entry = _copy_managed_file(name, temp_dir, fernet_key=fernet_key)
            if entry:
                files[name] = entry

        # v7.6.7: zusätzlich Verzeichnisse mitsichern
        dirs: dict[str, dict] = {}
        for name in MANAGED_DIRS:
            entry = _copy_managed_dir(name, temp_dir, fernet_key=fernet_key)
            if entry:
                dirs[name] = entry

        manifest = {
            "format": ("printix-mcp-backup-v1-encrypted" if encryption_meta
                       else "printix-mcp-backup-v1"),
            "created_at": created_at,
            "version": _version(),
            "data_dir": str(DATA_DIR),
            "files": files,
            "dirs":  dirs,
        }
        if encryption_meta:
            manifest["encryption"] = encryption_meta
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
        "encrypted": bool(encryption_meta),
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

        fmt = manifest.get("format")
        is_encrypted = (fmt == "printix-mcp-backup-v1-encrypted")
        if fmt not in ("printix-mcp-backup-v1", "printix-mcp-backup-v1-encrypted"):
            errors.append(f"Format-Version unbekannt: {fmt!r}")

        if is_encrypted:
            enc = manifest.get("encryption") or {}
            if enc.get("kdf") != "pbkdf2-hmac-sha256":
                errors.append(f"Unbekannter KDF: {enc.get('kdf')!r}")
            if not enc.get("salt"):
                errors.append("Encryption-Salt fehlt im Manifest")
            try:
                int(enc.get("iterations", 0))
            except Exception:
                errors.append("Encryption-Iterations ungültig")

        files = manifest.get("files") or {}
        for name, meta in MANAGED_FILES.items():
            if meta["required"] and name not in files:
                errors.append(f"Required file missing in manifest: {name}")
        for name, entry in files.items():
            arc = entry.get("archive_path", "")
            if arc not in names:
                errors.append(f"manifest referenziert {arc!r}, aber Datei nicht im ZIP")
            # SQLite-Header-Check nur sinnvoll bei UNVERSCHLÜSSELTEN
            # Backups — Encrypted-Blob hat den Fernet-Token-Header
            # `gAAAAA...`, was natürlich kein SQLite-Header ist.
            if not is_encrypted and entry.get("kind") == "sqlite" and arc in names:
                with zf.open(arc) as fh:
                    head = fh.read(16)
                if not head.startswith(b"SQLite format 3"):
                    errors.append(f"{arc}: kein gültiger SQLite-Header")

        # v7.6.7: Verzeichnis-Einträge prüfen
        dirs_manifest = manifest.get("dirs") or {}
        for dir_name, dir_entry in dirs_manifest.items():
            for member in dir_entry.get("members", []):
                arc = member.get("archive_path", "")
                if arc and arc not in names:
                    errors.append(
                        f"manifest referenziert dir-Mitglied {arc!r}, "
                        f"aber Datei nicht im ZIP"
                    )

    return {
        "ok":       len(errors) == 0,
        "errors":   errors,
        "warnings": warnings,
        "manifest": manifest,
        "size":     size,
    }


def restore_backup(uploaded_zip_path: str,
                    passphrase: Optional[str] = None) -> dict:
    """
    Restore a previously created backup ZIP into DATA_DIR.

    Returns metadata and requires an application restart afterwards so the
    running process reloads the restored encryption key and state.

    v7.6.6: Wenn das Backup verschlüsselt ist (manifest.format ==
    `printix-mcp-backup-v1-encrypted`), wird ``passphrase`` benötigt.
    Falsche Passphrase → ``RuntimeError`` mit klarer Meldung. Beim
    Decrypten kommt Fernet's authentication-mode zum Einsatz —
    Manipulation am ZIP wird erkannt.
    """
    # v7.6.5: Pre-flight verify — verhindert dass ein halbgültiges
    # Archive teilweise extracted wird und die laufende Installation
    # in einen inkonsistenten Zustand bringt.
    verdict = verify_backup(uploaded_zip_path)
    if not verdict["ok"]:
        raise RuntimeError("Backup-Validierung fehlgeschlagen: " +
                           "; ".join(verdict["errors"]))

    manifest_pre = verdict.get("manifest") or {}
    is_encrypted = manifest_pre.get("format") == "printix-mcp-backup-v1-encrypted"
    if is_encrypted and not passphrase:
        raise RuntimeError(
            "Backup ist verschlüsselt — Passphrase erforderlich"
        )

    fernet_key: Optional[bytes] = None
    if is_encrypted:
        enc = manifest_pre.get("encryption") or {}
        salt = base64.b64decode(enc["salt"])
        iterations = int(enc.get("iterations", PBKDF2_ITERATIONS))
        fernet_key = _derive_fernet_key(passphrase or "", salt, iterations)

    with tempfile.TemporaryDirectory(prefix="printix-restore-") as tmp_root:
        temp_dir = Path(tmp_root)
        with zipfile.ZipFile(uploaded_zip_path, "r") as zf:
            zf.extractall(temp_dir)

        manifest_path = temp_dir / "manifest.json"
        if not manifest_path.exists():
            raise RuntimeError("Backup manifest missing")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("format") not in (
                "printix-mcp-backup-v1",
                "printix-mcp-backup-v1-encrypted"):
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

            # v7.6.6: Bei verschlüsselten Backups erst entschlüsseln,
            # dann an _restore_to_target geben. Wir schreiben die
            # entschlüsselten Bytes in eine zweite Temp-Datei, weil
            # _restore_to_target eine Source-Datei erwartet.
            if fernet_key is not None and archive_path.endswith(".enc"):
                ciphertext = extracted_file.read_bytes()
                plaintext = _decrypt_bytes(ciphertext, fernet_key)
                decrypted_path = extracted_file.with_suffix("")  # ".db.enc" → ".db"
                # Wenn der ursprüngliche Pfad schon `.db` etc. drinnen
                # hat (z.B. data/printix_multi.db.enc → with_suffix("")
                # → data/printix_multi.db), passt das. Für plain-Files
                # wie fernet.key.enc → fernet.key passt auch.
                decrypted_path.write_bytes(plaintext)
                extracted_file = decrypted_path

            _restore_to_target(extracted_file, target, entry.get("kind", meta["kind"]))
            restored_files.append(name)

        # v7.6.7: Verzeichnisse wiederherstellen
        restored_dirs: list[str] = []
        manifest_dirs = manifest.get("dirs") or {}
        for dir_name, dir_entry in manifest_dirs.items():
            if dir_name not in MANAGED_DIRS:
                # Unbekanntes Dir im Manifest → ignorieren (Forward-
                # Compat: alte Backups sehen evtl. neue Dir-Einträge
                # nicht; neue Backups dürfen Dirs haben die ältere
                # Versionen nicht kennen)
                continue
            target_root = DATA_DIR / dir_name
            target_root.mkdir(parents=True, exist_ok=True)
            for member in dir_entry.get("members", []):
                rel = member.get("rel", "")
                archive_path = member.get("archive_path", "")
                extracted_file = temp_dir / archive_path
                if not extracted_file.exists():
                    raise RuntimeError(f"Backup-Verzeichnis-Eintrag fehlt: {archive_path}")
                if fernet_key is not None and archive_path.endswith(".enc"):
                    ciphertext = extracted_file.read_bytes()
                    plaintext = _decrypt_bytes(ciphertext, fernet_key)
                    decrypted_path = extracted_file.with_suffix("")
                    decrypted_path.write_bytes(plaintext)
                    extracted_file = decrypted_path
                target_file = target_root / rel
                target_file.parent.mkdir(parents=True, exist_ok=True)
                _restore_to_target(extracted_file, target_file, "plain")
            restored_dirs.append(dir_name)

    return {
        "restored_at": _utc_now(),
        "backup_version": manifest.get("version", ""),
        "backup_created_at": manifest.get("created_at", ""),
        "restored_files": restored_files,
        "restored_dirs":  restored_dirs,
        "restart_required": True,
    }
