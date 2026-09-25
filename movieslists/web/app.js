'use strict';

// ---------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);
// Read from the stylesheet rather than repeated here: the virtual list
// positions rows by this number, so a value that disagreed with the CSS
// would misplace every row on screen.
const ROW_HEIGHT = Number.parseInt(
  getComputedStyle(document.documentElement).getPropertyValue('--row-height'), 10
) || 46;
const OVERSCAN = 6;

const dateFmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
const longDateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'short' });

// TV.app hands back UTC timestamps; render them in the viewer's zone.
const parseDate = (s) => (s ? new Date(s) : null);
const showDate = (s) => { const d = parseDate(s); return d && !isNaN(d) ? dateFmt.format(d) : null; };

const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

// Irregulars this app actually uses; everything else takes a plain "s".
const PLURALS = { entry: 'entries', copy: 'copies' };
const plural = (n, word) => `${Number(n).toLocaleString()} `
  + (n === 1 ? word : (PLURALS[word] || `${word}s`));

const duration = (seconds) => {
  if (!seconds) return null;
  const total = Math.round(seconds / 60);
  const h = Math.floor(total / 60);
  return h ? `${h}h ${total % 60}m` : `${total}m`;
};

// Mirrors split_directors() in db.py: "Joel Coen & Ethan Coen" is two people.
const splitDirectors = (v) => (v ? v.split(/\s*(?:,|&)\s*/).map((s) => s.trim()).filter(Boolean) : []);

// Every field the list can be ordered by. `get` pulls the value actually
// shown, so sorting agrees with what is on screen -- the play-date sort uses
// the same added-date fallback the column displays.
const SORT_FIELDS = {
  name:         { label: 'Title',       type: 'text',   get: (r) => r.name },
  director:     { label: 'Director',    type: 'text',   get: (r) => r.director },
  show:         { label: 'Show',        type: 'text',   get: (r) => r.show },
  year:         { label: 'Year',        type: 'number', get: (r) => r.year },
  genre:        { label: 'Genre',       type: 'text',   get: (r) => r.genre },
  played_count: { label: 'Plays',       type: 'number', get: (r) => r.played_count || 0 },
  played_date:  { label: 'Last played', type: 'text',   get: (r) => r.played_date || r.date_added },
  rating:       { label: 'Rating',      type: 'number', get: (r) => r.rating || 0 },
  date_added:   { label: 'Date added',  type: 'text',   get: (r) => r.date_added },
  episodes:     { label: 'Episodes',    type: 'number', get: (r) => r.episodes },
  seasons:      { label: 'Seasons',     type: 'number', get: (r) => r.seasons },
  watched:      { label: 'Watched',     type: 'number', get: (r) => r.watched },
};

// Fields that read better largest-first when you first click them.
const DESC_FIRST = new Set([
  'played_count', 'played_date', 'rating', 'date_added',
  'episodes', 'seasons', 'watched',
]);

const nullsLast = (get, cmp) => (a, b) => {
  const x = get(a), y = get(b);
  if (x == null && y == null) return 0;
  if (x == null) return 1;
  if (y == null) return -1;
  return cmp(x, y);
};
const byText = (get) => nullsLast(get, (x, y) => String(x).localeCompare(String(y), undefined, { sensitivity: 'base' }));
const byNumber = (get, dir = 1) => nullsLast(get, (x, y) => (x - y) * dir);

// Chain of {field, dir}: earlier levels win, later ones break ties. Missing
// values sink to the bottom whichever way the level is pointing, so "worst
// first" never means "empty first".
function chainComparator(chain) {
  const levels = chain
    .map(({ field, dir }) => {
      const spec = SORT_FIELDS[field];
      if (!spec) return null;
      const sign = dir === 'desc' ? -1 : 1;
      const compare = spec.type === 'number'
        ? (x, y) => (x - y) * sign
        : (x, y) => String(x).localeCompare(String(y), undefined, { sensitivity: 'base' }) * sign;
      return nullsLast(spec.get, compare);
    })
    .filter(Boolean);
  return (a, b) => {
    for (const level of levels) {
      const result = level(a, b);
      if (result) return result;
    }
    return 0;
  };
}

// ------------------------------------------------------------------- state

const state = {
  items: [], episodes: [], byKey: new Map(), shows: [],
  stats: null, editableFields: [],
  view: 'movies', search: '', genre: '', decade: '',
  // One scope control: All, Library, Played, Unplayed. The last three are
  // all about films you actually own, so each of them excludes titles that
  // exist only on Letterboxd.
  scope: '', playedDetail: 'any', facetView: null,
  scopeCounts: { all: 0, library: 0, played: 0, unplayed: 0 },
  sortChain: [],
  visible: [], selectedKey: null, detail: null, dirty: new Map(),
};

// ------------------------------------------------------------------- views

function safeName(name) {
  return [...name].map(c => /[\w-]/.test(c) ? c : '_').join('').slice(0, 180);
}

// TV.app supplies 16:9 stills; a film it does not have falls back to a
// portrait poster, so the cell holds either shape.
function artCell(row) {
  const src = row.has_art ? `art/thumb/${row.tv_id}.jpg`
    : row.has_poster ? `art/poster/thumb/${encodeURIComponent(row.key)}.jpg`
    : null;
  if (!src) {
    return '<div class="cell"><div class="art art--empty" role="img" aria-label="no artwork"></div></div>';
  }
  const shape = row.has_art ? 'art' : 'art art--poster';
  return `<div class="cell"><img class="${shape}" src="${src}" alt="" `
    + `decoding="async" onerror="this.className='art art--empty'"></div>`;
}

function showRatingCell(s) {
  if (s.userRating) {
    const stars = (s.userRating / 20).toString().replace(/\.0$/, '');
    return `<div class="cell num">${stars}\u2605</div>`;
  }
  if (s.tmdbRating) {
    return `<div class="cell num muted" title="TMDb average">${Number(s.tmdbRating).toFixed(1)}</div>`;
  }
  return dash();
}

function showArtCell(show) {
  // Prefer TV.app episode art; fall back to TMDb show poster.
  if (show.artId != null) {
    return `<div class="cell"><img class="art" src="art/thumb/${show.artId}.jpg" alt="" `
      + `decoding="async" onerror="this.className='art art--empty'"></div>`;
  }
  const safe = safeName(show.name);
  if ((state.showPosters || new Set()).has(safe)) {
    return `<div class="cell"><img class="art art--poster" `
      + `src="art/show/thumb/${encodeURIComponent(safe)}.jpg" alt="" `
      + `decoding="async" onerror="this.className='art art--empty'"></div>`;
  }
  return '<div class="cell"><div class="art art--empty" role="img" aria-label="no artwork"></div></div>';
}

const dash = () => '<div class="cell"><span class="dash">—</span></div>';
const cellOrDash = (v) => (v ? `<div class="cell">${escapeHTML(v)}</div>` : dash());
// Which sources know about this film. A film only Letterboxd knows about is
// one you have watched but do not own, which is worth seeing at a glance.
const badges = (row) => (row.deleted
    ? '<span class="tag-deleted" title="deleted — shown because you asked to see them">DELETED</span>'
    : '')
  + (row.sources === 'lb'
    ? '<span class="tag-lb" title="on Letterboxd; not in your TV.app library">LB</span>'
    : '')
  + (row.liked
      ? `<span class="tag-heart" data-clearflag="liked" data-key="${escapeHTML(row.key)}"`
        + ` title="liked — click to unlike">\u2665</span>` : '')
  + (row.watchlisted
      ? `<span class="tag-watch" data-clearflag="watchlisted" data-key="${escapeHTML(row.key)}"`
        + ` title="${row.sources === 'lb' ? 'on your Letterboxd watchlist'
                    : 'in your library, never played'} — click to remove">WATCH</span>` : '')
  + (row.edited ? '<span class="tag-edited">edited</span>' : '')
  + (row.reviewed ? '<span class="tag-review" title="you wrote a review">R</span>' : '');
const editedTag = badges;

const episodeCode = (e) => {
  if (e.season_number && e.episode_number) return `S${e.season_number} · E${e.episode_number}`;
  if (e.episode_number) return `Episode ${e.episode_number}`;
  return null;
};

// TV.app keeps only the most recent play, and for most items not even that.
// Where no play date exists we fall back to the date the item entered the
// library, marked so it is never mistaken for a real viewing.
function playedCell(item) {
  const when = showDate(item.played_date);
  if (when) return `<div class="cell num muted">${when}</div>`;
  const added = showDate(item.date_added);
  if (added) {
    return `<div class="cell num fallback" title="No play date recorded — showing the date it was added to the library">${added}</div>`;
  }
  return dash();
}

const playsCell = (i) => `<div class="cell num muted">${
  i.played_count ? i.played_count : '<span class="dash">—</span>'}</div>`;

// Every rating in the library imports as 0, so these are yours to set: a
// click writes an override and leaves the imported value alone.
function starCell(item) {
  const half = Math.round((item.rating || 0) / 10);  // 0-10 in half-stars
  const display = half ? (half / 2).toString().replace(/\.0$/, '') : '';
  const title = half
    ? `${display} of 5${item.lb_rating ? ` (Letterboxd: ${item.lb_rating})` : ''}`
    : 'not rated';
  let html = `<div class="cell stars" data-rate="${escapeHTML(item.key)}" title="${title}">`;
  for (let n = 1; n <= 5; n++) {
    const full = n * 2 <= half;
    const halfOn = n * 2 - 1 === half;
    html += `<span class="star${full ? ' is-on' : halfOn ? ' is-half' : ''}" data-star="${n}" data-half="${n * 2 - 1}">\u2605</span>`;
  }
  return html + '</div>';
}

// Each view declares its columns once; the same spec renders the header and
// every row, so the two can never drift apart.
const VIEWS = {
  movies: {
    grid: '64px minmax(134px,2.4fr) minmax(100px,1.5fr) 60px minmax(124px,1.1fr) 60px 112px 82px',
    columns: ['', 'Title', 'Director', 'Year', 'Genre', 'Plays', 'Last played', 'Rating'],
    // Aligned with `columns`; null means the column cannot be sorted on.
    sortColumns: [null, 'name', 'director', 'year', 'genre', 'played_count', 'played_date', 'rating'],
    sortFields: ['name', 'director', 'year', 'genre', 'played_count', 'played_date', 'rating', 'date_added'],
    defaultSort: [{ field: 'name', dir: 'asc' }],
    source: () => state.items,
    cells: (m) => [
      artCell(m),
      `<div class="cell title">${escapeHTML(m.name)}${editedTag(m)}</div>`,
      cellOrDash(m.director),
      `<div class="cell num muted">${m.year ?? '<span class="dash">—</span>'}</div>`,
      m.genre ? `<div class="cell"><span class="pill">${escapeHTML(m.genre)}</span></div>` : dash(),
      playsCell(m),
      playedCell(m),
      starCell(m),
    ],
  },

  shows: {
    grid: '72px minmax(190px,3fr) 88px 78px minmax(96px,1.2fr) 82px 50px 140px',
    columns: ['', 'Show', 'Episodes', 'Seasons', 'Genre', 'Rating', '\u2665', 'Last played'],
    sortColumns: [null, 'name', 'episodes', 'seasons', 'genre', 'rating', 'liked', 'played_date'],
    sortFields: ['name', 'episodes', 'seasons', 'watched', 'genre', 'rating', 'liked', 'played_date'],
    defaultSort: [{ field: 'name', dir: 'asc' }],
    source: () => state.shows,
    cells: (s) => [
      showArtCell(s),
      `<div class="cell title">${escapeHTML(s.name)}<div class="sub">${s.watched} of ${s.episodes} watched</div></div>`,
      `<div class="cell num muted">${s.episodes}</div>`,
      `<div class="cell num muted">${s.seasons || '<span class="dash">—</span>'}</div>`,
      s.genre ? `<div class="cell"><span class="pill">${escapeHTML(s.genre)}</span></div>` : dash(),
      showRatingCell(s),
      `<div class="cell"><span class="heart${s.liked ? ' is-on' : ''}" data-showlike="${escapeHTML(s.name)}" title="${s.liked ? 'liked' : 'like'}">${s.liked ? '\u2665' : '\u2661'}</span></div>`,
      playedCell(s),
    ],
  },

};

// -------------------------------------------------------------- aggregation

function buildShows(items) {
  const groups = new Map();
  for (const item of items) {
    if (!item.show) continue;
    let g = groups.get(item.show);
    if (!g) {
      g = { key: `show:${item.show}`, id: `show:${item.show}`, name: item.show,
            episodes: 0, watched: 0,
            seasonSet: new Set(), genres: new Map(), played_date: null,
            played_count: 0, years: [], media_kind: 'show', artId: null, artRank: Infinity };
      groups.set(item.show, g);
    }
    g.episodes += 1;
    if (item.played_count > 0) g.watched += 1;
    g.played_count += item.played_count;
    if (item.season_number) g.seasonSet.add(item.season_number);
    if (item.genre) g.genres.set(item.genre, (g.genres.get(item.genre) || 0) + 1);
    if (item.year) g.years.push(item.year);
    if (item.played_date && (!g.played_date || item.played_date > g.played_date)) g.played_date = item.played_date;
    // Earliest episode that actually has art represents the series.
    if (item.has_art) {
      const rank = (item.season_number ?? 99) * 10000 + (item.episode_number ?? 0);
      if (rank < g.artRank) { g.artRank = rank; g.artId = item.tv_id; }
    }
  }
  const meta = state.showMeta || {};
  return [...groups.values()].map((g) => {
    const m = meta[g.name] || {};
    return {
      ...g,
      seasons: g.seasonSet.size,
      genre: [...g.genres.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null,
      year: g.years.length ? Math.min(...g.years) : null,
      director: null,
      tmdbRating: m.rating || null,
      userRating: m.rating_override || null,
      liked: m.liked || 0,
      rating: m.rating_override || (m.rating ? Math.round(m.rating * 10) : 0),
    };
  });
}

// ----------------------------------------------------------- filter and sort

const isPlayed = (r) => (r.played_count || 0) > 0 || !!r.played_date;
const inLibrary = (r) => r.sources !== 'lb';

function applyFilters() {
  let rows = VIEWS[state.view].source();

  const q = state.search.trim().toLowerCase();
  if (q) {
    const terms = q.split(/\s+/);
    rows = rows.filter((r) => {
      const hay = [r.name, r.director, r.show, r.genre].filter(Boolean).join(' ').toLowerCase();
      return terms.every((t) => hay.includes(t));
    });
  }
  if (state.genre) rows = rows.filter((r) => r.genre === state.genre);
  if (state.decade) {
    const from = Number(state.decade);
    rows = rows.filter((r) => r.year != null && r.year >= from && r.year < from + 10);
  }

  // Counted before the scope is applied, so each segment can show what it
  // would yield under the filters already active.
  const owned = rows.filter(inLibrary);
  state.scopeCounts = {
    all: rows.length,
    library: owned.length,
    played: owned.filter(isPlayed).length,
    unplayed: owned.filter((r) => !isPlayed(r)).length,
  };

  if (state.scope === 'library') {
    rows = rows.filter(inLibrary);
  } else if (state.scope === 'played') {
    rows = rows.filter((r) => inLibrary(r) && isPlayed(r));
    if (state.playedDetail === 'dated') rows = rows.filter((r) => r.played_date);
    else if (state.playedDetail === 'undated') rows = rows.filter((r) => !r.played_date);
  } else if (state.scope === 'unplayed') {
    rows = rows.filter((r) => inLibrary(r) && !isPlayed(r));
  }

  state.visible = state.sortChain.length
    ? [...rows].sort(chainComparator(state.sortChain))
    : rows;
}

// ------------------------------------------------------------------ render

const NUMERIC_HEADS = /^(Year|Films|Watched|Episodes|Seasons|Plays|Rating)$/;

function renderHead() {
  const view = VIEWS[state.view];
  const head = $('listhead');
  head.style.gridTemplateColumns = view.grid;
  const rank = new Map(state.sortChain.map((level, i) => [level.field, { i, dir: level.dir }]));

  head.innerHTML = view.columns.map((label, i) => {
    const field = (view.sortColumns || [])[i] || null;
    const active = field ? rank.get(field) : null;
    // The rank number only appears once more than one level is in play.
    const marker = active
      ? `<span class="sort-marker">${active.dir === 'desc' ? '\u25be' : '\u25b4'}`
        + `${state.sortChain.length > 1 ? active.i + 1 : ''}</span>`
      : '';
    const classes = ['cell'];
    if (NUMERIC_HEADS.test(label)) classes.push('num');
    if (field) classes.push('sortable');
    if (active) classes.push('is-sorted');
    const attrs = field ? ` data-sort-field="${field}" role="button" tabindex="0"` : '';
    return `<div class="${classes.join(' ')}"${attrs}>${label}${marker}</div>`;
  }).join('');
}

const sameChain = (a, b) => a.length === b.length
  && a.every((l, i) => l.field === b[i].field && l.dir === b[i].dir);

function renderSortBar() {
  const view = VIEWS[state.view];

  $('sortChips').innerHTML = state.sortChain.map((level, i) => {
    const spec = SORT_FIELDS[level.field];
    if (!spec) return '';
    return '<span class="chip">'
      + `<span class="chip-rank">${i + 1}</span>`
      + `<button class="chip-name" data-flip="${level.field}" title="flip direction">`
      + `${escapeHTML(spec.label)} `
      + `<span class="chip-dir">${level.dir === 'desc' ? '\u25be' : '\u25b4'}</span></button>`
      + `<button class="chip-drop" data-drop="${level.field}" title="remove this level"`
      + ` aria-label="remove ${escapeHTML(spec.label)}">\u00d7</button></span>`;
  }).join('');

  const used = new Set(state.sortChain.map((l) => l.field));
  const available = view.sortFields.filter((f) => !used.has(f) && SORT_FIELDS[f]);
  const add = $('sortAdd');
  add.innerHTML = '<option value="">+ add level</option>'
    + available.map((f) => `<option value="${f}">${escapeHTML(SORT_FIELDS[f].label)}</option>`).join('');
  add.value = '';
  add.hidden = available.length === 0;
  $('sortReset').hidden = sameChain(state.sortChain, view.defaultSort);
}

// One button, cycled by clicking. Library, Played and Unplayed are all about
// films you own, so each of them drops the Letterboxd-only titles.
const SCOPES = [
  { key: '',         label: 'All' },
  { key: 'library',  label: 'Library' },
  { key: 'played',   label: 'Played' },
  { key: 'unplayed', label: 'Unplayed' },
];

function renderScopeToggle() {
  const at = SCOPES.findIndex((s) => s.key === state.scope);
  const current = SCOPES[at < 0 ? 0 : at];
  const next = SCOPES[((at < 0 ? 0 : at) + 1) % SCOPES.length];
  const count = state.scopeCounts[current.key || 'all'] ?? 0;
  const button = $('scopeToggle');
  button.innerHTML = `${escapeHTML(current.label)}`
    + `<span class="seg-count">${count.toLocaleString()}</span>`;
  button.classList.toggle('is-filtered', Boolean(current.key));
  button.title = `Showing ${current.label.toLowerCase()} — click for ${next.label.toLowerCase()}`;
  // The date refinement only means anything for items that were played.
  $('playedDetail').hidden = state.scope !== 'played';
}

function setSort(field, additive) {
  if (!SORT_FIELDS[field]) return;
  const at = state.sortChain.findIndex((l) => l.field === field);
  const firstDir = DESC_FIRST.has(field) ? 'desc' : 'asc';

  if (additive) {
    if (at >= 0) state.sortChain[at].dir = state.sortChain[at].dir === 'asc' ? 'desc' : 'asc';
    else state.sortChain.push({ field, dir: firstDir });
  } else if (at === 0 && state.sortChain.length === 1) {
    // Clicking the only active column flips it rather than resetting it.
    state.sortChain = [{ field, dir: state.sortChain[0].dir === 'asc' ? 'desc' : 'asc' }];
  } else {
    state.sortChain = [{ field, dir: firstDir }];
  }
  render();
}

let _lastStart = -1, _lastEnd = -1, _lastView = '', _forceRows = false;

function renderRows(force) {
  if (force) _forceRows = true;
  const scroller = $('scroller');
  const total = state.visible.length;
  $('sizer').style.height = `${total * ROW_HEIGHT}px`;
  $('empty').hidden = total > 0;

  const start = Math.max(0, Math.floor(scroller.scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(total, start + Math.ceil(scroller.clientHeight / ROW_HEIGHT) + OVERSCAN * 2);

  if (!_forceRows && start === _lastStart && end === _lastEnd && state.view === _lastView) return;
  _lastStart = start; _lastEnd = end; _lastView = state.view; _forceRows = false;

  const view = VIEWS[state.view];
  const html = [];
  for (let i = start; i < end; i++) {
    const row = state.visible[i];
    const selected = row.key === state.selectedKey ? ' is-selected' : '';
    html.push(`<div class="row${selected}" data-index="${i}" style="grid-template-columns:${view.grid}">${view.cells(row).join('')}</div>`);
  }
  $('rows').style.transform = `translateY(${start * ROW_HEIGHT}px)`;
  $('rows').innerHTML = html.join('');
}

function renderCount() {
  const noun = { movies: 'film', shows: 'show' }[state.view] || 'item';
  const hidden = state.view === 'movies' && state.deletedCount
    ? ` <button class="linkish" id="toggleDeleted">`
      + `${state.showDeleted ? 'hide' : `${state.deletedCount} deleted`}</button>`
    : '';
  $('count').innerHTML = escapeHTML(plural(state.visible.length, noun)) + hidden;
  $('reset').hidden = !(state.search || state.genre || state.decade || state.scope);
}

function render() {
  if (state.facetView !== state.view) {
    renderFacets();                       // counts belong to this view
    state.facetView = state.view;
    state.genre = $('genre').value;
    state.decade = $('decade').value;
  }
  applyFilters();
  renderHead();
  renderSortBar();
  renderScopeToggle();
  $('scroller').scrollTop = 0;
  renderRows(true);
  renderCount();
}

// ------------------------------------------------------------------ banners

function renderChanges(summary) {
  if (!summary) return;
  const removed = summary.removed_items || [];
  if (removed.length) {
    const movies = removed.filter((r) => r.media_kind === 'movie');
    const others = removed.filter((r) => r.media_kind !== 'movie');
    $('alertTitle').textContent = movies.length
      ? `RED ALERT — ${plural(movies.length, 'movie')} disappeared from the library`
      : `${plural(removed.length, 'item')} disappeared from the library`;
    const names = movies.concat(others).map((r) =>
      `${escapeHTML(r.name)}${r.media_kind !== 'movie' ? ' <span class="muted">(episode)</span>' : ''}`);
    $('alertList').innerHTML = names.join('<br>');
    $('alert').hidden = false;
  }
  const quiet = (summary.added || 0) + (summary.modified || 0);
  if (quiet) {
    $('noticeText').textContent =
      `Since the last import: ${summary.added || 0} added, ${summary.modified || 0} changed.`;
    $('notice').hidden = false;
  }
}

// ---------------------------------------------------------- link questions
//
// Where the evidence for two records being one film is suggestive but not
// conclusive, the app asks instead of guessing. Either answer is kept, so a
// pair is never raised twice.

async function loadQuestions() {
  try {
    const response = await fetch('api/questions');
    if (!response.ok) return;
    const data = await response.json();
    state.questions = data.questions || [];
    renderQuestionBanner();
  } catch { /* the banner is optional; a failure here is not worth reporting */ }
}

function renderQuestionBanner() {
  const n = (state.questions || []).length;
  $('links').hidden = n === 0;
  if (n) {
    $('linksText').textContent = n === 1
      ? 'One film may be the same in both sources — is it?'
      : `${n} films may be the same in both sources — are they?`;
  }
}

function sideCard(side, label) {
  // The director is what usually settles these, so it leads. A Letterboxd
  // export has none, so that side's comes from TMDb.
  const t = side.tmdb || {};
  const director = (side.tv && side.tv.director) || t.directors || null;
  const genre = (side.tv && side.tv.genre) || t.genres || null;
  // Both sides through the same formatter: comparing "1h 40m" against "99m"
  // is exactly the sort of thing these questions turn on.
  const runtime = (side.tv && side.tv.duration)
    ? duration(side.tv.duration)
    : (t.runtime ? duration(t.runtime * 60) : null);

  const bits = [];
  if (director) bits.push(`<strong>${escapeHTML(director)}</strong>`);
  if (genre) bits.push(escapeHTML(genre));
  if (runtime) bits.push(escapeHTML(runtime));
  if (side.tv && side.tv.played_count) bits.push(plural(side.tv.played_count, 'play'));
  if (side.lb) {
    if (side.lb.rating != null) bits.push(`rated ${side.lb.rating}`);
    if (side.lb.watchlisted_date) bits.push('watchlisted');
  }
  if (!director) bits.push('<span class="muted">director unknown</span>');

  const others = side.titles.filter((title) => title !== side.title);
  return `<div class="side">`
    + `<div class="side-label">${escapeHTML(label)}</div>`
    + `<div class="side-title">${escapeHTML(side.title)} `
    + `<span class="muted">(${side.year ?? '—'})</span></div>`
    + `<div class="side-bits">${bits.join(' \u00b7 ')}</div>`
    + (others.length
        ? `<div class="side-bits muted">also known as: ${
             others.slice(0, 3).map(escapeHTML).join(', ')}</div>` : '')
    + `</div>`;
}

function renderQuestions() {
  const list = state.questions || [];
  if (!list.length) {
    $('detailBody').innerHTML = '<div class="detail-inner"><h2>Nothing to settle</h2>'
      + '<p class="muted">Every film that could be matched has been.</p></div>';
    return;
  }
  const cards = list.map((q) => {
    const left = q.left.sources.includes('tv') ? q.left : q.right;
    const right = left === q.left ? q.right : q.left;
    return `<div class="question" data-question="${escapeHTML(q.id)}">`
      + `<div class="question-why">${escapeHTML(q.reason)}</div>`
      + sideCard(left, 'In your library (TV.app)')
      + sideCard(right, 'On Letterboxd')
      + `<div class="question-actions">`
      + `<button class="btn btn--primary" data-same="1">Same film</button>`
      + `<button class="btn" data-same="0">Different films</button>`
      + `</div></div>`;
  }).join('');

  $('detailBody').innerHTML = `<div class="detail-inner">`
    + `<h2>Are these the same film?</h2>`
    + `<p class="lede">${plural(list.length, 'pair')} the app could not settle on its own. `
    + `Your answer is remembered and survives re-importing.</p>${cards}</div>`;
}

// ------------------------------------------------------ logging a new film
//
// Most films you have just watched are already here, from TV.app or from the
// Letterboxd export, so the library is searched first. TMDb is the fallback
// for the ones neither knows, and adopting one creates the film here so a
// viewing has something to attach to.

function openLogger() {
  state.detail = null;
  state.selectedKey = null;
  $('detail').hidden = false;
  $('scrim').hidden = false;
  renderLogger([], null);
  const box = $('findFilm');
  if (box) box.focus();
}

let _completionCache = null;

function renderLogger(matches, tmdb, query = '', suggestions = null) {
  const local = matches.slice(0, 8).map((row) => {
    const bits = [row.year, row.director].filter(Boolean).join(' \u00b7 ');
    return `<button class="found" data-openkey="${escapeHTML(row.key)}" type="button">`
      + `<span class="found-title">${escapeHTML(row.name)}</span>`
      + `<span class="found-bits">${escapeHTML(bits)}</span></button>`;
  }).join('');

  const suggest = (suggestions || []).map((r) => {
    const bits = [r.year, r.overview ? r.overview.slice(0, 80) + '…' : null]
      .filter(Boolean).join(' \u00b7 ');
    return `<button class="found" data-adopt="${escapeHTML(r.tmdb_id)}" type="button">`
      + `<span class="found-title">${escapeHTML(r.title)}</span>`
      + `<span class="found-bits">${escapeHTML(bits)}</span></button>`;
  }).join('');

  const remote = (tmdb || []).map((r) => {
    const bits = [r.year, r.overview ? r.overview.slice(0, 70) + '…' : null]
      .filter(Boolean).join(' \u00b7 ');
    return `<button class="found" data-adopt="${escapeHTML(r.tmdb_id)}" type="button">`
      + `<span class="found-title">${escapeHTML(r.title)}</span>`
      + `<span class="found-bits">${escapeHTML(bits)}</span></button>`;
  }).join('');

  $('detailBody').innerHTML = `<div class="detail-inner">`
    + `<h2>Log a film</h2>`
    + `<p class="lede">Find it in your library, or look it up if it is not here yet.</p>`
    + `<input type="search" id="findFilm" class="find" placeholder="Title…" `
    + `value="${escapeHTML(query)}" autocomplete="off">`
    + (local ? `<div class="section-head"><h3>In your library</h3></div>${local}` : '')
    + (suggest ? `<div class="section-head"><h3>From TMDb</h3></div>${suggest}` : '')
    + (remote ? `<div class="section-head"><h3>From TMDb</h3></div><div class="found-list">${remote}</div>` : '')
    + ((tmdb && !tmdb.length) || (suggestions && !suggestions.length)
        ? '<p class="muted">Nothing at TMDb for that.</p>' : '')
    + `</div>`;
  const box = $('findFilm');
  if (box) {
    box.focus();
    box.setSelectionRange(box.value.length, box.value.length);
  }
}

async function adoptFilm(tmdbId) {
  $('detailBody').innerHTML = '<div class="detail-inner"><p class="muted">adding…</p></div>';
  const response = await fetch('api/tmdb/adopt', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tmdb_id: tmdbId }),
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    $('detailBody').innerHTML = '<div class="detail-inner"><p class="muted">'
      + escapeHTML(error.error || 'could not add that film') + '</p></div>';
    return;
  }
  const added = await response.json();
  // The film is new, so the list has to learn about it before we open it.
  const lib = await fetch('api/library');
  if (lib.ok) {
    const data = await lib.json();
    state.items = unpack(data.columns, data.rows);
    state.byKey = new Map(state.items.map((i) => [i.key, i]));
    render();
  }
  const row = state.byKey.get(added.work_key);
  if (row) openDetail(row);
}

function openQuestions() {
  state.detail = null;
  state.selectedKey = null;
  $('detail').hidden = false;
  $('scrim').hidden = false;
  renderQuestions();
}

async function answerQuestion(id, same) {
  const card = document.querySelector(`[data-question="${id}"]`);
  if (card) card.style.opacity = '0.4';
  const response = await fetch('api/questions/answer', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id, same }),
  });
  if (!response.ok) { if (card) card.style.opacity = ''; return; }
  state.questions = (state.questions || []).filter((q) => q.id !== id);
  renderQuestionBanner();
  renderQuestions();
  if (same) {
    // A merge changes which films exist, so reload the list underneath.
    const lib = await fetch('api/library');
    if (lib.ok) {
      const data = await lib.json();
      state.items = unpack(data.columns, data.rows);
      state.byKey = new Map(state.items.map((i) => [i.key, i]));
      applyFilters();
      renderRows();
      renderCount();
    }
  }
}

async function acknowledge() {
  try {
    await fetch('api/changes/ack', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}',
    });
  } catch { /* dismissing is cosmetic; a failure here is not worth reporting */ }
}

// ------------------------------------------------------------------ detail

// Fields offered for editing, in panel order.
const EDIT_FIELDS = [
  ['review', 'Your review (Markdown)', 'textarea'],
  ['name', 'Title', 'text'],
  ['director', 'Director', 'text'],
  ['genre', 'Genre', 'text'],
  ['year', 'Year', 'number'],
  ['show', 'Show', 'text'],
  ['season_number', 'Season', 'number'],
  ['episode_number', 'Episode', 'number'],
  ['played_count', 'Times played', 'number'],
  ['played_date', 'Last played', 'text'],
  ['long_description', 'Description', 'textarea'],
];

// A small Markdown subset for reviews. The source is HTML-escaped before any
// markup is added, and links are restricted to http(s), so a review can never
// inject markup or a javascript: URL.
function renderMarkdown(source) {
  if (!source) return '';
  const lines = escapeHTML(source).replace(/\r\n?/g, '\n').split('\n');

  const inline = (text) => text
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    // *** *** first, or the bold rule would claim two of the three stars.
    .replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/(^|[^_\w])_([^_\n]+)_/g, '$1<em>$2</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>')
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');

  const out = [];
  let list = null;      // 'ul' | 'ol' while one is open
  let paragraph = [];
  let fenced = null;    // collected lines while inside ```

  const closeParagraph = () => {
    if (paragraph.length) {
      out.push(`<p>${inline(paragraph.join(' '))}</p>`);
      paragraph = [];
    }
  };
  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };
  const openList = (kind) => {
    if (list !== kind) { closeList(); out.push(`<${kind}>`); list = kind; }
  };

  for (const line of lines) {
    if (fenced !== null) {
      if (/^```/.test(line)) {
        out.push(`<pre><code>${fenced.join('\n')}</code></pre>`);
        fenced = null;
      } else fenced.push(line);
      continue;
    }
    if (/^```/.test(line)) { closeParagraph(); closeList(); fenced = []; continue; }

    if (!line.trim()) { closeParagraph(); closeList(); continue; }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      closeParagraph(); closeList();
      const level = Math.min(heading[1].length + 2, 6);   // #  ->  h3
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      continue;
    }
    if (/^\s*(?:---|\*\*\*|___)\s*$/.test(line)) {
      closeParagraph(); closeList(); out.push('<hr>'); continue;
    }
    const quote = line.match(/^&gt;\s?(.*)$/);
    if (quote) {
      closeParagraph(); closeList();
      out.push(`<blockquote>${inline(quote[1])}</blockquote>`);
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.*)$/);
    if (bullet) { closeParagraph(); openList('ul'); out.push(`<li>${inline(bullet[1])}</li>`); continue; }
    const numbered = line.match(/^\s*\d+[.)]\s+(.*)$/);
    if (numbered) { closeParagraph(); openList('ol'); out.push(`<li>${inline(numbered[1])}</li>`); continue; }

    closeList();
    paragraph.push(line.trim());
  }
  if (fenced !== null) out.push(`<pre><code>${fenced.join('\n')}</code></pre>`);
  closeParagraph();
  closeList();
  return out.join('');
}

// TV.app writes 0 where it means "absent"; an edit box should show nothing
// rather than a spurious zero.
const ZERO_IS_BLANK = new Set([
  'season_number', 'episode_number', 'year', 'disc_number', 'disc_count',
  'track_number', 'track_count', 'bit_rate', 'sample_rate', 'volume_adjustment',
]);

// Fields whose control should be a textarea rather than a single line.
const LONG_FIELDS = new Set(['long_description', 'description', 'comment', 'review']);

// Every editable column, priority ones first, the rest after. Nothing the
// import captured is off limits.
function allEditFields() {
  const priority = EDIT_FIELDS.map(([key]) => key);
  const rest = (state.editableFields || [])
    .filter((f) => !priority.includes(f))
    .sort();
  return {
    priority: EDIT_FIELDS,
    rest: rest.map((f) => [f, humanLabel(f), controlType(f)]),
  };
}

const humanLabel = (field) => field.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());

function controlType(field) {
  if (LONG_FIELDS.has(field)) return 'textarea';
  const kind = (state.fieldTypes || {})[field];
  return kind === 'integer' || kind === 'real' ? 'number' : 'text';
}

// Direct links where an identifier is known, a search otherwise. An IMDb id
// comes from TMDb and a Letterboxd URI from the export, so most films land on
// the right page rather than a results list.
function lookupLinks(eff, detail) {
  const title = eff.media_kind === 'TV show' ? (eff.show || eff.name) : eff.name;
  if (!title) return '';
  const tmdb = (detail && detail.sources && detail.sources.tmdb) || {};
  const film = (detail && detail.sources && (detail.sources.letterboxd || [])[0]) || {};
  const withYear = encodeURIComponent([title, eff.year].filter(Boolean).join(' '));
  const plain = encodeURIComponent(title);

  const links = [];
  links.push(tmdb.imdb_id
    ? ['IMDb', `https://www.imdb.com/title/${encodeURIComponent(tmdb.imdb_id)}/`, true]
    : ['IMDb', `https://www.imdb.com/find/?q=${withYear}&s=tt`, false]);
  links.push(film.uri
    ? ['Letterboxd', film.uri, true]
    : ['Letterboxd', `https://letterboxd.com/search/${plain}/`, false]);
  if (tmdb.tmdb_id) {
    links.push(['TMDb', `https://www.themoviedb.org/movie/${encodeURIComponent(tmdb.tmdb_id)}`, true]);
  }
  links.push(['Rotten Tomatoes',
              `https://www.rottentomatoes.com/search?search=${plain}`, false]);

  return `<div class="lookup">${links.map(([name, href, direct]) =>
    `<a href="${escapeHTML(href)}" target="_blank" rel="noopener noreferrer"`
    + `${direct ? '' : ' class="is-search" title="a search, since no id is known"'}`
    + `>${name} \u2197</a>`).join('')}</div>`;
}

// Watchlist and liked come from Letterboxd but are yours to change; an edit
// is an override and leaves the export alone.
function flagToggles(eff) {
  const liked = Boolean(eff.liked);
  const listed = Boolean(eff.watchlisted);
  return '<div class="flags">'
    + `<button class="heart${liked ? ' is-on' : ''}" data-flag="liked" type="button" `
    + `aria-pressed="${liked}" aria-label="${liked ? 'Liked' : 'Not liked'}" `
    + `title="${liked ? 'Liked — click to unlike' : 'Click to like'}">`
    + `${liked ? '\u2665' : '\u2661'}</button>`
    + `<button class="flag${listed ? ' is-on' : ''}" data-flag="watchlisted" type="button" `
    + `aria-pressed="${listed}" `
    + `title="${listed ? 'On your watchlist — click to remove' : 'Click to add to your watchlist'}">`
    + `Watchlist</button>`
    + '</div>';
}

function starPicker(rating) {
  const half = Math.round((rating || 0) / 10);  // 0-10 in half-stars
  let html = `<div class="stars stars--large" data-rate="detail">`;
  for (let n = 1; n <= 5; n++) {
    const full = n * 2 <= half;
    const halfOn = n * 2 - 1 === half;
    html += `<span class="star${full ? ' is-on' : halfOn ? ' is-half' : ''}" data-star="${n}" data-half="${n * 2 - 1}">\u2605</span>`;
  }
  return html + (half ? ` <button class="clear-stars" data-star="0">clear</button>` : '') + '</div>';
}

async function openDetail(row) {
  state.selectedKey = row.key;
  state.dirty = new Map();
  $('detail').hidden = false;
  $('scrim').hidden = false;
  renderRows();

  if (row.media_kind === 'show' || row.media_kind === 'director') {
    state.detail = null;
    renderAggregate(row);
    return;
  }
  $('detailBody').innerHTML = '<div class="detail-inner"><p class="muted">loading…</p></div>';
  try {
    const response = await fetch(`api/work/${encodeURIComponent(row.key)}`);
    if (!response.ok) throw new Error(`server returned ${response.status}`);
    state.detail = await response.json();
    renderItemDetail();
  } catch (err) {
    $('detailBody').innerHTML =
      `<div class="detail-inner"><p class="muted">Could not load this item: ${escapeHTML(err.message)}</p></div>`;
  }
}

function renderAggregate(row) {
  const rows = [];
  const add = (k, v) => { if (v != null && v !== '') rows.push([k, v]); };
  let lede;
  if (row.media_kind === 'show') {
    lede = `${plural(row.episodes, 'episode')} · ${row.watched} watched`;
    add('Seasons', row.seasons || null);
    add('Genre', row.genre);
    add('First year', row.year);
  }
  const when = parseDate(row.played_date);
  add('Last played', when ? longDateFmt.format(when) : null);

  const hero = row.artId != null
    ? `<img class="hero" src="art/full/${row.artId}.jpg" alt="">` : '';
  $('detailBody').innerHTML = hero + `<div class="detail-inner">`
    + `<h2>${escapeHTML(row.name)}</h2><p class="lede">${escapeHTML(lede)}</p>`
    + `<dl>${rows.map(([k, v]) => `<dt>${escapeHTML(k)}</dt><dd>${v}</dd>`).join('')}</dl></div>`;
}

// Some titles already carry their year -- "Dunkirk (2017)" -- so only append
// one when it is not there already.
function filmLabel(film) {
  const name = escapeHTML(film.name);
  if (!film.year) return name;
  if (new RegExp(`\\(${film.year}\\)\\s*$`).test(film.name)) return name;
  return `${name} <span class="muted">(${film.year})</span>`;
}

function sourceSummary(detail) {
  const tv = detail.sources.tv || [];
  const lb = detail.sources.letterboxd || [];
  const entries = detail.entries || [];
  const parts = [];

  if (tv.length) {
    const copies = tv.length > 1 ? ` (${tv.length} copies)` : '';
    const pid = tv[0].persistent_id;
    const reveal = pid
      ? ` <button class="linkish" data-reveal="${escapeHTML(pid)}">open \u2197</button>`
      : '';
    parts.push(`<div class="src src--tv"><span class="src-name">TV.app</span>`
      + `<span class="src-detail">in your library${escapeHTML(copies)}${reveal}</span></div>`);
  }
  if (lb.length || entries.length) {
    const film = lb[0] || {};
    const bits = [];
    if (film.rating != null) bits.push(`rated ${film.rating}`);
    if (entries.length) bits.push(`${plural(entries.length, 'log')}`);
    if (film.watchlisted_date) bits.push('on your watchlist');
    if (film.liked_date) bits.push('liked');
    const href = film.uri
      ? ` <a href="${escapeHTML(film.uri)}" target="_blank" rel="noopener noreferrer">open \u2197</a>` : '';
    parts.push(`<div class="src src--lb"><span class="src-name">Letterboxd</span>`
      + `<span class="src-detail">${escapeHTML(bits.join(' \u00b7 ')) || 'listed'}${href}</span></div>`);
  }
  const tmdb = detail.sources.tmdb;
  if (tmdb) {
    const bits = [];
    if (tmdb.directors) bits.push(escapeHTML(tmdb.directors));
    if (tmdb.genres) bits.push(escapeHTML(tmdb.genres));
    if (tmdb.runtime) bits.push(`${tmdb.runtime}m`);
    if (tmdb.vote_average) bits.push(`${Number(tmdb.vote_average).toFixed(1)}/10`);
    const href = tmdb.tmdb_id
      ? ` <a href="https://www.themoviedb.org/movie/${encodeURIComponent(tmdb.tmdb_id)}"`
        + ` target="_blank" rel="noopener noreferrer">open \u2197</a>` : '';
    parts.push(`<div class="src src--tmdb"><span class="src-name">TMDb</span>`
      + `<span class="src-detail">${bits.join(' \u00b7 ') || 'matched'}${href}</span></div>`);
  }
  if (!parts.length) return '';
  return `<div class="sources">${parts.join('')}</div>`;
}

const today = () => new Date().toISOString().slice(0, 10);

// Both kinds of viewing in one list, newest first, each saying where it came
// from. Only the ones logged here can be removed; Letterboxd's belong to the
// export.
function viewingHistory(detail) {
  const mine = (detail.watches || []).map((w) => ({
    when: w.watched_date, rating: w.rating ? w.rating / 20 : null,
    rewatch: w.rewatch, note: w.note, venue: w.venue, id: w.id, own: true,
  }));
  const theirs = (detail.entries || []).map((e) => ({
    when: e.watched_date || e.logged_date, rating: e.rating,
    rewatch: e.rewatch, note: e.review || null, venue: null, id: null, own: false,
  }));
  const all = [...mine, ...theirs]
    .filter((v) => v.when)
    .sort((a, b) => String(b.when).localeCompare(String(a.when)));

  const rows = all.slice(0, 60).map((v) => {
    // Half stars are real: 4.5 must not read as 5.
    const shown = v.rating == null ? null
      : (Math.round(v.rating * 2) / 2).toString().replace(/\.0$/, '');
    const stars = shown ? ` <span class="muted">${shown}\u2605</span>` : '';
    const again = v.rewatch ? ' <span class="muted">rewatch</span>' : '';
    const where = v.venue ? ` <span class="muted">${escapeHTML(v.venue)}</span>` : '';
    const note = v.note ? `<div class="hist-note md">${renderMarkdown(v.note)}</div>` : '';
    const drop = v.own
      ? ` <button class="hist-drop" data-dropwatch="${v.id}" title="remove">\u00d7</button>`
      : '';
    const tag = v.own ? '' : ' <span class="hist-src">LB</span>';
    return `<li>${showDate(v.when) || v.when}${stars}${again}${where}${tag}${drop}${note}</li>`;
  }).join('');

  const form = `<div class="logger">`
    + `<input type="date" id="logDate" value="${today()}" max="${today()}">`
    + `<input type="number" id="logRating" min="0" max="5" step="0.5" placeholder="\u2605">`
    + `<input type="text" id="logVenue" placeholder="where (optional)">`
    + `<button class="btn btn--primary" id="logWatch" type="button">Log a watch</button>`
    + `<textarea id="logNote" rows="2" placeholder="note (optional, Markdown)"></textarea>`
    + `</div>`;

  return `<div class="section-head"><h3>Viewing history</h3>`
    + `<span class="save-note">${all.length ? plural(all.length, 'viewing') : 'none recorded'}`
    + `</span></div>`
    + (rows ? `<ul class="history">${rows}</ul>` : '')
    + form;
}

function fieldControl(key, label, type, eff, imported, over) {
  const overridden = Object.prototype.hasOwnProperty.call(over, key);
  let value = state.dirty.has(key) ? state.dirty.get(key) : (eff[key] ?? '');
  if (value === 0 && ZERO_IS_BLANK.has(key)) value = '';
  const source = imported[key];
  // Genre is a closed list: free text would put a typo in the filter menu,
  // where it is indistinguishable from a real category.
  if (key === 'genre') {
    const options = ['<option value="">—</option>'].concat(
      (state.genreVocabulary || []).map((g) =>
        `<option value="${escapeHTML(g)}"${g === value ? ' selected' : ''}>${escapeHTML(g)}</option>`));
    const note = overridden
      ? `<div class="field-note"><span class="imported">from source: ${
           source == null || source === '' ? '—' : escapeHTML(source)
         }</span><button data-revert="${key}">revert</button></div>` : '';
    return `<div class="field${overridden ? ' is-overridden' : ''}">`
      + `<label>${escapeHTML(label)}${overridden ? '<span class="tag-edited">edited</span>' : ''}</label>`
      + `<select data-field="${key}" class="field-select">${options.join('')}</select>`
      + note + '</div>';
  }

  const control = type === 'textarea'
    ? `<textarea data-field="${key}" rows="${key === 'review' ? 8 : 4}"`
      + ` placeholder="${key === 'review' ? 'Markdown: **bold**, *italic*, # heading, - list, > quote' : ''}"`
      + `>${escapeHTML(value)}</textarea>`
      + (key === 'review'
          ? '<div class="md-preview" id="reviewPreview"></div>' : '')
    : `<input data-field="${key}" type="${type === 'number' ? 'number' : 'text'}" value="${escapeHTML(value)}">`;
  const note = overridden
    ? `<div class="field-note"><span class="imported">from source: ${
         source == null || source === '' || (source === 0 && ZERO_IS_BLANK.has(key))
           ? '—' : escapeHTML(source)
       }</span><button data-revert="${key}">revert</button></div>`
    : '';
  return `<div class="field${overridden ? ' is-overridden' : ''}">`
    + `<label>${escapeHTML(label)}${overridden ? '<span class="tag-edited">edited</span>' : ''}</label>`
    + control + note + '</div>';
}

function renderItemDetail() {
  const d = state.detail;
  const eff = d.effective, over = d.overrides;
  const tv = (d.sources.tv || [])[0] || {};
  const film = (d.sources.letterboxd || [])[0] || {};

  // What the sources say, before any edit -- the baseline a revert returns to.
  const tmdb = d.sources.tmdb || {};
  const fromSource = {
    ...tmdb,
    ...tv,
    genre: tv.genre || (tmdb.genres || '').split(', ')[0] || null,
    director: tv.director || tmdb.directors || null,
    long_description: tv.long_description || tmdb.overview || null,
    year: tv.year || film.year || tmdb.year,
    rating: film.rating != null ? Math.round(film.rating * 20) : 0,
    review: (d.entries || []).map((e) => e.review).find(Boolean) || null,
  };

  const bits = [eff.media_kind === 'movie' ? 'Movie' : 'TV episode'];
  if (eff.year) bits.push(eff.year);
  if (eff.genre) bits.push(eff.genre);
  if (eff.duration) bits.push(duration(eff.duration));

  const groups = allEditFields();
  const primary = groups.priority
    .map(([k, label, type]) => fieldControl(k, label, type, eff, fromSource, over)).join('');
  const secondary = groups.rest
    .map(([k, label, type]) => fieldControl(k, label, type, eff, fromSource, over)).join('');

  const raw = Object.entries(tv)
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `<dt>${escapeHTML(k)}</dt><dd>${escapeHTML(v)}</dd>`).join('');
  const rawLb = Object.entries(film)
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `<dt>${escapeHTML(k)}</dt><dd>${escapeHTML(v)}</dd>`).join('');

  const lists = (d.lists || []).length
    ? `<p class="lists">On your lists: ${d.lists.map((l) =>
        escapeHTML(l.name)).join(', ')}</p>` : '';

  const hero = d.art_id
    ? `<img class="hero" src="art/full/${d.art_id}.jpg" alt="" onerror="this.remove()">`
    : `<img class="hero hero--poster" src="art/poster/full/${encodeURIComponent(d.key)}.jpg"`
      + ` alt="" onerror="this.remove()">`;

  $('detailBody').innerHTML = hero + `<div class="detail-inner">`
    + `<h2>${escapeHTML(eff.name ?? '')}</h2>`
    + `<p class="lede">${escapeHTML(bits.join(' \u00b7 '))}</p>`
    + starPicker(eff.rating)
    + flagToggles(eff)
    + sourceSummary(d)
    + lookupLinks(eff, d)
    + lists
    + (eff.long_description ? `<p class="blurb">${escapeHTML(eff.long_description)}</p>` : '')
    + viewingHistory(d)
    + `<div class="section-head"><h3>Edit</h3>`
    + `<span class="save-note" id="saveNote">edits are stored separately from every source</span></div>`
    + `<div class="edit">${primary}`
    + `<div class="edit-actions">`
    + `<button class="btn btn--primary" id="saveEdits" disabled>Save</button>`
    + `<button class="btn" id="revertAll"${Object.keys(over).length ? '' : ' disabled'}>Remove all edits</button>`
    + `<button class="btn btn--quiet" id="deleteWork">`
    + `${over.deleted ? 'Restore this film' : 'Delete this film'}</button>`
    + `</div></div>`
    + `<details class="raw"><summary>Every other field (${groups.rest.length}) — all editable</summary>`
    + `<div class="edit" style="margin-top:12px">${secondary}</div></details>`
    + (raw ? `<details class="raw"><summary>TV.app, as stored</summary><dl>${raw}</dl></details>` : '')
    + (rawLb ? `<details class="raw"><summary>Letterboxd, as stored</summary><dl>${rawLb}</dl></details>` : '')
    + `</div>`;
}

function markDirty(field, value) {
  const original = state.detail.effective[field];
  const same = String(original ?? '') === String(value ?? '');
  if (same) state.dirty.delete(field); else state.dirty.set(field, value);
  const save = $('saveEdits');
  if (save) save.disabled = state.dirty.size === 0;
  const note = $('saveNote');
  if (note) {
    note.textContent = state.dirty.size
      ? `${plural(state.dirty.size, 'unsaved change')}`
      : 'edits are stored separately from the import';
  }
}

// TV.app stores a rating as 0-100, twenty points per star.
async function rate(row, halfStars) {
  if (!row) return;
  const current = Math.round((row.rating || 0) / 10);  // current in half-stars
  // Clicking the same value clears the rating.
  const next = (halfStars === current || halfStars === 0) ? 0 : halfStars * 10;
  const response = await fetch(`api/work/${encodeURIComponent(row.key)}/override`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fields: { rating: next } }),
  });
  if (!response.ok) return;
  const payload = await response.json();
  patchRow(payload.item);
  if (state.detail && state.detail.key === payload.item.key) {
    state.detail = payload.item;
    renderItemDetail();
  }
  renderRows();
}

async function setFlag(row, field, value) {
  if (!row) return;
  const route = field === 'liked' ? 'like' : 'watchlist';
  const response = await fetch(
    `api/work/${encodeURIComponent(row.key)}/${route}`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(value === undefined ? {} : { value }) });
  if (!response.ok) return;
  const payload = await response.json();
  patchRow(payload.item);
  if (state.detail && state.detail.key === payload.item.key) {
    state.detail = payload.item;
    renderItemDetail();
  }
  renderRows();
}

async function logWatch(key) {
  const stars = Number.parseFloat($('logRating').value);
  const body = {
    watched_date: $('logDate').value || today(),
    rating: Number.isFinite(stars) && stars > 0 ? Math.round(stars * 20) : null,
    venue: $('logVenue').value.trim() || null,
    note: $('logNote').value.trim() || null,
  };
  const response = await fetch(`api/work/${encodeURIComponent(key)}/watch`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body) });
  if (!response.ok) return;
  const payload = await response.json();
  state.detail = payload.item;
  patchRow(payload.item);
  renderItemDetail();
  renderRows();
}

async function dropWatch(id) {
  const response = await fetch(`api/watch/${id}`, { method: 'DELETE' });
  if (!response.ok) return;
  const payload = await response.json();
  state.detail = payload.item;
  patchRow(payload.item);
  renderItemDetail();
  renderRows();
}

// Hiding rather than removing: the film comes back from its sources on every
// import, so only a flag can outlast one. That also makes it undoable.
async function deleteWork(key, gone) {
  const response = await fetch(`api/work/${encodeURIComponent(key)}/delete`,
    { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value: gone }) });
  if (!response.ok) return;
  if (gone) {
    state.items = state.items.filter((r) => r.key !== key);
    state.byKey.delete(key);
    state.deletedCount = (state.deletedCount || 0) + 1;
    closeDetail();
    render();
  } else {
    const payload = await response.json();
    state.detail = payload.item;
    renderItemDetail();
  }
}

async function saveEdits() {
  if (!state.detail || !state.dirty.size) return;
  const fields = {};
  for (const [k, v] of state.dirty) fields[k] = v === '' ? null : v;
  const response = await fetch(`api/work/${encodeURIComponent(state.detail.key)}/override`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fields }),
  });
  const payload = await response.json();
  if (!response.ok) { alertNote(payload.error || 'could not save'); return; }
  state.detail = payload.item;
  state.dirty = new Map();
  patchRow(payload.item);
  renderItemDetail();
  renderRows();
}

async function revertField(field) {
  const base = `api/work/${encodeURIComponent(state.detail.key)}/override`;
  const url = field ? `${base}?field=${encodeURIComponent(field)}` : base;
  const response = await fetch(url, { method: 'DELETE' });
  const payload = await response.json();
  if (!response.ok) { alertNote(payload.error || 'could not revert'); return; }
  state.detail = payload.item;
  state.dirty.delete(field);
  patchRow(payload.item);
  renderItemDetail();
  renderRows();
}

function alertNote(message) {
  const note = $('saveNote');
  if (note) note.textContent = message;
}

// Keep the in-memory list in step with an edit without refetching the
// whole library.
function patchRow(detail) {
  const row = state.byKey.get(detail.key);
  if (!row) return;
  for (const key of ['name', 'genre', 'director', 'year', 'show', 'season_number',
                     'episode_number', 'media_kind', 'played_count', 'played_date',
                     'date_added', 'duration', 'long_description', 'rating']) {
    if (key in detail.effective) row[key] = detail.effective[key];
  }
  row.edited = Object.keys(detail.overrides).length > 0 ? 1 : 0;
  row.reviewed = detail.effective.review ? 1 : 0;
  row.watchlisted = detail.effective.watchlisted ? 1 : 0;
  row.my_watches = (detail.watches || []).length;
  row.liked = detail.effective.liked ? 1 : 0;
  state.shows = buildShows(state.episodes);
  applyFilters();
}

function closeDetail({ redraw = true } = {}) {
  state.selectedKey = null;
  state.detail = null;
  state.dirty = new Map();
  $('detail').hidden = true;
  $('scrim').hidden = true;
  if (redraw) renderRows();
}

// ------------------------------------------------------------------- wiring

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

function wire() {
  $('tabs').addEventListener('click', (e) => {
    const tab = e.target.closest('.tab');
    if (!tab) return;
    for (const t of $('tabs').children) t.classList.toggle('is-active', t === tab);
    state.view = tab.dataset.view;
    // Sort fields differ per view, so carrying a chain across would leave
    // levels that mean nothing here.
    state.sortChain = VIEWS[state.view].defaultSort.map((l) => ({ ...l }));
    // The rows still belong to the outgoing view here, so skip the redraw
    // and let render() rebuild them against the incoming one.
    closeDetail({ redraw: false });
    render();
  });

  $('search').addEventListener('input', debounce((e) => { state.search = e.target.value; render(); }, 120));
  for (const [id, key] of [['genre', 'genre'], ['decade', 'decade']]) {
    $(id).addEventListener('change', (e) => { state[key] = e.target.value; render(); });
  }

  // Remember each segment's label once; renderScopeToggle rewrites the
  // button contents to append a count.
  $('scopeToggle').addEventListener('click', () => {
    const at = SCOPES.findIndex((s) => s.key === state.scope);
    state.scope = SCOPES[((at < 0 ? 0 : at) + 1) % SCOPES.length].key;
    render();
  });
  $('playedDetail').addEventListener('change', (e) => {
    state.playedDetail = e.target.value;
    render();
  });

  $('reset').addEventListener('click', () => {
    state.search = state.genre = state.decade = state.scope = '';
    state.playedDetail = 'any';
    $('search').value = '';
    $('genre').value = $('decade').value = '';
    $('playedDetail').value = 'any';
    render();
  });

  // Click a column to sort by it; shift-click to append another level.
  $('listhead').addEventListener('click', (e) => {
    const cell = e.target.closest('[data-sort-field]');
    if (cell) setSort(cell.dataset.sortField, e.shiftKey);
  });
  $('listhead').addEventListener('keydown', (e) => {
    const cell = e.target.closest('[data-sort-field]');
    if (cell && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      setSort(cell.dataset.sortField, e.shiftKey);
    }
  });

  $('sortChips').addEventListener('click', (e) => {
    const flip = e.target.closest('[data-flip]');
    if (flip) {
      const level = state.sortChain.find((l) => l.field === flip.dataset.flip);
      if (level) level.dir = level.dir === 'asc' ? 'desc' : 'asc';
      return render();
    }
    const drop = e.target.closest('[data-drop]');
    if (drop) {
      state.sortChain = state.sortChain.filter((l) => l.field !== drop.dataset.drop);
      render();
    }
  });

  $('sortAdd').addEventListener('change', (e) => {
    const field = e.target.value;
    if (!field) return;
    state.sortChain.push({ field, dir: DESC_FIRST.has(field) ? 'desc' : 'asc' });
    render();
  });

  $('sortReset').addEventListener('click', () => {
    state.sortChain = VIEWS[state.view].defaultSort.map((l) => ({ ...l }));
    render();
  });

  let ticking = false;
  $('scroller').addEventListener('scroll', () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { renderRows(false); ticking = false; });
  }, { passive: true });

  $('rows').addEventListener('click', (e) => {
    const clear = e.target.closest('[data-clearflag]');
    if (clear) {
      e.stopPropagation();
      return void setFlag(state.byKey.get(clear.dataset.key), clear.dataset.clearflag, false);
    }
    const star = e.target.closest('.star');
    if (star) {
      e.stopPropagation();
      const holder = star.closest('[data-rate]');
      const row = state.byKey.get(holder.dataset.rate);
      // Left half of star = half star, right half = full star.
      const rect = star.getBoundingClientRect();
      const isLeftHalf = (e.clientX - rect.left) < rect.width / 2;
      const halfStars = isLeftHalf ? Number(star.dataset.half) : Number(star.dataset.star) * 2;
      return void rate(row, halfStars);
    }
    const showLike = e.target.closest('[data-showlike]');
    if (showLike) {
      e.stopPropagation();
      const name = showLike.dataset.showlike;
      fetch(`api/show/${encodeURIComponent(name)}/like`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: '{}',
      }).then((r) => r.ok && r.json()).then((data) => {
        if (!data) return;
        const m = state.showMeta[name] || (state.showMeta[name] = {});
        m.liked = data.value ? 1 : 0;
        state.shows = buildShows(state.episodes);
        render();
      });
      return;
    }
    const row = e.target.closest('.row');
    if (row) openDetail(state.visible[Number(row.dataset.index)]);
  });

  const body = $('detailBody');
  body.addEventListener('change', (e) => {
    const field = e.target.dataset?.field;
    if (field && e.target.tagName === 'SELECT') markDirty(field, e.target.value);
  });
  body.addEventListener('input', (e) => {
    const field = e.target.dataset?.field;
    if (!field) return;
    markDirty(field, e.target.value);
    if (field === 'review') {
      const preview = $('reviewPreview');
      if (preview) preview.innerHTML = renderMarkdown(e.target.value);
    }
  });
  body.addEventListener('click', (e) => {
    const reveal = e.target.closest('[data-reveal]');
    if (reveal) {
      e.preventDefault();
      e.stopPropagation();
      const pid = reveal.dataset.reveal;
      reveal.textContent = 'opening\u2026';
      fetch('api/reveal', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ persistent_id: pid }),
      }).then(() => { reveal.textContent = 'open \u2197'; });
      return;
    }
    if (e.target.id === 'saveEdits') return void saveEdits();
    if (e.target.id === 'revertAll') return void revertField(null);
    const star = e.target.closest('.star, .clear-stars');
    if (star && state.detail) {
      if (star.dataset.star === '0') return void rate(state.byKey.get(state.detail.key), 0);
      const rect = star.getBoundingClientRect();
      const isLeftHalf = (e.clientX - rect.left) < rect.width / 2;
      const halfStars = isLeftHalf ? Number(star.dataset.half) : Number(star.dataset.star) * 2;
      return void rate(state.byKey.get(state.detail.key), halfStars);
    }
    const flag = e.target.closest('[data-flag]');
    if (flag && state.detail) {
      return void setFlag(state.byKey.get(state.detail.key), flag.dataset.flag);
    }
    if (e.target.id === 'deleteWork' && state.detail) {
      return void deleteWork(state.detail.key, !state.detail.overrides.deleted);
    }
    if (e.target.id === 'logWatch' && state.detail) {
      return void logWatch(state.detail.key);
    }
    const drop = e.target.closest('[data-dropwatch]');
    if (drop) return void dropWatch(drop.dataset.dropwatch);
    const revert = e.target.dataset?.revert;
    if (revert) revertField(revert);
  });

  $('detailClose').addEventListener('click', () => closeDetail());
  $('scrim').addEventListener('click', () => closeDetail());
  $('linksOpen').addEventListener('click', openQuestions);
  $('logFilm').addEventListener('click', openLogger);
  $('count').addEventListener('click', async (e) => {
    if (e.target.id !== 'toggleDeleted') return;
    state.showDeleted = !state.showDeleted;
    const response = await fetch(`api/library${state.showDeleted ? '?deleted=1' : ''}`);
    if (!response.ok) return;
    const data = await response.json();
    state.items = unpack(data.columns, data.rows);
    state.byKey = new Map(state.items.map((i) => [i.key, i]));
    state.deletedCount = data.deleted_count || state.deletedCount;
    render();
  });

  $('detailBody').addEventListener('input', debounce(async (e) => {
    if (e.target.id !== 'findFilm') return;
    const raw = e.target.value;
    const q = raw.trim().toLowerCase();
    const matches = q.length < 2 ? [] : state.items.filter((r) =>
      (r.name || '').toLowerCase().includes(q)).slice(0, 8);
    // Show local matches immediately.
    renderLogger(matches, null, raw, null);
    // Then fetch TMDb suggestions (the input is preserved by renderLogger).
    if (q.length >= 2) {
      try {
        const r = await fetch('api/tmdb/complete?q=' + encodeURIComponent(q));
        if (r.ok) {
          const data = await r.json();
          // Only update if the input hasn't changed while we waited.
          const box = $('findFilm');
          if (box && box.value === raw) {
            const suggestions = (data.results || []).filter(
              (s) => !matches.some((m) => m.name.toLowerCase() === s.title.toLowerCase()
                                       && m.year === s.year));
            renderLogger(matches, null, raw, suggestions);
          }
        }
      } catch (_) {}
    }
  }, 250));

  $('detailBody').addEventListener('click', async (e) => {
    const open = e.target.closest('[data-openkey]');
    if (open) {
      const row = state.byKey.get(open.dataset.openkey);
      if (row) openDetail(row);
      return;
    }
    const adopt = e.target.closest('[data-adopt]');
    if (adopt) return void adoptFilm(adopt.dataset.adopt);
    if (e.target.id === 'searchTmdb') {
      const query = $('findFilm').value.trim();
      const box = $('searchTmdb');
      box.textContent = 'searching…';
      const response = await fetch('api/tmdb/search?q=' + encodeURIComponent(query));
      const data = response.ok ? await response.json() : { results: [] };
      const matches = state.items.filter((r) =>
        (r.name || '').toLowerCase().includes(query.toLowerCase())).slice(0, 8);
      renderLogger(matches, data.results || [], query);
    }
  });
  $('detailBody').addEventListener('click', (e) => {
    const button = e.target.closest('[data-same]');
    if (!button) return;
    const card = button.closest('[data-question]');
    if (card) answerQuestion(card.dataset.question, button.dataset.same === '1');
  });
  $('alertDismiss').addEventListener('click', () => { $('alert').hidden = true; acknowledge(); });
  $('noticeDismiss').addEventListener('click', () => { $('notice').hidden = true; acknowledge(); });

  window.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeDetail();
    if (e.key === '/' && document.activeElement !== $('search')
        && !/^(INPUT|TEXTAREA)$/.test(document.activeElement?.tagName || '')) {
      e.preventDefault();
      $('search').focus();
    }
  });
  window.addEventListener('resize', debounce(renderRows, 80));
}

// Facets are counted from the rows the current view actually shows, not from
// the whole library. Counting every item made "Comedy" read 3,076 in the
// Movies list, which was mostly sitcom episodes.
function renderFacets() {
  const rows = VIEWS[state.view].source();
  const hasGenre = true;

  const genres = new Map();
  const decades = new Map();
  for (const row of rows) {
    if (row.genre) genres.set(row.genre, (genres.get(row.genre) || 0) + 1);
    if (row.year != null) {
      const decade = Math.floor(row.year / 10) * 10;
      decades.set(decade, (decades.get(decade) || 0) + 1);
    }
  }

  fillSelect($('genre'), 'All genres', state.genre,
    [...genres.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
      .map(([value, n]) => [value, `${value} (${n.toLocaleString()})`]));
  fillSelect($('decade'), 'All decades', state.decade,
    [...decades.entries()].sort((a, b) => b[0] - a[0])
      .map(([value, n]) => [String(value), `${value}s (${n.toLocaleString()})`]));

  $('genre').disabled = !hasGenre;
}

function fillSelect(select, allLabel, current, options) {
  select.innerHTML = `<option value="">${escapeHTML(allLabel)}</option>`
    + options.map(([value, label]) =>
        `<option value="${escapeHTML(value)}">${escapeHTML(label)}</option>`).join('');
  // A genre that exists in one view may not in another; drop it if so.
  select.value = options.some(([value]) => value === current) ? current : '';
  return select.value;
}

// Rows travel as parallel arrays to keep the payload small; turn them back
// into objects.
function unpack(columns, rows) {
  return rows.map((row) => {
    const item = {};
    for (let i = 0; i < columns.length; i++) item[columns[i]] = row[i];
    return item;
  });
}

async function boot() {
  const response = await fetch('api/library');
  if (!response.ok) throw new Error(`the server returned ${response.status}`);
  const data = await response.json();

  // Films are works, drawn from TV.app and Letterboxd together. Episodes
  // have no Letterboxd counterpart and stay as plain TV.app rows.
  state.items = unpack(data.columns, data.rows);
  state.episodes = unpack(data.columns, data.episodes || []);
  state.byKey = new Map(state.items.map((i) => [i.key, i]));
  state.showPosters = new Set(data.show_posters || []);
  state.showMeta = data.show_meta || {};
  state.shows = buildShows(state.episodes);
  state.stats = data.stats;
  state.editableFields = data.editable_fields || [];
  state.fieldTypes = data.field_types || {};
  state.genreVocabulary = data.genres || [];
  state.deletedCount = data.deleted_count || 0;

  const s = data.stats;
  $('subtitle').textContent =
    `${plural(s.works, 'film')} \u00b7 ${s.both.toLocaleString()} in both \u00b7 `
    + `${s.tv_only.toLocaleString()} only in TV.app \u00b7 `
    + `${s.lb_only.toLocaleString()} only on Letterboxd \u00b7 `
    + `${plural(s.episodes, 'episode')}`;
  $('footnote').textContent =
    `TV.app records a play date for ${s.with_played_date.toLocaleString()} of the `
    + `${s.played.toLocaleString()} items it marks played, so the list falls back to `
    + `the date added, in italics. Ratings and reviews come from Letterboxd `
    + `(${s.lb_rated.toLocaleString()} rated, ${s.lb_reviews.toLocaleString()} reviewed) `
    + `unless you have edited them here.`;

  state.sortChain = VIEWS[state.view].defaultSort.map((l) => ({ ...l }));

  renderChanges(data.changes);
  wire();
  render();
  loadQuestions();
}

boot().catch((err) => {
  $('subtitle').textContent = 'could not load the library';
  $('empty').hidden = false;
  $('empty').textContent = `${err.message}. Run 'movieslists sync' and reload.`;
});

// Auto-refresh: poll for changes and update the list silently.
(function autoRefresh() {
  const INTERVAL = 10_000;
  let lastCount = -1;
  let polling = false;
  async function poll() {
    if (polling) return;
    polling = true;
    try {
      const r = await fetch('api/stats');
      if (!r.ok) return;
      const stats = await r.json();
      const sig = (stats.works || 0) + (stats.tmdb_described || 0);
      if (lastCount >= 0 && sig !== lastCount) {
        const lib = await fetch(`api/library${state.showDeleted ? '?deleted=1' : ''}`);
        if (!lib.ok) return;
        const data = await lib.json();
        state.items = unpack(data.columns, data.rows);
        state.episodes = unpack(data.columns, data.episodes || []);
        state.byKey = new Map(state.items.map((i) => [i.key, i]));
        state.showPosters = new Set(data.show_posters || []);
        state.showMeta = data.show_meta || {};
        state.shows = buildShows(state.episodes);
        state.stats = data.stats;
        state.deletedCount = data.deleted_count || 0;
        render();
      }
      lastCount = sig;
    } catch (_) {
    } finally {
      polling = false;
    }
  }
  setInterval(poll, INTERVAL);
})();
