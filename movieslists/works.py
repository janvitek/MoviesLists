"""Our own identity for a film, shared by every source that describes it.

TV.app and Letterboxd have no identifier in common, so a "work" is this app's
own notion of a film and each source is linked to it. Three things make that
workable:

* The key is *derived* from title and year rather than allocated, so a film
  bought today computes the same key as the Letterboxd record written years
  ago and the two unify with nothing to confirm.
* Every title any source has used is kept in `work_title`, so a film found
  once under any of its names is found again under all of them.
* Where the evidence is genuinely ambiguous the app asks rather than guessing.
  An answer is recorded, so the same pair is never raised twice.
"""

from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher

from .letterboxd import normalize_title, title_keys
from .sharing import now

# Auto-joined only this far apart; wider gaps become a question.
AUTO_YEAR_SLACK = 2
ASK_YEAR_SLACK = 8
ASK_TITLE_RATIO = 0.92


def make_key(title: str, year: int | None) -> str:
    """The key a title and year derive to. Deterministic across machines."""
    slug = re.sub(r"\s+", "-", normalize_title(title)).strip("-")
    return f"{slug or 'untitled'}-{year if year else 'unknown'}"


# --------------------------------------------------------------- titles

def record_title(conn: sqlite3.Connection, work_id: int, title: str,
                 year: int | None, source: str, primary: bool = False) -> None:
    """Remember a name this film has been called, for later lookups."""
    for variant in sorted(title_keys(title)) or [normalize_title(title)]:
        if not variant:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO work_title (work_id, title, normalized, year, "
            "source, is_primary, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (work_id, title, variant, year, source,
             1 if (primary and variant == normalize_title(title)) else 0, now()),
        )


def titles_for(conn: sqlite3.Connection, work_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT DISTINCT title, year, source, is_primary FROM work_title "
        "WHERE work_id = ? ORDER BY is_primary DESC, title COLLATE NOCASE",
        (work_id,),
    )]


# --------------------------------------------------------------- lookup

def find(conn: sqlite3.Connection, title: str, year: int | None) -> int | None:
    """The work already known by this title and year, if any.

    Deliberately strict: an exact year, or no year recorded on either side.
    Anything looser is a question for the user, not a guess made here.
    """
    variants = sorted(title_keys(title)) or [normalize_title(title)]
    for variant in variants:
        row = conn.execute(
            "SELECT work_id FROM work_title WHERE normalized = ? "
            "AND (year IS ? OR year = ?) LIMIT 1",
            (variant, year, year),
        ).fetchone()
        if row:
            return row[0]
    if year is None:
        for variant in variants:
            row = conn.execute(
                "SELECT work_id FROM work_title WHERE normalized = ? LIMIT 1",
                (variant,),
            ).fetchone()
            if row:
                return row[0]
    return None


def resolve(conn: sqlite3.Connection, title: str, year: int | None,
            source: str = "derived") -> int:
    """Find the work for a title and year, creating it if it is new."""
    existing = find(conn, title, year)
    if existing is not None:
        record_title(conn, existing, title, year, source)
        return existing

    cursor = conn.execute(
        "INSERT INTO work (key, title, year, created_at) VALUES (?, ?, ?, ?)",
        (_free_key(conn, title, year), title, year, now()),
    )
    work_id = cursor.lastrowid
    record_title(conn, work_id, title, year, source, primary=True)
    return work_id


def _free_key(conn: sqlite3.Connection, title: str, year: int | None) -> str:
    """The derived key, or the next free variant if two films truly collide."""
    base = make_key(title, year)
    key, n = base, 2
    while conn.execute("SELECT 1 FROM work WHERE key = ?", (key,)).fetchone():
        key, n = f"{base}-{n}", n + 1
    return key


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
    """Fold one work into another: its sources, names and edits all follow."""
    if keep == drop:
        return
    conn.execute("UPDATE OR REPLACE work_source SET work_id = ? WHERE work_id = ?",
                 (keep, drop))
    conn.execute("UPDATE OR IGNORE work_title SET work_id = ?, is_primary = 0 "
                 "WHERE work_id = ?", (keep, drop))
    conn.execute("DELETE FROM work_title WHERE work_id = ?", (drop,))

    dropped = conn.execute("SELECT key FROM work WHERE id = ?", (drop,)).fetchone()
    kept = conn.execute("SELECT key FROM work WHERE id = ?", (keep,)).fetchone()
    if dropped and kept:
        conn.execute("UPDATE OR IGNORE work_override SET work_key = ? "
                     "WHERE work_key = ?", (kept[0], dropped[0]))
        conn.execute("DELETE FROM work_override WHERE work_key = ?", (dropped[0],))
    conn.execute("DELETE FROM work WHERE id = ?", (drop,))


# -------------------------------------------------------------- building

def rebuild(conn: sqlite3.Connection) -> dict:
    """Give every film a work, unify the certain cases, ask about the rest.

    Works are derived, so this starts from nothing each time. Decisions are
    not derived, so they are replayed afterwards.
    """
    counts = {"tv": 0, "lb": 0, "entries": 0, "lists": 0}
    with conn:
        conn.execute("DELETE FROM work_source")
        conn.execute("DELETE FROM work_title")
        conn.execute("DELETE FROM work")
    before = 0

    for row in conn.execute(
        "SELECT persistent_id, name, NULLIF(year, 0) AS year FROM item "
        "WHERE media_kind = 'movie' AND persistent_id IS NOT NULL"
    ).fetchall():
        link(conn, resolve(conn, row[1], row[2], "tv"), "tv", row[0])
        counts["tv"] += 1

    for row in conn.execute("SELECT uri, name, year FROM lb_film").fetchall():
        link(conn, resolve(conn, row[1], row[2], "lb"), "lb", row[0])
        counts["lb"] += 1

    # Diary entries name a film rather than pointing at one, so they are tied
    # to a work the same way; a film logged only in the diary still gets one.
    for row in conn.execute("SELECT uri, name, year FROM lb_entry").fetchall():
        resolve(conn, row[1], row[2], "lb")
        counts["entries"] += 1

    # A list is another place a film gets named, and sometimes differently.
    for row in conn.execute(
        "SELECT name, year FROM lb_list_film WHERE name IS NOT NULL"
    ).fetchall():
        resolve(conn, row[0], row[1], "list")
        counts["lists"] += 1

    counts["created"] = conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] - before
    counts["merged"] = unify_obvious(conn)
    counts["replayed"] = replay_decisions(conn)
    counts["questions"] = propose_questions(conn)
    stamp_keys(conn)
    conn.commit()
    return counts


def replay_decisions(conn: sqlite3.Connection) -> int:
    """Re-apply the merges the user has already confirmed."""
    applied = 0
    for row in conn.execute(
        "SELECT key_a, key_b FROM link_decision WHERE status = 'merged'"
    ).fetchall():
        a = conn.execute("SELECT id FROM work WHERE key = ?", (row[0],)).fetchone()
        b = conn.execute("SELECT id FROM work WHERE key = ?", (row[1],)).fetchone()
        if a and b and a[0] != b[0]:
            merge(conn, a[0], b[0], "confirmed by hand")
            applied += 1
    return applied


def stamp_keys(conn: sqlite3.Connection) -> None:
    """Write each Letterboxd row's work key alongside it.

    Denormalised deliberately: every read of the unified list joins on it.
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


def _work_rows(conn: sqlite3.Connection) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT w.id, w.key, w.title, w.year, "
        "       SUM(s.source = 'tv') AS tv, SUM(s.source = 'lb') AS lb "
        "FROM work w LEFT JOIN work_source s ON s.work_id = w.id GROUP BY w.id"
    )]


def unify_obvious(conn: sqlite3.Connection) -> int:
    """Join the pairs that are not really in doubt.

    One work known solely to TV.app, another solely to Letterboxd, the same
    title, and years close enough to be the same release recorded twice.

    The Letterboxd side survives, because where the two disagree it is the one
    that tends to be right: TV.app files Batman Returns under 1997 and
    Byzantium under 2009.
    """
    by_title: dict[str, list[dict]] = {}
    for row in _work_rows(conn):
        by_title.setdefault(normalize_title(row["title"]), []).append(row)

    merged = 0
    for group in by_title.values():
        if len(group) != 2:
            continue
        tv = [w for w in group if (w["tv"] or 0) and not (w["lb"] or 0)]
        lb = [w for w in group if (w["lb"] or 0) and not (w["tv"] or 0)]
        if len(tv) != 1 or len(lb) != 1:
            continue
        if tv[0]["year"] is None or lb[0]["year"] is None:
            continue
        if abs(tv[0]["year"] - lb[0]["year"]) > AUTO_YEAR_SLACK:
            continue
        merge(conn, lb[0]["id"], tv[0]["id"], "year-drift")
        merged += 1
    return merged


def propose_questions(conn: sqlite3.Connection) -> int:
    """Raise the pairs that might be one film, for the user to settle.

    Only pairs where one side is TV.app's and the other Letterboxd's: two
    films from the same source with similar names are simply two films.
    """
    answered = {
        (r[0], r[1]) for r in conn.execute("SELECT key_a, key_b FROM link_decision")
    }
    rows = _work_rows(conn)
    tv_side = [w for w in rows if (w["tv"] or 0) and not (w["lb"] or 0)]
    lb_side = [w for w in rows if (w["lb"] or 0) and not (w["tv"] or 0)]

    by_norm: dict[str, list[dict]] = {}
    for w in lb_side:
        by_norm.setdefault(normalize_title(w["title"]), []).append(w)

    asked = 0
    for tv in tv_side:
        norm = normalize_title(tv["title"])
        candidates: list[tuple[float, dict, str]] = []

        # Same name, a year apart by more than we would join on our own.
        for lb in by_norm.get(norm, []):
            if tv["year"] is None or lb["year"] is None:
                candidates.append((0.8, lb, "same title, one of them undated"))
            else:
                gap = abs(tv["year"] - lb["year"])
                if AUTO_YEAR_SLACK < gap <= ASK_YEAR_SLACK:
                    candidates.append((0.75, lb, f"same title, {gap} years apart"))

        # Nearly the same name, the same year.
        if not candidates and tv["year"] is not None:
            for lb in lb_side:
                if lb["year"] != tv["year"]:
                    continue
                ratio = SequenceMatcher(None, norm,
                                        normalize_title(lb["title"])).ratio()
                if ratio >= ASK_TITLE_RATIO:
                    candidates.append((ratio, lb, f"similar title ({ratio:.0%}), same year"))

        for confidence, lb, reason in sorted(candidates, reverse=True,
                                             key=lambda c: c[0])[:1]:
            pair = tuple(sorted((tv["key"], lb["key"])))
            if pair in answered:
                continue
            answered.add(pair)
            conn.execute(
                "INSERT OR IGNORE INTO link_decision (key_a, key_b, title_a, "
                "year_a, title_b, year_b, reason, confidence, status, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
                (pair[0], pair[1], tv["title"], tv["year"], lb["title"],
                 lb["year"], reason, confidence, now()),
            )
            asked += 1
    return asked


# ------------------------------------------------------------- questions

def questions(conn: sqlite3.Connection, limit: int = 200) -> list[dict]:
    """Open questions, each with enough of both films to decide."""
    out = []
    for row in conn.execute(
        "SELECT key_a, key_b, reason, confidence FROM link_decision "
        "WHERE status = 'open' ORDER BY confidence DESC, key_a LIMIT ?", (limit,)
    ).fetchall():
        left = _describe(conn, row["key_a"])
        right = _describe(conn, row["key_b"])
        if not left or not right:
            continue                    # one of them has since been merged away
        out.append({"id": f"{row['key_a']}|{row['key_b']}",
                    "reason": row["reason"], "confidence": row["confidence"],
                    "left": left, "right": right})
    return out


def _describe(conn: sqlite3.Connection, key: str) -> dict | None:
    work = conn.execute(
        "SELECT id, key, title, year FROM work WHERE key = ?", (key,)
    ).fetchone()
    if work is None:
        return None
    info = dict(work)
    info["sources"] = [r[0] for r in conn.execute(
        "SELECT DISTINCT source FROM work_source WHERE work_id = ?", (work["id"],))]
    info["titles"] = [r["title"] for r in titles_for(conn, work["id"])]
    tv = conn.execute(
        "SELECT i.id, i.genre, i.director, NULLIF(i.year,0) AS year, i.duration, "
        "       i.played_count FROM work_source s "
        "JOIN item i ON i.persistent_id = s.source_id "
        "WHERE s.work_id = ? AND s.source = 'tv' LIMIT 1", (work["id"],)
    ).fetchone()
    info["tv"] = dict(tv) if tv else None
    lb = conn.execute(
        "SELECT f.uri, f.rating, f.year, f.watchlisted_date FROM work_source s "
        "JOIN lb_film f ON f.uri = s.source_id "
        "WHERE s.work_id = ? AND s.source = 'lb' LIMIT 1", (work["id"],)
    ).fetchone()
    info["lb"] = dict(lb) if lb else None

    # A Letterboxd export names no director, which is the one fact that
    # settles most of these questions. TMDb supplies it.
    tmdb = conn.execute(
        "SELECT tmdb_id, directors, genres, runtime, year, title, original_title "
        "FROM tmdb_film WHERE work_key = ? AND status = 'ok'", (key,)
    ).fetchone()
    info["tmdb"] = dict(tmdb) if tmdb else None
    return info


def answer(conn: sqlite3.Connection, question_id: str, same: bool) -> dict:
    """Settle one question. Either answer is remembered and replayed."""
    key_a, _, key_b = question_id.partition("|")
    row = conn.execute(
        "SELECT key_a, key_b FROM link_decision WHERE key_a = ? AND key_b = ?",
        (key_a, key_b),
    ).fetchone()
    if row is None:
        raise ValueError("no such question")

    status = "merged" if same else "separate"
    conn.execute(
        "UPDATE link_decision SET status = ?, resolved_at = ? "
        "WHERE key_a = ? AND key_b = ?", (status, now(), key_a, key_b))

    result = {"status": status}
    if same:
        a = conn.execute("SELECT id FROM work WHERE key = ?", (key_a,)).fetchone()
        b = conn.execute("SELECT id FROM work WHERE key = ?", (key_b,)).fetchone()
        if a and b and a[0] != b[0]:
            # The Letterboxd record survives: where the two disagree about a
            # year it is the likelier one. Both sources stay attached either
            # way; this only decides whose title and year the work carries.
            lb_first = conn.execute(
                "SELECT 1 FROM work_source WHERE work_id = ? AND source = 'lb'",
                (a[0],)).fetchone()
            keep, drop = (a[0], b[0]) if lb_first else (b[0], a[0])
            titles = titles_for(conn, drop)
            merge(conn, keep, drop, "confirmed by hand")
            for title in titles:
                record_title(conn, keep, title["title"], title["year"], "manual")
            kept = conn.execute("SELECT key FROM work WHERE id = ?",
                                (keep,)).fetchone()
            result["work_key"] = kept[0] if kept else None
    stamp_keys(conn)
    conn.commit()
    return result


def migrate_overrides(conn: sqlite3.Connection) -> int:
    """Move edits keyed by TV.app persistent ID onto their work."""
    rows = conn.execute(
        "SELECT o.persistent_id, o.field, o.value, o.updated_at, o.source, w.key "
        "FROM item_override o "
        "JOIN work_source s ON s.source = 'tv' AND s.source_id = o.persistent_id "
        "JOIN work w ON w.id = s.work_id"
    ).fetchall()
    for _pid, field, value, updated, source, key in rows:
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
        "titles": one("SELECT COUNT(*) FROM work_title"),
        "open_questions": one("SELECT COUNT(*) FROM link_decision "
                              "WHERE status = 'open'"),
        "answered": one("SELECT COUNT(*) FROM link_decision WHERE status <> 'open'"),
        "edited": one("SELECT COUNT(DISTINCT work_key) FROM work_override"),
    }
