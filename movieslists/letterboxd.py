"""Import a Letterboxd data export into the library.

Letterboxd's API is invite-only and explicitly closed to personal projects, so
the supported route is the account data export
(https://letterboxd.com/user/exportdata/), a ZIP of CSVs. Nothing here talks to
the network; you hand it the file you downloaded.

The hard part is matching. A Letterboxd export identifies films by
LetterboxdURI, tmdbID and imdbID, and TV.app exposes none of those, so the only
common ground is title and year. That is ambiguous often enough to matter, so
anything short of a confident match is queued for confirmation rather than
applied.
"""

from __future__ import annotations

import csv
import io
import json
import re
import sqlite3
import unicodedata
import zipfile
from difflib import SequenceMatcher
from pathlib import Path

from . import db

# Files worth reading, in the order that later ones should win when the same
# film appears twice (diary carries the richest per-viewing detail).
EXPORT_FILES = ("watched.csv", "ratings.csv", "reviews.csv", "diary.csv")

# Words that mark a trailing parenthetical as an edition or format note
# rather than part of the title: "(Unrated Director's Cut)", "(Swedish With
# English Subtitles)", "(2011)". Keyword-based rather than an exact list,
# because this library alone carries 68 distinct parentheticals.
_EDITION_WORDS = re.compile(
    r"\b(?:subtitle[sd]?|subtitles|dubbed|dub|cut|edition|version|remaster(?:ed)?|"
    r"unrated|uncut|redux|restored|director'?s|theatrical|extended|special|"
    r"final|collector'?s|anniversary|imax|3d|remux|widescreen|fullscreen)\b",
    re.I,
)
_TRAILING_PAREN = re.compile(r"\s*\(([^()]+)\)\s*$")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE = re.compile(r"\s+")


def _is_edition(text: str) -> bool:
    """True when a parenthetical is an edition note, not an alternate title."""
    stripped = text.strip()
    if re.fullmatch(r"(?:19|20)\d{2}", stripped):      # a bare year
        return True
    return bool(_EDITION_WORDS.search(stripped))


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("&", " and ")
    text = _PUNCT.sub(" ", text).lower()
    return _SPACE.sub(" ", text).strip()


def normalize_title(title: str) -> str:
    """Fold a title down to something two sources can agree on.

    Trailing edition notes are dropped; an alternate title in parentheses is
    kept here and surfaced separately by `title_keys`.
    """
    text = title or ""
    while True:
        match = _TRAILING_PAREN.search(text)
        if not match or not _is_edition(match.group(1)):
            break
        text = text[:match.start()]
    return _fold(text)


def title_keys(title: str) -> set[str]:
    """Every name a film might reasonably be found under.

    "A Prophet (Un prophète)" is findable as both "a prophet" and
    "un prophete", because Letterboxd may list either.
    """
    keys = {normalize_title(title)}
    text = title or ""
    while True:
        match = _TRAILING_PAREN.search(text)
        if not match:
            break
        inner = match.group(1)
        text = text[:match.start()]
        if not _is_edition(inner):
            folded = _fold(inner)
            # Ignore fragments too short to identify anything.
            if len(folded) >= 3 and any(c.isalpha() for c in folded):
                keys.add(folded)
            keys.add(_fold(text))
    keys.discard("")
    return keys


# --------------------------------------------------------------- reading

def _normalize_header(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


# Export uses "Letterboxd URI"/"Watched Date"; the import format documents
# "LetterboxdURI"/"WatchedDate". Both fold to the same key.
_FIELD_MAP = {
    "name": "title", "title": "title", "year": "year",
    "letterboxduri": "letterboxd_uri", "url": "letterboxd_uri",
    "tmdbid": "tmdb_id", "imdbid": "imdb_id",
    "directors": "directors", "rating": "rating", "rating10": "rating10",
    "review": "review", "watcheddate": "watched_date", "date": "date",
    "rewatch": "rewatch", "tags": "tags",
}


def _read_csv(text: str) -> list[dict]:
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {}
        for key, value in raw.items():
            mapped = _FIELD_MAP.get(_normalize_header(key))
            if mapped and value not in (None, ""):
                row[mapped] = value.strip()
        if row.get("title"):
            rows.append(row)
    return rows


def read_export(path: Path) -> list[dict]:
    """Read a Letterboxd export -- a .zip, or an unpacked directory.

    One record per film, merged across the export's files so a film that was
    rated, reviewed and logged arrives as a single entry.
    """
    path = Path(path).expanduser()
    contents: dict[str, str] = {}

    if path.is_dir():
        for name in EXPORT_FILES:
            candidate = path / name
            if candidate.is_file():
                contents[name] = candidate.read_text(encoding="utf-8-sig")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                base = Path(member).name.lower()
                if base in EXPORT_FILES:
                    contents[base] = archive.read(member).decode("utf-8-sig")
    elif path.suffix.lower() == ".csv":
        contents[path.name.lower()] = path.read_text(encoding="utf-8-sig")
    else:
        raise ValueError(f"{path} is not a Letterboxd export (.zip, folder or .csv)")

    if not contents:
        raise ValueError(
            f"no Letterboxd CSVs found in {path}. Expected one of: "
            + ", ".join(EXPORT_FILES)
        )

    merged: dict[tuple, dict] = {}
    for name in EXPORT_FILES:
        for row in _read_csv(contents.get(name, "")):
            year = _int(row.get("year"))
            key = (row.get("letterboxd_uri") or "", normalize_title(row["title"]), year)
            entry = merged.setdefault(key, {
                "title": row["title"], "year": year,
                "letterboxd_uri": row.get("letterboxd_uri"),
                "tmdb_id": row.get("tmdb_id"), "imdb_id": row.get("imdb_id"),
                "directors": row.get("directors"), "rating": None, "review": None,
                "watched_date": None, "rewatch": 0, "tags": None, "watch_count": 0,
            })
            rating = _rating(row)
            if rating is not None:
                entry["rating"] = rating
            if row.get("review"):
                entry["review"] = html_to_markdown(row["review"])
            if row.get("tags"):
                entry["tags"] = row["tags"]
            if str(row.get("rewatch", "")).lower() in ("yes", "true", "1"):
                entry["rewatch"] = 1
            # diary.csv carries one row per viewing; keep the latest date.
            watched = row.get("watched_date") or (
                row.get("date") if name == "diary.csv" else None
            )
            if watched:
                entry["watch_count"] += 1
                if not entry["watched_date"] or watched > entry["watched_date"]:
                    entry["watched_date"] = watched
    return list(merged.values())


def _int(value):
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _rating(row) -> float | None:
    """Letterboxd rates 0.5-5 in half steps; Rating10 is the 1-10 variant."""
    if row.get("rating"):
        try:
            return float(row["rating"])
        except ValueError:
            pass
    if row.get("rating10"):
        try:
            return float(row["rating10"]) / 2
        except ValueError:
            pass
    return None


# Letterboxd stores reviews as HTML; this library keeps them as Markdown.
# Only the tags Letterboxd's editor actually produces are translated --
# anything else is left as literal text, because a review that mentions
# <expletive> in angle brackets means the word, not a tag.
_TAG_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_TAG_LINK = re.compile(r'<\s*a\b[^>]*?href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)<\s*/\s*a\s*>',
                       re.I | re.S)
_TAG_QUOTE = re.compile(r"<\s*blockquote[^>]*>(.*?)<\s*/\s*blockquote\s*>", re.I | re.S)
# Letterboxd's editor emits attributes -- <i style="-webkit-text-size-adjust:
# 100%;"> -- so these must tolerate anything up to the closing bracket. The
# \b after the name keeps <b> from also swallowing <blockquote>.
_TAG_BOLD = re.compile(r"<\s*/?\s*(?:b|strong)\b[^>]*>", re.I)
_TAG_ITALIC = re.compile(r"<\s*/?\s*(?:i|em)\b[^>]*>", re.I)
_TAG_STRIKE = re.compile(r"<\s*/?\s*(?:del|s|strike)\b[^>]*>", re.I)
_TAG_PARA = re.compile(r"<\s*/?\s*p\b[^>]*>", re.I)

# Anything of Letterboxd's own making that survives the passes above -- an
# orphaned </a>, a stray <span> -- is dropped. Tags outside this set are left
# alone, because a review saying <expletive> means the word.
_TAG_LEFTOVER = re.compile(
    r"<\s*/?\s*(?:a|b|i|em|strong|br|p|blockquote|span|u|div|font)\b[^>]*>", re.I
)


def html_to_markdown(text: str | None) -> str | None:
    """Convert a Letterboxd review's HTML into the Markdown this app stores."""
    if not text:
        return text
    import html as html_module

    out = _TAG_BR.sub("\n", text)
    out = _TAG_PARA.sub("\n\n", out)
    out = _TAG_LINK.sub(lambda m: f"[{m.group(2).strip()}]({m.group(1)})", out)
    out = _TAG_QUOTE.sub(
        lambda m: "\n" + "\n".join(
            f"> {line.strip()}" for line in m.group(1).strip().splitlines() if line.strip()
        ) + "\n",
        out,
    )
    # Open and close both become the same marker, so an unclosed <i> -- which
    # does occur -- still yields a balanced pair rather than a stray tag.
    out = _TAG_BOLD.sub("**", out)
    out = _TAG_ITALIC.sub("*", out)
    out = _TAG_STRIKE.sub("~~", out)
    out = _TAG_LEFTOVER.sub("", out)
    out = html_module.unescape(out)
    out = out.replace("\xa0", " ")
    # Markdown wants no space just inside an emphasis marker; Letterboxd's
    # HTML often has one. Shift it outside rather than dropping it.
    for marker in ("\\*\\*", "\\*"):
        out = re.sub(
            rf"(?<!\*){marker}(\s*)([^*\n]+?)(\s*){marker}(?!\*)",
            lambda m, k=marker.replace("\\", ""): f"{m.group(1)}{k}{m.group(2)}{k}{m.group(3)}",
            out,
        )
    # Collapse the runs of whitespace the substitutions leave behind.
    out = re.sub(r"[ \t]{2,}", " ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def stars_to_tv(rating: float | None) -> int | None:
    """0.5-5 stars -> TV.app's 0-100 scale, twenty points a star. Exact."""
    if rating is None:
        return None
    return max(0, min(100, int(round(rating * 20))))


# -------------------------------------------------------------- matching

CONFIDENT = 0.9
YEAR_SLACK_UNIQUE = 2     # a title unique in the library can drift a little
YEAR_SLACK_AMBIGUOUS = 1  # a title shared by several films cannot


def build_index(conn: sqlite3.Connection) -> dict[str, list[dict]]:
    """Library movies grouped by normalised title.

    Only movies: Letterboxd logs films, and matching a film against 5,336 TV
    episodes invites nonsense.
    """
    index: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT id, persistent_id, name, NULLIF(year, 0) AS year, director "
        "FROM item WHERE media_kind = 'movie'"
    ):
        item = dict(row)
        for key in title_keys(item["name"]):
            index.setdefault(key, []).append(item)
    return index


def match_entry(entry: dict, index: dict[str, list[dict]]) -> dict:
    """Decide what a Letterboxd entry corresponds to, and how sure we are."""
    year = entry.get("year")
    key = normalize_title(entry["title"])
    # The export's own title may carry a parenthetical as well.
    candidates, seen = [], set()
    for candidate_key in [key] + sorted(title_keys(entry["title"]) - {key}):
        for item in index.get(candidate_key, []):
            if item["id"] not in seen:
                seen.add(item["id"])
                candidates.append(item)

    if candidates:
        slack = YEAR_SLACK_UNIQUE if len(candidates) == 1 else YEAR_SLACK_AMBIGUOUS
        if year is None:
            if len(candidates) == 1:
                return _match(candidates[0], 0.9, "title matched; no year given")
            return _queue(candidates, "several films share this title, no year given")

        scored = sorted(
            candidates,
            key=lambda c: abs((c["year"] or 9999) - year),
        )
        best = scored[0]
        drift = abs((best["year"] or 9999) - year)
        runner_up = abs((scored[1]["year"] or 9999) - year) if len(scored) > 1 else None

        if drift == 0 and (runner_up is None or runner_up > 0):
            return _match(best, 1.0, "exact title and year")
        if drift <= slack and (runner_up is None or runner_up > drift):
            return _match(best, 0.93, f"title matched, year off by {drift}")
        return _queue(candidates, f"title matched but year differs by {drift}")

    # No exact normalised hit: look for a near-identical title.
    best_ratio, best_item = 0.0, None
    for title, items in index.items():
        ratio = SequenceMatcher(None, key, title).ratio()
        if ratio > best_ratio:
            best_ratio, best_item = ratio, items[0]
    if best_item and best_ratio >= 0.88:
        return _queue([best_item], f"similar title ({best_ratio:.0%})")
    return {"status": "unmatched", "item": None, "confidence": 0.0,
            "reason": "no film in the library resembles this title",
            "candidates": []}


def _match(item, confidence, reason):
    return {"status": "applied" if confidence >= CONFIDENT else "queued",
            "item": item, "confidence": confidence, "reason": reason,
            "candidates": [item["id"]]}


def _queue(candidates, reason):
    return {"status": "queued", "item": None, "confidence": 0.5,
            "reason": reason, "candidates": [c["id"] for c in candidates]}


# --------------------------------------------------------------- applying

# What an entry contributes, in order: (library field, entry key).
APPLIED_FIELDS = ("review", "rating", "played_date")


def _values(entry: dict, wanted: set[str]) -> dict:
    out = {}
    if "review" in wanted and entry.get("review"):
        out["review"] = entry["review"]
    if "rating" in wanted and entry.get("rating") is not None:
        out["rating"] = stars_to_tv(entry["rating"])
    if "played_date" in wanted and entry.get("watched_date"):
        # Letterboxd logs a calendar date; store it the way TV.app would.
        out["played_date"] = f"{entry['watched_date']}T00:00:00.000Z"
    return out


def apply_entry(conn: sqlite3.Connection, entry: dict, item: dict,
                wanted: set[str], dates: str = "fill-missing",
                force: bool = False) -> list[str]:
    """Write one matched entry's values as overrides. Returns fields written.

    An edit you made here is never overwritten by an import unless `force`
    says so; a previous Letterboxd import is refreshed freely.
    """
    values = _values(entry, wanted)
    if not values:
        return []

    sources = db.override_sources(conn, item["persistent_id"])
    existing = db.overrides_for(conn, item["persistent_id"])
    written = []

    for field, value in values.items():
        if not force and sources.get(field) == "manual":
            continue                              # your own edit wins
        if field == "played_date" and dates == "fill-missing":
            already = existing.get("played_date") or item.get("played_date")
            if already:
                continue
        db.set_override(conn, item["persistent_id"], field, value,
                        source="letterboxd")
        written.append(field)
    return written


def import_export(conn: sqlite3.Connection, path: Path, wanted: set[str],
                  dates: str = "fill-missing", force: bool = False,
                  dry_run: bool = False) -> dict:
    """Read an export, match every entry, apply the confident ones."""
    entries = read_export(path)
    index = build_index(conn)
    by_id = {
        row["id"]: dict(row)
        for row in conn.execute(
            "SELECT id, persistent_id, name, NULLIF(year, 0) AS year, played_date "
            "FROM item WHERE media_kind = 'movie'"
        )
    }

    summary = {"entries": len(entries), "applied": 0, "queued": 0,
               "unmatched": 0, "skipped": 0, "fields": 0, "rows": []}

    for entry in entries:
        verdict = match_entry(entry, index)
        item = verdict["item"]
        if item is not None:
            item = by_id.get(item["id"], item)

        written = []
        status = verdict["status"]
        if status == "applied" and not dry_run:
            written = apply_entry(conn, entry, item, wanted, dates, force)
            if not written:
                status = "skipped"

        summary["rows"].append({
            "title": entry["title"], "year": entry.get("year"),
            "status": status, "confidence": verdict["confidence"],
            "reason": verdict["reason"],
            "matched": item["name"] if item else None,
            "matched_year": item.get("year") if item else None,
            "fields": written,
        })
        summary[status if status in summary else "skipped"] += 1
        summary["fields"] += len(written)

        if not dry_run:
            conn.execute(
                "INSERT INTO letterboxd_entry (imported_at, letterboxd_uri, "
                "tmdb_id, imdb_id, title, year, directors, rating, review, "
                "watched_date, rewatch, tags, status, item_id, persistent_id, "
                "confidence, match_reason, candidates) "
                "VALUES (datetime('now'), ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?) "
                "ON CONFLICT(letterboxd_uri, title, year) DO UPDATE SET "
                "imported_at = excluded.imported_at, rating = excluded.rating, "
                "review = excluded.review, watched_date = excluded.watched_date, "
                "status = excluded.status, item_id = excluded.item_id, "
                "persistent_id = excluded.persistent_id, "
                "confidence = excluded.confidence, "
                "match_reason = excluded.match_reason, "
                "candidates = excluded.candidates",
                (entry.get("letterboxd_uri"), entry.get("tmdb_id"),
                 entry.get("imdb_id"), entry["title"], entry.get("year"),
                 entry.get("directors"), entry.get("rating"), entry.get("review"),
                 entry.get("watched_date"), entry.get("rewatch"),
                 entry.get("tags"), status, item["id"] if item else None,
                 item["persistent_id"] if item else None,
                 verdict["confidence"], verdict["reason"],
                 json.dumps(verdict["candidates"])),
            )
    if not dry_run:
        conn.commit()
    return summary


# ----------------------------------------------------------- the queue

def queue(conn: sqlite3.Connection, limit: int = 500) -> list[dict]:
    """Entries awaiting a decision, with their candidate films."""
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM letterboxd_entry WHERE status = 'queued' "
        "ORDER BY confidence DESC, title COLLATE NOCASE LIMIT ?", (limit,)
    )]
    for row in rows:
        ids = json.loads(row.get("candidates") or "[]")
        row["candidate_items"] = [
            dict(r) for r in conn.execute(
                "SELECT id, name, NULLIF(year, 0) AS year, director, genre "
                f"FROM item WHERE id IN ({','.join('?' * len(ids))})", ids
            )
        ] if ids else []
    return rows


def resolve(conn: sqlite3.Connection, entry_id: int, item_id: int | None,
            wanted: set[str], dates: str = "fill-missing") -> dict:
    """Accept a queued entry against a chosen film, or reject it."""
    row = conn.execute(
        "SELECT * FROM letterboxd_entry WHERE id = ?", (entry_id,)
    ).fetchone()
    if row is None:
        raise ValueError("no such Letterboxd entry")

    if item_id is None:
        conn.execute(
            "UPDATE letterboxd_entry SET status = 'rejected', item_id = NULL, "
            "persistent_id = NULL WHERE id = ?", (entry_id,)
        )
        conn.commit()
        return {"status": "rejected", "fields": []}

    item = conn.execute(
        "SELECT id, persistent_id, name, NULLIF(year, 0) AS year, played_date "
        "FROM item WHERE id = ?", (item_id,)
    ).fetchone()
    if item is None:
        raise ValueError("no such library item")

    entry = dict(row)
    written = apply_entry(conn, entry, dict(item), wanted, dates)
    conn.execute(
        "UPDATE letterboxd_entry SET status = 'applied', item_id = ?, "
        "persistent_id = ?, confidence = 1.0, "
        "match_reason = 'confirmed by hand' WHERE id = ?",
        (item["id"], item["persistent_id"], entry_id),
    )
    conn.commit()
    return {"status": "applied", "fields": written, "item": dict(item)}


def status(conn: sqlite3.Connection) -> dict:
    counts = {r[0]: r[1] for r in conn.execute(
        "SELECT status, COUNT(*) FROM letterboxd_entry GROUP BY status"
    )}
    return {
        "applied": counts.get("applied", 0),
        "queued": counts.get("queued", 0),
        "unmatched": counts.get("unmatched", 0),
        "rejected": counts.get("rejected", 0),
        "skipped": counts.get("skipped", 0),
        "from_letterboxd": conn.execute(
            "SELECT COUNT(*) FROM item_override WHERE source = 'letterboxd'"
        ).fetchone()[0],
    }


# ------------------------------------------------------- full-fidelity import

def read_files(path: Path) -> dict[str, str]:
    """Every CSV in an export, keyed by path relative to its root."""
    path = Path(path).expanduser()
    out: dict[str, str] = {}
    if path.is_dir():
        for csv_path in sorted(path.rglob("*.csv")):
            out[csv_path.relative_to(path).as_posix()] = csv_path.read_text(
                encoding="utf-8-sig", errors="replace")
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            # A zip usually wraps everything in one top folder; drop it.
            roots = {n.split("/", 1)[0] for n in names if "/" in n}
            prefix = f"{roots.pop()}/" if len(roots) == 1 else ""
            for member in names:
                if member.lower().endswith(".csv"):
                    key = member[len(prefix):] if member.startswith(prefix) else member
                    out[key] = archive.read(member).decode("utf-8-sig", "replace")
    else:
        raise ValueError(f"{path} is not a Letterboxd export (.zip or folder)")
    if not out:
        raise ValueError(f"no CSVs found in {path}")
    return out


def _rows(text: str) -> list[dict]:
    """Rows with their headers folded to the names used here."""
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = {}
        for key, value in raw.items():
            mapped = _FIELD_MAP.get(_normalize_header(key))
            if mapped and value not in (None, ""):
                row[mapped] = value.strip()
        if row.get("title"):
            rows.append(row)
    return rows


def parse_list(text: str) -> tuple[dict, list[dict]]:
    """A list export: a metadata block, a blank line, then the films."""
    blocks = re.split(r"\r?\n\r?\n", text.strip(), maxsplit=1)
    meta: dict = {}
    if blocks:
        head = list(csv.DictReader(io.StringIO(
            blocks[0].split("\n", 1)[1] if blocks[0].lower().startswith("letterboxd list")
            else blocks[0])))
        if head:
            meta = {k.lower().replace(" ", "_"): v for k, v in head[0].items() if k}
    films = []
    if len(blocks) > 1:
        for row in csv.DictReader(io.StringIO(blocks[1])):
            keys = {k.lower().strip(): (v or "").strip() for k, v in row.items() if k}
            if keys.get("name"):
                films.append({
                    "position": _int(keys.get("position")),
                    "name": keys["name"],
                    "year": _int(keys.get("year")),
                    "uri": keys.get("url") or None,
                    "description": keys.get("description") or None,
                })
    return meta, films


# Which files name a film by its own URI, and which name an entry.
FILM_FILES = {
    "ratings.csv": "rating", "watched.csv": "watched",
    "watchlist.csv": "watchlist", "likes/films.csv": "like",
}
ENTRY_FILES = {"diary.csv", "reviews.csv"}


def import_full(conn: sqlite3.Connection, path: Path) -> dict:
    """Load an entire Letterboxd export into its own tables, verbatim.

    Mirrors what the TV.app importer does: keep what the source said and
    leave interpretation to the read side.
    """
    files = read_files(path)
    stamp = db._now_iso()
    films: dict[str, dict] = {}
    entries: dict[str, dict] = {}

    for name, text in sorted(files.items()):
        base = name.split("/")[-1]
        folder = name.rsplit("/", 1)[0] if "/" in name else ""
        deleted = 1 if folder == "deleted" else 0
        orphaned = 1 if folder == "orphaned" else 0

        kind = FILM_FILES.get(name)
        if kind is None and folder in ("deleted", "orphaned"):
            kind = FILM_FILES.get(base)
        is_entry = base in ENTRY_FILES

        if kind is None and not is_entry:
            continue

        for row in _rows(text):
            uri = row.get("letterboxd_uri")
            if not uri:
                continue
            year = _int(row.get("year"))
            rating = _rating(row)

            if is_entry:
                entry = entries.setdefault(uri, {
                    "uri": uri, "name": row["title"], "year": year,
                    "logged_date": None, "watched_date": None, "rating": None,
                    "rewatch": 0, "tags": None, "review": None,
                    "review_html": None, "is_orphaned": orphaned,
                    "is_deleted": deleted, "work_key": None,
                    "imported_at": stamp,
                })
                entry["logged_date"] = row.get("date") or entry["logged_date"]
                entry["watched_date"] = (row.get("watched_date")
                                         or entry["watched_date"]
                                         or row.get("date"))
                if rating is not None:
                    entry["rating"] = rating
                if str(row.get("rewatch", "")).lower() in ("yes", "true", "1"):
                    entry["rewatch"] = 1
                if row.get("tags"):
                    entry["tags"] = row["tags"]
                if row.get("review"):
                    entry["review_html"] = row["review"]
                    entry["review"] = html_to_markdown(row["review"])
                continue

            film = films.setdefault(uri, {
                "uri": uri, "name": row["title"], "year": year, "rating": None,
                "rating_date": None, "watched_date": None,
                "watchlisted_date": None, "liked_date": None,
                "is_deleted": deleted, "work_key": None, "imported_at": stamp,
            })
            if year and not film["year"]:
                film["year"] = year
            if kind == "rating":
                film["rating"] = rating
                film["rating_date"] = row.get("date")
            elif kind == "watched":
                film["watched_date"] = row.get("date")
            elif kind == "watchlist":
                film["watchlisted_date"] = row.get("date")
            elif kind == "like":
                film["liked_date"] = row.get("date")

    with conn:
        conn.execute("DELETE FROM lb_film")
        conn.execute("DELETE FROM lb_entry")
        for table, records in (("lb_film", films), ("lb_entry", entries)):
            if not records:
                continue
            columns = list(next(iter(records.values())).keys())
            conn.executemany(
                f"INSERT INTO {table} ({', '.join(columns)}) "
                f"VALUES ({', '.join(':' + c for c in columns)})",
                list(records.values()),
            )

        conn.execute("DELETE FROM lb_list")
        conn.execute("DELETE FROM lb_list_film")
        lists = 0
        for name, text in sorted(files.items()):
            if not name.startswith("lists/"):
                continue
            slug = name[len("lists/"):-len(".csv")]
            meta, items = parse_list(text)
            conn.execute(
                "INSERT OR REPLACE INTO lb_list (slug, name, url, description, "
                "created, tags) VALUES (?, ?, ?, ?, ?, ?)",
                (slug, meta.get("name") or slug, meta.get("url"),
                 meta.get("description"), meta.get("date"), meta.get("tags")),
            )
            conn.executemany(
                "INSERT OR REPLACE INTO lb_list_film (list_slug, position, name, "
                "year, uri, description) VALUES (?, ?, ?, ?, ?, ?)",
                [(slug, e["position"], e["name"], e["year"], e["uri"],
                  e["description"]) for e in items],
            )
            lists += 1

        if "profile.csv" in files:
            for row in csv.DictReader(io.StringIO(files["profile.csv"])):
                keys = {k.lower().replace(" ", "_"): v for k, v in row.items() if k}
                if keys.get("username"):
                    conn.execute(
                        "INSERT OR REPLACE INTO lb_profile (username, date_joined, "
                        "given_name, family_name, location, website, bio, pronoun, "
                        "favorite_films) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (keys.get("username"), keys.get("date_joined"),
                         keys.get("given_name"), keys.get("family_name"),
                         keys.get("location"), keys.get("website"),
                         keys.get("bio"), keys.get("pronoun"),
                         keys.get("favorite_films")),
                    )
                break

    return {"films": len(films), "entries": len(entries), "lists": lists,
            "files": len(files)}
