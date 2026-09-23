"""Your own record of having watched something.

Three sources describe a viewing and none of them is complete. TV.app knows
it played a file and remembers one date. Letterboxd knows what you chose to
log there. Neither knows about the cinema, or a friend's sofa, or the film you
watched before either of them existed.

So a watch log is a table of its own rather than an override: a viewing is not
a field, and one film can have many. Rows carry a uuid because two machines
may add one before they next sync, and a deletion leaves a tombstone for the
same reason.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date

from . import db, works

FIELDS = ("watched_date", "rating", "note", "rewatch", "venue")


def log(conn: sqlite3.Connection, work_key: str, watched_date: str | None = None,
        rating: int | None = None, note: str | None = None,
        rewatch: bool | None = None, venue: str | None = None) -> dict:
    """Record one viewing. Returns the row."""
    if not conn.execute("SELECT 1 FROM work WHERE key = ?", (work_key,)).fetchone():
        raise ValueError(f"no film with key {work_key!r}")
    when = (watched_date or date.today().isoformat())[:10]
    if rewatch is None:
        # Anything already seen -- by any source -- makes this a rewatch.
        rewatch = seen_before(conn, work_key, when)

    row = {
        "id": uuid.uuid4().hex, "work_key": work_key, "watched_date": when,
        "rating": rating, "note": note, "rewatch": 1 if rewatch else 0,
        "venue": venue, "created_at": db._now_iso(), "updated_at": db._now_iso(),
        "deleted": 0,
    }
    with conn:
        conn.execute(
            "INSERT INTO watch_log (id, work_key, watched_date, rating, note, "
            "rewatch, venue, created_at, updated_at, deleted) "
            "VALUES (:id, :work_key, :watched_date, :rating, :note, :rewatch, "
            ":venue, :created_at, :updated_at, :deleted)", row)
    return row


def seen_before(conn: sqlite3.Connection, work_key: str, before: str) -> bool:
    """Whether any source already records a viewing earlier than this one."""
    earlier = conn.execute(
        "SELECT 1 FROM watch_log WHERE work_key = ? AND deleted = 0 "
        "AND watched_date < ? LIMIT 1", (work_key, before)).fetchone()
    if earlier:
        return True
    lb = conn.execute(
        "SELECT 1 FROM lb_entry WHERE work_key = ? AND watched_date < ? LIMIT 1",
        (work_key, before)).fetchone()
    if lb:
        return True
    tv = conn.execute(
        "SELECT 1 FROM work_source s JOIN item i ON i.persistent_id = s.source_id "
        "JOIN work w ON w.id = s.work_id "
        "WHERE w.key = ? AND s.source = 'tv' AND COALESCE(i.played_count, 0) > 0 "
        "LIMIT 1", (work_key,)).fetchone()
    return bool(tv)


def update(conn: sqlite3.Connection, watch_id: str, **changes) -> dict | None:
    fields = {k: v for k, v in changes.items() if k in FIELDS}
    if not fields:
        return get(conn, watch_id)
    fields["updated_at"] = db._now_iso()
    assignments = ", ".join(f"{k} = :{k}" for k in fields)
    fields["id"] = watch_id
    with conn:
        conn.execute(f"UPDATE watch_log SET {assignments} WHERE id = :id", fields)
    return get(conn, watch_id)


def remove(conn: sqlite3.Connection, watch_id: str) -> bool:
    """Tombstone rather than delete, so the removal reaches other machines."""
    with conn:
        cursor = conn.execute(
            "UPDATE watch_log SET deleted = 1, updated_at = ? WHERE id = ?",
            (db._now_iso(), watch_id))
    return cursor.rowcount > 0


def get(conn: sqlite3.Connection, watch_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM watch_log WHERE id = ?", (watch_id,)).fetchone()
    return dict(row) if row else None


def for_work(conn: sqlite3.Connection, work_key: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM watch_log WHERE work_key = ? AND deleted = 0 "
        "ORDER BY watched_date DESC, created_at DESC", (work_key,))]


def counts(conn: sqlite3.Connection) -> dict[str, dict]:
    """Per film: how many viewings logged here, and the most recent."""
    return {
        r["work_key"]: {"count": r["n"], "last": r["last"]}
        for r in conn.execute(
            "SELECT work_key, COUNT(*) AS n, MAX(watched_date) AS last "
            "FROM watch_log WHERE deleted = 0 GROUP BY work_key")
    }


def recent(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT l.*, w.title, w.year FROM watch_log l "
        "JOIN work w ON w.key = l.work_key "
        "WHERE l.deleted = 0 ORDER BY l.watched_date DESC, l.created_at DESC "
        "LIMIT ?", (limit,))]


def adopt_tmdb(conn: sqlite3.Connection, tmdb_id: str, key: str | None = None,
               database=None) -> dict:
    """Make a work out of a TMDb film, for something neither source has.

    Used when logging a viewing of a film that is not in TV.app and was never
    on Letterboxd: the film has to exist here before it can be watched.
    """
    from . import posters

    token = posters.api_key(key)
    if not token:
        raise RuntimeError("no TMDb key configured; run 'movieslists tmdb --status'")

    record = posters.parse_details(posters.details(str(tmdb_id), token))
    title = record.get("title") or record.get("original_title")
    if not title:
        raise ValueError(f"TMDb film {tmdb_id} has no title")

    work_id = works.resolve(conn, title, record.get("year"), "tmdb")
    work_key = conn.execute("SELECT key FROM work WHERE id = ?",
                            (work_id,)).fetchone()[0]
    for name in (record.get("title"), record.get("original_title")):
        if name:
            works.record_title(conn, work_id, name, record.get("year"), "tmdb")

    posters._record(conn, {"key": work_key, "title": title,
                           "year": record.get("year")}, record, "ok", None)
    conn.commit()

    if database is not None and record.get("poster_path"):
        try:
            posters.download_poster(database, work_key, record["poster_path"])
        except Exception:
            pass                      # a missing image must not fail the log
    return {"work_key": work_key, "title": title, "year": record.get("year"),
            "tmdb_id": record.get("tmdb_id")}
