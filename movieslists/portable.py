"""Share the library's bulk data across machines via a synced folder.

Overrides and viewings travel as individual JSON files (one per film),
which merge per-field and tolerate sync conflicts gracefully.  The bulk
data -- Letterboxd imports, TMDb lookups, link decisions -- changes rarely
and is large enough that per-record files would be unwieldy, so it travels
as a single compressed JSON snapshot.

The snapshot is written atomically (temp file + rename) after any operation
that changes its contents, and imported on startup when the shared copy is
newer than what the local database already has.
"""

from __future__ import annotations

import gzip
import json
import sqlite3
from pathlib import Path

from . import db
from .sharing import now

BUNDLE_NAME = "library-data.json.gz"
META_KEY = "portable_bundle"

# Tables whose content is the same on every machine.  TV.app's own tables
# (`item`, `item_director`, `sync_run`, `library_change`) stay local.
# `work_override` and `watch_log` are already handled by the shared store.
# `work`, `work_title`, `work_source` are derived and rebuilt.
SHARED_TABLES = [
    "lb_film", "lb_entry", "lb_list", "lb_list_film", "lb_profile",
    "tmdb_film", "tmdb_show", "link_decision",
]


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _dump_table(conn: sqlite3.Connection, table: str) -> dict:
    cols = _table_columns(conn, table)
    if not cols:
        return {"columns": [], "rows": []}
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return {"columns": cols, "rows": [list(r) for r in rows]}


def export_bundle(conn: sqlite3.Connection, shared_root: Path) -> Path:
    """Write the shareable tables to a compressed JSON file.

    Returns the path written.  The write is atomic: a temporary file is
    filled first, then renamed into place, so a reader never sees a
    partial file and a sync service never picks up a half-written one.
    """
    bundle = {"exported_at": now(), "tables": {}}
    for table in SHARED_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists:
            bundle["tables"][table] = _dump_table(conn, table)

    shared_root.mkdir(parents=True, exist_ok=True)
    path = shared_root / BUNDLE_NAME
    temp = path.with_suffix(".tmp")
    temp.write_bytes(gzip.compress(
        json.dumps(bundle, ensure_ascii=False, default=str).encode("utf-8"),
        compresslevel=6,
    ))
    temp.replace(path)

    # Remember when we last wrote, so import can skip when nothing changed.
    with conn:
        conn.execute(
            "INSERT INTO sync_run (synced_at, exported_at, elapsed_ms, item_count) "
            "VALUES (datetime('now'), ?, 0, 0)",
            (f"bundle:{bundle['exported_at']}",),
        )
    return path


def _read_bundle(path: Path) -> dict:
    return json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))


def bundle_stamp(path: Path) -> str | None:
    """When the bundle was exported, without reading the whole file."""
    try:
        data = _read_bundle(path)
        return data.get("exported_at")
    except (OSError, ValueError):
        return None


def _local_stamp(conn: sqlite3.Connection) -> str | None:
    """The exported_at of the last bundle we imported or wrote."""
    row = conn.execute(
        "SELECT exported_at FROM sync_run "
        "WHERE exported_at LIKE 'bundle:%' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row and row[0]:
        return row[0].removeprefix("bundle:")
    return None


def import_bundle(conn: sqlite3.Connection, shared_root: Path,
                  force: bool = False) -> dict | None:
    """Load the shared bundle into the local database if it is newer.

    Returns a summary of what was loaded, or None if the local copy was
    already up to date.
    """
    path = shared_root / BUNDLE_NAME
    if not path.is_file():
        return None

    data = _read_bundle(path)
    remote_stamp = data.get("exported_at")
    if not force:
        local = _local_stamp(conn)
        if local and remote_stamp and local >= remote_stamp:
            return None

    counts = {}
    with conn:
        for table in SHARED_TABLES:
            table_data = data.get("tables", {}).get(table)
            if not table_data:
                continue
            columns = table_data["columns"]
            rows = table_data["rows"]

            # Ensure the table exists (it should, from db.connect's schema).
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue

            conn.execute(f"DELETE FROM {table}")
            if rows:
                placeholders = ", ".join("?" * len(columns))
                conn.executemany(
                    f"INSERT OR REPLACE INTO {table} "
                    f"({', '.join(columns)}) VALUES ({placeholders})",
                    rows,
                )
            counts[table] = len(rows)

        conn.execute(
            "INSERT INTO sync_run (synced_at, exported_at, elapsed_ms, item_count) "
            "VALUES (datetime('now'), ?, 0, 0)",
            (f"bundle:{remote_stamp}",),
        )

    return {"imported_at": remote_stamp, "tables": counts}
