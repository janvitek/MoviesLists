"""Tests for the work graph."""

import sqlite3

import pytest

from movieslists import db
from movieslists.works import make_key, find, resolve, link, merge, rebuild
from movieslists.letterboxd import normalize_title, title_keys


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite3")
    c.row_factory = sqlite3.Row
    yield c
    c.close()


class TestMakeKey:
    def test_basic(self):
        assert make_key("The Grand Budapest Hotel", 2014) == "the-grand-budapest-hotel-2014"

    def test_no_year(self):
        assert make_key("Untitled", None) == "untitled-unknown"

    def test_special_chars(self):
        key = make_key("Amélie", 2001)
        assert "amelie" in key


class TestTitleKeys:
    def test_basic(self):
        keys = title_keys("A Prophet (Un prophète)")
        assert "a prophet" in keys
        assert "un prophete" in keys

    def test_edition_stripped(self):
        keys = title_keys("Blade Runner (Final Cut)")
        assert "blade runner" in keys
        assert "final cut" not in keys

    def test_year_stripped(self):
        keys = title_keys("Mary Queen of Scots (2018)")
        assert normalize_title("Mary Queen of Scots (2018)") == "mary queen of scots"


class TestResolve:
    def test_creates_work(self, conn):
        work_id = resolve(conn, "Test Film", 2020)
        assert work_id is not None
        row = conn.execute("SELECT * FROM work WHERE id = ?", (work_id,)).fetchone()
        assert row["title"] == "Test Film"
        assert row["year"] == 2020

    def test_finds_existing(self, conn):
        id1 = resolve(conn, "Test Film", 2020)
        id2 = resolve(conn, "Test Film", 2020)
        assert id1 == id2

    def test_different_year_different_work(self, conn):
        id1 = resolve(conn, "Test", 2020)
        id2 = resolve(conn, "Test", 1990)
        assert id1 != id2


class TestLink:
    def test_link_source(self, conn):
        work_id = resolve(conn, "Film", 2020)
        link(conn, work_id, "tv", "persistent-123")
        row = conn.execute(
            "SELECT * FROM work_source WHERE source = 'tv' AND source_id = 'persistent-123'"
        ).fetchone()
        assert row["work_id"] == work_id


class TestMerge:
    def test_merge_works(self, conn):
        a = resolve(conn, "Film A", 2020, "tv")
        b = resolve(conn, "Film B", 2020, "lb")
        link(conn, a, "tv", "tv-1")
        link(conn, b, "lb", "lb-1")
        merge(conn, a, b, "test")
        # b should be gone.
        assert conn.execute("SELECT 1 FROM work WHERE id = ?", (b,)).fetchone() is None
        # b's source should point to a.
        row = conn.execute(
            "SELECT work_id FROM work_source WHERE source = 'lb'"
        ).fetchone()
        assert row["work_id"] == a


class TestRebuild:
    def test_rebuild_from_items(self, conn):
        # Insert some items.
        for i, name in enumerate(["Film A", "Film B"], start=1):
            conn.execute(
                "INSERT INTO item (id, persistent_id, name, year, media_kind) "
                "VALUES (?, ?, ?, 2020, 'movie')", (i, f"pid-{i}", name))
        conn.commit()
        counts = rebuild(conn)
        assert counts["tv"] == 2
        assert counts["created"] == 2

    def test_rebuild_idempotent(self, conn):
        conn.execute(
            "INSERT INTO item (id, persistent_id, name, year, media_kind) "
            "VALUES (1, 'pid-1', 'Film', 2020, 'movie')")
        conn.commit()
        rebuild(conn)
        counts2 = rebuild(conn)
        assert counts2["tv"] == 1
        assert conn.execute("SELECT COUNT(*) FROM work").fetchone()[0] == 1
