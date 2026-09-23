"""Sharing the library's user data across machines over a synced folder.

The cache and the edits are different kinds of data and belong in different
places:

* `item`, `item_director`, `sync_run`, `library_change` are derived from
  whatever TV.app on *this* machine reports. They are disposable, differ
  legitimately between machines, and stay in the local SQLite file.
* Overrides -- your ratings, reviews, corrected fields -- and the Letterboxd
  import log exist nowhere else. Those go in the shared folder.

The shared side deliberately avoids SQLite. A sync service can copy a database
mid-write, or a WAL file out of step with its main file, and the result is a
corrupt database rather than a lost edit; SQLite's own locking is POSIX
advisory locking, which does not cross machines. So the shared store is a
directory of small JSON documents, one per film, and every field carries its
own timestamp. That buys three things a shared database cannot:

* two machines editing *different* fields of the same film both survive,
  because the merge is per field, not per file;
* a sync conflict damages one film, not the library;
* everything is plain text, so a bad merge is inspectable and repairable.

The lock is a coarse courtesy, not a guarantee -- see `lock()`.
"""

from __future__ import annotations

import json
import os
import socket
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

CONFIG_DIR = Path("~/.config/movieslists").expanduser()
DEVICE_FILE = CONFIG_DIR / "device.json"
CONFIG_FILE = CONFIG_DIR / "config.json"

OVERRIDES_DIRNAME = "overrides"
LOCK_NAME = ".lock.json"

# How long before a lock is presumed abandoned. Generous, because a sync
# service can take a while to propagate the file that releases it.
LOCK_STALE_SECONDS = 120
LOCK_POLL_SECONDS = 0.25

# Names sync services leave behind when two machines write the same file.
CONFLICT_MARKERS = ("conflicted copy", "conflict", "(case conflict)", "~$")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ device

def device() -> dict:
    """A stable identity for this machine, so edits can be attributed."""
    if DEVICE_FILE.is_file():
        try:
            return json.loads(DEVICE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    record = {
        "id": uuid.uuid4().hex[:12],
        "host": socket.gethostname(),
        "created_at": now(),
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    DEVICE_FILE.write_text(json.dumps(record, indent=1), encoding="utf-8")
    return record


# ------------------------------------------------------------------ config

def load_config() -> dict:
    if CONFIG_FILE.is_file():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
    return {}


def save_config(config: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(config, indent=1), encoding="utf-8")


def shared_root(explicit: str | None = None) -> Path | None:
    """Where the shared store lives, or None when sharing is off."""
    value = explicit or os.environ.get("MOVIESLISTS_SHARED") \
        or load_config().get("shared_dir")
    return Path(value).expanduser() if value else None


# -------------------------------------------------------------------- lock

class LockBusy(RuntimeError):
    pass


@contextmanager
def lock(root: Path, purpose: str = "write", timeout: float = 20.0,
         steal_after: float = LOCK_STALE_SECONDS):
    """Best-effort mutual exclusion across machines.

    This is NOT a correctness guarantee and must not be treated as one: the
    lock file itself travels by the same sync service as the data, so two
    machines can hold it at once while a sync is in flight, and a machine that
    is offline sees no lock at all. It is here to serialise the ordinary case
    -- two machines you are actually using -- and to leave a breadcrumb saying
    who was writing when something does go wrong.

    The real safety comes from the store's shape: per-field timestamps and one
    file per film, so a lost race costs one field, not the library.
    """
    root.mkdir(parents=True, exist_ok=True)
    path = root / LOCK_NAME
    me = device()
    deadline = time.monotonic() + timeout
    stolen_from = None

    while True:
        try:
            # O_EXCL is atomic on a local filesystem, which is where the
            # contention we can actually win happens.
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(handle, "w", encoding="utf-8") as fh:
                json.dump({"device": me["id"], "host": me["host"],
                           "pid": os.getpid(), "purpose": purpose,
                           "acquired_at": now()}, fh)
            break
        except FileExistsError:
            held = _read_lock(path)
            age = _lock_age(held)
            if age is not None and age > steal_after:
                stolen_from = held
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                who = f"{held.get('host', '?')} (pid {held.get('pid', '?')})" \
                    if held else "another machine"
                raise LockBusy(
                    f"{who} is already writing to {root}. "
                    f"Wait for it to finish, or remove {path} if that machine "
                    f"is no longer running."
                )
            time.sleep(LOCK_POLL_SECONDS)

    try:
        yield {"stolen_from": stolen_from}
    finally:
        try:
            current = _read_lock(path)
            # Only release a lock that is still ours.
            if not current or current.get("device") == me["id"]:
                path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_lock(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _lock_age(held: dict | None) -> float | None:
    if not held or not held.get("acquired_at"):
        return None
    try:
        acquired = datetime.fromisoformat(held["acquired_at"])
    except ValueError:
        return None
    if acquired.tzinfo is None:
        acquired = acquired.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - acquired).total_seconds()


# ------------------------------------------------------------------- store

class SharedStore:
    """One JSON document per film, each field stamped with when it changed.

    A document looks like:

        {"persistent_id": "5C480B39468AF151",
         "fields": {"rating": {"value": "80",
                               "updated_at": "2026-09-23T17:40:00.000+00:00",
                               "source": "manual",
                               "device": "a1b2c3"}}}
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.overrides = self.root / OVERRIDES_DIRNAME

    def path_for(self, persistent_id: str) -> Path:
        safe = "".join(c for c in persistent_id if c.isalnum() or c in "-_")
        return self.overrides / f"{safe or 'unknown'}.json"

    def read(self, persistent_id: str) -> dict:
        path = self.path_for(persistent_id)
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("fields", {})
        except (OSError, ValueError):
            return {}

    def read_all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        if not self.overrides.is_dir():
            return out
        for path in sorted(self.overrides.glob("*.json")):
            if _looks_conflicted(path.name):
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            pid = doc.get("persistent_id") or path.stem
            out[pid] = doc.get("fields", {})
        return out

    def write(self, persistent_id: str, fields: dict) -> None:
        """Replace one film's document. Written atomically via rename."""
        self.overrides.mkdir(parents=True, exist_ok=True)
        path = self.path_for(persistent_id)
        payload = {"persistent_id": persistent_id, "fields": fields,
                   "written_at": now(), "device": device()["id"]}
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(payload, indent=1, ensure_ascii=False),
                        encoding="utf-8")
        temp.replace(path)

    def stamp(self, persistent_id: str, field: str, value, source: str) -> None:
        """Record one field, leaving the film's other fields alone."""
        fields = self.read(persistent_id)
        fields[field] = {
            "value": None if value is None else str(value),
            "updated_at": now(), "source": source, "device": device()["id"],
        }
        self.write(persistent_id, fields)

    def forget(self, persistent_id: str, field: str | None = None) -> None:
        """Mark a field removed.

        A deletion has to be recorded, not just applied: if the file simply
        lost the field, the next merge would see it present on the other
        machine and helpfully restore it. So a removal leaves a tombstone
        carrying the time it happened, and the merge treats that like any
        other dated change.
        """
        fields = self.read(persistent_id)
        targets = list(fields) if field is None else [field]
        for name in targets:
            fields[name] = {"value": None, "deleted": True,
                            "updated_at": now(), "source": "manual",
                            "device": device()["id"]}
        if fields:
            self.write(persistent_id, fields)

    # --- viewings ------------------------------------------------------
    #
    # Kept in the same per-film document as the overrides, under their own
    # key. A viewing carries a uuid and an updated_at, so two machines that
    # each logged one before syncing end up with both, and a deletion --
    # which travels as a tombstone -- is not undone by the other side.

    def watches(self, persistent_id: str) -> list[dict]:
        path = self.path_for(persistent_id)
        try:
            return json.loads(path.read_text(encoding="utf-8")).get("watches", [])
        except (OSError, ValueError):
            return []

    def set_watches(self, persistent_id: str, rows: list[dict]) -> None:
        self.overrides.mkdir(parents=True, exist_ok=True)
        path = self.path_for(persistent_id)
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = {"persistent_id": persistent_id, "fields": {}}
        doc["watches"] = rows
        doc["written_at"] = now()
        doc["device"] = device()["id"]
        temp = path.with_suffix(".json.tmp")
        temp.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                        encoding="utf-8")
        temp.replace(path)

    def all_watches(self) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        if not self.overrides.is_dir():
            return out
        for path in sorted(self.overrides.glob("*.json")):
            if _looks_conflicted(path.name):
                continue
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows = doc.get("watches")
            if rows:
                out[doc.get("persistent_id") or path.stem] = rows
        return out

    def conflicts(self) -> list[Path]:
        """Files a sync service left behind after a collision."""
        if not self.root.is_dir():
            return []
        return sorted(p for p in self.root.rglob("*")
                      if p.is_file() and _looks_conflicted(p.name))


def _looks_conflicted(name: str) -> bool:
    lowered = name.lower()
    return any(marker in lowered for marker in CONFLICT_MARKERS)


# ----------------------------------------------------------------- merging

def _as_utc(value) -> float:
    """Seconds since the epoch for either timestamp spelling we store."""
    if not value:
        return 0.0
    text = str(value).strip().replace(" ", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if moment.tzinfo is None:                 # SQLite datetime('now') is UTC
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def reconcile(conn, store: "SharedStore") -> dict:
    """Merge the shared store and the local overrides, newest change wins.

    The unit of comparison is one field of one film, so two machines that
    touched different fields of the same film both keep their work.
    """
    local: dict[tuple, dict] = {}
    for row in conn.execute(
        "SELECT work_key, field, value, updated_at, source FROM work_override"
    ):
        local[(row[0], row[1])] = {"value": row[2], "updated_at": row[3],
                                   "source": row[4], "deleted": False}

    shared: dict[tuple, dict] = {}
    for pid, fields in store.read_all().items():
        for field, record in fields.items():
            shared[(pid, field)] = record

    counts = {"pulled": 0, "pushed": 0, "deleted_locally": 0,
              "unchanged": 0, "conflicts_resolved": 0}

    for key in set(local) | set(shared):
        here, there = local.get(key), shared.get(key)
        pid, field = key

        if there is None:
            store.stamp(pid, field, here["value"], here.get("source") or "manual")
            counts["pushed"] += 1
            continue
        if here is None:
            if there.get("deleted"):
                counts["unchanged"] += 1
                continue
            _write_local(conn, pid, field, there)
            counts["pulled"] += 1
            continue

        mine, theirs = _as_utc(here["updated_at"]), _as_utc(there.get("updated_at"))
        if theirs > mine:
            if there.get("deleted"):
                conn.execute(
                    "DELETE FROM work_override WHERE work_key = ? AND field = ?",
                    (pid, field))
                counts["deleted_locally"] += 1
            else:
                _write_local(conn, pid, field, there)
                counts["pulled"] += 1
            if here["value"] != there.get("value"):
                counts["conflicts_resolved"] += 1
        elif mine > theirs:
            store.stamp(pid, field, here["value"], here.get("source") or "manual")
            counts["pushed"] += 1
        else:
            counts["unchanged"] += 1

    counts["watches"] = _reconcile_watches(conn, store)
    conn.commit()
    return counts


def _reconcile_watches(conn, store: "SharedStore") -> int:
    """Merge logged viewings, newest edit of each row winning.

    Rows are identified by uuid rather than position, so two machines that
    each logged a viewing keep both rather than one overwriting the other.
    """
    local = {
        r[0]: dict(zip(
            ("id", "work_key", "watched_date", "rating", "note", "rewatch",
             "venue", "created_at", "updated_at", "deleted"), r))
        for r in conn.execute(
            "SELECT id, work_key, watched_date, rating, note, rewatch, venue, "
            "created_at, updated_at, deleted FROM watch_log")
    }
    shared: dict[str, dict] = {}
    for rows in store.all_watches().values():
        for row in rows:
            if row.get("id"):
                shared[row["id"]] = row

    changed = 0
    for watch_id in set(local) | set(shared):
        here, there = local.get(watch_id), shared.get(watch_id)
        if there is None:
            continue                       # ours; pushed below
        if here is None or _as_utc(there.get("updated_at")) > _as_utc(here["updated_at"]):
            conn.execute(
                "INSERT INTO watch_log (id, work_key, watched_date, rating, note, "
                "rewatch, venue, created_at, updated_at, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET work_key = excluded.work_key, "
                "watched_date = excluded.watched_date, rating = excluded.rating, "
                "note = excluded.note, rewatch = excluded.rewatch, "
                "venue = excluded.venue, updated_at = excluded.updated_at, "
                "deleted = excluded.deleted",
                (there["id"], there.get("work_key"), there.get("watched_date"),
                 there.get("rating"), there.get("note"), there.get("rewatch", 0),
                 there.get("venue"), there.get("created_at") or now(),
                 there.get("updated_at") or now(), there.get("deleted", 0)))
            changed += 1

    # Push every film whose local rows differ from the shared copy.
    by_work: dict[str, list[dict]] = {}
    for row in local.values():
        by_work.setdefault(row["work_key"], []).append(row)
    for work_key, rows in by_work.items():
        if store.watches(work_key) != rows:
            store.set_watches(work_key, rows)
    return changed


def _write_local(conn, pid: str, field: str, record: dict) -> None:
    conn.execute(
        "INSERT INTO work_override (work_key, field, value, updated_at, source) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(work_key, field) DO UPDATE SET "
        "value = excluded.value, updated_at = excluded.updated_at, "
        "source = excluded.source",
        (pid, field, record.get("value"), record.get("updated_at") or now(),
         record.get("source") or "manual"),
    )
