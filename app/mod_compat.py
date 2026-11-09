"""Persistent, auditable mod fixes for private CurseForge-export server profiles.

Source exports are immutable inputs. The effective ZIP is derived locally and can
be rebuilt from the original plus saved profile edits. No network calls or mod
scripts are executed by the portal.
"""
import hashlib
import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

from app.pack_import import inspect_curseforge_export

SOURCE = "curseforge-pack.zip"
EFFECTIVE = "curseforge-effective.zip"
PROJECT_LIMIT = 200
SAFE_TEXT = re.compile(r"^[^\x00-\x1f\x7f]{1,120}$")


def zip_location(profile_dir):
    return Path(profile_dir) / "minecraft" / ".mcd"


def source_file(profile_dir):
    return zip_location(profile_dir) / SOURCE


def _digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verified_source(profile_dir, meta):
    """Reject missing/tampered source, even if a generated ZIP happens to exist."""
    path = source_file(profile_dir)
    if not path.is_file() or path.is_symlink():
        raise ValueError("Original CurseForge export is missing")
    if _digest(path) != meta.get("pack_sha256"):
        raise ValueError("Original CurseForge export no longer matches its recorded checksum")
    detected = inspect_curseforge_export(path)
    if not detected or any(meta.get(k) != v for k, v in detected.items()):
        raise ValueError("Original CurseForge export does not match this profile")
    return path


def source_manifest(path):
    with zipfile.ZipFile(path) as archive:
        return json.loads(archive.read("manifest.json"))


def validate_changes(manifest, changes):
    """Validate project IDs and typed overrides against the original manifest."""
    if not isinstance(changes, list) or len(changes) > PROJECT_LIMIT:
        raise ValueError("Provide at most 200 mod compatibility entries")
    files = {entry["projectID"]: entry for entry in manifest["files"]}
    seen = set()
    clean = []
    for entry in changes:
        if not isinstance(entry, dict):
            raise ValueError("A mod compatibility entry must be an object")
        project = entry.get("project_id")
        action = entry.get("action")
        if type(project) is not int or project not in files or project in seen:
            raise ValueError("Each change needs a unique project ID from the uploaded manifest")
        seen.add(project)
        if action not in ("replace", "exclude", "notice"):
            raise ValueError("Mod action must be replace, exclude, or notice")
        name = entry.get("name")
        if not isinstance(name, str) or not SAFE_TEXT.fullmatch(name.strip()):
            raise ValueError("Provide a readable mod name (1-120 characters)")
        item = {"project_id": project, "name": name.strip(), "action": action}
        if action == "replace":
            file_id = entry.get("replacement_file_id")
            if type(file_id) is not int or not 1 <= file_id <= 2147483647:
                raise ValueError("Replacement CurseForge file ID must be a positive integer")
            item["replacement_file_id"] = file_id
        for field in ("client_version", "server_version", "reason"):
            val = entry.get(field, "")
            if not isinstance(val, str) or len(val) > 160 or any(ord(c) < 32 or ord(c) == 127 for c in val):
                raise ValueError(field + " must be plain text (max 160 characters)")
            if val.strip():
                item[field] = val.strip()
        notify = entry.get("client_update_required", action in ("replace", "notice"))
        if type(notify) is not bool:
            raise ValueError("Client update required must be true or false")
        if action == "notice" and not notify:
            raise ValueError("A notice must tell players to update their client")
        item["client_update_required"] = notify
        clean.append(item)
    return clean


def runtime_changes(meta):
    return [{k: item[k] for k in ("project_id", "action", "replacement_file_id") if k in item}
            for item in meta.get("mod_changes", [])
            if item["action"] in ("replace", "exclude")]


def runtime_signature(meta):
    payload = json.dumps({"source": meta["pack_sha256"], "changes": runtime_changes(meta)},
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def environment(meta):
    """Config for itzg AUTO_CURSEFORGE, including revision for pending detection."""
    changes = meta.get("mod_changes", [])
    replacements = any(x["action"] == "replace" for x in changes)
    exclusions = [str(x["project_id"]) for x in changes if x["action"] == "exclude"]
    env = {
        "CF_MODPACK_ZIP": "/data/.mcd/" + (EFFECTIVE if replacements else SOURCE),
        "MCD_MOD_REVISION": runtime_signature(meta),
    }
    if exclusions:
        env["CF_EXCLUDE_MODS"] = ",".join(exclusions)
    if replacements or exclusions:
        # The installer must reconcile stale mods after a version/exclusion change.
        env["CF_FORCE_SYNCHRONIZE"] = "true"
    return env


def client_notices(meta):
    """Explicit advice only; we do not claim all client/server versions must match."""
    notices = []
    for change in meta.get("mod_changes", []):
        if not change.get("client_update_required", False):
            continue
        notice = {
            "project_id": change["project_id"],
            "name": change["name"],
            "action": change["action"],
            "original_file_id": None,
            "required_file_id": change.get("replacement_file_id"),
            "client_version": change.get("client_version", ""),
            "server_version": change.get("server_version", ""),
            "reason": change.get("reason", ""),
        }
        notices.append(notice)
    return notices


def generate_effective(profile_dir, meta, changes):
    """Atomically produce a derived ZIP for changed file IDs; never rewrite SOURCE."""
    original = verified_source(profile_dir, meta)
    manifest = source_manifest(original)
    clean = validate_changes(manifest, changes)
    replacements = {e["project_id"]: e["replacement_file_id"]
                    for e in clean if e["action"] == "replace"}
    if not replacements:
        return clean
    target = zip_location(profile_dir) / EFFECTIVE
    # Same directory, so os.replace is atomic on the bind-mounted filesystem.
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".zip", delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        with zipfile.ZipFile(original) as src, zipfile.ZipFile(temp_path, "w") as dst:
            dst.comment = src.comment
            for info in src.infolist():
                data = src.read(info)
                if info.filename == "manifest.json":
                    edited = json.loads(data)
                    for entry in edited["files"]:
                        replacement = replacements.get(entry["projectID"])
                        if replacement is not None:
                            entry["fileID"] = replacement
                    data = json.dumps(edited, indent=2).encode("utf-8")
                dst.writestr(info, data)
        with zipfile.ZipFile(temp_path) as archive:
            if archive.testzip() is not None:
                raise ValueError("Generated CurseForge ZIP failed integrity validation")
        os.chmod(temp_path, original.stat().st_mode & 0o777)
        os.replace(temp_path, target)
    finally:
        temp_path.unlink(missing_ok=True)
    return clean


def validate_effective(profile_dir, meta):
    """Enforce complete on-disk plan before switching away from a running game."""
    source = verified_source(profile_dir, meta)
    manifest = source_manifest(source)
    clean = validate_changes(manifest, meta.get("mod_changes", []))
    if clean != meta.get("mod_changes", []):
        raise ValueError("Stored mod compatibility settings are invalid")
    expected = {e["project_id"]: e["replacement_file_id"] for e in clean if e["action"] == "replace"}
    if not expected:
        return
    effective = zip_location(profile_dir) / EFFECTIVE
    if not effective.is_file() or effective.is_symlink():
        raise ValueError("Effective CurseForge ZIP is missing; save mod changes again")
    detected = inspect_curseforge_export(effective)
    if not detected or any(meta.get(k) != v for k, v in detected.items()):
        raise ValueError("Effective CurseForge ZIP has invalid runtime metadata")
    actual = {e["projectID"]: e["fileID"] for e in source_manifest(effective)["files"]}
    original = {e["projectID"]: e["fileID"] for e in manifest["files"]}
    intended = {project: expected.get(project, file_id) for project, file_id in original.items()}
    if actual != intended:
        raise ValueError("Effective CurseForge ZIP does not match saved mod replacements")


def diagnostics(log):
    """Conservative diagnostics from the *latest* Minecraft boot attempt only.

    Never infer a mod upgrade solely from a stack trace: a mod ID and missing
    client-side class can be reported, but replacement versions require an
    administrator's explicit verified choice.
    """
    if isinstance(log, bytes):
        log = log.decode("utf-8", "replace")
    boot = log.rsplit("[init] Running as uid=", 1)[-1][-100000:]
    if re.search(r"Done \([0-9.]+s\)! For help, type", boot):
        return None
    lines = boot.splitlines()
    for idx, line in enumerate(lines):
        if "@Mixin target" in line and "was not found" in line:
            mod = re.search(r"from mod ([a-zA-Z0-9_-]{1,80})", line)
            missing = re.search(r"@Mixin target ([\w.$/]+) was not found", line)
            if mod:
                client = bool(missing and (".client." in missing.group(1) or "/client/" in missing.group(1)))
                return {
                    "kind": "missing-client-class" if client else "missing-mixin-target",
                    "mod": mod.group(1),
                    "message": (f"{mod.group(1)} references a client-only Minecraft class on the dedicated server."
                                if client else f"{mod.group(1)} has a mixin target missing from this runtime."),
                    "guidance": "Check this mod's dedicated-server release or exclude it if it is not required. The portal cannot select a safe replacement automatically.",
                }
    if any("A mod crashed on startup!" in line or "Minecraft server failed." in line for line in lines):
        return {"kind": "startup-failure", "mod": None,
                "message": "Minecraft failed during startup.",
                "guidance": "Review the server logs. A compatible replacement cannot be inferred from this error alone."}
    return None
