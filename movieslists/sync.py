"""Pull the TV.app library through AppleScript/JXA into the SQLite cache."""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from . import db

EXTRACT_JS = Path(__file__).with_name("extract.js")


class SyncError(RuntimeError):
    pass


def export_library(timeout: int = 600) -> dict:
    """Run the JXA exporter and return the parsed payload.

    TV.app must be running or launchable; the first run raises a macOS
    Automation consent prompt, which surfaces here as error -1743.
    """
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "library.json"
        proc = subprocess.run(
            ["osascript", "-l", "JavaScript", str(EXTRACT_JS), str(out)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            stderr = proc.stderr.strip()
            if "-1743" in stderr or "not allowed" in stderr.lower():
                raise SyncError(
                    "macOS denied automation access to TV.app.\n"
                    "Grant it in System Settings > Privacy & Security > "
                    "Automation, then run sync again."
                )
            raise SyncError(f"the TV.app export failed:\n{stderr}")
        if not out.exists():
            raise SyncError("the exporter reported success but wrote no file")
        with open(out, encoding="utf-8") as fh:
            return json.load(fh)


def sync(database: Path) -> dict:
    """Import TV.app into the cache and report what changed."""
    payload = export_library()
    conn = db.connect(database)
    try:
        result = db.load(conn, payload)
    finally:
        conn.close()
    result["elapsed_ms"] = payload.get("elapsedMs")
    result["unavailable_fields"] = payload.get("unavailableFields") or {}
    return result
