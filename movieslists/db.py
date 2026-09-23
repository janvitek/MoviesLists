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
SCHEMA_VERSION = 9

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

-- ---------------------------------------------------------------- works
--
-- A "work" is one film, independent of where we learned about it. TV.app and
-- Letterboxd each describe films in their own terms and share no identifier,
-- so this is our own.
--
-- The key is derived from the title and year rather than allocated, which is
-- what makes a purchase unify by itself: a film bought today computes the
-- same key as the Letterboxd record written years ago, and the two land on
-- the same work with nothing to confirm.
CREATE TABLE IF NOT EXISTS work (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    key        TEXT NOT NULL UNIQUE,   -- "the-grand-budapest-hotel-2014"
    title      TEXT NOT NULL,
    year       INTEGER,
    created_at TEXT NOT NULL
);

-- Every title a film is known by, from every source that named it: TV.app's
-- spelling, Letterboxd's, the alternate title in parentheses, whatever a list
-- called it. This is what an incoming title is looked up against, so a film
-- found once under any name is found again under all of them.
CREATE TABLE IF NOT EXISTS work_title (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    work_id    INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
    title      TEXT NOT NULL,          -- as that source spelled it
    normalized TEXT NOT NULL,          -- folded for comparison
    year       INTEGER,
    source     TEXT,                   -- tv | lb | list | alt | manual
    is_primary INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (work_id, normalized, year)
);

CREATE INDEX IF NOT EXISTS idx_work_title_lookup ON work_title(normalized, year);
CREATE INDEX IF NOT EXISTS idx_work_title_work   ON work_title(work_id);

-- Links the app is unsure about. Rather than guess or stay silent it asks,
-- and the answer -- either answer -- is kept.
--
-- Keyed by work *key* rather than row id on purpose. Works are derived from
-- the sources and rebuilt whenever either is re-imported, but a decision you
-- made is not derived from anything and must outlive that. Keys are stable
-- because they come from the title and year, so a stored answer still names
-- the same two films after a rebuild, and `rebuild` replays it.
CREATE TABLE IF NOT EXISTS link_decision (
    key_a       TEXT NOT NULL,
    key_b       TEXT NOT NULL,
    title_a     TEXT,
    year_a      INTEGER,
    title_b     TEXT,
    year_b      INTEGER,
    reason      TEXT,
    confidence  REAL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | merged | separate
    created_at  TEXT NOT NULL,
    resolved_at TEXT,
    PRIMARY KEY (key_a, key_b)
);

CREATE INDEX IF NOT EXISTS idx_link_decision_status ON link_decision(status);

CREATE TABLE IF NOT EXISTS work_source (
    source     TEXT NOT NULL,          -- tv | lb
    source_id  TEXT NOT NULL,          -- item.persistent_id | lb_film.uri
    work_id    INTEGER NOT NULL REFERENCES work(id) ON DELETE CASCADE,
    method     TEXT,
    linked_at  TEXT NOT NULL,
    PRIMARY KEY (source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_work_source_work ON work_source(work_id);

-- Edits hang off the work, not off a TV.app track, so a film you only have
-- on Letterboxd can still be rated and reviewed here.
CREATE TABLE IF NOT EXISTS work_override (
    work_key   TEXT NOT NULL,
    field      TEXT NOT NULL,
    value      TEXT,
    updated_at TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'manual',
    PRIMARY KEY (work_key, field)
);

-- ----------------------------------------------------------- letterboxd
--
-- The export, kept whole. Every column of every film-level CSV, verbatim,
-- exactly as `item` keeps TV.app's own values.
-- One row per film, keyed by its Letterboxd film URI.
--
-- Only some of the export's files identify a film: ratings, watched,
-- watchlist and likes carry the film's own URI. diary.csv and reviews.csv
-- carry the URI of the *entry*, not the film, which is why those live in
-- lb_entry and are tied back by title and year like any other source.
CREATE TABLE IF NOT EXISTS lb_film (
    uri              TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    year             INTEGER,
    rating           REAL,        -- 0.5-5, as Letterboxd records it
    rating_date      TEXT,
    watched_date     TEXT,        -- when it was logged as watched
    watchlisted_date TEXT,
    liked_date       TEXT,
    is_deleted       INTEGER NOT NULL DEFAULT 0,
    work_key         TEXT,        -- filled by works.rebuild
    imported_at      TEXT NOT NULL
);

-- One row per diary entry or review: a film watched three times has three.
CREATE TABLE IF NOT EXISTS lb_entry (
    uri          TEXT PRIMARY KEY,   -- the entry's URI, not the film's
    name         TEXT NOT NULL,
    year         INTEGER,
    logged_date  TEXT,
    watched_date TEXT,
    rating       REAL,
    rewatch      INTEGER NOT NULL DEFAULT 0,
    tags         TEXT,
    review       TEXT,               -- converted to Markdown
    review_html  TEXT,               -- and kept as it arrived
    is_orphaned  INTEGER NOT NULL DEFAULT 0,
    is_deleted   INTEGER NOT NULL DEFAULT 0,
    work_key     TEXT,
    imported_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lb_film_work  ON lb_film(work_key);
CREATE INDEX IF NOT EXISTS idx_lb_entry_work ON lb_entry(work_key);
CREATE INDEX IF NOT EXISTS idx_lb_entry_date ON lb_entry(watched_date);

CREATE TABLE IF NOT EXISTS lb_list (
    slug        TEXT PRIMARY KEY,
    name        TEXT,
    url         TEXT,
    description TEXT,
    created     TEXT,
    tags        TEXT
);

CREATE TABLE IF NOT EXISTS lb_list_film (
    list_slug   TEXT NOT NULL,
    position    INTEGER,
    name        TEXT NOT NULL,
    year        INTEGER,
    uri         TEXT,
    description TEXT,
    PRIMARY KEY (list_slug, position)
);

CREATE TABLE IF NOT EXISTS lb_profile (
    username     TEXT PRIMARY KEY,
    date_joined  TEXT,
    given_name   TEXT,
    family_name  TEXT,
    location     TEXT,
    website      TEXT,
    bio          TEXT,
    pronoun      TEXT,
    favorite_films TEXT
);

CREATE INDEX IF NOT EXISTS idx_lb_film_year ON lb_film(year);

-- Posters for films that are not in TV.app, which therefore have no artwork
-- of their own. Looked up by title and year at a poster service; the answer
-- is kept so a title is never searched twice, including the misses.
CREATE TABLE IF NOT EXISTS poster (
    work_key   TEXT PRIMARY KEY,
    service    TEXT NOT NULL,          -- tmdb
    remote_id  TEXT,
    remote_path TEXT,
    title      TEXT,
    year       INTEGER,
    status     TEXT NOT NULL,          -- ok | none | error
    detail     TEXT,
    fetched_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_poster_status ON poster(status);

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
            # Caches are rebuilt; the user's own work is not. `item` comes
            # back from TV.app and the lb_* tables from the export file, but
            # work_override, work, work_alias and work_source stay put.
            "DROP TABLE IF EXISTS item_director;"
            "DROP TABLE IF EXISTS item;"
            "DROP TABLE IF EXISTS lb_diary;"
            "DROP TABLE IF EXISTS lb_film;"
            "DROP TABLE IF EXISTS lb_entry;"
            "DROP TABLE IF EXISTS lb_list;"
            "DROP TABLE IF EXISTS lb_list_film;"
            "DROP TABLE IF EXISTS lb_profile;"
            "DROP TABLE IF EXISTS work_alias;"
            "DROP TABLE IF EXISTS letterboxd_entry;"
            "DROP TABLE IF EXISTS link_question;"
            # work, work_title and work_source are derived from the two
            # sources and are rebuilt; link_decision and work_override are
            # not, and stay.
            "DROP TABLE IF EXISTS work_title;"
            "DROP TABLE IF EXISTS work_source;"
            "DROP TABLE IF EXISTS work;"
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

# Fields that exist only here: neither source has a notion of them, so they
# live in the override store like any other edit.
USER_FIELDS = {"review": "markdown"}

# Fields a film gets from Letterboxd. An override shadows these exactly as it
# shadows TV.app's, so a rating or a watchlist flag can be corrected here
# without the export being touched.
LETTERBOXD_FIELDS = {
    "rating": "integer",        # 0-100, twenty points a star
    "watchlisted": "integer",
    "liked": "integer",
    "tags": "text",
    "lb_watched_date": "text",
}

EDITABLE_FIELDS = sorted(set(
    [col for _, col, _ in COLUMNS if col not in NON_EDITABLE]
    + list(LETTERBOXD_FIELDS) + list(USER_FIELDS)
))
_COLUMN_TYPES = {col: typ for _, col, typ in COLUMNS}


def coerce(field: str, text: str | None):
    """Turn a stored override string back into that field's own type.

    Consults every field the app knows, not just TV.app's columns: a
    Letterboxd-backed flag stored as "0" must come back as the number 0, since
    the string "0" is true and would invert the meaning of every such flag.
    """
    if text is None or text == "":
        return None
    declared = (_COLUMN_TYPES.get(field) or "").upper()
    kind = (LETTERBOXD_FIELDS.get(field) or USER_FIELDS.get(field) or "")
    try:
        if declared.startswith("INTEGER") or kind == "integer":
            return int(float(text))
        if declared.startswith("REAL") or kind == "real":
            return float(text)
    except (TypeError, ValueError):
        return None
    return text


def overrides(conn: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """All overrides, as {work_key: {field: value}}."""
    out: dict[str, dict[str, object]] = {}
    for row in conn.execute("SELECT work_key, field, value FROM work_override"):
        out.setdefault(row[0], {})[row[1]] = coerce(row[1], row[2])
    return out


def overrides_for(conn: sqlite3.Connection, work_key: str) -> dict:
    return {
        row[0]: coerce(row[0], row[1])
        for row in conn.execute(
            "SELECT field, value FROM work_override WHERE work_key = ?",
            (work_key,),
        )
    }


# When sharing is configured, every override write is mirrored into the
# shared store so another machine can pick it up. Set via attach_store().
_STORE = None


def attach_store(store) -> None:
    """Mirror override writes into a shared store (or None to stop)."""
    global _STORE
    _STORE = store


def set_override(conn: sqlite3.Connection, work_key: str, field: str,
                 value, source: str = "manual") -> None:
    """Shadow one field of a work. Every source's own values stay untouched."""
    if field not in EDITABLE_FIELDS:
        raise ValueError(f"{field!r} is not an editable field")
    stamp = _now_iso()
    with conn:
        conn.execute(
            "INSERT INTO work_override (work_key, field, value, updated_at, "
            "source) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(work_key, field) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "source = excluded.source",
            (work_key, field, None if value is None else str(value),
             stamp, source),
        )
    if _STORE is not None:
        _STORE.stamp(work_key, field, value, source)


def _now_iso() -> str:
    """UTC ISO-8601, the same spelling the shared store writes.

    Both sides must agree on the format or the merge cannot order them.
    """
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def override_sources(conn: sqlite3.Connection, work_key: str) -> dict[str, str]:
    return {
        row[0]: row[1] for row in conn.execute(
            "SELECT field, source FROM work_override WHERE work_key = ?",
            (work_key,),
        )
    }


def clear_override(conn: sqlite3.Connection, work_key: str,
                   field: str | None = None) -> int:
    """Drop an override so the sources' own value shows through again."""
    with conn:
        if field is None:
            cur = conn.execute(
                "DELETE FROM work_override WHERE work_key = ?", (work_key,)
            )
        else:
            cur = conn.execute(
                "DELETE FROM work_override WHERE work_key = ? AND field = ?",
                (work_key, field),
            )
    # A removal is recorded in the shared store rather than simply vanishing,
    # or the next merge would restore it from the other machine.
    if _STORE is not None:
        _STORE.forget(work_key, field)
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
    kinds.update(LETTERBOXD_FIELDS)
    kinds.update(USER_FIELDS)
    return kinds
