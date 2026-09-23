"""Read-side queries that shape the stored library into what the UI renders.

The `item` table holds TV.app's values verbatim, so the tidying happens here:
episodes credit their own show under `director`, and 0 stands in for a missing
season, episode or year.
"""

from __future__ import annotations

import sqlite3

from . import db

# An episode's `director` is really its show name, and some films are filed
# under a literal "Unknown".
DIRECTOR = """CASE
    WHEN media_kind = 'TV show' AND director = show THEN NULL
    WHEN lower(trim(COALESCE(director, ''))) IN
         ('', 'unknown', 'n/a', 'various', 'various artists') THEN NULL
    ELSE director
END"""

# Columns sent to the browser, as (SQL expression, output name). They travel
# as parallel arrays rather than objects: at ~7k rows the repeated JSON keys
# would roughly triple the payload.
ROW_FIELDS: list[tuple[str, str]] = [
    ("id", "id"),
    ("persistent_id", "persistent_id"),
    ("name", "name"),
    ("media_kind", "media_kind"),
    ("genre", "genre"),
    (DIRECTOR, "director"),
    ("NULLIF(year, 0)", "year"),
    ("show", "show"),
    ("NULLIF(season_number, 0)", "season_number"),
    ("NULLIF(episode_number, 0)", "episode_number"),
    ("COALESCE(played_count, 0)", "played_count"),
    ("played_date", "played_date"),
    ("date_added", "date_added"),
    ("duration", "duration"),
    ("COALESCE(rating, 0)", "rating"),
    ("long_description", "long_description"),
    ("has_art", "has_art"),
    ("edited", "edited"),
    ("reviewed", "reviewed"),
]

# Overrides shadow these in the list; the rest only show in the detail panel.
OVERRIDABLE_IN_LIST = {
    "name", "genre", "director", "year", "show", "season_number",
    "episode_number", "media_kind", "played_count", "played_date",
    "date_added", "duration", "long_description", "rating",
}

ROW_COLUMNS = [name for _, name in ROW_FIELDS]


def library(conn: sqlite3.Connection, art_ids: set[int] | None = None) -> dict:
    """The whole library as the UI sees it: imported values with any
    overrides applied on top. The stored rows are never modified."""
    art_ids = art_ids or set()
    select = ", ".join(
        f"{expr} AS {name}" for expr, name in ROW_FIELDS
        if name not in ("has_art", "edited", "reviewed")
    )
    rows = conn.execute(
        f"SELECT {select} FROM item "
        "ORDER BY COALESCE(sort_name, name) COLLATE NOCASE"
    ).fetchall()

    edits = db.overrides(conn)
    packed = []
    for row in rows:
        values = dict(row)
        override = edits.get(row["persistent_id"] or "")
        if override:
            for field, value in override.items():
                if field in OVERRIDABLE_IN_LIST:
                    values[field] = value
        reviewed = bool((override or {}).get("review"))
        packed.append(
            [values.get(name) for _, name in ROW_FIELDS
             if name not in ("has_art", "edited", "reviewed")]
            + [1 if row["id"] in art_ids else 0,
               1 if override else 0,
               1 if reviewed else 0]
        )

    # Re-sort in case a name was overridden.
    name_at = ROW_COLUMNS.index("name")
    packed.sort(key=lambda r: (r[name_at] or "").lower())

    return {
        "columns": ROW_COLUMNS,
        "rows": packed,
        "facets": facets(conn),
        "stats": stats(conn),
        "sync": last_sync(conn),
        "changes": change_summary(conn),
        "editable_fields": db.EDITABLE_FIELDS,
        "field_types": db.field_types(),
    }


def facets(conn: sqlite3.Connection) -> dict:
    genres = [
        {"value": r["genre"], "count": r["n"]}
        for r in conn.execute(
            "SELECT genre, COUNT(*) AS n FROM item WHERE genre IS NOT NULL "
            "GROUP BY genre ORDER BY n DESC"
        )
    ]
    shows = [
        {"value": r["show"], "count": r["n"]}
        for r in conn.execute(
            "SELECT show, COUNT(*) AS n FROM item WHERE show IS NOT NULL "
            "GROUP BY show ORDER BY show COLLATE NOCASE"
        )
    ]
    decades = [
        {"value": r["decade"], "count": r["n"]}
        for r in conn.execute(
            "SELECT (NULLIF(year, 0) / 10) * 10 AS decade, COUNT(*) AS n FROM item "
            "WHERE decade IS NOT NULL GROUP BY decade ORDER BY decade DESC"
        )
    ]
    return {"genres": genres, "shows": shows, "decades": decades}


def directors(conn: sqlite3.Connection) -> list[dict]:
    """Credited directors with their films, most prolific first."""
    rows = conn.execute(
        "SELECT d.director, COUNT(*) AS n, "
        "       SUM(CASE WHEN i.played_count > 0 THEN 1 ELSE 0 END) AS played, "
        "       MIN(NULLIF(i.year, 0)) AS first_year, "
        "       MAX(NULLIF(i.year, 0)) AS last_year "
        "FROM item_director d JOIN item i ON i.id = d.item_id "
        "GROUP BY d.director COLLATE NOCASE "
        "ORDER BY n DESC, d.director COLLATE NOCASE"
    ).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict:
    one = lambda sql: conn.execute(sql).fetchone()[0]
    return {
        "total": one("SELECT COUNT(*) FROM item"),
        "movies": one("SELECT COUNT(*) FROM item WHERE media_kind = 'movie'"),
        "episodes": one("SELECT COUNT(*) FROM item WHERE media_kind = 'TV show'"),
        "shows": one("SELECT COUNT(DISTINCT show) FROM item WHERE show IS NOT NULL"),
        "directors": one(
            "SELECT COUNT(DISTINCT director COLLATE NOCASE) FROM item_director"
        ),
        "genres": one("SELECT COUNT(DISTINCT genre) FROM item WHERE genre IS NOT NULL"),
        # The gap worth being honest about: many items are marked played but
        # carry no date, because TV.app keeps only the most recent play.
        "played": one("SELECT COUNT(*) FROM item WHERE played_count > 0"),
        "with_played_date": one(
            "SELECT COUNT(*) FROM item WHERE played_date IS NOT NULL"
        ),
        "never_played": one(
            "SELECT COUNT(*) FROM item "
            "WHERE COALESCE(played_count, 0) = 0 AND played_date IS NULL"
        ),
    }


def item_detail(conn: sqlite3.Connection, item_id: int) -> dict | None:
    """One item as imported, plus its overrides, kept separate.

    The panel shows both so an edit never hides what TV.app actually said.
    """
    row = conn.execute("SELECT * FROM item WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        return None
    imported = {k: row[k] for k in row.keys()}
    override = db.overrides_for(conn, row["persistent_id"] or "")

    # `effective` gets the same tidying the list view applies, so the panel
    # never offers TV.app's show-name-as-director for editing. `imported`
    # stays exactly as stored.
    effective = dict(imported)
    director = (effective.get("director") or "").strip()
    if effective.get("media_kind") == "TV show" and director == (effective.get("show") or "").strip():
        effective["director"] = None
    elif director.lower() in db.UNKNOWN_DIRECTORS:
        effective["director"] = None
    effective.update(override)
    return {
        "id": item_id,
        "persistent_id": row["persistent_id"],
        "imported": imported,
        "overrides": override,
        "effective": effective,
        "credited_directors": [
            r[0] for r in conn.execute(
                "SELECT director FROM item_director WHERE item_id = ? "
                "ORDER BY position", (item_id,),
            )
        ],
        "editable_fields": db.EDITABLE_FIELDS,
    }


def change_summary(conn: sqlite3.Connection) -> dict:
    """Unacknowledged changes from the most recent imports."""
    counts = {
        r["change_type"]: r["n"] for r in conn.execute(
            "SELECT change_type, COUNT(*) AS n FROM library_change "
            "WHERE acknowledged = 0 GROUP BY change_type"
        )
    }
    removed = [
        dict(r) for r in conn.execute(
            "SELECT id, name, media_kind, detected_at, item_id, persistent_id "
            "FROM library_change WHERE acknowledged = 0 AND change_type = 'removed' "
            "ORDER BY media_kind = 'movie' DESC, name COLLATE NOCASE"
        )
    ]
    return {
        "added": counts.get("added", 0),
        "removed": counts.get("removed", 0),
        "modified": counts.get("modified", 0),
        "removed_items": removed,
        "movies_removed": sum(1 for r in removed if r["media_kind"] == "movie"),
    }


def changes(conn: sqlite3.Connection, limit: int = 500,
            include_acknowledged: bool = False) -> list[dict]:
    where = "" if include_acknowledged else "WHERE acknowledged = 0"
    return [
        dict(r) for r in conn.execute(
            f"SELECT * FROM library_change {where} "
            "ORDER BY severity = 'alert' DESC, id DESC LIMIT ?", (limit,)
        )
    ]


def last_sync(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT synced_at, exported_at, elapsed_ms, item_count "
        "FROM sync_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
