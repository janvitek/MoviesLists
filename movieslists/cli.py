"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import DEFAULT_DATABASE, __version__


def _database(args) -> Path:
    return Path(args.database).expanduser()


def cmd_sync(args) -> int:
    from . import sync as sync_module

    database = _database(args)
    print("reading the TV.app library ...")
    try:
        result = sync_module.sync(database)
    except sync_module.SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"synced {result['count']} items into {database}")
    if result["unavailable_fields"]:
        names = ", ".join(sorted(result["unavailable_fields"]))
        print(f"note: TV.app did not expose these fields: {names}")
    report_changes(result)
    return 0


def report_changes(result: dict) -> None:
    """Say what moved since the last import, loudly if something vanished."""
    if result.get("first_import"):
        print("first import: nothing to compare against yet")
        return
    counts = result.get("changes") or {}
    removed = result.get("removed_items") or []
    if not any(counts.values()):
        print("no changes since the last import")
        return
    print(f"changes since the last import: "
          f"{counts.get('added', 0)} added, {counts.get('removed', 0)} removed, "
          f"{counts.get('modified', 0)} modified")
    if removed:
        movies = [r for r in removed if r["media_kind"] == "movie"]
        if movies:
            print(f"\n  *** RED ALERT: {len(movies)} "
                  f"movie{'s' if len(movies) != 1 else ''} disappeared ***")
            for item in movies[:20]:
                print(f"      - {item['name']}")
            if len(movies) > 20:
                print(f"      ... and {len(movies) - 20} more")
        others = [r for r in removed if r["media_kind"] != "movie"]
        if others:
            print(f"\n  {len(others)} other item{'s' if len(others) != 1 else ''} "
                  f"removed:")
            for item in others[:10]:
                print(f"      - {item['name']}")
            if len(others) > 10:
                print(f"      ... and {len(others) - 10} more")
        print()


def cmd_serve(args) -> int:
    from . import server, sync as sync_module

    database = _database(args)
    # Starting the app re-imports, so the library on screen is current and
    # anything that changed since last time gets reported up front.
    if not args.no_sync:
        print("importing the TV.app library ...")
        try:
            result = sync_module.sync(database)
        except sync_module.SyncError as exc:
            if not database.exists():
                print(f"error: {exc}", file=sys.stderr)
                return 1
            print(f"warning: import failed, serving the last cache instead\n  {exc}",
                  file=sys.stderr)
        else:
            print(f"imported {result['count']} items")
            report_changes(result)
    elif not database.exists():
        print(f"error: no library cache at {database}\nrun 'movieslists sync' first.",
              file=sys.stderr)
        return 1

    server.serve(database, host=args.host, port=args.port,
                 open_browser=not args.no_browser)
    return 0


def cmd_stats(args) -> int:
    import sqlite3

    from . import queries

    database = _database(args)
    if not database.exists():
        print(f"error: no library cache at {database}\nrun 'movieslists sync' first.",
              file=sys.stderr)
        return 1
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        for key, value in queries.stats(conn).items():
            print(f"{key:>18}: {value}")
    finally:
        conn.close()
    return 0


def cmd_artwork(args) -> int:
    from . import artwork

    database = _database(args)
    if not database.exists():
        print(f"error: no library cache at {database}\nrun 'movieslists sync' first.",
              file=sys.stderr)
        return 1
    if args.clear:
        artwork.clear(database)
        print("artwork cache cleared")
        return 0
    if args.shrink:
        result = artwork.shrink(database)
        mb = result["bytes_saved"] / 1_000_000
        print(f"re-encoded {result['shrunk']} images and rebuilt "
              f"{result['thumbnails']} thumbnails, saving {mb:.0f} MB")
        for key, value in artwork.summary(database).items():
            print(f"{key:>8}: {value}")
        return 0
    if args.status:
        for key, value in artwork.summary(database).items():
            print(f"{key:>8}: {value}")
        return 0
    result = artwork.fetch(
        database, limit=args.limit, scope=args.scope,
        refresh=args.refresh, log_path=Path(args.log).expanduser() if args.log else None,
    )
    print(f"requested {result['requested']}, fetched {result['fetched']}, "
          f"thumbnails {result['thumbed']}, without art {result['skipped']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="movieslists",
        description="Browse your Apple TV.app library in the browser.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--database", default=DEFAULT_DATABASE,
        help=f"library cache location (default: {DEFAULT_DATABASE})",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sync = sub.add_parser("sync", help="read TV.app into the local cache")
    sync.set_defaults(func=cmd_sync)

    serve = sub.add_parser("serve", help="serve the browser UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-browser", action="store_true",
                       help="do not open a browser window")
    serve.add_argument("--no-sync", action="store_true",
                       help="skip the import and serve the existing cache")
    serve.set_defaults(func=cmd_serve)

    stats = sub.add_parser("stats", help="print a summary of the cache")
    stats.set_defaults(func=cmd_stats)

    art = sub.add_parser("artwork", help="download cover art from TV.app")
    art.add_argument("--limit", type=int, default=None,
                     help="stop after this many items")
    art.add_argument("--scope", choices=["posters", "all"], default="posters",
                     help="'posters': every movie plus one image per show "
                          "(default); 'all': every episode too")
    art.add_argument("--refresh", action="store_true",
                     help="re-fetch art that is already cached")
    art.add_argument("--clear", action="store_true", help="delete the art cache")
    art.add_argument("--status", action="store_true", help="report cache size")
    art.add_argument("--shrink", action="store_true",
                     help="re-encode cached images smaller and rebuild thumbnails")
    art.add_argument("--log", default=None, help="write per-item progress here")
    art.set_defaults(func=cmd_artwork)

    return parser


def main(argv=None) -> int:
    # `serve` prints its import report and then blocks, so a block-buffered
    # stdout (which is what Python gives a pipe or a log file) would withhold
    # the report for the lifetime of the server.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)
