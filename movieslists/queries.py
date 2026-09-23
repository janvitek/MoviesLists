"""Read-side shaping: one list of films, drawn from every source that has them.

`item` holds what TV.app said, `lb_film`/`lb_entry` hold what Letterboxd said,
and neither is edited. This module joins them through the work graph, applies
any overrides on top, and hands the UI a single row per film.

Tidying that is a matter of presentation rather than fact happens here too:
episodes credit their own show under `director`, and TV.app writes 0 where it
means "absent".
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

# Sent to the browser as parallel arrays: at several thousand rows the
# repeated JSON keys would roughly triple the payload.
ROW_COLUMNS = [
    "key", "name", "year", "genre", "director", "media_kind",
    "show", "season_number", "episode_number",
    "played_count", "played_date", "date_added", "duration", "rating",
    "sources", "tv_id", "lb_uri", "lb_rating", "lb_entries",
    "watchlisted", "liked", "has_art", "has_poster", "edited", "reviewed",
]

# Overrides may shadow any of these in the list.
OVERRIDABLE_IN_LIST = {
    "name", "genre", "director", "year", "media_kind", "show",
    "season_number", "episode_number", "played_count", "played_date",
    "date_added", "duration", "rating", "watchlisted", "liked",
}


def _tv_side(conn):
    """One TV.app item per work. A work with two copies -- say a film and its
    final cut -- is represented by the one added first."""
    rows = {}
    for row in conn.execute(
        f"SELECT w.key AS key, i.id, i.persistent_id, i.name, NULLIF(i.year,0) AS year, "
        f"       i.genre, {DIRECTOR} AS director, i.media_kind, i.show, "
        f"       NULLIF(i.season_number,0) AS season_number, "
        f"       NULLIF(i.episode_number,0) AS episode_number, "
        f"       COALESCE(i.played_count,0) AS played_count, i.played_date, "
        f"       i.date_added, i.duration, i.long_description "
        f"FROM work w "
        f"JOIN work_source s ON s.work_id = w.id AND s.source = 'tv' "
        f"JOIN item i ON i.persistent_id = s.source_id "
        f"ORDER BY i.date_added"
    ):
        rows.setdefault(row["key"], dict(row))
    return rows


def _lb_side(conn):
    films = {}
    for row in conn.execute(
        "SELECT work_key AS key, uri, name, year, rating, rating_date, "
        "       watched_date, watchlisted_date, liked_date "
        "FROM lb_film WHERE work_key IS NOT NULL ORDER BY rating_date"
    ):
        films.setdefault(row["key"], dict(row))

    entries = {}
    for row in conn.execute(
        "SELECT work_key AS key, COUNT(*) AS n, MAX(watched_date) AS last_watched, "
        "       MIN(watched_date) AS first_watched "
        "FROM lb_entry WHERE work_key IS NOT NULL GROUP BY work_key"
    ):
        entries[row["key"]] = dict(row)

    # The most recent review stands for the film in the list.
    reviews = {}
    for row in conn.execute(
        "SELECT work_key AS key, review FROM lb_entry "
        "WHERE work_key IS NOT NULL AND review IS NOT NULL AND review <> '' "
        "ORDER BY COALESCE(watched_date, logged_date)"
    ):
        reviews[row["key"]] = row["review"]
    return films, entries, reviews


def library(conn: sqlite3.Connection, art_ids: set[int] | None = None,
            poster_keys: set[str] | None = None) -> dict:
    art_ids = art_ids or set()
    poster_keys = poster_keys or set()
    tv = _tv_side(conn)
    lb_films, lb_entries, lb_reviews = _lb_side(conn)
    edits = db.overrides(conn)

    rows = []
    for work in conn.execute("SELECT key, title, year FROM work"):
        key = work["key"]
        t = tv.get(key)
        f = lb_films.get(key)
        e = lb_entries.get(key)
        override = edits.get(key) or {}

        sources = ("both" if t and (f or e) else "tv" if t else "lb")

        # TV.app's own play count and Letterboxd's diary describe the same
        # viewing history from two sides; neither is complete, so take
        # whichever saw more rather than adding them up.
        tv_plays = (t or {}).get("played_count") or 0
        lb_plays = (e or {}).get("n") or 0
        played_count = max(tv_plays, lb_plays)

        # Likewise the most recent viewing either source knows about.
        candidates = [d for d in ((t or {}).get("played_date"),
                                  (e or {}).get("last_watched"),
                                  (f or {}).get("watched_date")) if d]
        played_date = max(candidates) if candidates else None

        values = {
            "key": key,
            "name": (t or {}).get("name") or (f or {}).get("name") or work["title"],
            "year": (t or {}).get("year") or (f or {}).get("year") or work["year"],
            "genre": (t or {}).get("genre"),
            "director": (t or {}).get("director"),
            "media_kind": (t or {}).get("media_kind") or "movie",
            "show": (t or {}).get("show"),
            "season_number": (t or {}).get("season_number"),
            "episode_number": (t or {}).get("episode_number"),
            "played_count": played_count,
            "played_date": played_date,
            "date_added": (t or {}).get("date_added") or (f or {}).get("watched_date"),
            "duration": (t or {}).get("duration"),
            # TV.app rates nothing, so a star comes from Letterboxd unless
            # overridden here.
            "rating": db.coerce("rating", None) if False else (
                int(round((f or {}).get("rating") * 20))
                if (f or {}).get("rating") is not None else 0),
            "sources": sources,
            "tv_id": (t or {}).get("id"),
            "lb_uri": (f or {}).get("uri"),
            "lb_rating": (f or {}).get("rating"),
            "lb_entries": lb_plays,
            "watchlisted": 1 if (f or {}).get("watchlisted_date") else 0,
            "liked": 1 if (f or {}).get("liked_date") else 0,
        }
        for field, value in override.items():
            if field in OVERRIDABLE_IN_LIST:
                values[field] = value

        values["has_art"] = 1 if values["tv_id"] in art_ids else 0
        # Films not in TV.app have no artwork of their own; a poster stands in.
        values["has_poster"] = 1 if _safe_key(key) in poster_keys else 0
        values["edited"] = 1 if override else 0
        values["reviewed"] = 1 if (override.get("review") or lb_reviews.get(key)) else 0
        rows.append([values[c] for c in ROW_COLUMNS])

    rows.sort(key=lambda r: (r[ROW_COLUMNS.index("name")] or "").lower())

    return {
        "columns": ROW_COLUMNS,
        "rows": rows,
        "episodes": episodes(conn, art_ids),
        "facets": facets(conn),
        "stats": stats(conn),
        "sync": last_sync(conn),
        "changes": change_summary(conn),
        "editable_fields": db.EDITABLE_FIELDS,
        "field_types": db.field_types(),
    }


def _safe_key(work_key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in work_key)[:180]


def episodes(conn: sqlite3.Connection, art_ids: set[int] | None = None) -> list:
    """TV episodes, which have no Letterboxd counterpart and no work."""
    art_ids = art_ids or set()
    out = []
    for row in conn.execute(
        f"SELECT id, persistent_id, name, NULLIF(year,0) AS year, genre, "
        f"       {DIRECTOR} AS director, media_kind, show, "
        f"       NULLIF(season_number,0) AS season_number, "
        f"       NULLIF(episode_number,0) AS episode_number, "
        f"       COALESCE(played_count,0) AS played_count, played_date, "
        f"       date_added, duration FROM item WHERE media_kind = 'TV show' "
        f"ORDER BY COALESCE(sort_name, name) COLLATE NOCASE"
    ):
        values = dict(row)
        values.update({
            "key": f"tv:{row['persistent_id']}", "rating": 0, "sources": "tv",
            "tv_id": row["id"], "lb_uri": None, "lb_rating": None,
            "lb_entries": 0, "watchlisted": 0, "liked": 0,
            "has_art": 1 if row["id"] in art_ids else 0, "has_poster": 0,
            "edited": 0, "reviewed": 0,
        })
        out.append([values.get(c) for c in ROW_COLUMNS])
    return out


def facets(conn: sqlite3.Connection) -> dict:
    genres = [
        {"value": r["genre"], "count": r["n"]}
        for r in conn.execute(
            "SELECT genre, COUNT(*) AS n FROM item WHERE genre IS NOT NULL "
            "GROUP BY genre ORDER BY n DESC"
        )
    ]
    decades = [
        {"value": r["decade"], "count": r["n"]}
        for r in conn.execute(
            "SELECT (year / 10) * 10 AS decade, COUNT(*) AS n FROM work "
            "WHERE year IS NOT NULL GROUP BY decade ORDER BY decade DESC"
        )
    ]
    shows = [
        {"value": r["show"], "count": r["n"]}
        for r in conn.execute(
            "SELECT show, COUNT(*) AS n FROM item WHERE show IS NOT NULL "
            "GROUP BY show ORDER BY show COLLATE NOCASE"
        )
    ]
    return {"genres": genres, "shows": shows, "decades": decades}


def directors(conn: sqlite3.Connection) -> list[dict]:
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
        "works": one("SELECT COUNT(*) FROM work"),
        "both": one("SELECT COUNT(*) FROM (SELECT work_id FROM work_source "
                    "GROUP BY work_id HAVING COUNT(DISTINCT source) = 2)"),
        "tv_only": one("SELECT COUNT(*) FROM (SELECT work_id FROM work_source "
                       "GROUP BY work_id HAVING COUNT(DISTINCT source) = 1 "
                       "AND MIN(source) = 'tv')"),
        "lb_only": one("SELECT COUNT(*) FROM (SELECT work_id FROM work_source "
                       "GROUP BY work_id HAVING COUNT(DISTINCT source) = 1 "
                       "AND MIN(source) = 'lb')"),
        "total": one("SELECT COUNT(*) FROM item"),
        "movies": one("SELECT COUNT(*) FROM item WHERE media_kind = 'movie'"),
        "episodes": one("SELECT COUNT(*) FROM item WHERE media_kind = 'TV show'"),
        "shows": one("SELECT COUNT(DISTINCT show) FROM item WHERE show IS NOT NULL"),
        "directors": one("SELECT COUNT(DISTINCT director COLLATE NOCASE) "
                         "FROM item_director"),
        "genres": one("SELECT COUNT(DISTINCT genre) FROM item WHERE genre IS NOT NULL"),
        "played": one("SELECT COUNT(*) FROM item WHERE played_count > 0"),
        "with_played_date": one("SELECT COUNT(*) FROM item WHERE played_date IS NOT NULL"),
        "never_played": one("SELECT COUNT(*) FROM item WHERE "
                            "COALESCE(played_count,0) = 0 AND played_date IS NULL"),
        "lb_films": one("SELECT COUNT(*) FROM lb_film"),
        "lb_entries": one("SELECT COUNT(*) FROM lb_entry"),
        "lb_reviews": one("SELECT COUNT(*) FROM lb_entry WHERE review IS NOT NULL "
                          "AND review <> ''"),
        "lb_rated": one("SELECT COUNT(*) FROM lb_film WHERE rating IS NOT NULL"),
        "watchlist": one("SELECT COUNT(*) FROM lb_film WHERE watchlisted_date "
                         "IS NOT NULL"),
        "liked": one("SELECT COUNT(*) FROM lb_film WHERE liked_date IS NOT NULL"),
        "open_questions": one("SELECT COUNT(*) FROM link_decision "
                              "WHERE status = 'open'"),
        "titles": one("SELECT COUNT(*) FROM work_title"),
    }


def work_detail(conn: sqlite3.Connection, key: str) -> dict | None:
    """Everything known about one film, kept source by source."""
    work = conn.execute("SELECT * FROM work WHERE key = ?", (key,)).fetchone()
    if work is None:
        return None

    tv = [
        dict(r) for r in conn.execute(
            "SELECT i.* FROM work_source s JOIN item i "
            "ON i.persistent_id = s.source_id "
            "WHERE s.work_id = ? AND s.source = 'tv'", (work["id"],)
        )
    ]
    films = [
        dict(r) for r in conn.execute(
            "SELECT f.* FROM work_source s JOIN lb_film f ON f.uri = s.source_id "
            "WHERE s.work_id = ? AND s.source = 'lb'", (work["id"],)
        )
    ]
    entries = [
        dict(r) for r in conn.execute(
            "SELECT * FROM lb_entry WHERE work_key = ? "
            "ORDER BY COALESCE(watched_date, logged_date) DESC", (key,)
        )
    ]
    lists = [
        dict(r) for r in conn.execute(
            "SELECT l.slug, l.name, lf.position FROM lb_list_film lf "
            "JOIN lb_list l ON l.slug = lf.list_slug "
            "WHERE lf.uri IN (SELECT uri FROM lb_film WHERE work_key = ?) "
            "   OR (lower(lf.name) = lower(?) )", (key, work["title"])
        )
    ]
    override = db.overrides_for(conn, key)

    primary = tv[0] if tv else {}
    film = films[0] if films else {}
    effective = {
        "name": primary.get("name") or film.get("name") or work["title"],
        "year": primary.get("year") or film.get("year") or work["year"],
        "genre": primary.get("genre"),
        "director": primary.get("director"),
        "media_kind": primary.get("media_kind") or "movie",
        "duration": primary.get("duration"),
        "long_description": primary.get("long_description"),
        "played_count": max(primary.get("played_count") or 0, len(entries)),
        "played_date": max(
            [d for d in (primary.get("played_date"),
                         max((e["watched_date"] for e in entries if e["watched_date"]),
                             default=None)) if d] or [None]
        ),
        "rating": int(round(film["rating"] * 20)) if film.get("rating") else 0,
        "review": next((e["review"] for e in entries if e.get("review")), None),
        "watchlisted": 1 if film.get("watchlisted_date") else 0,
        "liked": 1 if film.get("liked_date") else 0,
        "tags": next((e["tags"] for e in entries if e.get("tags")), None),
        "lb_watched_date": film.get("watched_date"),
    }
    # An episode's director is really its show name; hide it as the list does.
    if effective["media_kind"] == "TV show" and \
            effective["director"] == primary.get("show"):
        effective["director"] = None
    effective.update(override)

    return {
        "key": key,
        "work": dict(work),
        "sources": {"tv": tv, "letterboxd": films},
        "entries": entries,
        "lists": lists,
        "overrides": override,
        "override_sources": db.override_sources(conn, key),
        "effective": effective,
        "editable_fields": db.EDITABLE_FIELDS,
        "art_id": tv[0]["id"] if tv else None,
    }


def change_summary(conn: sqlite3.Connection) -> dict:
    counts = {
        r["change_type"]: r["n"] for r in conn.execute(
            "SELECT change_type, COUNT(*) AS n FROM library_change "
            "WHERE acknowledged = 0 GROUP BY change_type"
        )
    }
    removed = [
        dict(r) for r in conn.execute(
            "SELECT id, name, media_kind, detected_at FROM library_change "
            "WHERE acknowledged = 0 AND change_type = 'removed' "
            "ORDER BY media_kind = 'movie' DESC, name COLLATE NOCASE"
        )
    ]
    return {
        "added": counts.get("added", 0), "removed": counts.get("removed", 0),
        "modified": counts.get("modified", 0), "removed_items": removed,
        "movies_removed": sum(1 for r in removed if r["media_kind"] == "movie"),
    }


def last_sync(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT synced_at, exported_at, elapsed_ms, item_count "
        "FROM sync_run ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None
