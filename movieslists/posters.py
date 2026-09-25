"""TMDb: a third source, for the films the other two describe poorly.

A Letterboxd export carries a title, a year and your own opinions -- no
director, genre, runtime, synopsis or image. So a film you have watched but do
not own arrives almost bare, while one from TV.app arrives with all of it.
TMDb fills that in, which makes it a source like the others rather than a
decoration: its answers are stored verbatim in `tmdb_film` and the read side
decides what to prefer.

This is the one place the app talks to the network, it sends only a title and
a year, and it does nothing at all until a key is configured. Every lookup is
recorded including the misses, so a title is searched once and not again.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import artwork, db, sharing

SEARCH_URL = "https://api.themoviedb.org/3/search/movie"
TV_SEARCH_URL = "https://api.themoviedb.org/3/search/tv"
TV_DETAIL_URL = "https://api.themoviedb.org/3/tv"

# A series is taken over a film of the same name only when it is this many
# times better attested. The two populations are far apart -- a real film
# outvotes its namesake series by orders of magnitude and vice versa -- so
# the threshold only has to fall in the gap.
SERIES_MARGIN = 5
SERIES_MIN_VOTES = 20
EXPORT_URL = "http://files.tmdb.org/p/exports/movie_ids_{month}_{day}_{year}.json.gz"
DETAIL_URL = "https://api.themoviedb.org/3/movie"
IMAGE_BASE = "https://image.tmdb.org/t/p"
FULL_SIZE, THUMB_SIZE = "w500", "w185"
KEY_FILE = sharing.CONFIG_DIR / "tmdb.key"
USER_AGENT = "MoviesLists/0.1 (personal library tool)"

# TMDb allows far more than this; the pause is politeness, not a limit.
PAUSE_SECONDS = 0.06

# Commit this often. A full run is thousands of lookups over many minutes, and
# holding them all in one transaction would mean an interruption threw away
# every answer already paid for.
COMMIT_EVERY = 25


def api_key(explicit: str | None = None) -> str | None:
    """The TMDb key, from the argument, the environment, or a file.

    Deliberately never a command-line flag: a key typed as an argument ends up
    in shell history.
    """
    if explicit:
        return explicit.strip()
    env = os.environ.get("TMDB_API_KEY")
    if env:
        return env.strip()
    if KEY_FILE.is_file():
        return KEY_FILE.read_text(encoding="utf-8").strip() or None
    return None


def details(tmdb_id: str, key: str) -> dict:
    """Full record for one film, with credits in the same round trip."""
    params = urllib.parse.urlencode({"api_key": key,
                                     "append_to_response": "credits"})
    try:
        return json.loads(_request(f"{DETAIL_URL}/{tmdb_id}?{params}"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"TMDb returned HTTP {exc.code} for film {tmdb_id}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not reach TMDb: {exc}") from exc


def parse_details(payload: dict) -> dict:
    """Pick out the fields that make a Letterboxd film look like a TV.app one."""
    crew = (payload.get("credits") or {}).get("crew") or []
    directors = [c.get("name") for c in crew
                 if c.get("job") == "Director" and c.get("name")]
    people = (payload.get("credits") or {}).get("cast") or []
    release = payload.get("release_date") or ""
    return {
        "tmdb_id": str(payload.get("id")) if payload.get("id") else None,
        "imdb_id": payload.get("imdb_id") or None,
        "title": payload.get("title") or None,
        "original_title": payload.get("original_title") or None,
        "year": int(release[:4]) if release[:4].isdigit() else None,
        "release_date": release or None,
        "runtime": payload.get("runtime") or None,
        "overview": payload.get("overview") or None,
        "genres": ", ".join(g["name"] for g in payload.get("genres") or []
                            if g.get("name")) or None,
        "directors": ", ".join(directors) or None,
        "cast_list": ", ".join(p["name"] for p in people[:8] if p.get("name")) or None,
        "original_language": payload.get("original_language") or None,
        "poster_path": payload.get("poster_path") or None,
        "vote_average": payload.get("vote_average") or None,
        "vote_count": payload.get("vote_count") or None,
    }


def candidates(title: str, year: int | None, key: str | None = None,
               limit: int = 8) -> list[dict]:
    """Search results to choose from, for logging a film nothing here has."""
    token = api_key(key)
    if not token:
        raise RuntimeError("no TMDb key configured")
    params = {"api_key": token, "query": title, "include_adult": "false"}
    if year:
        params["year"] = str(year)
    try:
        payload = json.loads(_request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "TMDb rejected the key" if exc.code in (401, 403)
            else f"TMDb returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not reach TMDb: {exc}") from exc

    out = []
    for result in payload.get("results", [])[:limit]:
        release = result.get("release_date") or ""
        out.append({
            "tmdb_id": str(result.get("id")),
            "title": result.get("title") or result.get("original_title"),
            "original_title": result.get("original_title"),
            "year": int(release[:4]) if release[:4].isdigit() else None,
            "overview": (result.get("overview") or "")[:240] or None,
            "poster_url": (f"{IMAGE_BASE}/{THUMB_SIZE}{result['poster_path']}"
                           if result.get("poster_path") else None),
            "vote_average": result.get("vote_average"),
        })
    return out


def search_tv(title: str, key: str) -> dict | None:
    """Best TMDb *series* match, for a title that is no film.

    Letterboxd is a film diary, but people log television in it anyway, and it
    arrives here looking like a film with no director. Names an episode
    "Show: Episode", so the part before a colon is tried when the whole is
    not found.
    """
    attempts = [search_title(title)]
    if ":" in title:
        head = title.split(":", 1)[0].strip()
        if len(head) > 2:
            attempts.append(head)

    for query in attempts:
        params = urllib.parse.urlencode({"api_key": key, "query": query})
        try:
            results = json.loads(_request(f"{TV_SEARCH_URL}?{params}")).get("results", [])
        except urllib.error.HTTPError as exc:
            raise RuntimeError(
                "TMDb rejected the key" if exc.code in (401, 403)
                else f"TMDb returned HTTP {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"could not reach TMDb: {exc}") from exc
        if results:
            first = results[0]
            aired = first.get("first_air_date") or ""
            return {
                "series_id": str(first.get("id")),
                "series_name": first.get("name") or first.get("original_name"),
                "series_year": int(aired[:4]) if aired[:4].isdigit() else None,
                "overview": first.get("overview") or None,
                "poster_path": first.get("poster_path") or None,
                "vote_count": first.get("vote_count") or 0,
            }
    return None


def series_genres(series_id: str, key: str) -> str | None:
    """The series' own genres, so a wrong film match stops supplying them."""
    params = urllib.parse.urlencode({"api_key": key})
    try:
        payload = json.loads(_request(f"{TV_DETAIL_URL}/{series_id}?{params}"))
    except Exception:
        return None
    names = [g["name"] for g in payload.get("genres") or [] if g.get("name")]
    return ", ".join(names) or None


def classify_television(database: Path, key: str | None = None,
                        limit: int | None = None, progress=None) -> dict:
    """Find the entries that are television rather than film.

    Only the ones with no film match are asked about: a title that matched a
    film is a film.
    """
    token = api_key(key)
    if not token:
        raise RuntimeError("no TMDb key configured")

    conn = db.connect(database)
    try:
        # Entries with no film match, and entries that matched a film with
        # nobody credited as director. The second is the telling case: every
        # real film has a director, so a match without one is usually a
        # series that happened to share a title with something.
        rows = conn.execute(
            "SELECT work_key, searched_title, searched_year FROM tmdb_film "
            "WHERE (media_type IS NULL OR media_type = 'movie') "
            "  AND (status = 'none' OR directors IS NULL OR directors = '') "
            "ORDER BY searched_title"
        ).fetchall()
        if limit is not None:
            rows = rows[:limit]

        counts = {"checked": 0, "television": 0, "still_unknown": 0}
        for n, row in enumerate(rows, start=1):
            counts["checked"] += 1
            try:
                found = search_tv(row["searched_title"], token)
            except RuntimeError:
                continue
            if found:
                counts["television"] += 1
                _record_series(conn, database, row["work_key"], found, token)
            else:
                counts["still_unknown"] += 1
                conn.execute(
                    "UPDATE tmdb_film SET media_type = 'unknown' WHERE work_key = ?",
                    (row["work_key"],))
            if n % 20 == 0:
                conn.commit()
                if progress:
                    progress(n, len(rows), counts)
            time.sleep(PAUSE_SECONDS)
        conn.commit()
        return counts
    finally:
        conn.close()


def _record_series(conn, database: Path, work_key: str, found: dict,
                   token: str) -> None:
    """File an entry as television, taking its genres from the series.

    The film match it had was the wrong thing entirely, so its genres were
    too: Adolescence was carrying "Documentary" from "The Real Adolescence:
    Our Killer Kids".
    """
    genres = series_genres(found["series_id"], token)
    conn.execute(
        "UPDATE tmdb_film SET media_type = 'tv', series_id = ?, series_name = ?, "
        "series_year = ?, overview = COALESCE(?, overview), "
        "genres = COALESCE(?, genres), poster_path = COALESCE(?, poster_path), "
        "directors = NULL, fetched_at = ? WHERE work_key = ?",
        (found["series_id"], found["series_name"], found["series_year"],
         found["overview"], genres, found["poster_path"], db._now_iso(), work_key))
    if found.get("poster_path"):
        download_poster(database, work_key, found["poster_path"])


def find_series_namesakes(database: Path, key: str | None = None,
                          progress=None) -> dict:
    """Catch the series that matched a film of the same name.

    The earlier sweep relied on a missing director, which misses a series
    whose name also belongs to a real film: "Chernobyl" found the 2019
    Russian film, "Big Little Lies" found something. What separates them is
    weight of evidence -- the HBO series has 8,326 votes against the film's
    six, while a real film like Icarus has 858 against its namesake's none.
    """
    token = api_key(key)
    if not token:
        raise RuntimeError("no TMDb key configured")
    from .letterboxd import normalize_title

    conn = db.connect(database)
    try:
        rows = conn.execute(
            "SELECT t.work_key, t.searched_title, w.title, w.year, "
            "       COALESCE(t.vote_count, 0) AS film_votes "
            "FROM tmdb_film t JOIN work w ON w.key = t.work_key "
            # NULL as well as 'movie': rows written before the column
            # existed matched a film and were never labelled.
            "WHERE t.status = 'ok' "
            "  AND (t.media_type = 'movie' OR t.media_type IS NULL) "
            "  AND w.year IS NOT NULL "
            "  AND NOT EXISTS (SELECT 1 FROM work_source s "
            "                  WHERE s.work_id = w.id AND s.source = 'tv') "
            "ORDER BY w.title COLLATE NOCASE"
        ).fetchall()

        counts = {"checked": 0, "television": 0}
        for n, row in enumerate(rows, start=1):
            counts["checked"] += 1
            try:
                params = urllib.parse.urlencode(
                    {"api_key": token, "query": search_title(row["title"])})
                results = json.loads(
                    _request(f"{TV_SEARCH_URL}?{params}")).get("results", [])
            except Exception:
                continue

            for candidate in results[:3]:
                aired = (candidate.get("first_air_date") or "")[:4]
                if not aired.isdigit():
                    continue
                if normalize_title(candidate.get("name") or "") != \
                        normalize_title(row["title"]):
                    continue
                if abs(int(aired) - row["year"]) > 1:
                    continue
                votes = candidate.get("vote_count") or 0
                if votes < SERIES_MIN_VOTES:
                    break
                if votes < max(SERIES_MIN_VOTES, row["film_votes"] * SERIES_MARGIN):
                    break
                _record_series(conn, database, row["work_key"], {
                    "series_id": str(candidate.get("id")),
                    "series_name": candidate.get("name"),
                    "series_year": int(aired),
                    "overview": candidate.get("overview") or None,
                    "poster_path": candidate.get("poster_path") or None,
                }, token)
                counts["television"] += 1
                break

            if n % 50 == 0:
                conn.commit()
                if progress:
                    progress(n, len(rows), counts)
            time.sleep(PAUSE_SECONDS)
        conn.commit()
        return counts
    finally:
        conn.close()


def download_poster(database: Path, work_key: str, poster_path: str) -> bool:
    """Fetch one poster into the cache, both sizes."""
    full_dir, thumb_dir = poster_dirs(database)
    full_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)
    name = f"{_safe(work_key)}.jpg"
    try:
        (full_dir / name).write_bytes(_request(f"{IMAGE_BASE}/{FULL_SIZE}{poster_path}"))
        (thumb_dir / name).write_bytes(_request(f"{IMAGE_BASE}/{THUMB_SIZE}{poster_path}"))
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def poster_dirs(database: Path) -> tuple[Path, Path]:
    root = artwork.cache_dir(database) / "posters"
    return root / "full", root / "thumb"


def _safe(work_key: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in work_key)[:180]


def have(database: Path) -> set[str]:
    full, _ = poster_dirs(database)
    if not full.is_dir():
        return set()
    return {p.stem for p in full.glob("*.jpg") if p.stat().st_size > 0}


NO_TV = ("NOT EXISTS (SELECT 1 FROM work_source s "
         "            WHERE s.work_id = w.id AND s.source = 'tv')")

NO_DIRECTOR = (
    "EXISTS (SELECT 1 FROM work_source s JOIN item i "
    "        ON i.persistent_id = s.source_id "
    "        WHERE s.work_id = w.id AND s.source = 'tv' "
    "        AND (i.director IS NULL OR i.director = '' "
    "             OR lower(i.director) IN ('unknown', 'n/a')))"
)

SCOPES = {
    # The films that arrive with nothing but a title and a year.
    "missing": NO_TV,
    # Plus the ones TV.app has but filed without a director.
    "gaps": f"({NO_TV}) OR ({NO_DIRECTOR})",
    # Everything, which is what gives the library an IMDb id throughout --
    # the stable identifier neither source provides.
    "all": "1 = 1",
}


def wanted(conn: sqlite3.Connection, refresh: bool = False,
           scope: str = "all") -> list[dict]:
    """Films TMDb could usefully describe."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {sorted(SCOPES)}")
    rows = conn.execute(
        f"SELECT w.key, w.title, w.year FROM work w WHERE {SCOPES[scope]} "
        "ORDER BY w.title COLLATE NOCASE"
    ).fetchall()
    out = [dict(r) for r in rows]

    if refresh:
        # Only the ones that found nothing. Re-running the whole library would
        # be thousands of lookups to re-fetch what is already here.
        misses = {
            r[0] for r in conn.execute(
                "SELECT work_key FROM tmdb_film WHERE status = 'none'")
        }
        return [r for r in out if r["key"] in misses]

    # A previous answer is remembered, so a title is not searched twice.
    known = {
        r[0] for r in conn.execute(
            "SELECT work_key FROM tmdb_film WHERE status IN ('ok', 'none')")
    }
    return [r for r in out if r["key"] not in known]


def _request(url: str, timeout: float = 15.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def search_title(title: str) -> str:
    """The title as TMDb would spell it.

    Strips the trailing parenthetical a library title carries -- a repeated
    year, or an edition note -- while leaving case and punctuation alone,
    since TMDb is searching real titles rather than folded ones. "Mary Queen
    of Scots (2018)" finds nothing; "Mary Queen of Scots" finds it.
    """
    from .letterboxd import _TRAILING_PAREN, _is_edition

    text = title or ""
    while True:
        match = _TRAILING_PAREN.search(text)
        if not match or not _is_edition(match.group(1)):
            break
        text = text[:match.start()]
    return text.strip() or title


def search(title: str, year: int | None, key: str) -> dict | None:
    """Best TMDb match for a title, preferring an exact year."""
    params = {"api_key": key, "query": search_title(title),
              "include_adult": "false"}
    if year:
        params["year"] = str(year)
    try:
        payload = json.loads(_request(f"{SEARCH_URL}?{urllib.parse.urlencode(params)}"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            "TMDb rejected the key" if exc.code in (401, 403)
            else f"TMDb returned HTTP {exc.code}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"could not reach TMDb: {exc}") from exc

    results = [r for r in payload.get("results", []) if r.get("poster_path")]
    if not results and year:
        # Year metadata disagrees often enough to be worth one retry without it.
        return search(title, None, key)
    if not results:
        return None
    if year:
        results.sort(key=lambda r: abs(int((r.get("release_date") or "0")[:4] or 0) - year))
    return results[0]


def fetch(database: Path, limit: int | None = None, refresh: bool = False,
          key: str | None = None, with_posters: bool = True,
          scope: str = "all", progress=None) -> dict:
    """Look up the films TV.app does not have, and keep what TMDb says."""
    token = api_key(key)
    if not token:
        raise RuntimeError(
            "no TMDb key configured. Get a free one at "
            "https://www.themoviedb.org/settings/api, then either\n"
            f"  echo YOUR_KEY > {KEY_FILE}\n"
            "or set TMDB_API_KEY in your environment."
        )

    conn = db.connect(database)
    try:
        targets = wanted(conn, refresh, scope)
        # A film already in TV.app has artwork, and the list shows that in
        # preference to a poster, so fetching one would be bytes nobody sees.
        has_artwork = {
            r[0] for r in conn.execute(
                "SELECT w.key FROM work w JOIN work_source s "
                "ON s.work_id = w.id AND s.source = 'tv'")
        }
        if limit is not None:
            targets = targets[:limit]
        full_dir, thumb_dir = poster_dirs(database)
        full_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)

        counts = {"looked_up": 0, "found": 0, "missing": 0, "errors": 0,
                  "posters": 0, "directors": 0}
        for n, row in enumerate(targets, start=1):
            counts["looked_up"] += 1
            try:
                match = search(row["title"], row["year"], token)
                record = parse_details(details(str(match["id"]), token)) if match else None
            except RuntimeError as exc:
                counts["errors"] += 1
                _record(conn, row, None, "error", str(exc))
                if "rejected the key" in str(exc):
                    raise
                continue

            if not record:
                counts["missing"] += 1
                _record(conn, row, None, "none", "nothing matching at TMDb")
            else:
                counts["found"] += 1
                if record.get("directors"):
                    counts["directors"] += 1
                _record(conn, row, record, "ok", None)
                conn.execute(
                    "UPDATE tmdb_film SET media_type = 'movie' WHERE work_key = ?",
                    (row["key"],))
                if (with_posters and record.get("poster_path")
                        and row["key"] not in has_artwork):
                    name = f"{_safe(row['key'])}.jpg"
                    try:
                        (full_dir / name).write_bytes(
                            _request(f"{IMAGE_BASE}/{FULL_SIZE}{record['poster_path']}"))
                        (thumb_dir / name).write_bytes(
                            _request(f"{IMAGE_BASE}/{THUMB_SIZE}{record['poster_path']}"))
                        counts["posters"] += 1
                    except (urllib.error.URLError, TimeoutError, OSError):
                        counts["errors"] += 1
            if n % COMMIT_EVERY == 0:
                conn.commit()
                if progress:
                    progress(n, len(targets), counts)
            time.sleep(PAUSE_SECONDS)
        conn.commit()
        record_titles(conn)
        counts["remaining"] = len(wanted(conn, scope=scope))
        return counts
    finally:
        conn.close()


_FIELDS = ("tmdb_id", "imdb_id", "title", "original_title", "year",
           "release_date", "runtime", "overview", "genres", "directors",
           "cast_list", "original_language", "poster_path", "vote_average",
           "vote_count")


def _record(conn, row, record, status, detail) -> None:
    values = {f: (record or {}).get(f) for f in _FIELDS}
    values.update({"work_key": row["key"], "searched_title": row["title"],
                   "searched_year": row["year"], "status": status,
                   "detail": detail, "fetched_at": db._now_iso()})
    columns = list(values)
    conn.execute(
        f"INSERT INTO tmdb_film ({', '.join(columns)}) "
        f"VALUES ({', '.join(':' + c for c in columns)}) "
        "ON CONFLICT(work_key) DO UPDATE SET "
        + ", ".join(f"{c} = excluded.{c}" for c in columns if c != "work_key"),
        values,
    )


def record_titles(conn: sqlite3.Connection) -> int:
    """Feed TMDb's titles back into the titles table.

    An original title is another name the film is genuinely known by, and is
    exactly what a later import might arrive spelling.
    """
    from . import works

    added = 0
    for row in conn.execute(
        "SELECT t.work_key, t.title, t.original_title, t.year, w.id "
        "FROM tmdb_film t JOIN work w ON w.key = t.work_key "
        "WHERE t.status = 'ok'"
    ).fetchall():
        for name in (row["title"], row["original_title"]):
            if name:
                works.record_title(conn, row["id"], name, row["year"], "tmdb")
                added += 1
    conn.commit()
    return added


def show_poster_dirs(database: Path) -> tuple[Path, Path]:
    root = artwork.cache_dir(database) / "show_posters"
    return root / "full", root / "thumb"


def fetch_shows(database: Path, key: str | None = None,
                limit: int | None = None, progress=None) -> dict:
    """Look up TV shows at TMDb and cache their metadata and posters."""
    token = api_key(key)
    if not token:
        raise RuntimeError("no TMDb key configured")

    conn = db.connect(database)
    try:
        shows = conn.execute(
            "SELECT DISTINCT show FROM item WHERE show IS NOT NULL "
            "AND show NOT IN (SELECT show_name FROM tmdb_show) "
            "ORDER BY show COLLATE NOCASE"
        ).fetchall()
        names = [r[0] for r in shows]
        if limit is not None:
            names = names[:limit]

        full_dir, thumb_dir = show_poster_dirs(database)
        full_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)

        counts = {"looked_up": 0, "found": 0, "missing": 0,
                  "errors": 0, "posters": 0}
        for n, show_name in enumerate(names, start=1):
            counts["looked_up"] += 1
            try:
                found = search_tv(show_name, token)
            except RuntimeError as exc:
                counts["errors"] += 1
                conn.execute(
                    "INSERT OR REPLACE INTO tmdb_show "
                    "(show_name, status, fetched_at) VALUES (?, 'error', ?)",
                    (show_name, db._now_iso()))
                if "rejected the key" in str(exc):
                    raise
                continue

            if not found:
                counts["missing"] += 1
                conn.execute(
                    "INSERT OR REPLACE INTO tmdb_show "
                    "(show_name, status, fetched_at) VALUES (?, 'none', ?)",
                    (show_name, db._now_iso()))
            else:
                genres = series_genres(found["series_id"], token)
                counts["found"] += 1
                conn.execute(
                    "INSERT OR REPLACE INTO tmdb_show "
                    "(show_name, tmdb_id, name, year, overview, genres, "
                    "poster_path, vote_count, status, fetched_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ok', ?)",
                    (show_name, found["series_id"], found["series_name"],
                     found["series_year"], found["overview"], genres,
                     found["poster_path"], found.get("vote_count"),
                     db._now_iso()))
                if found.get("poster_path"):
                    safe = _safe(show_name)
                    try:
                        (full_dir / f"{safe}.jpg").write_bytes(
                            _request(f"{IMAGE_BASE}/{FULL_SIZE}{found['poster_path']}"))
                        (thumb_dir / f"{safe}.jpg").write_bytes(
                            _request(f"{IMAGE_BASE}/{THUMB_SIZE}{found['poster_path']}"))
                        counts["posters"] += 1
                    except (urllib.error.URLError, TimeoutError, OSError):
                        pass

            if n % COMMIT_EVERY == 0:
                conn.commit()
                if progress:
                    progress(n, len(names), counts)
            time.sleep(PAUSE_SECONDS)
        conn.commit()
        return counts
    finally:
        conn.close()


def summary(database: Path) -> dict:
    full, thumb = poster_dirs(database)
    count = lambda d: len(list(d.glob("*.jpg"))) if d.is_dir() else 0
    size = lambda d: sum(p.stat().st_size for p in d.glob("*.jpg")) if d.is_dir() else 0
    conn = db.connect(database)
    try:
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM tmdb_film GROUP BY status")}
        with_director = conn.execute(
            "SELECT COUNT(*) FROM tmdb_film WHERE directors IS NOT NULL").fetchone()[0]
        pending = len(wanted(conn))
        has_titles = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tmdb_title'"
        ).fetchone()
        titles = conn.execute(
            "SELECT COUNT(*) FROM tmdb_title").fetchone()[0] if has_titles else 0
    finally:
        conn.close()
    return {"films_described": by_status.get("ok", 0),
            "with_a_director": with_director,
            "not_found": by_status.get("none", 0),
            "errors": by_status.get("error", 0),
            "posters": count(full), "thumbnails": count(thumb),
            "bytes": size(full) + size(thumb), "pending": pending,
            "key_configured": bool(api_key()),
            "title_index": titles}


def load_title_index(database: Path, min_popularity: float = 2.0) -> dict:
    """Download TMDb's daily export and build a title index for autocomplete.

    Only non-adult films above the popularity threshold are kept -- about
    100K titles, enough for any film someone would search for.
    """
    import gzip
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    url = EXPORT_URL.format(
        month=f"{now.month:02d}", day=f"{now.day:02d}", year=now.year)
    print(f"downloading TMDb daily export ...")
    try:
        data = gzip.decompress(_request(url, timeout=60))
    except Exception:
        # Today's export may not be posted yet; try yesterday.
        yesterday = datetime.fromtimestamp(now.timestamp() - 86400, tz=timezone.utc)
        url = EXPORT_URL.format(
            month=f"{yesterday.month:02d}", day=f"{yesterday.day:02d}",
            year=yesterday.year)
        data = gzip.decompress(_request(url, timeout=60))

    rows = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        if entry.get("adult") or entry.get("video"):
            continue
        pop = entry.get("popularity", 0)
        if pop < min_popularity:
            continue
        rows.append((entry["id"], entry.get("original_title") or "", pop))

    print(f"parsed {len(rows)} titles (popularity >= {min_popularity})")
    conn = db.connect(database)
    try:
        with conn:
            conn.execute("DELETE FROM tmdb_title")
            conn.executemany(
                "INSERT OR REPLACE INTO tmdb_title (tmdb_id, title, popularity) "
                "VALUES (?, ?, ?)", rows)
    finally:
        conn.close()
    return {"titles": len(rows)}
