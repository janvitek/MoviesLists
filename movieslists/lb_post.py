"""Post a diary entry or review to Letterboxd.

Uses your Chrome session cookies — no headless browser, no CAPTCHA.
Log in to Letterboxd in Chrome first; the cookies are read directly.

Usage:
    from movieslists.lb_post import post_review, search_film

    # Find the film.
    results = search_film("Dune Part Two")
    film = results[0]   # {'uid': 'film:617443', 'name': 'Dune: Part Two', ...}

    # Post a diary entry with a review.
    post_review(film["uid"], rating=4.0, review="Spectacular.",
                date="2024-03-10")
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://letterboxd.com"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/131.0.0.0 Safari/537.36")
VALID_RATINGS = {r / 2 for r in range(1, 11)}


def _cookies() -> tuple[str, str]:
    """Read Letterboxd cookies from Chrome. Returns (cookie_header, csrf)."""
    try:
        import rookiepy
    except ImportError:
        raise RuntimeError("pip install rookiepy")
    cookies = rookiepy.chrome(domains=[".letterboxd.com", "letterboxd.com"])
    if not cookies:
        raise RuntimeError(
            "No Letterboxd cookies in Chrome. Log in at letterboxd.com first.")
    header = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
    csrf = next((c["value"] for c in cookies
                 if c["name"] == "com.xk72.webparts.csrf"), None)
    if not csrf:
        raise RuntimeError("CSRF cookie not found — are you logged in?")
    return header, csrf


def _request(url: str, cookie: str, data: dict | None = None) -> dict:
    """Make a request to Letterboxd, returning parsed JSON."""
    headers = {
        "Cookie": cookie,
        "User-Agent": USER_AGENT,
        "X-Requested-With": "XMLHttpRequest",
        "Referer": BASE + "/",
        "Origin": BASE,
    }
    body = None
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def search_film(query: str, limit: int = 5) -> list[dict]:
    """Search Letterboxd's film index. Returns a list of matches."""
    cookie, _ = _cookies()
    url = (f"{BASE}/s/autocompletefilm?"
           f"q={urllib.parse.quote(query)}&limit={limit}")
    result = _request(url, cookie)
    return [
        {
            "uid": r["uid"],
            "name": r["name"],
            "year": r.get("releaseYear"),
            "slug": r.get("slug"),
            "directors": [d["name"] for d in r.get("directors") or []],
        }
        for r in result.get("data", [])
        if r.get("type") == "film"
    ]


def post_review(
    film_uid: str,
    rating: float | None = None,
    review: str | None = None,
    date: str | None = None,
    liked: bool = False,
    rewatch: bool = False,
    contains_spoilers: bool = False,
    tags: str | None = None,
) -> dict:
    """Post a diary entry to Letterboxd.

    Args:
        film_uid:   The film's UID from search_film(), e.g. "film:617443".
        rating:     Half-star rating (0.5–5.0), or None to skip.
        review:     Review text, or None.
        date:       Watched date as YYYY-MM-DD, or None to skip diary.
        liked:      Mark the film as liked.
        rewatch:    Mark as a rewatch.
        contains_spoilers: Flag the review as containing spoilers.
        tags:       Comma-separated tags, or None.

    Returns:
        The JSON response from Letterboxd.
    """
    if rating is not None and rating not in VALID_RATINGS:
        raise ValueError(f"rating must be a half-star (0.5–5.0), got {rating}")
    if not film_uid.startswith("film:"):
        raise ValueError(f"expected a film UID like 'film:12345', got {film_uid!r}")

    cookie, csrf = _cookies()

    form: dict[str, str] = {
        "__csrf": csrf,
        "viewingableUid": film_uid,
        "viewingId": "",
    }
    if date:
        form["specifiedDate"] = "true"
        form["viewingDateStr"] = date
    if rating is not None:
        form["rating"] = str(int(rating * 2))
    if review:
        form["review"] = review
    if liked:
        form["liked"] = "true"
    if rewatch:
        form["rewatch"] = "true"
    if contains_spoilers:
        form["containsSpoilers"] = "true"
    if tags:
        form["tags"] = tags

    return _request(f"{BASE}/s/save-diary-entry", cookie, form)
