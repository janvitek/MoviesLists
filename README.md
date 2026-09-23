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

### Filtering and sorting

A three-way toggle switches between **All**, **Played** and **Unplayed**, each
labelled with how many items it would show under whatever other filters are
active. "Played" means a play count above zero *or* a recorded play date —
either signal counts, since TV.app supplies them inconsistently. Choosing
Played reveals one refinement, because the distinction matters here: whether
a date was actually recorded.

Sorting is a chain, not a single key. Click a column to sort by it, click
again to flip direction, shift-click another to add it as the next level —
so *play count, then year, then genre* is three clicks. The chain is shown as
numbered chips that can be flipped or removed, and levels that are not columns
(date added, for instance) can be added from the dropdown. Missing values sink
to the bottom whichever way a level points, so "fewest plays first" never
means "blanks first". Each view keeps its own default: films per director for
directors, title everywhere else.

### Play dates are mostly missing

TV.app keeps only the *most recent* play, and often not even that: of 5,957
items marked played, 942 carry a date. Where no date exists the list shows the
date the item was added instead, in italics, so a derived date is never
mistaken for a real viewing.

## Two machines, one shared folder

Edits are shared across machines; the cached library is not. That split is
deliberate, and the reason is worth stating plainly: **a SQLite database must
not live in a synced folder.** SQLite coordinates writers with POSIX advisory
locks, which do not cross machines, and a sync service can copy a database
mid-write or carry a WAL file out of step with its main file. The result is a
corrupt database rather than a lost edit. A lock file cannot rescue that
either, since the lock travels by the same sync service as the data.

So the two kinds of data live in different places:

| | Where | Why |
| --- | --- | --- |
| `item`, `sync_run`, `library_change` | local, `~/.local/share/movieslists/` | derived from *this* machine's TV.app, and regenerated on every start |
| Overrides, reviews, ratings, Letterboxd log | the shared folder | exists nowhere else |

The shared side is a directory of small JSON documents, one per film, each
field stamped with when it changed. That shape buys three things a shared
database cannot:

- two machines editing **different fields of the same film** both keep their
  work, because the merge is per field rather than per file;
- a sync collision damages one film, not the library;
- everything is plain text, so a bad merge can be read and repaired by hand.

Removals are recorded as tombstones rather than simply vanishing — otherwise
the next merge would see the field still present on the other machine and
helpfully restore what you deleted.

```sh
movieslists config --shared-dir ~/Dropbox/MoviesLists   # once per machine
movieslists share                                       # merge on demand
movieslists doctor                                      # check for trouble
```

`serve` merges on startup, so ordinarily you never run `share` yourself.

### About the lock

There is a lock file, and it is a **courtesy, not a guarantee**. It serialises
the ordinary case — two machines you are actually using — and leaves a record
of who was writing when something goes wrong. It cannot do more than that: the
lock travels by the same sync service as the data, so two machines can briefly
hold it at once, and a machine that is offline sees no lock at all. A lock held
for more than two minutes is presumed abandoned and taken over.

The real safety is the store's shape. A lost race costs one field, not the
library.

`movieslists doctor` checks the things a synced folder gets wrong: a database
that has wandered into the synced folder, sync-conflict files
(`… (conflicted copy).json`), a stale lock, and SQLite integrity.

## Snapshots

```sh
movieslists snapshot                    # take one now
movieslists snapshot --list
movieslists snapshot --restore NAME
```

A snapshot holds the shared edits plus a copy of the local database, taken
through SQLite's backup API rather than a file copy, so it is safe to take
while the app is running and can never capture a half-written page. `serve`
takes one on startup, before this machine changes anything.

Retention is tiered rather than a flat count: every snapshot from the last 24,
then one per day for 30 days, then one per week for a year. The oldest
snapshot is never pruned.

Restoring writes the edits back into the shared store; the cached library is
left alone unless you pass `--restore-database`, since it is rebuilt from
TV.app anyway.

## Letterboxd

Letterboxd's API is invite-only and explicitly closed to personal projects, so
the route is the account data export from
<https://letterboxd.com/user/exportdata/>. Nothing is sent anywhere — you
download the ZIP, this reads it locally.

```sh
movieslists letterboxd ~/Downloads/letterboxd-export.zip --dry-run
movieslists letterboxd ~/Downloads/letterboxd-export.zip
movieslists letterboxd --queue
```

Reviews, ratings and watch dates are imported. Ratings convert exactly:
Letterboxd's half-star scale is twenty points a star on TV.app's 0–100 scale,
so 3.5 stars is 70.

Letterboxd stores reviews as HTML, and this app stores Markdown, so reviews
are converted on the way in: `<b>`/`<i>`/`<em>`/`<strong>`, `<a href>`,
`<blockquote>` and `<br>` all have Markdown equivalents. Two details that
real exports turn out to need — the editor emits attributes
(`<i style="-webkit-text-size-adjust: 100%;">`), and an unclosed `<i>` should
still produce balanced emphasis rather than a stray tag. Tags outside that
set are left as literal text, because a review mentioning `<expletive>` in
angle brackets means the word.

**Matching is the hard part.** A Letterboxd export identifies films by
LetterboxdURI, tmdbID and imdbID; TV.app exposes none of the three, so title
and year are the only common ground. Titles are folded to compare —
accents stripped, and edition suffixes removed, so `Watchmen (Director's Cut)`
matches `Watchmen` and `Sucker Punch (Extended Cut) (2011)` matches
`Sucker Punch`. A title unique in the library may drift two years; a title
shared by several films (`Oldboy` 2003 and 2013) may not.

Anything short of a confident match is **queued rather than applied**, and
`--queue` lists each one with its candidates. Watch dates only fill films
TV.app never dated, unless you pass `--dates prefer-letterboxd`. An edit you
made by hand is never overwritten by an import unless you pass `--force`.

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
  letterboxd.py        Letterboxd export parsing and matching
  sharing.py           shared store, cross-machine merge, locking
  snapshots.py         snapshots, retention, restore
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
