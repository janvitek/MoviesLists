"""Point-in-time copies of everything that cannot be regenerated.

A snapshot holds the shared override store and a consistent copy of the local
SQLite file. The SQLite copy goes through sqlite3's own backup API rather than
a file copy, so it is safe to take while the app is running and can never
capture a half-written page.

Retention is tiered rather than a flat count: recent snapshots are worth
keeping densely, older ones sparsely, and the oldest should not disappear
merely because time passed.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from . import sharing

SNAPSHOT_DIRNAME = "snapshots"
STAMP = "%Y%m%dT%H%M%SZ"

# Kept: every snapshot from the last 24, then one per day for 30 days, then
# one per week for a year. Anything else is pruned.
KEEP_RECENT = 24
KEEP_DAILY = 30
KEEP_WEEKLY = 52


def snapshot_dir(root: Path) -> Path:
    return Path(root) / SNAPSHOT_DIRNAME


def take(root: Path, database: Path, reason: str = "manual") -> Path:
    """Write a new snapshot and return its path."""
    root = Path(root)
    target_dir = snapshot_dir(root)
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime(STAMP)
    archive = target_dir / f"movieslists-{stamp}.zip"

    manifest = {
        "created_at": sharing.now(),
        "reason": reason,
        "device": sharing.device(),
        "database": str(database),
        "shared_root": str(root),
    }

    temp_db = target_dir / f".{stamp}.sqlite3"
    copied_db = False
    if Path(database).exists():
        _backup_sqlite(Path(database), temp_db)
        copied_db = True

    store = sharing.SharedStore(root)
    overrides = store.read_all()
    manifest["films_with_edits"] = len(overrides)
    manifest["fields"] = sum(len(f) for f in overrides.values())

    try:
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=1))
            # Consolidated copy: restorable without the per-film files.
            zf.writestr("overrides.json",
                        json.dumps(overrides, indent=1, ensure_ascii=False))
            if store.overrides.is_dir():
                for path in sorted(store.overrides.glob("*.json")):
                    zf.write(path, f"overrides/{path.name}")
            if copied_db:
                zf.write(temp_db, "library.sqlite3")
    finally:
        temp_db.unlink(missing_ok=True)
    return archive


def _backup_sqlite(source: Path, destination: Path) -> None:
    """Copy a live database safely using SQLite's backup API."""
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def parse_stamp(path: Path) -> datetime | None:
    try:
        return datetime.strptime(path.stem.split("-", 1)[1], STAMP).replace(
            tzinfo=timezone.utc)
    except (IndexError, ValueError):
        return None


def listing(root: Path) -> list[dict]:
    out = []
    for path in sorted(snapshot_dir(root).glob("movieslists-*.zip"), reverse=True):
        when = parse_stamp(path)
        out.append({
            "path": path, "name": path.name, "taken_at": when,
            "bytes": path.stat().st_size,
        })
    return out


def prune(root: Path, keep_recent: int = KEEP_RECENT,
          keep_daily: int = KEEP_DAILY, keep_weekly: int = KEEP_WEEKLY) -> list[Path]:
    """Thin old snapshots out. Returns what was removed."""
    entries = [e for e in listing(root) if e["taken_at"]]
    keep: set[Path] = {e["path"] for e in entries[:keep_recent]}

    seen_days: dict[str, Path] = {}
    seen_weeks: dict[str, Path] = {}
    for entry in entries:                       # newest first
        day = entry["taken_at"].strftime("%Y-%m-%d")
        week = entry["taken_at"].strftime("%G-W%V")
        seen_days.setdefault(day, entry["path"])
        seen_weeks.setdefault(week, entry["path"])
    keep |= set(list(seen_days.values())[:keep_daily])
    keep |= set(list(seen_weeks.values())[:keep_weekly])
    # The very first snapshot is the last line of defence; never drop it.
    if entries:
        keep.add(entries[-1]["path"])

    removed = []
    for entry in entries:
        if entry["path"] not in keep:
            entry["path"].unlink(missing_ok=True)
            removed.append(entry["path"])
    return removed


def restore(root: Path, archive: Path, database: Path,
            overrides_only: bool = True) -> dict:
    """Put a snapshot's contents back.

    Overrides are restored by default; the cached library is not, since it is
    rebuilt from TV.app anyway and the local copy is usually the fresher one.
    """
    root = Path(root)
    store = sharing.SharedStore(root)
    result = {"films": 0, "database_restored": False}

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        if "overrides.json" in names:
            overrides = json.loads(zf.read("overrides.json").decode("utf-8"))
            store.overrides.mkdir(parents=True, exist_ok=True)
            for pid, fields in overrides.items():
                store.write(pid, fields)
                result["films"] += 1
        if not overrides_only and "library.sqlite3" in names:
            backup = Path(f"{database}.before-restore")
            if Path(database).exists():
                shutil.copy2(database, backup)
                result["previous_database"] = str(backup)
            Path(database).parent.mkdir(parents=True, exist_ok=True)
            with zf.open("library.sqlite3") as src, open(database, "wb") as dst:
                shutil.copyfileobj(src, dst)
            result["database_restored"] = True
    return result
