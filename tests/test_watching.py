"""Tests for the watch log."""

import sqlite3

import pytest

from movieslists import db
from movieslists.watching import log, update, remove, get, for_work, counts
from movieslists.works import resolve


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.sqlite3")
    c.row_factory = sqlite3.Row
    # Need a work to log against.
    resolve(c, "Test Film", 2020)
    c.commit()
    yield c
    c.close()


@pytest.fixture
def work_key(conn):
    return conn.execute("SELECT key FROM work LIMIT 1").fetchone()[0]


class TestLog:
    def test_log_viewing(self, conn, work_key):
        row = log(conn, work_key, watched_date="2024-06-15")
        assert row["work_key"] == work_key
        assert row["watched_date"] == "2024-06-15"
        assert row["deleted"] == 0

    def test_log_with_rating(self, conn, work_key):
        row = log(conn, work_key, rating=80, note="Great!")
        assert row["rating"] == 80
        assert row["note"] == "Great!"

    def test_invalid_work(self, conn):
        with pytest.raises(ValueError):
            log(conn, "nonexistent-key")


class TestUpdate:
    def test_update_rating(self, conn, work_key):
        row = log(conn, work_key)
        updated = update(conn, row["id"], rating=60)
        assert updated["rating"] == 60

    def test_update_nonexistent(self, conn):
        result = update(conn, "0" * 32)
        assert result is None


class TestRemove:
    def test_tombstone(self, conn, work_key):
        row = log(conn, work_key)
        assert remove(conn, row["id"]) is True
        got = get(conn, row["id"])
        assert got["deleted"] == 1

    def test_excluded_from_for_work(self, conn, work_key):
        row = log(conn, work_key)
        remove(conn, row["id"])
        assert for_work(conn, work_key) == []


class TestCounts:
    def test_counts(self, conn, work_key):
        log(conn, work_key, watched_date="2024-01-01")
        log(conn, work_key, watched_date="2024-06-01")
        result = counts(conn)
        assert result[work_key]["count"] == 2
        assert result[work_key]["last"] == "2024-06-01"
