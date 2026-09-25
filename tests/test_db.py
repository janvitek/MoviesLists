"""Tests for the database layer."""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from movieslists import db


@pytest.fixture
def database(tmp_path):
    return tmp_path / "test.sqlite3"


@pytest.fixture
def conn(database):
    c = db.connect(database)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestConnect:
    def test_creates_tables(self, conn):
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "item" in tables
        assert "work" in tables
        assert "work_override" in tables
        assert "tmdb_film" in tables
        assert "watch_log" in tables

    def test_schema_version(self, conn):
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION

    def test_reconnect_same_version_skips_writes(self, database):
        conn1 = db.connect(database)
        conn1.close()
        # Second connect should not need write lock.
        conn2 = db.connect(database)
        version = conn2.execute("PRAGMA user_version").fetchone()[0]
        assert version == db.SCHEMA_VERSION
        conn2.close()

    def test_new_tables_created_on_reconnect(self, database):
        conn1 = db.connect(database)
        conn1.close()
        conn2 = db.connect(database)
        tables = {r[0] for r in conn2.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "tmdb_show" in tables
        assert "tmdb_title" in tables
        conn2.close()


class TestNormalize:
    def test_basic(self):
        row = db.normalize({"databaseID": 1, "name": "Test Film", "year": 2020})
        assert row["id"] == 1
        assert row["name"] == "Test Film"
        assert row["year"] == 2020

    def test_empty_string_to_none(self):
        row = db.normalize({"databaseID": 1, "name": "Test", "director": ""})
        assert row["director"] is None

    def test_untitled(self):
        row = db.normalize({"databaseID": 1, "name": None})
        assert row["name"] == "(untitled)"

    def test_boolean_keys(self):
        row = db.normalize({"databaseID": 1, "name": "X", "bookmarkable": True})
        assert row["bookmarkable"] == 1


class TestDirectors:
    def test_split(self):
        assert db.split_directors("Joel Coen & Ethan Coen") == ["Joel Coen", "Ethan Coen"]

    def test_split_comma(self):
        assert db.split_directors("A, B & C") == ["A", "B", "C"]

    def test_unknown_filtered(self):
        assert db.split_directors("Unknown") == []
        assert db.split_directors("N/A") == []

    def test_none(self):
        assert db.split_directors(None) == []

    def test_credited_episode(self):
        raw = {"director": "Breaking Bad", "show": "Breaking Bad", "mediaKind": "TV show"}
        assert db.credited_directors(raw) == []

    def test_credited_movie(self):
        raw = {"director": "Spielberg", "show": None, "mediaKind": "movie"}
        assert db.credited_directors(raw) == ["Spielberg"]


class TestOverrides:
    def test_set_and_read(self, conn):
        conn.execute("INSERT INTO work (key, title, year, created_at) "
                     "VALUES ('test-2020', 'Test', 2020, '2024-01-01')")
        db.set_override(conn, "test-2020", "genre", "Comedy")
        result = db.overrides_for(conn, "test-2020")
        assert result["genre"] == "Comedy"

    def test_clear(self, conn):
        conn.execute("INSERT INTO work (key, title, year, created_at) "
                     "VALUES ('test-2020', 'Test', 2020, '2024-01-01')")
        db.set_override(conn, "test-2020", "genre", "Comedy")
        db.clear_override(conn, "test-2020", "genre")
        assert db.overrides_for(conn, "test-2020") == {}

    def test_invalid_field_rejected(self, conn):
        with pytest.raises(ValueError):
            db.set_override(conn, "x", "nonexistent_field", "value")


class TestGenres:
    def test_canonical(self):
        assert db.canonical_genre("sci-fi & fantasy") == "Science Fiction"
        assert db.canonical_genre("Comedy") == "Comedy"
        assert db.canonical_genre("comedy") == "Comedy"

    def test_check_valid(self):
        assert db.check_genre("Comedy") == "Comedy"
        assert db.check_genre("sci-fi") == "Science Fiction"

    def test_check_invalid(self):
        with pytest.raises(ValueError):
            db.check_genre("NotAGenre")

    def test_check_none(self):
        assert db.check_genre(None) is None
        assert db.check_genre("") is None


class TestCoerce:
    def test_integer(self):
        assert db.coerce("rating", "80") == 80

    def test_real(self):
        assert db.coerce("duration", "120.5") == 120.5

    def test_text(self):
        assert db.coerce("name", "hello") == "hello"

    def test_none(self):
        assert db.coerce("rating", None) is None

    def test_letterboxd_flag(self):
        assert db.coerce("liked", "0") == 0
        assert db.coerce("liked", "1") == 1


class TestDiff:
    def test_added(self):
        changes = db.diff([], [{"persistent_id": "a", "name": "New", "media_kind": "movie"}])
        assert len(changes) == 1
        assert changes[0]["change_type"] == "added"

    def test_removed(self):
        changes = db.diff(
            [{"persistent_id": "a", "name": "Gone", "media_kind": "movie"}], [])
        assert len(changes) == 1
        assert changes[0]["change_type"] == "removed"
        assert changes[0]["severity"] == "alert"

    def test_modified(self):
        old = [{"persistent_id": "a", "id": 1, "name": "Film", "year": 2020,
                "media_kind": "movie"}]
        new = [{"persistent_id": "a", "id": 1, "name": "Film", "year": 2021,
                "media_kind": "movie"}]
        changes = db.diff(old, new)
        modified = [c for c in changes if c["change_type"] == "modified"]
        assert any(c["field"] == "year" for c in modified)

    def test_noisy_fields_ignored(self):
        old = [{"persistent_id": "a", "id": 1, "name": "Film",
                "position": 1, "media_kind": "movie"}]
        new = [{"persistent_id": "a", "id": 1, "name": "Film",
                "position": 99, "media_kind": "movie"}]
        changes = db.diff(old, new)
        assert len(changes) == 0
