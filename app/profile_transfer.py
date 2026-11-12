"""Safe, bounded export of stopped Minecraft Java worlds and profile pack snapshots."""
import os
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

MAX_FILES = 100000
SPACE_RESERVE = 64 * 1024 * 1024


def world_path(profile_dir, meta):
    """Resolve a single Minecraft save, never an arbitrary filesystem path."""
    data = Path(profile_dir) / "minecraft"
    name = meta.get("world_name")
    props = data / "server.properties"
    if name is None and props.is_file():
        for line in props.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("level-name="):
                name = line.split("=", 1)[1].strip()
                break
    name = name or "world"
    if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9 _.-]{1,128}", name) or
            name in (".", "..") or name.startswith(".")):
        raise ValueError("World directory has an unsafe level-name")
    world = data / name
    if world.is_symlink() or not world.is_dir() or not (world / "level.dat").is_file():
        raise ValueError("No complete Minecraft world save found under " + name)
    if not world.resolve().is_relative_to(data.resolve()):
        raise ValueError("World path escapes the game data directory")
    return world


def inspect_world_files(world):
    files = []
    total = 0
    for directory, dirnames, names in os.walk(world, followlinks=False):
        parent = Path(directory)
        for name in dirnames:
            if (parent / name).is_symlink():
                raise ValueError("World contains a symlink directory and cannot be exported safely")
        for name in names:
            item = parent / name
            if item.is_symlink() or not item.is_file():
                raise ValueError("World contains a special file and cannot be exported safely")
            stat = item.stat()
            if not item.resolve().is_relative_to(world.resolve()):
                raise ValueError("World contains a path outside the save directory")
            files.append(item)
            total += stat.st_size
            if len(files) > MAX_FILES:
                raise ValueError("World contains too many files for a safe export")
    return files, total


def export_stopped_world(root, profile_dir, meta):
    """Build a temporary ZIP that remains outside game data; caller owns cleanup."""
    world = world_path(profile_dir, meta)
    files, total = inspect_world_files(world)
    if shutil.disk_usage(root).free < total + SPACE_RESERVE:
        raise ValueError("Not enough free disk space to stage this world download")
    fd, name = tempfile.mkstemp(prefix=".mcd-world-export-", suffix=".zip", dir=root)
    os.close(fd)
    archive = Path(name)
    try:
        with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED,
                             compresslevel=2, allowZip64=True) as output:
            for file in files:
                # Preserve a single enclosing save folder for easy reimport.
                path = file.relative_to(world.parent)
                output.write(file, arcname=path.as_posix())
        return archive
    except Exception:
        archive.unlink(missing_ok=True)
        raise
