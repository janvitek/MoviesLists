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
GZIP_MIN_BYTES = 1024
ART_ROUTE = re.compile(r"^/art/(thumb|full)/(\d+)\.jpg$")
ITEM_ROUTE = re.compile(r"^/api/item/(\d+)$")
OVERRIDE_ROUTE = re.compile(r"^/api/item/(\d+)/override$")


class Handler(BaseHTTPRequestHandler):
    server_version = "MoviesLists"

    def __init__(self, *args, database: Path, **kwargs):
        self.database = database
        super().__init__(*args, **kwargs)

    # --- plumbing --------------------------------------------------------

    def _read(self) -> sqlite3.Connection:
        conn = sqlite3.connect(f"file:{self.database}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def _write(self) -> sqlite3.Connection:
        return db.connect(self.database)

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

        item = ITEM_ROUTE.match(path)
        if item:
            conn = self._read()
            try:
                detail = queries.item_detail(conn, int(item.group(1)))
            finally:
                conn.close()
            if detail is None:
                return self.send_json({"error": "no such item"}, status=404)
            return self.send_json(detail)

        if path.startswith("/api/"):
            conn = self._read()
            try:
                if path == "/api/library":
                    return self.send_json(
                        queries.library(conn, artwork.have(self.database))
                    )
                if path == "/api/directors":
                    return self.send_json({"directors": queries.directors(conn)})
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
            finally:
                conn.close()
            return self.send_json({"error": "no such endpoint"}, status=404)

        return self.send_static(path)

    def _post(self):
        path = urlparse(self.path).path

        override = OVERRIDE_ROUTE.match(path)
        if override:
            return self.set_overrides(int(override.group(1)))

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

    def _delete(self):
        parsed = urlparse(self.path)
        override = OVERRIDE_ROUTE.match(parsed.path)
        if not override:
            return self.send_json({"error": "no such endpoint"}, status=404)

        field = parse_qs(parsed.query).get("field", [None])[0]
        conn = self._write()
        try:
            row = conn.execute(
                "SELECT persistent_id FROM item WHERE id = ?", (int(override.group(1)),)
            ).fetchone()
            if row is None or not row["persistent_id"]:
                return self.send_json({"error": "no such item"}, status=404)
            removed = db.clear_override(conn, row["persistent_id"], field)
            detail = queries.item_detail(conn, int(override.group(1)))
        finally:
            conn.close()
        return self.send_json({"cleared": removed, "item": detail})

    # --- handlers --------------------------------------------------------

    def set_overrides(self, item_id: int):
        """Record edits as overrides. Imported values are never written to."""
        body = self._body()
        fields = body.get("fields")
        if not isinstance(fields, dict) or not fields:
            raise ValueError("expected a 'fields' object with at least one entry")

        conn = self._write()
        try:
            row = conn.execute(
                "SELECT persistent_id FROM item WHERE id = ?", (item_id,)
            ).fetchone()
            if row is None or not row["persistent_id"]:
                return self.send_json({"error": "no such item"}, status=404)
            for field, value in fields.items():
                db.set_override(conn, row["persistent_id"], field, value)
            detail = queries.item_detail(conn, item_id)
        finally:
            conn.close()
        return self.send_json({"ok": True, "item": detail})

    def send_artwork(self, size: str, item_id: int):
        full_dir, thumb_dir = artwork.paths(self.database)
        source = (thumb_dir if size == "thumb" else full_dir) / f"{item_id}.jpg"
        if size == "thumb" and not source.exists():
            source = full_dir / f"{item_id}.jpg"   # thumbnail not built yet
        if not source.is_file():
            return self.send_json({"error": "no artwork"}, status=404)
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
