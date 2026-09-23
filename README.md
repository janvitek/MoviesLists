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

## One film, two sources

TV.app and Letterboxd share no identifier — TV.app exposes no TMDb or IMDb
id, and knows nothing of Letterboxd's URIs. So the app keeps its own notion of
a film, a **work**, and links each source to it:

```
work  the-grand-budapest-hotel-2014
 ├── tv  →  item.persistent_id   (genre, director, artwork, play count)
 └── lb  →  lb_film.uri          (your rating, review, diary, watchlist)
```

The key is **derived** from the title and year rather than allocated, and that
is the point: a film bought today computes the same key as the Letterboxd
record written three years ago, so the two unify on the next import with
nothing to confirm. `Watchmen (Director's Cut)` and Letterboxd's `Watchmen`
both derive `watchmen-2009`.

Derivation cannot cover everything — TV.app files `12 (2007)` under 2009 while
Letterboxd says 2007 — so a key *decided* to mean an existing work is recorded
as an alias, and that decision holds for every later import. Year drift is
joined only in the unambiguous shape: one work known solely to TV.app, another
solely to Letterboxd, same title, years within two. `Oldboy` 2003 and 2013
stay two films, as do `Damsel` 2018 and 2024.

Both tables keep their source's values verbatim, exactly as `item` does for
TV.app. Nothing is copied between them; the list joins them on read and lays
your own edits on top. The result is one list of every film either source
knows about — including the ones you have watched but do not own.

One subtlety worth recording, since it is not obvious from the export: the
`Letterboxd URI` column means different things in different files.
`ratings.csv`, `watched.csv`, `watchlist.csv` and `likes/films.csv` give the
**film's** URI; `diary.csv` and `reviews.csv` give the URI of the **entry**,
one per viewing. Films and entries are therefore separate tables, and entries
are tied back by title and year like any other source.

### The titles table

Every name any source has used for a film is kept in `work_title`: TV.app's
spelling, Letterboxd's, the alternate title in parentheses, whatever a list
called it. An incoming title is looked up against all of them, so a film found
once under any name is found again under all of them — `A Prophet` and
`Un prophète` reach the same work.

### When it is not sure, it asks

Derivation joins what is certain: one work known solely to TV.app, another
solely to Letterboxd, the same title, years within two. Beyond that the
evidence gets thin — `Damsel` 2018 and 2024 are different films, while
`Batman Returns` filed under 1997 and 1992 is plainly one — so rather than
guess or stay silent, the app raises a question and shows both sides with
enough detail to settle it: director, runtime, genre, rating, other names.

Answers are stored in `link_decision`, keyed by work key rather than row id.
Works are derived and rebuilt whenever either source is re-imported; a
decision is not derived from anything and must outlive that. Keys are stable
because they come from title and year, so a stored answer still names the same
two films after a rebuild, and `rebuild` replays it. Either answer is kept, so
a pair is never raised twice.

## TMDb, the third source

A Letterboxd export carries a title, a year and your own opinions — no
director, genre, runtime, synopsis or image. So a film you have watched but do
not own arrives almost bare, while one from TV.app arrives with all of it.

TMDb fills that in, and is treated as a source like the other two rather than
as decoration: its answers are stored verbatim in `tmdb_film`, nothing is
copied into the other tables, and the read side decides what to prefer. A
film's own sources always win; TMDb only supplies what they left blank.

```sh
# a free key from https://www.themoviedb.org/settings/api
echo YOUR_KEY > ~/.config/movieslists/tmdb.key

movieslists tmdb --status
movieslists tmdb                  # the films TV.app does not have
movieslists tmdb --include-gaps   # and the ones it files under "Unknown"
```

This is the only part of the app that uses the network. It sends a title and a
year, nothing else, and does nothing at all until a key is configured. The key
is read from that file or `TMDB_API_KEY`, never from a command-line flag,
since an argument ends up in shell history.

Director is taken from the crew credits, so a producer or screenwriter is not
mistaken for one, and a film with two directors keeps both. Runtime arrives in
minutes and is stored as seconds like everything else. Every lookup is
recorded including the misses, so a title is searched once and not again, and
TMDb's title and original title are fed back into the titles table — an
original title is exactly what a later import might arrive spelling.

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
| Overrides, reviews, ratings, link decisions | the shared folder | exists nowhere else |

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
