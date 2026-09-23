"""A small stdlib HTTP server for the library UI.

Binds to loopback only: the library is personal data and there is no auth.
"""

from __future__ import annotations

import gzip
import json
import mimetypes
import re
import sqlite3
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import artwork, db, queries

WEB_ROOT = Path(__file__).with_name("web")

# Set by the CLI when sharing is configured, so edits made in the browser are
# mirrored to the shared store like any other write.
_SHARED_STORE = None


def attach_sharing(store, shared_dir=None) -> None:
    global _SHARED_STORE
    _SHARED_STORE = store
GZIP_MIN_BYTES = 1024
ART_ROUTE = re.compile(r"^/art/(thumb|full)/(\d+)\.jpg$")
POSTER_ROUTE = re.compile(r"^/art/poster/(thumb|full)/(.+)\.jpg$")
WORK_ROUTE = re.compile(r"^/api/work/([^/]+)$")
OVERRIDE_ROUTE = re.compile(r"^/api/work/([^/]+)/override$")
# Both flags come from Letterboxd and both can be overridden here, so they
# share a route. "like" and "watchlist" are the names the UI uses.
FLAG_ROUTE = re.compile(r"^/api/work/([^/]+)/(like|watchlist|delete)$")
FLAG_FIELDS = {"like": "liked", "watchlist": "watchlisted", "delete": "deleted"}
WATCH_ROUTE = re.compile(r"^/api/work/([^/]+)/watch$")
WATCH_ID_ROUTE = re.compile(r"^/api/watch/([0-9a-f]{32})$")


class Handler(BaseHTTPRequestHandler):
    server_version = "MoviesLists"

    def __init__(self, *args, database: Path, **kwargs):
        self.database = database
        super().__init__(*args, **kwargs)

    # --- plumbing --------------------------------------------------------

    def _read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {db.BUSY_TIMEOUT_MS}")
        return conn

    def _write(self) -> sqlite3.Connection:
        conn = db.connect(self.database)
        if _SHARED_STORE is not None:
            db.attach_store(_SHARED_STORE)
        return conn

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _dispatch(self, handler):
        try:
            handler()
        except BrokenPipeError:
            pass
        except json.JSONDecodeError:
            self.send_json({"error": "malformed JSON body"}, status=400)
        except ValueError as exc:
            self.send_json({"error": str(exc)}, status=400)
        except Exception as exc:
            self.send_json({"error": str(exc)}, status=500)

    # --- routing ---------------------------------------------------------

    def do_GET(self):
        self._dispatch(self._get)

    def do_POST(self):
        self._dispatch(self._post)

    def do_DELETE(self):
        self._dispatch(self._delete)

    def _get(self):
        parsed = urlparse(self.path)
        path = parsed.path

        art = ART_ROUTE.match(path)
        if art:
            return self.send_artwork(art.group(1), int(art.group(2)))

        poster = POSTER_ROUTE.match(path)
        if poster:
            from urllib.parse import unquote
            return self.send_poster(poster.group(1), unquote(poster.group(2)))

        work = WORK_ROUTE.match(path)
        if work:
            from urllib.parse import unquote
            conn = self._read()
            try:
                detail = queries.work_detail(conn, unquote(work.group(1)))
            finally:
                conn.close()
            if detail is None:
                return self.send_json({"error": "no such film"}, status=404)
            return self.send_json(detail)

        if path.startswith("/api/"):
            conn = self._read()
            try:
                if path == "/api/library":
                    from . import posters
                    args = parse_qs(parsed.query)
                    return self.send_json(queries.library(
                        conn, artwork.have(self.database),
                        posters.have(self.database),
                        include_deleted=args.get("deleted", ["0"])[0] == "1",
                    ))
                if path == "/api/stats":
                    return self.send_json(queries.stats(conn))
                if path == "/api/changes":
                    args = parse_qs(parsed.query)
                    return self.send_json({
                        "summary": queries.change_summary(conn),
                        "changes": queries.changes(
                            conn,
                            include_acknowledged=args.get("all", ["0"])[0] == "1",
                        ),
                    })
                if path == "/api/artwork":
                    return self.send_json(artwork.summary(self.database))
                if path == "/api/watches":
                    from . import watching
                    return self.send_json({"watches": watching.recent(conn)})
                if path == "/api/tmdb/search":
                    from . import posters
                    args = parse_qs(parsed.query)
                    query = (args.get("q") or [""])[0].strip()
                    if not query:
                        raise ValueError("expected a 'q' parameter")
                    year = (args.get("year") or [None])[0]
                    return self.send_json({"results": posters.candidates(
                        query, int(year) if year and year.isdigit() else None)})
                if path == "/api/questions":
                    from . import works
                    return self.send_json({"questions": works.questions(conn),
                                           "stats": works.stats(conn)})
            finally:
                conn.close()
            return self.send_json({"error": "no such endpoint"}, status=404)

        return self.send_static(path)

    def _post(self):
        path = urlparse(self.path).path

        override = OVERRIDE_ROUTE.match(path)
        if override:
            from urllib.parse import unquote
            return self.set_overrides(unquote(override.group(1)))

        flag = FLAG_ROUTE.match(path)
        if flag:
            from urllib.parse import unquote
            return self.set_flag(unquote(flag.group(1)), FLAG_FIELDS[flag.group(2)])

        watch = WATCH_ROUTE.match(path)
        if watch:
            from urllib.parse import unquote
            from . import watching
            body = self._body()
            conn = self._write()
            try:
                row = watching.log(
                    conn, unquote(watch.group(1)),
                    watched_date=body.get("watched_date"),
                    rating=body.get("rating"), note=body.get("note"),
                    rewatch=body.get("rewatch"), venue=body.get("venue"))
                self._mirror_watch(conn, row["work_key"])
                detail = queries.work_detail(conn, row["work_key"])
            finally:
                conn.close()
            return self.send_json({"ok": True, "watch": row, "item": detail})

        edit = WATCH_ID_ROUTE.match(path)
        if edit:
            from . import watching
            conn = self._write()
            try:
                row = watching.update(conn, edit.group(1), **self._body())
                if row is None:
                    return self.send_json({"error": "no such viewing"}, status=404)
                self._mirror_watch(conn, row["work_key"])
                detail = queries.work_detail(conn, row["work_key"])
            finally:
                conn.close()
            return self.send_json({"ok": True, "watch": row, "item": detail})

        if path == "/api/tmdb/adopt":
            from . import watching
            body = self._body()
            if not body.get("tmdb_id"):
                raise ValueError("expected 'tmdb_id'")
            conn = self._write()
            try:
                result = watching.adopt_tmdb(conn, str(body["tmdb_id"]),
                                             database=self.database)
            finally:
                conn.close()
            return self.send_json({"ok": True, **result})

        if path == "/api/questions/answer":
            from . import works
            body = self._body()
            if "id" not in body or "same" not in body:
                raise ValueError("expected 'id' and 'same'")
            conn = self._write()
            try:
                result = works.answer(conn, str(body["id"]), bool(body["same"]))
                result["remaining"] = len(works.questions(conn))
            finally:
                conn.close()
            return self.send_json(result)

        if path == "/api/changes/ack":
            body = self._body()
            ids = body.get("ids")
            conn = self._write()
            try:
                with conn:
                    if ids:
                        marks = ",".join("?" * len(ids))
                        conn.execute(
                            f"UPDATE library_change SET acknowledged = 1 "
                            f"WHERE id IN ({marks})", ids
                        )
                    else:
                        conn.execute("UPDATE library_change SET acknowledged = 1")
            finally:
                conn.close()
            return self.send_json({"ok": True})

        if path == "/api/sync":
            from . import sync as sync_module
            result = sync_module.sync(self.database)
            result.pop("removed_items", None)
            return self.send_json(result)

        return self.send_json({"error": "no such endpoint"}, status=404)

    def _mirror_watch(self, conn, work_key: str) -> None:
        """Push this film's viewings to the shared store, if sharing is on."""
        if _SHARED_STORE is None:
            return
        from . import watching
        _SHARED_STORE.set_watches(work_key, watching.for_work(conn, work_key))

    def _delete(self):
        parsed = urlparse(self.path)
        watch = WATCH_ID_ROUTE.match(parsed.path)
        if watch:
            from . import watching
            conn = self._write()
            try:
                row = watching.get(conn, watch.group(1))
                if row is None:
                    return self.send_json({"error": "no such viewing"}, status=404)
                watching.remove(conn, watch.group(1))
                self._mirror_watch(conn, row["work_key"])
                detail = queries.work_detail(conn, row["work_key"])
            finally:
                conn.close()
            return self.send_json({"ok": True, "item": detail})

        override = OVERRIDE_ROUTE.match(parsed.path)
        if not override:
            return self.send_json({"error": "no such endpoint"}, status=404)

        from urllib.parse import unquote
        key = unquote(override.group(1))
        field = parse_qs(parsed.query).get("field", [None])[0]
        conn = self._write()
        try:
            removed = db.clear_override(conn, key, field)
            detail = queries.work_detail(conn, key)
        finally:
            conn.close()
        return self.send_json({"cleared": removed, "item": detail})

    # --- handlers --------------------------------------------------------

    def set_overrides(self, key: str):
        """Record edits as overrides. No source's own values are written to."""
        body = self._body()
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("expected a 'fields' object with at least one entry")

        conn = self._write()
        try:
            exists = conn.execute(
                "SELECT 1 FROM work WHERE key = ?", (key,)
            ).fetchone()
            if not exists:
                return self.send_json({"error": "no such film"}, status=404)
            for field, value in fields.items():
                db.set_override(conn, key, field, value)
            detail = queries.work_detail(conn, key)
        finally:
            conn.close()
        return self.send_json({"ok": True, "item": detail})

    def set_flag(self, key: str, field: str):
        """Set or clear a flag. Stored as an override like any other edit, so
        Letterboxd's own value is left exactly as it was exported."""
        body = self._body()
        conn = self._write()
        try:
            detail = queries.work_detail(conn, key)
            if detail is None:
                return self.send_json({"error": "no such film"}, status=404)
            wanted = body.get("value", body.get(field))
            if wanted is None:                      # no value given: toggle
                wanted = not bool(detail["effective"].get(field))
            db.set_override(conn, key, field, 1 if wanted else 0)
            detail = queries.work_detail(conn, key)
        finally:
            conn.close()
        return self.send_json({"ok": True, "field": field,
                               "value": bool(detail["effective"].get(field)),
                               "item": detail})

    def send_artwork(self, size: str, item_id: int):
        full_dir, thumb_dir = artwork.paths(self.database)
        source = (thumb_dir if size == "thumb" else full_dir) / f"{item_id}.jpg"
        if size == "thumb" and not source.exists():
            source = full_dir / f"{item_id}.jpg"   # thumbnail not built yet
        if not source.is_file():
            return self.send_json({"error": "no artwork"}, status=404)
        self.send_body(source.read_bytes(), "image/jpeg",
                       cache="private, max-age=86400")

    def send_poster(self, size: str, work_key: str):
        from . import posters
        full_dir, thumb_dir = posters.poster_dirs(self.database)
        name = f"{posters._safe(work_key)}.jpg"
        source = (thumb_dir if size == "thumb" else full_dir) / name
        if size == "thumb" and not source.exists():
            source = full_dir / name
        if not source.is_file():
            return self.send_json({"error": "no poster"}, status=404)
        self.send_body(source.read_bytes(), "image/jpeg",
                       cache="private, max-age=86400")

    def send_static(self, path: str):
        relative = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_ROOT / relative).resolve()
        if not target.is_file() or WEB_ROOT.resolve() not in target.parents:
            return self.send_json({"error": "not found"}, status=404)
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript", "application/json"
        ):
            content_type += "; charset=utf-8"
        self.send_body(target.read_bytes(), content_type)

    # --- responses -------------------------------------------------------

    def send_json(self, payload, status: int = 200):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_body(body, "application/json; charset=utf-8", status=status)

    def send_body(self, body: bytes, content_type: str, status: int = 200,
                  cache: str = "no-store"):
        headers = {"Content-Type": content_type, "Cache-Control": cache}
        if len(body) >= GZIP_MIN_BYTES and not content_type.startswith("image/") \
                and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


def serve(database: Path, host: str = "127.0.0.1", port: int = 8765,
          open_browser: bool = True) -> None:
    handler = partial(Handler, database=database)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{httpd.server_port}/"
    print(f"MoviesLists running at {url}\nPress Control-C to stop.")
    if open_browser:
        threading.Timer(0.4, lambda: __import__("webbrowser").open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        httpd.server_close()
