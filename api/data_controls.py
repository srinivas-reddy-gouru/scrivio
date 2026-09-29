"""What Scrivio holds about you, and how to take it out or remove it.

Resumes and interview answers are about as personal as working documents
get. Two statements have to be true and findable:

  Where it is.     On this machine, in the folders listed by inventory().
  Where it goes.   To whichever model provider you configured, each time
                   something is analysed. "Stored locally" and "processed
                   locally" are different claims, and only the first is
                   true of Scrivio unless the provider is a local model.

This module is the inventory, the export, the restore, and the delete.
An export is a zip of the records exactly as stored, with a manifest, and
is the backup format: restore reads the same file.

Nothing here claims compliance with any regulation. It describes what
the software does.
"""
from __future__ import annotations

import io
import json
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path

FORMAT = 1
CONFIRMATION = "delete everything"

# Record kinds: (folder under the output directory, file pattern)
KINDS: dict[str, tuple[str, str]] = {
    "resumes": ("resumes", "*.json"),
    "job_targets": ("job_profiles", "*.json"),
    "interviews": ("interviews", "*.json"),
}
_NOT_ARTICLES = {"resumes", "job_profiles", "interviews", "_jobs", "demo-mode"}


def _articles(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        d for d in root.iterdir()
        if d.is_dir() and d.name not in _NOT_ARTICLES and (d / "meta.json").is_file())


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def inventory(root: Path, cache: Path | None = None) -> dict:
    """Counts, sizes, and locations. No contents."""
    kinds = {}
    for kind, (folder, pattern) in KINDS.items():
        files = sorted((root / folder).glob(pattern)) if (root / folder).is_dir() else []
        kinds[kind] = {"count": len(files), "bytes": sum(_size(f) for f in files),
                       "folder": str(root / folder)}
    articles = _articles(root)
    kinds["articles"] = {"count": len(articles), "bytes": sum(_size(a) for a in articles),
                         "folder": str(root)}
    runs = root / "_jobs"
    kinds["run_records"] = {
        "count": len(list(runs.glob("*.json"))) if runs.is_dir() else 0,
        "bytes": _size(runs) if runs.is_dir() else 0, "folder": str(runs)}
    set_aside = [p for p in root.rglob("*.corrupt")] if root.is_dir() else []
    kinds["set_aside"] = {"count": len(set_aside), "bytes": sum(_size(p) for p in set_aside),
                          "folder": str(root)}
    if cache is not None:
        kinds["stage_cache"] = {
            "count": len(list(cache.glob("*.json"))) if cache.is_dir() else 0,
            "bytes": _size(cache) if cache.is_dir() else 0, "folder": str(cache)}
    return kinds


def export_all(root: Path) -> bytes:
    """Every record, as stored, in one zip. Also the backup format."""
    out = io.BytesIO()
    manifest = {"format": FORMAT, "application": "scrivio",
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "counts": {}}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for kind, (folder, pattern) in KINDS.items():
            files = sorted((root / folder).glob(pattern)) if (root / folder).is_dir() else []
            manifest["counts"][kind] = len(files)
            for path in files:
                archive.write(path, f"{folder}/{path.name}")
        articles = _articles(root)
        manifest["counts"]["articles"] = len(articles)
        for article in articles:
            for path in sorted(article.iterdir()):
                if path.is_file():
                    archive.write(path, f"articles/{article.name}/{path.name}")
        archive.writestr("manifest.json", json.dumps(manifest, indent=2))
    return out.getvalue()


class RestoreRefused(ValueError):
    pass


def _safe_members(archive: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """A backup is a file someone hands you. Its paths are checked before
    anything is written: no absolute paths, no "..", nothing outside the
    folders a backup is supposed to contain, and no enormous members."""
    allowed = {folder for folder, _ in KINDS.values()} | {"articles"}
    members = []
    total = 0
    for info in archive.infolist():
        if info.is_dir() or info.filename == "manifest.json":
            continue
        parts = Path(info.filename).parts
        if (info.filename.startswith(("/", "\\")) or ".." in parts
                or not parts or parts[0] not in allowed):
            raise RestoreRefused(
                f"The backup contains a path it should not: {info.filename!r}")
        total += info.file_size
        if info.file_size > 50_000_000 or total > 2_000_000_000:
            raise RestoreRefused("The backup is larger than a backup should be.")
        members.append(info)
    return members


def restore(root: Path, backup: bytes, *, replace: bool = False) -> dict:
    """Put a backup's records back. Existing records are left alone unless
    `replace` is set, and even then the current state is exported first,
    to `root/_before-restore-<time>.zip`, so that a restore can itself be
    undone."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(backup))
        manifest = json.loads(archive.read("manifest.json"))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise RestoreRefused("That file is not a Scrivio backup.") from exc
    if manifest.get("application") != "scrivio":
        raise RestoreRefused("That file is not a Scrivio backup.")
    if int(manifest.get("format", 0)) > FORMAT:
        raise RestoreRefused(
            "That backup was made by a newer version of Scrivio than this one. "
            "Update Scrivio, then restore it.")
    members = _safe_members(archive)

    saved_first = None
    if replace and root.is_dir() and any(root.iterdir()):
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        saved_first = root / f"_before-restore-{stamp}.zip"
        saved_first.write_bytes(export_all(root))

    restored, skipped = 0, 0
    for info in members:
        parts = Path(info.filename).parts
        target = (root.joinpath(*parts[1:]) if parts[0] == "articles"
                  else root.joinpath(*parts))
        if target.exists() and not replace:
            skipped += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(archive.read(info))
        restored += 1
    return {"restored": restored, "skipped_existing": skipped,
            "saved_first": str(saved_first) if saved_first else None,
            "backup_created_at": manifest.get("created_at")}


def delete_all(root: Path, cache: Path | None = None) -> dict:
    """Every record, run record, set-aside file, and cached stage output.
    Settings and the session key are not data about the user's job search
    and are left in place."""
    removed = {}
    for kind, (folder, pattern) in KINDS.items():
        files = list((root / folder).glob("*")) if (root / folder).is_dir() else []
        for path in files:
            if path.is_file():
                path.unlink()
        removed[kind] = len([f for f in files if f.suffix == ".json"])
    articles = _articles(root)
    for article in articles:
        shutil.rmtree(article)
    removed["articles"] = len(articles)
    runs = root / "_jobs"
    if runs.is_dir():
        removed["run_records"] = len(list(runs.glob("*.json")))
        shutil.rmtree(runs)
    if cache is not None and cache.is_dir():
        removed["stage_cache"] = len(list(cache.glob("*.json")))
        for path in cache.glob("*.json"):
            path.unlink()
    return removed
