# MoviesLists

Browse your Apple TV.app library in the browser: movies, shows, directors,
genres and play dates, with your own edits layered on top of the import.

Everything is local. Nothing is uploaded, and there are no third-party
dependencies — Python's standard library, AppleScript/JXA, and `sips`.

## Quick start

```sh
python3 -m movieslists serve
```

That imports the library, reports anything that changed since last time, and
opens the UI at <http://127.0.0.1:8765/>.

Cover art is fetched separately, because it is slow (roughly five items a
second):

```sh
python3 -m movieslists artwork          # every movie + one image per show
```

## Commands

| Command | What it does |
| --- | --- |
| `serve` | Import, report changes, then serve the UI. `--no-sync` to skip the import |
| `sync` | Import only |
| `stats` | Print a summary of the cache |
| `artwork` | Download cover art. `--scope all`, `--limit N`, `--shrink`, `--status`, `--clear` |

Data lives in `~/.local/share/movieslists/` — `library.sqlite3` plus an
`artwork/` cache. Change it with `--database`.

## How it works

`extract.js` asks TV.app for every scriptable property of every track through
a single Apple Event per property (`tracks.genre()` returns the whole column),
which exports ~7,000 items in about four seconds; fetching per track instead
would take many minutes. The JSON lands in SQLite.

### Nothing is thrown away

All 53 properties TV.app exposes are stored verbatim, including the odd ones.
Tidying is applied when reading, never when writing, so the table always holds
what TV.app actually said:

- **Every TV episode lists its own show as the `director`.** Stored as given;
  episodes credit nobody in the UI. Director only means something for movies.
- **0 stands in for "absent"** in season, episode and year. Stored as 0,
  read as null.
- **Co-directors arrive as one string** — `"Joel Coen & Ethan Coen"` — and are
  split so a film is reachable from either name.

### Your edits never overwrite the import

An edit is stored as an *override*: a row in `item_override` keyed by the
track's persistent ID, holding one field. The imported value stays untouched
in `item`, and the panel shows both. Overrides survive re-imports — the table
has no foreign key to `item` precisely so that replacing the library on each
sync cannot delete your work. Every one of the 53 fields is editable.

Two fields are yours alone:

- **Star ratings.** TV.app reports 0 for every item in this library, so the
  stars exist only as overrides. Click them straight from the list; clicking
  the star you are already on clears the rating.
- **Reviews**, written in Markdown, with a live preview while you type.
  TV.app has no such field at all, so a review is stored in the same override
  table rather than shadowing anything. Headings, bold, italic, lists,
  blockquotes, code and links are supported; the text is escaped before any
  markup is applied and links are restricted to `http(s)`.

### Imports are compared

Each import is diffed against the previous one, field by field, and the result
is recorded in `library_change`. Additions and edits are reported quietly.
**A disappearance is a red alert**, called out separately for movies, since
losing a film is the one change worth interrupting you for. Playlist position
and playback scrub position are ignored as noise.

### Play dates are mostly missing

TV.app keeps only the *most recent* play, and often not even that: of 5,957
items marked played, 942 carry a date. Where no date exists the list shows the
date the item was added instead, in italics, so a derived date is never
mistaken for a real viewing.

## Layout

```
movieslists/
  extract.js           bulk property export from TV.app (JXA)
  artwork.applescript  per-track cover art extraction
  db.py                schema, normalisation, overrides, diffing
  queries.py           read-side shaping: overrides applied, sentinels cleaned
  sync.py              runs the exporter, loads the cache
  artwork.py           art extraction, downscaling, thumbnails
  server.py            stdlib HTTP server and JSON API
  cli.py               command line
  web/                 the UI
```

## API

| Route | Purpose |
| --- | --- |
| `GET /api/library` | Every item, plus facets, stats and pending changes |
| `GET /api/item/<id>` | One item: imported, overrides and effective, kept apart |
| `POST /api/item/<id>/override` | `{"fields": {...}}` — record edits |
| `DELETE /api/item/<id>/override` | Drop edits; `?field=` for just one |
| `GET /api/changes` | What the last imports changed |
| `POST /api/sync` | Re-import |
| `GET /art/{thumb,full}/<id>.jpg` | Cover art |

## Requirements

macOS with TV.app and Python 3.9+. The first run raises a macOS Automation
prompt for TV.app; allow it. Reading TV.app's own SQLite database directly
would need Full Disk Access, but nothing here does that — the AppleScript
interface is enough.
