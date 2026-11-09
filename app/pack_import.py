"""Discover supported server-pack layouts without executing uploaded startup scripts."""
import re
import zipfile
from pathlib import Path

# This module deliberately never shells out or runs anything from uploaded packs.
_VERSION = re.compile(r"\d+(?:\.\d+){1,3}")
_SUPPORTED_JAVA = {17, 21}


def _variables(path: Path) -> dict[str, str]:
    if path.stat().st_size > 65536:
        raise ValueError("ServerPackCreator variables.txt is too large")
    parsed = {}
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)\s*=\s*(.*?)\s*", line)
        if match:
            value = match.group(2).strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            parsed[match.group(1)] = value
    return parsed


def prepare_pack(data: Path, safe_extract) -> dict:
    """Inspect/extract one outer wrapper and return a validated runtime profile.

    Normal upload and old, already-installed outer ZIPs both use this path.
    The original pack's config/mods are preserved, but its scripts are NOT run.
    """
    dregora_readme = data / "SERVER README OR DIE.txt"
    if not (data / "server.jar").is_file() and not (data / "variables.txt").is_file() and not dregora_readme.is_file():
        wrappers = [p for p in data.iterdir() if p.is_file() and p.suffix.lower() == ".zip"]
        if len(wrappers) == 1:
            nested = wrappers[0]
            if not zipfile.is_zipfile(nested):
                raise ValueError("Nested server pack is not a valid ZIP")
            safe_extract(nested, data)
            nested.unlink()

    # Some server ZIPs wrap all files inside one directory. Only flatten an
    # unmistakable Dregora server-pack root; refuse conflicting destinations.
    if not dregora_readme.is_file():
        contents = list(data.iterdir())
        if len(contents) == 1 and contents[0].is_dir():
            wrapped = contents[0]
            if (wrapped / "SERVER README OR DIE.txt").is_file():
                for item in wrapped.iterdir():
                    destination = data / item.name
                    if destination.exists():
                        raise ValueError("Dregora wrapper conflicts with an existing file")
                    item.rename(destination)
                wrapped.rmdir()

    variables = data / "variables.txt"
    if variables.is_file():
        values = _variables(variables)
        loader = values.get("MODLOADER", "").upper()
        version = values.get("MINECRAFT_VERSION", "")
        loader_version = values.get("MODLOADER_VERSION", "")
        java_text = values.get("RECOMMENDED_JAVA_VERSION", "")
        if loader != "FABRIC":
            raise ValueError(f"ServerPackCreator modloader {loader or '(missing)'} is not supported yet; this build supports Fabric")
        if not _VERSION.fullmatch(version) or not _VERSION.fullmatch(loader_version):
            raise ValueError("ServerPackCreator has an invalid Minecraft or Fabric loader version")
        if not java_text.isdigit() or int(java_text) not in _SUPPORTED_JAVA:
            raise ValueError(f"ServerPackCreator Java version {java_text or '(missing)'} is unsupported; supported versions: 17, 21")
        return {
            "mode": "fabric",
            "minecraft_version": version,
            "loader_version": loader_version,
            "java_version": int(java_text),
            "pack_format": "serverpackcreator",
        }

    # Dregora v1.1.2b ships mods/configuration and expects the administrator
    # to install Forge 1.12.2-14.23.5.2860 separately. Do not execute pack
    # startup scripts. The itzg Forge runtime installs Forge from its source.
    dregora_readme = data / "SERVER README OR DIE.txt"
    if dregora_readme.is_file():
        if dregora_readme.stat().st_size > 65536:
            raise ValueError("Dregora server README is unexpectedly large")
        instructions = dregora_readme.read_text(encoding="utf-8-sig", errors="replace")
        expected = (
            "DREGORA WILL NOT GENERATE CORRECTLY",
            "Forge 2860 server installer",
            "level-type=OTG",
            "level-name=DregoraRL",
        )
        if not all(marker in instructions for marker in expected):
            raise ValueError("Unrecognized Dregora server README; refusing to guess a Forge version or world generator")
        if not (data / "mods").is_dir() or not (data / "config").is_dir():
            raise ValueError("Incomplete Dregora server pack: expected both mods/ and config/ directories")
        return {
            "mode": "forge",
            "minecraft_version": "1.12.2",
            "loader_version": "14.23.5.2860",
            "java_version": 8,
            "world_name": "DregoraRL",
            "pack_format": "rlcraft-dregora",
        }

    if (data / "server.jar").is_file():
        return {"mode": "zip", "java_version": 21, "pack_format": "jar"}

    if (data / "manifest.json").is_file():
        raise ValueError("CurseForge client-export ZIP is not a server pack; provide a dedicated server pack ZIP")

    raise ValueError("No supported server launcher found (expected server.jar or ServerPackCreator variables.txt)")
