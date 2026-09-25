"""Tests for the portable bundle export/import."""

import sqlite3

import pytest

from movieslists import db
from movieslists.portable import export_bundle, import_bundle


@pytest.fixture
def database(tmp_path):
    return tmp_path / "test.sqlite3"


@pytest.fixture
def conn(database):
    c = db.connect(database)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


@pytest.fixture
def shared(tmp_path):
    d = tmp_path / "shared"
    d.mkdir()
    return d


class TestExportImport:
    def test_roundtrip(self, conn, shared):
        conn.execute(
            "INSERT INTO lb_film (uri, name, year, imported_at) "
            "VALUES ('uri-1', 'Film A', 2020, '2024-01-01')")
        conn.execute(
            "INSERT INTO lb_film (uri, name, year, imported_at) "
            "VALUES ('uri-2', 'Film B', 2021, '2024-01-01')")
        conn.commit()

        path = export_bundle(conn, shared)
        assert path.exists()
        assert path.suffix == ".gz"

        # Clear local data.
        conn.execute("DELETE FROM lb_film")
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM lb_film").fetchone()[0] == 0

        # Import should restore it.
        result = import_bundle(conn, shared, force=True)
        assert result is not None
        assert result["tables"]["lb_film"] == 2
        assert conn.execute("SELECT COUNT(*) FROM lb_film").fetchone()[0] == 2

    def test_skip_when_up_to_date(self, conn, shared):
        export_bundle(conn, shared)
        result = import_bundle(conn, shared)
        assert result is None  # Already up to date.

    def test_force_reimport(self, conn, shared):
        export_bundle(conn, shared)
        result = import_bundle(conn, shared, force=True)
        assert result is not None

    def test_empty_tables(self, conn, shared):
        path = export_bundle(conn, shared)
        assert path.exists()
        result = import_bundle(conn, shared, force=True)
        assert result is not None

    def test_missing_bundle(self, conn, shared):
        result = import_bundle(conn, shared)
        assert result is None
