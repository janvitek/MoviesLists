"""Extract and cache cover art from TV.app.

Artwork is pulled one track at a time over Apple Events, which is slow
(roughly 5-10 items a second), so it lives outside the main sync: run it in
the background and let the UI fill in as images land.

Layout under <cache>/artwork/:
    full/<id>.jpg    as TV.app supplied it
    thumb/<id>.jpg   downscaled for list rows
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).with_name("artwork.applescript")

# TV.app hands over 1600x900 JPEGs at 100-500KB each, which is far more than
# either the list or the detail panel can use. Both sizes are re-encoded with
# sips, which ships with macOS.
THUMB_MAX_PX = 320
FULL_MAX_PX = 800
JPEG_QUALITY = 70


def cache_dir(database: Path) -> Path:
    return database.parent / "artwork"


def paths(database: Path) -> tuple[Path, Path]:
    root = cache_dir(database)
    return root / "full", root / "thumb"


def have(database: Path) -> set[int]:
    """Database IDs that already have full-size art cached."""
    full, _ = paths(database)
    if not full.is_dir():
        return set()
    out = set()
    for p in full.glob("*.jpg"):
        if p.stat().st_size > 0:
            try:
                out.add(int(p.stem))
            except ValueError:
                pass
    return out


def wanted(conn: sqlite3.Connection, scope: str = "posters") -> list[int]:
    """Which items are worth fetching art for, in display order.

    'posters' covers every movie plus one representative episode per show,
    which is what the list and show views actually display. 'all' is every
    track, and takes correspondingly longer.
    """
    if scope == "all":
        rows = conn.execute("SELECT id FROM item ORDER BY id")
        return [r[0] for r in rows]

    movies = [
        r[0] for r in conn.execute(
            "SELECT id FROM item WHERE media_kind = 'movie' "
            "ORDER BY COALESCE(sort_name, name) COLLATE NOCASE"
        )
    ]
    # One episode stands in for each series. SQLite gives the bare `id` from
    # whichever row supplied the MIN(), so this picks the earliest episode.
    shows = [
        r[0] for r in conn.execute(
            "SELECT id, MIN(COALESCE(season_number, 99) * 10000 "
            "               + COALESCE(episode_number, 0)) "
            "FROM item WHERE media_kind = 'TV show' AND show IS NOT NULL "
            "GROUP BY show ORDER BY show COLLATE NOCASE"
        )
    ]
    return movies + shows


def _library_index(conn: sqlite3.Connection) -> dict[int, int]:
    """Map database ID to its 1-based position in the library playlist.

    The position is recorded by the exporter as it walks the playlist. It is
    NOT derivable from rowid: `id INTEGER PRIMARY KEY` aliases rowid, so
    rowid order is database-ID order, which is a different sequence entirely.
    """
    rows = conn.execute(
        "SELECT id, COALESCE(position, library_index) FROM item "
        "WHERE COALESCE(position, library_index) IS NOT NULL"
    )
    return {item_id: pos for item_id, pos in rows}


def _resize(src: Path, dest: Path, max_px: int) -> bool:
    dest.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["sips", "-Z", str(max_px),
         "--setProperty", "formatOptions", str(JPEG_QUALITY),
         str(src), "--out", str(dest)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and dest.exists() and dest.stat().st_size > 0


def make_thumb(src: Path, dest: Path) -> bool:
    return _resize(src, dest, THUMB_MAX_PX)


def shrink_full(path: Path) -> int:
    """Re-encode a full-size image in place. Returns bytes saved."""
    before = path.stat().st_size
    temp = path.with_suffix(".shrink.jpg")
    if not _resize(path, temp, FULL_MAX_PX):
        temp.unlink(missing_ok=True)
        return 0
    if temp.stat().st_size >= before:
        temp.unlink(missing_ok=True)   # already small enough
        return 0
    temp.replace(path)
    return before - path.stat().st_size


def shrink(database: Path, rebuild_thumbs: bool = True) -> dict:
    """Downscale every cached image that is still oversized."""
    full_dir, thumb_dir = paths(database)
    saved = shrunk = thumbs = 0
    for image in sorted(full_dir.glob("*.jpg")):
        delta = shrink_full(image)
        if delta:
            saved += delta
            shrunk += 1
        if rebuild_thumbs and make_thumb(image, thumb_dir / image.name):
            thumbs += 1
    return {"shrunk": shrunk, "thumbnails": thumbs, "bytes_saved": saved}


def fetch(database: Path, limit: int | None = None, scope: str = "posters",
          refresh: bool = False, log_path: Path | None = None) -> dict:
    """Pull missing artwork. Returns a summary dict."""
    full_dir, thumb_dir = paths(database)
    full_dir.mkdir(parents=True, exist_ok=True)
    thumb_dir.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(database)
    try:
        targets = wanted(conn, scope)
        positions = _library_index(conn)
    finally:
        conn.close()

    if not refresh:
        already = have(database)
        targets = [i for i in targets if i not in already]
    if limit is not None:
        targets = targets[:limit]

    if not targets:
        return {"requested": 0, "fetched": 0, "thumbed": 0, "skipped": 0}

    with tempfile.TemporaryDirectory() as tmp:
        job = Path(tmp) / "job.tsv"
        job.write_text(
            "".join(f"{positions[i]}\t{i}\n" for i in targets if i in positions),
            encoding="utf-8",
        )
        log = log_path or (Path(tmp) / "artwork.log")
        log.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["osascript", str(SCRIPT), str(job), str(full_dir), str(log)],
            capture_output=True, text=True,
        )

    fetched = [i for i in targets if (full_dir / f"{i}.jpg").exists()]
    thumbed = saved = 0
    for item_id in fetched:
        src = full_dir / f"{item_id}.jpg"
        saved += shrink_full(src)
        dest = thumb_dir / f"{item_id}.jpg"
        if refresh or not dest.exists():
            if make_thumb(src, dest):
                thumbed += 1
    return {
        "requested": len(targets),
        "fetched": len(fetched),
        "thumbed": thumbed,
        "skipped": len(targets) - len(fetched),
        "bytes_saved": saved,
    }


def summary(database: Path) -> dict:
    full_dir, thumb_dir = paths(database)
    count = lambda d: len(list(d.glob("*.jpg"))) if d.is_dir() else 0
    size = lambda d: sum(p.stat().st_size for p in d.glob("*.jpg")) if d.is_dir() else 0
    return {
        "full": count(full_dir),
        "thumbs": count(thumb_dir),
        "bytes": size(full_dir) + size(thumb_dir),
    }


def clear(database: Path) -> None:
    shutil.rmtree(cache_dir(database), ignore_errors=True)
