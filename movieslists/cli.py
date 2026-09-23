"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import DEFAULT_DATABASE, __version__


def _database(args) -> Path:
    return Path(args.database).expanduser()


def _shared(args):
    """The shared store, or None when sharing has not been configured."""
    from . import sharing

    root = sharing.shared_root(getattr(args, "shared_dir", None))
    return sharing.SharedStore(root) if root else None


def _attach_and_merge(conn, args, quiet: bool = False):
    """Hook the shared store up and merge before doing anything else.

    Every command that reads or writes edits goes through here, so a machine
    always acts on the newest state it can see rather than its own stale copy.
    """
    from . import db, sharing

    store = _shared(args)
    if store is None:
        return None
    db.attach_store(store)

    stale = store.conflicts()
    if stale and not quiet:
        print(f"warning: {len(stale)} sync-conflict file(s) in {store.root}; "
              f"run 'movieslists doctor' to look at them", file=sys.stderr)
    try:
        with sharing.lock(store.root, purpose="merge"):
            counts = sharing.reconcile(conn, store)
    except sharing.LockBusy as exc:
        print(f"warning: {exc}\n  continuing without merging", file=sys.stderr)
        return store
    if not quiet and (counts["pulled"] or counts["deleted_locally"]):
        print(f"merged from other machines: {counts['pulled']} edits in, "
              f"{counts['deleted_locally']} removed"
              + (f", {counts['conflicts_resolved']} conflicting fields resolved "
                 f"by recency" if counts["conflicts_resolved"] else ""))
    return store


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

    # Pick up other machines' edits, and snapshot before this machine starts
    # changing anything.
    store = None
    if database.exists():
        import sqlite3

        from . import db as db_module
        conn = db_module.connect(database)
        conn.row_factory = sqlite3.Row
        try:
            store = _attach_and_merge(conn, args)
        finally:
            conn.close()
        if store is not None and not args.no_snapshot:
            from . import snapshots
            try:
                archive = snapshots.take(store.root, database, reason="serve")
                removed = snapshots.prune(store.root)
                print(f"snapshot {archive.name}"
                      + (f" ({len(removed)} older pruned)" if removed else ""))
            except OSError as exc:
                print(f"warning: could not snapshot: {exc}", file=sys.stderr)

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

    if store is not None:
        server.attach_sharing(store, getattr(args, "shared_dir", None))
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


WANTED_CHOICES = {"reviews": "review", "ratings": "rating", "dates": "played_date"}


def cmd_letterboxd(args) -> int:
    import sqlite3

    from . import db, letterboxd, works

    database = _database(args)
    if not database.exists():
        print(f"error: no library cache at {database}\nrun 'movieslists sync' first.",
              file=sys.stderr)
        return 1

    conn = db.connect(database)
    conn.row_factory = sqlite3.Row
    _attach_and_merge(conn, args)
    try:
        if args.status:
            for key, value in works.stats(conn).items():
                print(f"{key:>10}: {value}")
            row = conn.execute(
                "SELECT COUNT(*), SUM(rating IS NOT NULL), SUM(review IS NOT NULL), "
                "SUM(watchlisted_date IS NOT NULL), SUM(liked_date IS NOT NULL) "
                "FROM lb_film").fetchone()
            print(f"\n letterboxd films: {row[0] or 0}")
            print(f"    rated         : {row[1] or 0}")
            print(f"    reviewed      : {row[2] or 0}")
            print(f"    watchlisted   : {row[3] or 0}")
            print(f"    liked         : {row[4] or 0}")
            print(f"    diary entries : "
                  f"{conn.execute('SELECT COUNT(*) FROM lb_diary').fetchone()[0]}")
            print(f"    lists         : "
                  f"{conn.execute('SELECT COUNT(*) FROM lb_list').fetchone()[0]}")
            return 0

        if args.relink:
            counts = works.rebuild(conn)
            print(f"linked {counts['tv']} TV.app movies and {counts['lb']} "
                  f"Letterboxd films; {counts['merged']} joined by year drift")
            for key, value in works.stats(conn).items():
                print(f"{key:>10}: {value}")
            return 0

        if not args.export:
            print("error: give the path to a Letterboxd export (.zip or folder), "
                  "or use --status / --relink", file=sys.stderr)
            return 1

        try:
            result = letterboxd.import_full(conn, Path(args.export))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        print(f"read {result['files']} files: {result['films']} films, "
              f"{result['entries']} diary entries and reviews, "
              f"{result['lists']} lists")

        counts = works.rebuild(conn)
        moved = works.migrate_overrides(conn)
        if moved:
            print(f"moved {moved} edits onto their works")
        # Ratings and reviews now live in lb_film and are read from there, so
        # the copies a previous import made into the override table are stale
        # duplicates rather than edits.
        stale = conn.execute(
            "DELETE FROM work_override WHERE source = 'letterboxd'").rowcount
        conn.commit()
        if stale:
            print(f"dropped {stale} override copies now read from lb_film directly")

        print(f"\nlinked {counts['tv']} TV.app movies and {counts['lb']} "
              f"Letterboxd films")
        for key, value in works.stats(conn).items():
            print(f"{key:>10}: {value}")
        return 0
    finally:
        conn.close()


def cmd_config(args) -> int:
    from . import sharing

    config = sharing.load_config()
    if args.shared_dir:
        root = Path(args.shared_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        config["shared_dir"] = str(root)
        sharing.save_config(config)
        print(f"shared store: {root}")
        print("your edits, reviews and ratings will live here and merge across "
              "machines.\nthe cached library stays local, since each machine "
              "reads its own TV.app.")
    if args.no_sharing:
        config.pop("shared_dir", None)
        sharing.save_config(config)
        print("sharing disabled; edits stay on this machine")

    device = sharing.device()
    print(f"\nthis machine : {device['host']} ({device['id']})")
    root = sharing.shared_root(getattr(args, "shared_dir", None))
    print(f"shared store : {root or 'not configured'}")
    print(f"database     : {_database(args)}")
    return 0


def cmd_share(args) -> int:
    import sqlite3

    from . import db, sharing

    database = _database(args)
    store = _shared(args)
    if store is None:
        print("error: no shared store configured.\n"
              "  movieslists config --shared-dir ~/Dropbox/MoviesLists",
              file=sys.stderr)
        return 1
    conn = db.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        db.attach_store(store)
        with sharing.lock(store.root, purpose="share"):
            counts = sharing.reconcile(conn, store)
        for key, value in counts.items():
            print(f"{key:>20}: {value}")
    except sharing.LockBusy as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    return 0


def cmd_snapshot(args) -> int:
    from . import snapshots

    store = _shared(args)
    if store is None:
        print("error: snapshots need a shared store.\n"
              "  movieslists config --shared-dir ~/Dropbox/MoviesLists",
              file=sys.stderr)
        return 1
    database = _database(args)

    if args.list:
        rows = snapshots.listing(store.root)
        if not rows:
            print("no snapshots yet")
            return 0
        for row in rows:
            when = row["taken_at"].strftime("%Y-%m-%d %H:%M UTC") if row["taken_at"] else "?"
            print(f"  {row['name']}  {when}  {row['bytes'] / 1000:.0f} KB")
        print(f"\n{len(rows)} snapshots in {snapshots.snapshot_dir(store.root)}")
        return 0

    if args.restore:
        archive = snapshots.snapshot_dir(store.root) / args.restore
        if not archive.is_file():
            print(f"error: no snapshot named {args.restore}", file=sys.stderr)
            return 1
        result = snapshots.restore(store.root, archive, database,
                                   overrides_only=not args.restore_database)
        print(f"restored edits for {result['films']} films")
        if result.get("database_restored"):
            print(f"restored the cached library too "
                  f"(previous copy kept at {result['previous_database']})")
        print("run 'movieslists share' to merge the restored edits back in")
        return 0

    if args.prune_only:
        removed = snapshots.prune(store.root)
        print(f"pruned {len(removed)} snapshots")
        return 0

    archive = snapshots.take(store.root, database, reason=args.reason)
    removed = snapshots.prune(store.root)
    print(f"wrote {archive.name} ({archive.stat().st_size / 1000:.0f} KB)")
    if removed:
        print(f"pruned {len(removed)} older snapshots")
    return 0


def cmd_doctor(args) -> int:
    """Look for the things a synced folder gets wrong."""
    import sqlite3

    from . import db, sharing, snapshots

    database = _database(args)
    problems = 0

    print(f"database     : {database}"
          f"{'' if database.exists() else '   MISSING'}")
    if database.exists():
        conn = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
            print(f"integrity    : {check}")
            if check != "ok":
                problems += 1
            print(f"overrides    : "
                  f"{conn.execute('SELECT COUNT(*) FROM item_override').fetchone()[0]}")
        finally:
            conn.close()

    # A database living on the synced folder is the failure mode worth naming.
    shared = sharing.shared_root(getattr(args, "shared_dir", None))
    print(f"shared store : {shared or 'not configured'}")
    if shared:
        try:
            database.resolve().relative_to(Path(shared).resolve())
            print("  PROBLEM: the SQLite database is inside the synced folder.\n"
                  "  A sync service can copy it mid-write and corrupt it. Move it\n"
                  "  back with --database ~/.local/share/movieslists/library.sqlite3")
            problems += 1
        except ValueError:
            print("  ok: the database is outside the synced folder")

        store = sharing.SharedStore(shared)
        conflicts = store.conflicts()
        if conflicts:
            problems += 1
            print(f"  PROBLEM: {len(conflicts)} sync-conflict file(s):")
            for path in conflicts[:10]:
                print(f"    {path}")
            print("  Each is another machine's version of the same film. Compare\n"
                  "  with the original, keep what you want, then delete the copy.")
        else:
            print("  ok: no sync-conflict files")

        held = sharing._read_lock(Path(shared) / sharing.LOCK_NAME)
        if held:
            age = sharing._lock_age(held)
            stale = age is not None and age > sharing.LOCK_STALE_SECONDS
            print(f"  lock held by {held.get('host')} (pid {held.get('pid')}), "
                  f"{age:.0f}s old{'  STALE' if stale else ''}")
            if stale:
                print("  It will be taken over automatically on the next write.")
        else:
            print("  ok: no lock held")

        records = store.read_all()
        print(f"  films with edits: {len(records)}, "
              f"fields: {sum(len(f) for f in records.values())}")
        rows = snapshots.listing(shared)
        newest = rows[0]["taken_at"].strftime("%Y-%m-%d %H:%M UTC") if rows and rows[0]["taken_at"] else "never"
        print(f"  snapshots: {len(rows)}, newest {newest}")
        if not rows:
            print("  suggestion: take one with 'movieslists snapshot'")

    print(f"\n{'no problems found' if not problems else str(problems) + ' problem(s) above'}")
    return 1 if problems else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="movieslists",
        description="Browse your Apple TV.app library in the browser.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument(
        "--database", default=DEFAULT_DATABASE,
        help=f"library cache location (default: {DEFAULT_DATABASE}). "
             "Keep this OFF any synced folder.",
    )
    parser.add_argument(
        "--shared-dir", default=None,
        help="synced folder holding edits shared between machines "
             "(default: whatever 'movieslists config' has set)",
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
    serve.add_argument("--no-snapshot", action="store_true",
                       help="do not snapshot before starting")
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

    lb = sub.add_parser(
        "letterboxd",
        help="import reviews, ratings and watch dates from a Letterboxd export",
        description="Import a Letterboxd data export "
                    "(https://letterboxd.com/user/exportdata/). Nothing is sent "
                    "anywhere; the file is read locally.",
    )
    lb.add_argument("export", nargs="?",
                    help="the export .zip, an unpacked folder, or a single .csv")
    lb.add_argument("--relink", action="store_true",
                    help="rebuild the links between TV.app and Letterboxd")
    lb.add_argument("--status", action="store_true",
                    help="summarise what has been imported and linked")
    lb.set_defaults(func=cmd_letterboxd)

    cfg = sub.add_parser("config", help="show or change where shared edits live")
    cfg.add_argument("--shared-dir", dest="shared_dir", default=None,
                     help="synced folder to keep edits in, e.g. ~/Dropbox/MoviesLists")
    cfg.add_argument("--no-sharing", action="store_true",
                     help="stop sharing; edits stay on this machine")
    cfg.set_defaults(func=cmd_config)

    share = sub.add_parser("share", help="merge edits with the shared store now")
    share.set_defaults(func=cmd_share)

    snap = sub.add_parser("snapshot", help="snapshot edits and the database")
    snap.add_argument("--list", action="store_true", help="list snapshots")
    snap.add_argument("--restore", metavar="NAME", help="restore a snapshot")
    snap.add_argument("--restore-database", action="store_true",
                      help="with --restore, also put back the cached library")
    snap.add_argument("--prune-only", action="store_true",
                      help="apply the retention policy without taking one")
    snap.add_argument("--reason", default="manual", help="note stored in the snapshot")
    snap.set_defaults(func=cmd_snapshot)

    doc = sub.add_parser("doctor", help="check for sync and integrity problems")
    doc.set_defaults(func=cmd_doctor)

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
