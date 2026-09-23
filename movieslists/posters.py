"""Posters for films that exist only on Letterboxd.

A Letterboxd export contains no images, and those films are not in TV.app, so
they have no artwork of their own. The only way to show one is to ask a poster
service, which means sending the title and year out of this machine -- the one
place this app talks to the network, and only when you have configured a key.

TMDb is used because it is free for personal use, indexes by title and year,
and is what Letterboxd itself draws artwork from. Every lookup is recorded,
misses included, so a title is searched once and not again.
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
IMAGE_BASE = "https://image.tmdb.org/t/p"
FULL_SIZE, THUMB_SIZE = "w500", "w185"
KEY_FILE = sharing.CONFIG_DIR / "tmdb.key"
USER_AGENT = "MoviesLists/0.1 (personal library tool)"

# TMDb allows far more than this; the pause is politeness, not a limit.
PAUSE_SECONDS = 0.06


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


def wanted(conn: sqlite3.Connection, refresh: bool = False) -> list[dict]:
    """Films with no artwork: the ones Letterboxd knows and TV.app does not."""
    rows = conn.execute(
        "SELECT w.key, w.title, w.year FROM work w "
        "WHERE NOT EXISTS (SELECT 1 FROM work_source s "
        "                  WHERE s.work_id = w.id AND s.source = 'tv') "
        "ORDER BY w.title COLLATE NOCASE"
    ).fetchall()
    out = [dict(r) for r in rows]
    if refresh:
        return out
    # A previous miss is remembered, so the same title is not searched again.
    known = {
        r[0] for r in conn.execute(
            "SELECT work_key FROM poster WHERE status IN ('ok', 'none')")
    }
    return [r for r in out if r["key"] not in known]


def _request(url: str, timeout: float = 15.0) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def search(title: str, year: int | None, key: str) -> dict | None:
    """Best TMDb match for a title, preferring an exact year."""
    params = {"api_key": key, "query": title, "include_adult": "false"}
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
          key: str | None = None, progress=None) -> dict:
    """Look up and download the posters that are missing."""
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
        targets = wanted(conn, refresh)
        if limit is not None:
            targets = targets[:limit]
        full_dir, thumb_dir = poster_dirs(database)
        full_dir.mkdir(parents=True, exist_ok=True)
        thumb_dir.mkdir(parents=True, exist_ok=True)

        counts = {"looked_up": 0, "found": 0, "missing": 0, "errors": 0}
        for n, row in enumerate(targets, start=1):
            counts["looked_up"] += 1
            try:
                match = search(row["title"], row["year"], token)
            except RuntimeError as exc:
                counts["errors"] += 1
                _record(conn, row, None, "error", str(exc))
                if "rejected the key" in str(exc):
                    raise
                continue

            if not match:
                counts["missing"] += 1
                _record(conn, row, None, "none", "no poster at TMDb")
            else:
                name = f"{_safe(row['key'])}.jpg"
                try:
                    (full_dir / name).write_bytes(
                        _request(f"{IMAGE_BASE}/{FULL_SIZE}{match['poster_path']}"))
                    (thumb_dir / name).write_bytes(
                        _request(f"{IMAGE_BASE}/{THUMB_SIZE}{match['poster_path']}"))
                    counts["found"] += 1
                    _record(conn, row, match, "ok", None)
                except (urllib.error.URLError, TimeoutError, OSError) as exc:
                    counts["errors"] += 1
                    _record(conn, row, match, "error", str(exc))
            if progress and n % 25 == 0:
                progress(n, len(targets), counts)
            time.sleep(PAUSE_SECONDS)
        conn.commit()
        counts["remaining"] = len(wanted(conn))
        return counts
    finally:
        conn.close()


def _record(conn, row, match, status, detail) -> None:
    conn.execute(
        "INSERT INTO poster (work_key, service, remote_id, remote_path, title, "
        "year, status, detail, fetched_at) VALUES (?, 'tmdb', ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(work_key) DO UPDATE SET remote_id = excluded.remote_id, "
        "remote_path = excluded.remote_path, status = excluded.status, "
        "detail = excluded.detail, fetched_at = excluded.fetched_at",
        (row["key"], str(match["id"]) if match else None,
         match.get("poster_path") if match else None, row["title"], row["year"],
         status, detail, db._now_iso()),
    )


def summary(database: Path) -> dict:
    full, thumb = poster_dirs(database)
    count = lambda d: len(list(d.glob("*.jpg"))) if d.is_dir() else 0
    size = lambda d: sum(p.stat().st_size for p in d.glob("*.jpg")) if d.is_dir() else 0
    conn = db.connect(database)
    try:
        by_status = {r[0]: r[1] for r in conn.execute(
            "SELECT status, COUNT(*) FROM poster GROUP BY status")}
        pending = len(wanted(conn))
    finally:
        conn.close()
    return {"downloaded": count(full), "thumbnails": count(thumb),
            "bytes": size(full) + size(thumb), "not_found": by_status.get("none", 0),
            "errors": by_status.get("error", 0), "pending": pending,
            "key_configured": bool(api_key())}
