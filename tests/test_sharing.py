"""Tests for the shared store."""

import sqlite3

import pytest

from movieslists import db
from movieslists.sharing import SharedStore


@pytest.fixture
def store(tmp_path):
    return SharedStore(tmp_path / "shared")


class TestSharedStore:
    def test_stamp_and_read(self, store):
        store.stamp("film-key", "rating", "80", "manual")
        fields = store.read("film-key")
        assert fields["rating"]["value"] == "80"
        assert fields["rating"]["source"] == "manual"

    def test_overwrite(self, store):
        store.stamp("film-key", "rating", "80", "manual")
        store.stamp("film-key", "rating", "60", "manual")
        fields = store.read("film-key")
        assert fields["rating"]["value"] == "60"

    def test_multiple_fields(self, store):
        store.stamp("film-key", "rating", "80", "manual")
        store.stamp("film-key", "genre", "Comedy", "manual")
        fields = store.read("film-key")
        assert len(fields) == 2

    def test_forget(self, store):
        store.stamp("film-key", "rating", "80", "manual")
        store.forget("film-key", "rating")
        fields = store.read("film-key")
        assert fields["rating"]["deleted"] is True
        assert fields["rating"]["value"] is None

    def test_read_all(self, store):
        store.stamp("a", "rating", "80", "manual")
        store.stamp("b", "genre", "Drama", "manual")
        all_data = store.read_all()
        assert len(all_data) == 2

    def test_read_missing(self, store):
        assert store.read("nonexistent") == {}

    def test_watches(self, store):
        watches = [{"id": "abc123", "work_key": "film", "watched_date": "2024-01-01"}]
        store.set_watches("film", watches)
        assert store.watches("film") == watches

    def test_all_watches(self, store):
        store.set_watches("a", [{"id": "1", "watched_date": "2024-01-01"}])
        store.set_watches("b", [{"id": "2", "watched_date": "2024-02-01"}])
        all_w = store.all_watches()
        assert len(all_w) == 2

    def test_conflicts_empty(self, store):
        store.root.mkdir(parents=True, exist_ok=True)
        assert store.conflicts() == []
