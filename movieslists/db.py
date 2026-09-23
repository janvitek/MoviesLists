"""SQLite cache of the Apple TV.app library.

Every scriptable property TV.app exposes is stored verbatim. Cleanup that is
a matter of presentation rather than fact -- TV.app filing an episode's show
name under `director`, or writing 0 where it means "absent" -- is applied at
read time in queries.py, so nothing the library knows is lost here.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

# Bumped whenever the column set changes; a mismatch rebuilds the cache.
SCHEMA_VERSION = 4

# (JSON key from extract.js, SQL column, SQL type). Order defines the table.
COLUMNS: list[tuple[str, str, str]] = [
    ("databaseID",        "id",                 "INTEGER PRIMARY KEY"),
    ("persistentID",      "persistent_id",      "TEXT"),
    ("index",             "library_index",      "INTEGER"),
    ("position",          "position",           "INTEGER"),
    ("name",              "name",               "TEXT NOT NULL"),
    ("album",             "album",              "TEXT"),
    ("albumRating",       "album_rating",       "INTEGER"),
    ("albumRatingKind",   "album_rating_kind",  "TEXT"),
    ("bitRate",           "bit_rate",           "INTEGER"),
    ("bookmark",          "bookmark",           "REAL"),
    ("bookmarkable",      "bookmarkable",       "INTEGER"),
    ("category",          "category",           "TEXT"),
    ("comment",           "comment",            "TEXT"),
    ("dateAdded",         "date_added",         "TEXT"),
    ("description",       "description",        "TEXT"),
    ("director",          "director",           "TEXT"),
    ("discCount",         "disc_count",         "INTEGER"),
    ("discNumber",        "disc_number",        "INTEGER"),
    ("downloaderAccount", "downloader_account", "TEXT"),
    ("downloaderName",    "downloader_name",    "TEXT"),
    ("duration",          "duration",           "REAL"),
    ("enabled",           "enabled",            "INTEGER"),
    ("episodeID",         "episode_id",         "TEXT"),
    ("episodeNumber",     "episode_number",     "INTEGER"),
    ("finish",            "finish",             "REAL"),
    ("genre",             "genre",              "TEXT"),
    ("grouping",          "grouping",           "TEXT"),
    ("kind",              "kind",               "TEXT"),
    ("longDescription",   "long_description",   "TEXT"),
    ("mediaKind",         "media_kind",         "TEXT"),
    ("modificationDate",  "modification_date",  "TEXT"),
    ("playedCount",       "played_count",       "INTEGER"),
    ("playedDate",        "played_date",        "TEXT"),
    ("purchaserAccount",  "purchaser_account",  "TEXT"),
    ("purchaserName",     "purchaser_name",     "TEXT"),
    ("rating",            "rating",             "INTEGER"),
    ("ratingKind",        "rating_kind",        "TEXT"),
    ("releaseDate",       "release_date",       "TEXT"),
    ("sampleRate",        "sample_rate",        "INTEGER"),
    ("seasonNumber",      "season_number",      "INTEGER"),
    ("show",              "show",               "TEXT"),
    ("skippedCount",      "skipped_count",      "INTEGER"),
    ("skippedDate",       "skipped_date",       "TEXT"),
    ("size",              "size",               "INTEGER"),
    ("sortAlbum",         "sort_album",         "TEXT"),
    ("sortDirector",      "sort_director",      "TEXT"),
    ("sortName",          "sort_name",          "TEXT"),
    ("sortShow",          "sort_show",          "TEXT"),
    ("start",             "start",              "REAL"),
    ("time",              "time",               "TEXT"),
    ("trackCount",        "track_count",        "INTEGER"),
    ("trackNumber",       "track_number",       "INTEGER"),
    ("unplayed",          "unplayed",           "INTEGER"),
    ("volumeAdjustment",  "volume_adjustment",  "INTEGER"),
    ("year",              "year",               "INTEGER"),
    # Present only on some track subclasses; null when TV.app withheld them.
    ("location",          "location",           "TEXT"),
    ("address",           "address",            "TEXT"),
]

BOOLEAN_KEYS = {"bookmarkable", "enabled", "unplayed"}

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS item (
    {',\n    '.join(f'{col} {typ}' for _, col, typ in COLUMNS)}
);

-- One row per credited director, so a co-directed film is reachable from
-- either name. TV.app packs them into one string: "Joel Coen & Ethan Coen".
CREATE TABLE IF NOT EXISTS item_director (
    item_id  INTEGER NOT NULL REFERENCES item(id) ON DELETE CASCADE,
    director TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (item_id, position)
);

-- Edits live beside the imported values, never on top of them. A row here
-- shadows one field of one item; `item` itself always keeps what TV.app said.
-- Keyed by persistent ID, which survives re-imports, and deliberately without
-- a foreign key so that replacing `item` on every sync cannot delete edits.
CREATE TABLE IF NOT EXISTS item_override (
    persistent_id TEXT NOT NULL,
    field         TEXT NOT NULL,
    value         TEXT,              -- NULL means "overridden to empty"
    updated_at    TEXT NOT NULL,
    source        TEXT NOT NULL DEFAULT 'manual',   -- manual | letterboxd
    PRIMARY KEY (persistent_id, field)
);

-- One row per entry read from a Letterboxd export, with how it was matched.
-- Entries that could not be matched with confidence sit here as 'queued'
-- until confirmed, so a fuzzy guess never silently rewrites a film.
CREATE TABLE IF NOT EXISTS letterboxd_entry (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    imported_at    TEXT NOT NULL,
    letterboxd_uri TEXT,
    tmdb_id        TEXT,
    imdb_id        TEXT,
    title          TEXT NOT NULL,
    year           INTEGER,
    directors      TEXT,
    rating         REAL,             -- 0.5-5 as Letterboxd records it
    review         TEXT,
    watched_date   TEXT,
    rewatch        INTEGER,
    tags           TEXT,
    status         TEXT NOT NULL,    -- applied | queued | rejected | unmatched
    item_id        INTEGER,
    persistent_id  TEXT,
    confidence     REAL,
    match_reason   TEXT,
    candidates     TEXT,             -- JSON: candidate item ids for the queue
    UNIQUE (letterboxd_uri, title, year)
);

CREATE INDEX IF NOT EXISTS idx_lb_status ON letterboxd_entry(status);

-- What each import changed relative to the one before it.
CREATE TABLE IF NOT EXISTS library_change (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    sync_id       INTEGER NOT NULL,
    detected_at   TEXT NOT NULL,
    persistent_id TEXT,
    item_id       INTEGER,
    name          TEXT,
    media_kind    TEXT,
    change_type   TEXT NOT NULL,     -- added | removed | modified
    field         TEXT,
    old_value     TEXT,
    new_value     TEXT,
    severity      TEXT NOT NULL,     -- alert | info
    acknowledged  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_change_sync ON library_change(sync_id);
CREATE INDEX IF NOT EXISTS idx_change_ack  ON library_change(acknowledged, severity);

CREATE TABLE IF NOT EXISTS sync_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    synced_at   TEXT NOT NULL,
    exported_at TEXT,
    elapsed_ms  INTEGER,
    item_count  INTEGER
);

CREATE INDEX IF NOT EXISTS idx_item_kind     ON item(media_kind);
CREATE INDEX IF NOT EXISTS idx_item_genre    ON item(genre);
CREATE INDEX IF NOT EXISTS idx_item_played   ON item(played_date);
CREATE INDEX IF NOT EXISTS idx_item_show     ON item(show);
CREATE INDEX IF NOT EXISTS idx_item_year     ON item(year);
CREATE INDEX IF NOT EXISTS idx_item_position ON item(position);
CREATE INDEX IF NOT EXISTS idx_dir_name      ON item_director(director);
"""

# "Joel Coen & Ethan Coen", "Alex Gibney, Ophelia Harutyunyan & Suzanne
# Hillinger" -- split on comma and ampersand both.
_DIRECTOR_SPLIT = re.compile(r"\s*(?:,|&)\s*")

# Values TV.app writes to mean "no director recorded".
UNKNOWN_DIRECTORS = {"unknown", "n/a", "various", "various artists"}


def _clean(value, key: str):
    """Store what TV.app gave us, with "" normalised to NULL."""
    if value is None:
        return None
    if key in BOOLEAN_KEYS:
        return 1 if value else 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def split_directors(director: str | None) -> list[str]:
    if not director:
        return []
    seen, out = set(), []
    for part in (p.strip() for p in _DIRECTOR_SPLIT.split(director)):
        key = part.lower()
        if not part or key in UNKNOWN_DIRECTORS or key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out


def credited_directors(raw: dict) -> list[str]:
    """Real directors for an item, or none.

    Every TV episode carries `director` set to its own show name, which is not
    a director at all, so episodes credit nobody.
    """
    director = (raw.get("director") or "").strip()
    show = (raw.get("show") or "").strip()
    if not director or (raw.get("mediaKind") == "TV show" and director == show):
        return []
    return split_directors(director)


def normalize(raw: dict) -> dict:
    row = {col: _clean(raw.get(key), key) for key, col, _ in COLUMNS}
    row["name"] = row["name"] or "(untitled)"
    return row


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    version = conn.execute("PRAGMA user_version").fetchone()[0]
    existing = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='item'"
    ).fetchone()
    if existing and version != SCHEMA_VERSION:
        # The column set changed; the cache is disposable, so rebuild it.
        conn.executescript(
            # item_override and letterboxd_entry are NOT dropped: those hold
            # the user's own work, not cached library data.
            "DROP TABLE IF EXISTS item_director;"
            "DROP TABLE IF EXISTS item;"
        )
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Widen user-data tables in place rather than rebuilding them."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(item_override)")}
    if "source" not in have:
        conn.execute(
            "ALTER TABLE item_override ADD COLUMN source TEXT NOT NULL "
            "DEFAULT 'manual'"
        )


# Fields that churn on their own and would bury real changes: playback
# position, and where an item happens to sit in the playlist.
NOISY_FIELDS = {
    "position", "library_index", "bookmark", "start", "finish",
    "modification_date",
}

# Losing something is the alarming direction. Gaining something is not.
REMOVAL_SEVERITY = "alert"


def _key(row: dict):
    """Identity that survives a re-import."""
    return row.get("persistent_id") or f"id:{row.get('id')}"


def diff(previous: list[dict], current: list[dict]) -> list[dict]:
    """Compare two imports field by field.

    Returns one record per added item, per removed item, and per changed
    field of a surviving item.
    """
    before = {_key(r): r for r in previous}
    after = {_key(r): r for r in current}
    changes = []

    for key, row in after.items():
        if key not in before:
            changes.append({
                "persistent_id": row.get("persistent_id"), "item_id": row.get("id"),
                "name": row.get("name"), "media_kind": row.get("media_kind"),
                "change_type": "added", "field": None,
                "old_value": None, "new_value": None, "severity": "info",
            })

    for key, row in before.items():
        if key not in after:
            changes.append({
                "persistent_id": row.get("persistent_id"), "item_id": row.get("id"),
                "name": row.get("name"), "media_kind": row.get("media_kind"),
                "change_type": "removed", "field": None,
                "old_value": None, "new_value": None,
                "severity": REMOVAL_SEVERITY,
            })
            continue
        new = after[key]
        for _, col, _t in COLUMNS:
            if col in NOISY_FIELDS:
                continue
            old_value, new_value = row.get(col), new.get(col)
            if old_value != new_value:
                changes.append({
                    "persistent_id": new.get("persistent_id"), "item_id": new.get("id"),
                    "name": new.get("name"), "media_kind": new.get("media_kind"),
                    "change_type": "modified", "field": col,
                    "old_value": None if old_value is None else str(old_value),
                    "new_value": None if new_value is None else str(new_value),
                    "severity": "info",
                })
    return changes


def load(conn: sqlite3.Connection, payload: dict) -> dict:
    """Replace the cached library with a fresh export.

    Returns a summary including everything that changed since the previous
    import. User edits in item_override are untouched.
    """
    raws = [r for r in payload.get("items", []) if r.get("databaseID") is not None]
    rows = [normalize(r) for r in raws]

    had_previous = conn.execute("SELECT COUNT(*) FROM item").fetchone()[0] > 0
    previous = [dict(r) for r in conn.execute("SELECT * FROM item")] if had_previous else []
    # The very first import is not a change; everything would read as "added".
    changes = diff(previous, rows) if had_previous else []

    names = [col for _, col, _ in COLUMNS]
    placeholders = ", ".join(f":{c}" for c in names)

    with conn:
        conn.execute("DELETE FROM item_director")
        conn.execute("DELETE FROM item")
        if rows:
            conn.executemany(
                f"INSERT INTO item ({', '.join(names)}) VALUES ({placeholders})", rows
            )
            conn.executemany(
                "INSERT INTO item_director (item_id, director, position) "
                "VALUES (?, ?, ?)",
                [
                    (raw["databaseID"], name, position)
                    for raw in raws
                    for position, name in enumerate(credited_directors(raw))
                ],
            )
        cursor = conn.execute(
            "INSERT INTO sync_run (synced_at, exported_at, elapsed_ms, item_count) "
            "VALUES (datetime('now'), ?, ?, ?)",
            (payload.get("exportedAt"), payload.get("elapsedMs"), len(rows)),
        )
        sync_id = cursor.lastrowid
        if changes:
            conn.executemany(
                "INSERT INTO library_change (sync_id, detected_at, persistent_id, "
                "item_id, name, media_kind, change_type, field, old_value, "
                "new_value, severity) VALUES (?, datetime('now'), :persistent_id, "
                ":item_id, :name, :media_kind, :change_type, :field, :old_value, "
                ":new_value, :severity)".replace("(?,", f"({sync_id},"),
                changes,
            )

    counts = {"added": 0, "removed": 0, "modified": 0}
    for change in changes:
        counts[change["change_type"]] += 1
    return {
        "count": len(rows),
        "sync_id": sync_id,
        "first_import": not had_previous,
        "changes": counts,
        "removed_items": [c for c in changes if c["change_type"] == "removed"],
    }


def load_file(conn: sqlite3.Connection, json_path: Path) -> dict:
    with open(json_path, encoding="utf-8") as fh:
        return load(conn, json.load(fh))


# Identity and bookkeeping are not the user's to rewrite.
NON_EDITABLE = {"id", "persistent_id", "library_index", "position"}

# Fields that exist only here: TV.app has no notion of them, so they live in
# the override store like any other edit, and never collide with an import.
USER_FIELDS = {"review": "markdown"}

EDITABLE_FIELDS = (
    [col for _, col, _ in COLUMNS if col not in NON_EDITABLE]
    + sorted(USER_FIELDS)
)
_COLUMN_TYPES = {col: typ for _, col, typ in COLUMNS}


def coerce(field: str, text: str | None):
    """Turn a stored override string back into the column's own type."""
    if text is None or text == "":
        return None
    declared = _COLUMN_TYPES.get(field, "TEXT")
    try:
        if declared.startswith("INTEGER"):
            return int(float(text))
        if declared.startswith("REAL"):
            return float(text)
    except (TypeError, ValueError):
        return None
    return text


def overrides(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """All overrides, as {persistent_id: {field: value}}."""
    out: dict[str, dict[str, object]] = {}
    for row in conn.execute("SELECT persistent_id, field, value FROM item_override"):
        out.setdefault(row[0], {})[row[1]] = coerce(row[1], row[2])
    return out


def overrides_for(conn: sqlite3.Connection, persistent_id: str) -> dict:
    return {
        row[0]: coerce(row[0], row[1])
        for row in conn.execute(
            "SELECT field, value FROM item_override WHERE persistent_id = ?",
            (persistent_id,),
        )
    }


# When sharing is configured, every override write is mirrored into the
# shared store so another machine can pick it up. Set via attach_store().
_STORE = None


def attach_store(store) -> None:
    """Mirror override writes into a shared store (or None to stop)."""
    global _STORE
    _STORE = store


def set_override(conn: sqlite3.Connection, persistent_id: str, field: str,
                 value, source: str = "manual") -> None:
    """Shadow one field. The imported value is left untouched."""
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"{field!r} is not an editable field")
    stamp = _now_iso()
    with conn:
        conn.execute(
            "INSERT INTO item_override (persistent_id, field, value, updated_at, "
            "source) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(persistent_id, field) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "source = excluded.source",
            (persistent_id, field, None if value is None else str(value),
             stamp, source),
        )
    if _STORE is not None:
        _STORE.stamp(persistent_id, field, value, source)


def _now_iso() -> str:
    """UTC ISO-8601, the same spelling the shared store writes.

    Both sides must agree on the format or the merge cannot order them.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def override_sources(conn: sqlite3.Connection, persistent_id: str) -> dict[str, str]:
    return {
        row[0]: row[1] for row in conn.execute(
            "SELECT field, source FROM item_override WHERE persistent_id = ?",
            (persistent_id,),
        )
    }


def clear_override(conn: sqlite3.Connection, persistent_id: str,
                   field: str | None = None) -> int:
    """Drop an override so the imported value shows through again."""
    with conn:
        if field is None:
            cur = conn.execute(
                "DELETE FROM item_override WHERE persistent_id = ?", (persistent_id,)
            )
        else:
            cur = conn.execute(
                "DELETE FROM item_override WHERE persistent_id = ? AND field = ?",
                (persistent_id, field),
            )
    # A removal is recorded in the shared store rather than simply vanishing,
    # or the next merge would restore it from the other machine.
    if _STORE is not None:
        _STORE.forget(persistent_id, field)
    return cur.rowcount


def field_types() -> dict[str, str]:
    """Declared type per editable column, so the UI can pick a control."""
    kinds = {}
    for _, col, typ in COLUMNS:
        if col in NON_EDITABLE:
            continue
        if typ.startswith("INTEGER"):
            kinds[col] = "integer"
        elif typ.startswith("REAL"):
            kinds[col] = "real"
        else:
            kinds[col] = "text"
    kinds.update(USER_FIELDS)
    return kinds
