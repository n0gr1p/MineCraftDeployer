"""Fail-closed offline Minecraft profile backups.

The backup target is configured separately from the game-data bind mount.
Never create its root automatically: a missing HDD mount must not silently
redirect large backup writes to the Linux root filesystem.
"""
import hashlib
import json
import os
import re
import tarfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

BACKUP_ROOT = Path("/backups")
BACKUP_HOST_DIR = os.getenv("MCD_BACKUP_HOST_DIR", "")
BACKUP_VOLUME_UUID = os.getenv("MCD_BACKUP_VOLUME_UUID", "").lower()
MARKER = ".minecraftdeployer-volume-uuid"
BACKUP_RE = re.compile(r"backup-(\\d{8}T\\d{6}Z)-([0-9a-f]{8})\\.tar")


class BackupUnavailable(RuntimeError):
    pass


def backup_root():
    if not BACKUP_HOST_DIR or not BACKUP_VOLUME_UUID:
        raise BackupUnavailable("Backups are not configured. Set MCD_BACKUP_HOST_DIR and MCD_BACKUP_VOLUME_UUID.")
    if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", BACKUP_VOLUME_UUID):
        raise BackupUnavailable("MCD_BACKUP_VOLUME_UUID is invalid.")
    root = BACKUP_ROOT
    if root.is_symlink() or not root.is_dir() or not root.is_mount():
        raise BackupUnavailable("Backup destination is not an accessible Docker bind mount.")
    marker = root / MARKER
    if marker.is_symlink() or not marker.is_file():
        raise BackupUnavailable("Backup volume marker missing; refusing to write to a possibly unmounted drive.")
    try:
        if marker.read_text(encoding="ascii").strip().lower() != BACKUP_VOLUME_UUID:
            raise BackupUnavailable("Backup drive UUID marker mismatch; refusing to write.")
    except (OSError, UnicodeError) as exc:
        raise BackupUnavailable("Backup drive marker is unreadable.") from exc
    return root


def _archives(folder):
    for path in folder.glob("backup-*.tar"):
        if path.is_file() and not path.is_symlink() and BACKUP_RE.fullmatch(path.name):
            yield path


def list_backups(sid):
    folder = backup_root() / sid
    if not folder.exists():
        return []
    if folder.is_symlink() or not folder.is_dir():
        raise BackupUnavailable("Unsafe backup directory.")
    result = []
    for archive in sorted(_archives(folder), reverse=True):
        manifest = archive.with_suffix(".json")
        if manifest.is_symlink() or not manifest.is_file():
            continue
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
            if info.get("filename") != archive.name or info.get("server_id") != sid:
                continue
            result.append({"filename": archive.name, "created_utc": info["created_utc"],
                           "size_bytes": archive.stat().st_size,
                           "sha256": info["sha256"]})
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return result


def create_offline_backup(sid, profile):
    """Create an atomic uncompressed tar, with SHA256 metadata.

    Call under the portal's deployment lock only after confirming the Docker
    game container is stopped. Does not modify any game file.
    """
    root = backup_root()
    if not re.fullmatch(r"[0-9a-f]{32}", sid):
        raise BackupUnavailable("Invalid server profile ID.")
    if profile.is_symlink() or profile.resolve().name != sid:
        raise BackupUnavailable("Unsafe Minecraft profile source.")
    if not (profile / "meta.json").is_file() or not (profile / "minecraft").is_dir():
        raise BackupUnavailable("Minecraft profile files are missing.")

    target = root / sid
    if target.is_symlink():
        raise BackupUnavailable("Unsafe backup destination.")
    target.mkdir(mode=0o700, exist_ok=True)
    if target.is_symlink() or not target.is_dir():
        raise BackupUnavailable("Unsafe backup destination.")

    moment = datetime.now(timezone.utc)
    name = "backup-" + moment.strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8] + ".tar"
    final = target / name
    temporary = target / ("." + name + ".partial")
    manifest = target / (name[:-4] + ".json")
    manifest_temp = target / ("." + name + ".json.partial")
    try:
        with tarfile.open(temporary, mode="w", dereference=False) as archive:
            archive.add(profile / "meta.json", arcname="meta.json", recursive=False)
            archive.add(profile / "minecraft", arcname="minecraft", recursive=True)
        with temporary.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        manifest_data = {"format": 1, "server_id": sid, "filename": name,
                         "created_utc": moment.isoformat(), "sha256": digest}
        # Flush both files before advertising the backup via an atomic rename.
        with manifest_temp.open("w", encoding="utf-8") as out:
            json.dump(manifest_data, out)
            out.flush()
            os.fsync(out.fileno())
        with temporary.open("rb") as out:
            os.fsync(out.fileno())
        temporary.replace(final)
        manifest_temp.replace(manifest)
        return {"filename": name, "created_utc": moment.isoformat(),
                "size_bytes": final.stat().st_size, "sha256": digest}
    finally:
        temporary.unlink(missing_ok=True)
        manifest_temp.unlink(missing_ok=True)
