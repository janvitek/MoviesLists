"""Tests for Letterboxd import and matching."""

from movieslists.letterboxd import (
    normalize_title, title_keys, html_to_markdown, stars_to_tv,
)


class TestNormalizeTitle:
    def test_basic(self):
        assert normalize_title("The Grand Budapest Hotel") == "the grand budapest hotel"

    def test_edition_stripped(self):
        assert normalize_title("Blade Runner (Final Cut)") == "blade runner"

    def test_year_stripped(self):
        assert normalize_title("Film (2018)") == "film"

    def test_alternate_kept(self):
        assert normalize_title("A Prophet (Un prophète)") == "a prophet un prophete"

    def test_ampersand(self):
        assert normalize_title("Thelma & Louise") == "thelma and louise"

    def test_accents(self):
        assert normalize_title("Amélie") == "amelie"


class TestTitleKeys:
    def test_alternate_title(self):
        keys = title_keys("A Prophet (Un prophète)")
        assert "a prophet" in keys
        assert "un prophete" in keys

    def test_edition_not_a_key(self):
        keys = title_keys("Blade Runner (Final Cut)")
        assert "final cut" not in keys

    def test_short_fragment_ignored(self):
        keys = title_keys("Film (AB)")
        assert "ab" not in keys


class TestHtmlToMarkdown:
    def test_basic_tags(self):
        assert html_to_markdown("<b>bold</b>") == "**bold**"
        assert html_to_markdown("<i>italic</i>") == "*italic*"

    def test_br(self):
        result = html_to_markdown("line1<br>line2")
        assert "line1\nline2" in result

    def test_link(self):
        result = html_to_markdown('<a href="http://example.com">text</a>')
        assert "[text](http://example.com)" in result

    def test_none(self):
        assert html_to_markdown(None) is None
        assert html_to_markdown("") == ""


class TestStarsToTv:
    def test_conversion(self):
        assert stars_to_tv(5.0) == 100
        assert stars_to_tv(2.5) == 50
        assert stars_to_tv(0.5) == 10

    def test_none(self):
        assert stars_to_tv(None) is None
