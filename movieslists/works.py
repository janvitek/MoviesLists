"""Our own identity for a film, shared by every source that describes it.

TV.app and Letterboxd have no identifier in common: TV.app exposes no TMDb or
IMDb id, and Letterboxd knows nothing of TV.app's persistent IDs. So a "work"
is this app's own notion of a film, and each source is linked to it.

The key is *derived* from the title and year rather than allocated. That is
the whole point: a film bought on TV.app today computes exactly the same key
as the Letterboxd record written three years ago, so the two unify on the
next import with nothing to confirm.

Derivation alone cannot cover everything -- TV.app files "12 (2007)" under
2009 while Letterboxd says 2007 -- so a key that has been *decided* to mean an
existing work is recorded as an alias. That decision, whether made by the
matcher or by hand, then holds for every later import.
"""

from __future__ import annotations

import re
import sqlite3

from .letterboxd import normalize_title, title_keys
from .sharing import now

# Years this far apart may still be the same film when the title is unique.
YEAR_SLACK = 2


def make_key(title: str, year: int | None) -> str:
    """The key a title and year derive to. Deterministic across machines."""
    slug = re.sub(r"\s+", "-", normalize_title(title)).strip("-")
    return f"{slug or 'untitled'}-{year if year else 'unknown'}"


def candidate_keys(title: str, year: int | None) -> list[str]:
    """Every key this film might already be filed under, best first."""
    primary = make_key(title, year)
    keys = [primary]
    for alt in sorted(title_keys(title)):
        key = f"{re.sub(r'[\s]+', '-', alt).strip('-')}-{year if year else 'unknown'}"
        if key not in keys:
            keys.append(key)
    return keys


# ------------------------------------------------------------------ lookup

def find(conn: sqlite3.Connection, title: str, year: int | None) -> int | None:
    for key in candidate_keys(title, year):
        row = conn.execute(
            "SELECT work_id FROM work_alias WHERE key = ?", (key,)
        ).fetchone()
        if row:
            return row[0]
    return None


def resolve(conn: sqlite3.Connection, title: str, year: int | None) -> int:
    """Find the work for a title and year, creating it if it is new."""
    existing = find(conn, title, year)
    if existing is not None:
        return existing

    primary = make_key(title, year)
    cursor = conn.execute(
        "INSERT INTO work (key, title, year, created_at) VALUES (?, ?, ?, ?)",
        (primary, title, year, now()),
    )
    work_id = cursor.lastrowid
    for key in candidate_keys(title, year):
        # An alt-title key already spoken for is left alone; the work keeps
        # the keys nobody else claims.
        conn.execute(
            "INSERT OR IGNORE INTO work_alias (key, work_id, reason, created_at) "
            "VALUES (?, ?, ?, ?)",
            (key, work_id, "derived" if key == primary else "alt-title", now()),
        )
    return work_id


def link(conn: sqlite3.Connection, work_id: int, source: str, source_id: str,
         method: str = "derived") -> None:
    conn.execute(
        "INSERT INTO work_source (source, source_id, work_id, method, linked_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(source, source_id) DO UPDATE SET "
        "work_id = excluded.work_id, method = excluded.method",
        (source, source_id, work_id, method, now()),
    )


def merge(conn: sqlite3.Connection, keep: int, drop: int, reason: str) -> None:
    """Fold one work into another, moving its sources and keys across."""
    if keep == drop:
        return
    conn.execute("UPDATE OR REPLACE work_source SET work_id = ? WHERE work_id = ?",
                 (keep, drop))
    conn.execute("UPDATE OR REPLACE work_alias SET work_id = ?, reason = ? "
                 "WHERE work_id = ?", (keep, reason, drop))
    # Any edits made against the dropped key follow the film.
    dropped_key = conn.execute("SELECT key FROM work WHERE id = ?", (drop,)).fetchone()
    kept_key = conn.execute("SELECT key FROM work WHERE id = ?", (keep,)).fetchone()
    if dropped_key and kept_key:
        conn.execute(
            "UPDATE OR IGNORE work_override SET work_key = ? WHERE work_key = ?",
            (kept_key[0], dropped_key[0]),
        )
        conn.execute("DELETE FROM work_override WHERE work_key = ?", (dropped_key[0],))
    conn.execute("DELETE FROM work WHERE id = ?", (drop,))


# ----------------------------------------------------------------- building

def rebuild(conn: sqlite3.Connection) -> dict:
    """Give every TV.app movie and Letterboxd film a work, then unify them."""
    counts = {"tv": 0, "lb": 0, "created": 0, "merged": 0}
    before = conn.execute("SELECT COUNT(*) FROM work").fetchone()[0]

    for row in conn.execute(
        "SELECT persistent_id, name, NULLIF(year, 0) AS year FROM item "
        "WHERE media_kind = 'movie' AND persistent_id IS NOT NULL"
    ).fetchall():
        work_id = resolve(conn, row[1], row[2])
        link(conn, work_id, "tv", row[0])
        counts["tv"] += 1

    for row in conn.execute("SELECT uri, name, year FROM lb_film").fetchall():
        work_id = resolve(conn, row[1], row[2])
        link(conn, work_id, "lb", row[0])
        counts["lb"] += 1

    # Diary entries and reviews name a film rather than pointing at one, so
    # they are tied to a work the same way -- and a film logged only in the
    # diary still gets a work of its own.
    for row in conn.execute(
        "SELECT uri, name, year FROM lb_entry"
    ).fetchall():
        resolve(conn, row[1], row[2])
        counts["entries"] = counts.get("entries", 0) + 1

    counts["created"] = conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] - before
    counts["merged"] = unify_year_drift(conn)
    stamp_keys(conn)
    conn.commit()
    return counts


def stamp_keys(conn: sqlite3.Connection) -> None:
    """Write each Letterboxd row's work key alongside it.

    Denormalised on purpose: every read of the unified list joins on it, and
    resolving a title through the alias table per row would be far slower.
    """
    for table in ("lb_film", "lb_entry"):
        updates = []
        for row in conn.execute(f"SELECT uri, name, year FROM {table}").fetchall():
            work_id = find(conn, row[1], row[2])
            if work_id is None:
                continue
            key = conn.execute("SELECT key FROM work WHERE id = ?",
                               (work_id,)).fetchone()
            if key:
                updates.append((key[0], row[0]))
        conn.executemany(f"UPDATE {table} SET work_key = ? WHERE uri = ?", updates)


def unify_year_drift(conn: sqlite3.Connection) -> int:
    """Join works that are one film recorded under two different years.

    Only the unambiguous shape is joined: one work known solely to TV.app,
    another known solely to Letterboxd, the same title, and years close
    enough. Anything else -- "Oldboy" 2003 and 2013, "Damsel" 2018 and 2024 --
    is two films and stays two works.
    """
    rows = conn.execute(
        "SELECT w.id, w.key, w.title, w.year, "
        "       SUM(s.source = 'tv') AS tv, SUM(s.source = 'lb') AS lb "
        "FROM work w LEFT JOIN work_source s ON s.work_id = w.id "
        "GROUP BY w.id"
    ).fetchall()

    by_title: dict[str, list] = {}
    for row in rows:
        by_title.setdefault(normalize_title(row[2]), []).append(row)

    merged = 0
    for title, group in by_title.items():
        if len(group) != 2:
            continue                      # three or more: too murky to guess
        first, second = group
        tv_side = [w for w in group if (w[4] or 0) and not (w[5] or 0)]
        lb_side = [w for w in group if (w[5] or 0) and not (w[4] or 0)]
        if len(tv_side) != 1 or len(lb_side) != 1:
            continue                      # not one-of-each
        if first[3] is None or second[3] is None:
            continue
        if abs(first[3] - second[3]) > YEAR_SLACK:
            continue                      # different films that share a name
        merge(conn, tv_side[0][0], lb_side[0][0], "year-drift")
        merged += 1
    return merged


def link_by_hand(conn: sqlite3.Connection, source: str, source_id: str,
                 work_key: str) -> None:
    """Attach a source to a work chosen by the user, and remember the choice."""
    row = conn.execute("SELECT id FROM work WHERE key = ?", (work_key,)).fetchone()
    if row is None:
        raise ValueError(f"no work with key {work_key!r}")
    work_id = row[0]
    current = conn.execute(
        "SELECT work_id FROM work_source WHERE source = ? AND source_id = ?",
        (source, source_id),
    ).fetchone()
    link(conn, work_id, source, source_id, "manual")
    if current and current[0] != work_id:
        # The key that used to describe this source now means the chosen work,
        # so the same decision is not asked for again.
        old = conn.execute("SELECT key FROM work WHERE id = ?", (current[0],)).fetchone()
        if old:
            conn.execute(
                "INSERT INTO work_alias (key, work_id, reason, created_at) "
                "VALUES (?, ?, 'manual', ?) "
                "ON CONFLICT(key) DO UPDATE SET work_id = excluded.work_id, "
                "reason = 'manual'", (old[0], work_id, now()),
            )
        remaining = conn.execute(
            "SELECT COUNT(*) FROM work_source WHERE work_id = ?", (current[0],)
        ).fetchone()[0]
        if remaining == 0:
            merge(conn, work_id, current[0], "manual")
    conn.commit()


def migrate_overrides(conn: sqlite3.Connection) -> int:
    """Move edits keyed by TV.app persistent ID onto their work.

    Overrides used to hang off a TV.app track, which left no home for a film
    that exists only on Letterboxd.
    """
    rows = conn.execute(
        "SELECT o.persistent_id, o.field, o.value, o.updated_at, o.source, w.key "
        "FROM item_override o "
        "JOIN work_source s ON s.source = 'tv' AND s.source_id = o.persistent_id "
        "JOIN work w ON w.id = s.work_id"
    ).fetchall()
    for pid, field, value, updated, source, key in rows:
        conn.execute(
            "INSERT INTO work_override (work_key, field, value, updated_at, source) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(work_key, field) DO UPDATE SET "
            "value = excluded.value, updated_at = excluded.updated_at, "
            "source = excluded.source WHERE excluded.updated_at > work_override.updated_at",
            (key, field, value, updated, source),
        )
    if rows:
        conn.execute("DELETE FROM item_override")
    conn.commit()
    return len(rows)


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
        "aliases": one("SELECT COUNT(*) FROM work_alias"),
        "edited": one("SELECT COUNT(DISTINCT work_key) FROM work_override"),
    }
