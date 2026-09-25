"""Export diary entries and reviews for upload to Letterboxd.

Generates a CSV in Letterboxd's import format from viewings logged in
this app, overridden ratings, and reviews written here. The result is
ready to upload at https://letterboxd.com/import/.

Only entries that originated here are exported — anything that came from
a Letterboxd export is already there and would duplicate.
"""

from __future__ import annotations

import csv
import io
import sqlite3
from pathlib import Path

from . import db

# Letterboxd's import columns, in the order their docs list them.
COLUMNS = [
    "Title", "Year", "LetterboxdURI", "tmdbID", "imdbID",
    "WatchedDate", "Rating", "Rewatch", "Tags", "Review",
]


def exportable(conn: sqlite3.Connection) -> list[dict]:
    """Entries worth sending to Letterboxd, newest first.

    Includes:
    - Viewings logged in this app (watch_log), which Letterboxd has never
      seen. Each becomes a diary entry.
    - Reviews and ratings edited here (work_override) that differ from
      what the Letterboxd export had. These update the existing entry.
    """
    rows = []

    # 1. Watch log entries — these are viewings Letterboxd doesn't know.
    for row in conn.execute(
        "SELECT l.work_key, l.watched_date, l.rating, l.note, l.rewatch, "
        "       w.title, w.year, t.tmdb_id, t.imdb_id, "
        "       f.uri AS lb_uri "
        "FROM watch_log l "
        "JOIN work w ON w.key = l.work_key "
        "LEFT JOIN tmdb_film t ON t.work_key = l.work_key AND t.status = 'ok' "
        "LEFT JOIN lb_film f ON f.work_key = l.work_key "
        "WHERE l.deleted = 0 "
        "ORDER BY l.watched_date DESC"
    ):
        rating = row["rating"]
        rows.append({
            "Title": row["title"],
            "Year": row["year"],
            "LetterboxdURI": row["lb_uri"] or "",
            "tmdbID": row["tmdb_id"] or "",
            "imdbID": row["imdb_id"] or "",
            "WatchedDate": row["watched_date"] or "",
            "Rating": _to_stars(rating) if rating else "",
            "Rewatch": "true" if row["rewatch"] else "",
            "Tags": "",
            "Review": row["note"] or "",
        })

    # 2. Reviews written here that aren't attached to a watch log entry.
    for row in conn.execute(
        "SELECT o.work_key, o.value AS review, w.title, w.year, "
        "       t.tmdb_id, t.imdb_id, f.uri AS lb_uri "
        "FROM work_override o "
        "JOIN work w ON w.key = o.work_key "
        "LEFT JOIN tmdb_film t ON t.work_key = o.work_key AND t.status = 'ok' "
        "LEFT JOIN lb_film f ON f.work_key = o.work_key "
        "WHERE o.field = 'review' AND o.source = 'manual' "
        "  AND o.value IS NOT NULL AND o.value <> '' "
        "  AND NOT EXISTS (SELECT 1 FROM watch_log l "
        "                  WHERE l.work_key = o.work_key AND l.deleted = 0 "
        "                  AND l.note = o.value)"
    ):
        rows.append({
            "Title": row["title"],
            "Year": row["year"],
            "LetterboxdURI": row["lb_uri"] or "",
            "tmdbID": row["tmdb_id"] or "",
            "imdbID": row["imdb_id"] or "",
            "WatchedDate": "",
            "Rating": "",
            "Rewatch": "",
            "Tags": "",
            "Review": row["review"],
        })

    # 3. Ratings set here that differ from what Letterboxd has.
    for row in conn.execute(
        "SELECT o.work_key, o.value AS rating, w.title, w.year, "
        "       t.tmdb_id, t.imdb_id, f.uri AS lb_uri, f.rating AS lb_rating "
        "FROM work_override o "
        "JOIN work w ON w.key = o.work_key "
        "LEFT JOIN tmdb_film t ON t.work_key = o.work_key AND t.status = 'ok' "
        "LEFT JOIN lb_film f ON f.work_key = o.work_key "
        "WHERE o.field = 'rating' AND o.source = 'manual' "
        "  AND o.value IS NOT NULL "
        "  AND NOT EXISTS (SELECT 1 FROM watch_log l "
        "                  WHERE l.work_key = o.work_key AND l.deleted = 0)"
    ):
        our_stars = _to_stars(int(row["rating"])) if row["rating"] else ""
        lb_stars = str(row["lb_rating"]) if row["lb_rating"] else ""
        if our_stars and our_stars != lb_stars:
            rows.append({
                "Title": row["title"],
                "Year": row["year"],
                "LetterboxdURI": row["lb_uri"] or "",
                "tmdbID": row["tmdb_id"] or "",
                "imdbID": row["imdb_id"] or "",
                "WatchedDate": "",
                "Rating": our_stars,
                "Rewatch": "",
                "Tags": "",
                "Review": "",
            })

    return rows


def to_csv(rows: list[dict]) -> str:
    """Format rows as a CSV string in Letterboxd's import format."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def export(database: Path, output: Path | None = None) -> dict:
    """Export to a CSV file. Returns a summary."""
    conn = db.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        rows = exportable(conn)
    finally:
        conn.close()

    text = to_csv(rows)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")

    watches = sum(1 for r in rows if r["WatchedDate"])
    reviews = sum(1 for r in rows if r["Review"])
    ratings = sum(1 for r in rows if r["Rating"])
    return {
        "entries": len(rows), "watches": watches,
        "reviews": reviews, "ratings": ratings,
        "csv": text if output is None else None,
        "path": str(output) if output else None,
    }


def _to_stars(rating_100: int) -> str:
    """Convert 0-100 internal rating to Letterboxd's 0.5-5 scale."""
    if not rating_100:
        return ""
    stars = round(rating_100 / 20 * 2) / 2  # round to nearest 0.5
    return str(stars).replace(".0", "")
