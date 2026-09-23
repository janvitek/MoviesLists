'use strict';

// ---------------------------------------------------------------- utilities

const $ = (id) => document.getElementById(id);
const ROW_HEIGHT = 58;
const OVERSCAN = 6;

const dateFmt = new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
const longDateFmt = new Intl.DateTimeFormat(undefined, { dateStyle: 'full', timeStyle: 'short' });

// TV.app hands back UTC timestamps; render them in the viewer's zone.
const parseDate = (s) => (s ? new Date(s) : null);
const showDate = (s) => { const d = parseDate(s); return d && !isNaN(d) ? dateFmt.format(d) : null; };

const escapeHTML = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
));

const plural = (n, word) => `${Number(n).toLocaleString()} ${word}${n === 1 ? '' : 's'}`;

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
  films:        { label: 'Films',       type: 'number', get: (r) => r.count },
};

// Fields that read better largest-first when you first click them.
const DESC_FIRST = new Set([
  'played_count', 'played_date', 'rating', 'date_added',
  'episodes', 'seasons', 'watched', 'films',
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
  items: [], byId: new Map(), shows: [], directors: [],
  stats: null, editableFields: [],
  view: 'movies', search: '', genre: '', decade: '',
  played: '', playedDetail: 'any', playedCounts: { all: 0, any: 0, never: 0 },
  sortChain: [],
  visible: [], selectedId: null, detail: null, dirty: new Map(),
};

// ------------------------------------------------------------------- views

const artCell = (id, hasArt, alt) => (hasArt
  ? `<div class="cell"><img class="art" src="art/thumb/${id}.jpg" alt="" loading="lazy" decoding="async"></div>`
  : `<div class="cell"><div class="art art--empty" role="img" aria-label="no artwork"></div></div>`);

const dash = () => '<div class="cell"><span class="dash">—</span></div>';
const cellOrDash = (v) => (v ? `<div class="cell">${escapeHTML(v)}</div>` : dash());
const editedTag = (row) => (row.edited ? '<span class="tag-edited">edited</span>' : '')
  + (row.reviewed ? '<span class="tag-review" title="you wrote a review">review</span>' : '');

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
  const filled = Math.round((item.rating || 0) / 20);
  let html = `<div class="cell stars" data-rate="${item.id}" title="${
    filled ? `${filled} of 5` : 'not rated'}">`;
  for (let n = 1; n <= 5; n++) {
    html += `<span class="star${n <= filled ? ' is-on' : ''}" data-star="${n}">\u2605</span>`;
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
    source: () => state.items.filter((i) => i.media_kind === 'movie'),
    cells: (m) => [
      artCell(m.id, m.has_art),
      `<div class="cell title">${escapeHTML(m.name)}${editedTag(m)}</div>`,
      cellOrDash(m.director),
      `<div class="cell num muted">${m.year ?? '<span class="dash">—</span>'}</div>`,
      m.genre ? `<div class="cell"><span class="pill">${escapeHTML(m.genre)}</span></div>` : dash(),
      playsCell(m),
      playedCell(m),
      starCell(m),
    ],
  },

  episodes: {
    grid: '64px minmax(134px,2.4fr) minmax(104px,1.5fr) 60px minmax(124px,1.1fr) 60px 112px 82px',
    columns: ['', 'Episode', 'Show', 'Year', 'Genre', 'Plays', 'Last played', 'Rating'],
    sortColumns: [null, 'name', 'show', 'year', 'genre', 'played_count', 'played_date', 'rating'],
    sortFields: ['name', 'show', 'year', 'genre', 'played_count', 'played_date', 'rating', 'date_added'],
    defaultSort: [{ field: 'name', dir: 'asc' }],
    source: () => state.items.filter((i) => i.media_kind === 'TV show'),
    cells: (e) => [
      artCell(e.id, e.has_art),
      `<div class="cell title">${escapeHTML(e.name)}${editedTag(e)}</div>`,
      `<div class="cell">${escapeHTML(e.show ?? '')}<div class="sub">${escapeHTML(episodeCode(e) || '')}</div></div>`,
      `<div class="cell num muted">${e.year ?? '<span class="dash">—</span>'}</div>`,
      e.genre ? `<div class="cell"><span class="pill">${escapeHTML(e.genre)}</span></div>` : dash(),
      playsCell(e),
      playedCell(e),
      starCell(e),
    ],
  },

  shows: {
    grid: '72px minmax(190px,3fr) 88px 78px minmax(96px,1.2fr) 140px',
    columns: ['', 'Show', 'Episodes', 'Seasons', 'Genre', 'Last played'],
    sortColumns: [null, 'name', 'episodes', 'seasons', 'genre', 'played_date'],
    sortFields: ['name', 'episodes', 'seasons', 'watched', 'genre', 'played_date'],
    defaultSort: [{ field: 'name', dir: 'asc' }],
    source: () => state.shows,
    cells: (s) => [
      artCell(s.artId, s.artId != null),
      `<div class="cell title">${escapeHTML(s.name)}<div class="sub">${s.watched} of ${s.episodes} watched</div></div>`,
      `<div class="cell num muted">${s.episodes}</div>`,
      `<div class="cell num muted">${s.seasons || '<span class="dash">—</span>'}</div>`,
      s.genre ? `<div class="cell"><span class="pill">${escapeHTML(s.genre)}</span></div>` : dash(),
      playedCell(s),
    ],
  },

  directors: {
    grid: 'minmax(190px,3fr) 72px 82px 140px',
    columns: ['Director', 'Films', 'Watched', 'Years'],
    sortColumns: ['name', 'films', 'watched', 'year'],
    sortFields: ['name', 'films', 'watched', 'year'],
    defaultSort: [{ field: 'films', dir: 'desc' }, { field: 'name', dir: 'asc' }],
    source: () => state.directors,
    cells: (d) => [
      `<div class="cell title">${escapeHTML(d.name)}<div class="sub">${escapeHTML(d.films.map((f) => f.name).slice(0, 3).join(' · '))}${d.films.length > 3 ? ' …' : ''}</div></div>`,
      `<div class="cell num muted">${d.count}</div>`,
      `<div class="cell num muted">${d.watched}</div>`,
      `<div class="cell num muted">${d.years}</div>`,
    ],
  },
};

// -------------------------------------------------------------- aggregation

function buildShows(items) {
  const groups = new Map();
  for (const item of items) {
    if (item.media_kind !== 'TV show' || !item.show) continue;
    let g = groups.get(item.show);
    if (!g) {
      g = { id: `show:${item.show}`, name: item.show, episodes: 0, watched: 0,
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
      if (rank < g.artRank) { g.artRank = rank; g.artId = item.id; }
    }
  }
  return [...groups.values()].map((g) => ({
    ...g,
    seasons: g.seasonSet.size,
    genre: [...g.genres.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? null,
    year: g.years.length ? Math.min(...g.years) : null,
    director: null,
  }));
}

function buildDirectors(items) {
  const groups = new Map();
  for (const item of items) {
    if (item.media_kind !== 'movie') continue;
    for (const name of splitDirectors(item.director)) {
      const key = name.toLowerCase();
      let g = groups.get(key);
      if (!g) {
        g = { id: `dir:${key}`, name, films: [], watched: 0, played_date: null,
              played_count: 0, media_kind: 'director' };
        groups.set(key, g);
      }
      g.films.push(item);
      if (item.played_count > 0) g.watched += 1;
      g.played_count += item.played_count;
      if (item.played_date && (!g.played_date || item.played_date > g.played_date)) g.played_date = item.played_date;
    }
  }
  return [...groups.values()].map((g) => {
    const years = g.films.map((f) => f.year).filter(Boolean);
    const lo = years.length ? Math.min(...years) : null;
    const hi = years.length ? Math.max(...years) : null;
    return { ...g, count: g.films.length, year: lo, genre: null, director: g.name,
             years: lo == null ? '—' : (lo === hi ? `${lo}` : `${lo}–${hi}`) };
  });
}

// ----------------------------------------------------------- filter and sort

const isPlayed = (r) => (r.played_count || 0) > 0 || !!r.played_date;

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

  // Counted before the played filter is applied, so the toggle can show how
  // many each choice would yield under the filters already active.
  const played = rows.filter(isPlayed).length;
  state.playedCounts = { all: rows.length, any: played, never: rows.length - played };

  if (state.played === 'any') {
    rows = rows.filter(isPlayed);
    if (state.playedDetail === 'dated') rows = rows.filter((r) => r.played_date);
    else if (state.playedDetail === 'undated') rows = rows.filter((r) => !r.played_date);
  } else if (state.played === 'never') {
    rows = rows.filter((r) => !isPlayed(r));
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

function renderPlayedToggle() {
  const counts = state.playedCounts;
  for (const button of $('playedToggle').children) {
    const key = button.dataset.played;
    const n = key === 'any' ? counts.any : key === 'never' ? counts.never : counts.all;
    button.classList.toggle('is-active', state.played === key);
    button.innerHTML = `${escapeHTML(button.dataset.label)}`
      + `<span class="seg-count">${n.toLocaleString()}</span>`;
  }
  // The date refinement only means anything for items that were played.
  $('playedDetail').hidden = state.played !== 'any';
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

function renderRows() {
  const scroller = $('scroller');
  const total = state.visible.length;
  $('sizer').style.height = `${total * ROW_HEIGHT}px`;
  $('empty').hidden = total > 0;

  const start = Math.max(0, Math.floor(scroller.scrollTop / ROW_HEIGHT) - OVERSCAN);
  const end = Math.min(total, start + Math.ceil(scroller.clientHeight / ROW_HEIGHT) + OVERSCAN * 2);

  const view = VIEWS[state.view];
  const html = [];
  for (let i = start; i < end; i++) {
    const row = state.visible[i];
    const selected = row.id === state.selectedId ? ' is-selected' : '';
    html.push(`<div class="row${selected}" data-index="${i}" style="grid-template-columns:${view.grid}">${view.cells(row).join('')}</div>`);
  }
  $('rows').style.transform = `translateY(${start * ROW_HEIGHT}px)`;
  $('rows').innerHTML = html.join('');
}

function renderCount() {
  const noun = { movies: 'movie', episodes: 'episode', shows: 'show', directors: 'director' }[state.view];
  $('count').textContent = plural(state.visible.length, noun);
  $('reset').hidden = !(state.search || state.genre || state.decade || state.played);
}

function render() {
  applyFilters();
  renderHead();
  renderSortBar();
  renderPlayedToggle();
  $('scroller').scrollTop = 0;
  renderRows();
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

// Search links, built from the title alone -- no API, no lookup traffic from
// this app; the request only happens if you click.
function lookupLinks(eff) {
  const title = eff.media_kind === 'TV show' ? (eff.show || eff.name) : eff.name;
  if (!title) return '';
  const withYear = encodeURIComponent([title, eff.year].filter(Boolean).join(' '));
  const plain = encodeURIComponent(title);
  const links = [
    ['IMDb', `https://www.imdb.com/find/?q=${withYear}&s=tt`],
    ['Rotten Tomatoes', `https://www.rottentomatoes.com/search?search=${plain}`],
  ];
  if (eff.media_kind === 'movie') {
    links.push(['Letterboxd', `https://letterboxd.com/search/${plain}/`]);
  }
  return `<div class="lookup">${links.map(([name, href]) =>
    `<a href="${href}" target="_blank" rel="noopener noreferrer">${name} \u2197</a>`).join('')}</div>`;
}

function starPicker(rating) {
  const filled = Math.round((rating || 0) / 20);
  let html = `<div class="stars stars--large" data-rate="detail">`;
  for (let n = 1; n <= 5; n++) {
    html += `<span class="star${n <= filled ? ' is-on' : ''}" data-star="${n}">\u2605</span>`;
  }
  return html + (filled ? ` <button class="clear-stars" data-star="0">clear</button>` : '') + '</div>';
}

async function openDetail(row) {
  state.selectedId = row.id;
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
    const response = await fetch(`api/item/${row.id}`);
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
  } else {
    lede = `${plural(row.count, 'film')} · ${row.watched} watched · ${row.years}`;
    rows.push(['Films', row.films.slice()
      .sort((a, b) => (b.year ?? 0) - (a.year ?? 0)).map(filmLabel).join('<br>')]);
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

function fieldControl(key, label, type, eff, imported, over) {
  const overridden = Object.prototype.hasOwnProperty.call(over, key);
  let value = state.dirty.has(key) ? state.dirty.get(key) : (eff[key] ?? '');
  if (value === 0 && ZERO_IS_BLANK.has(key)) value = '';
  const control = type === 'textarea'
    ? `<textarea data-field="${key}" rows="${key === 'review' ? 8 : 4}"`
      + ` placeholder="${key === 'review' ? 'Markdown: **bold**, *italic*, # heading, - list, > quote' : ''}"`
      + `>${escapeHTML(value)}</textarea>`
      + (key === 'review'
          ? `<div class="md-preview" id="reviewPreview">${renderMarkdown(value)}</div>` : '')
    : `<input data-field="${key}" type="${type === 'number' ? 'number' : 'text'}" value="${escapeHTML(value)}">`;
  const note = overridden
    ? `<div class="field-note"><span class="imported">imported: ${
         imported[key] == null || imported[key] === ''
         || (imported[key] === 0 && ZERO_IS_BLANK.has(key))
         ? '—' : escapeHTML(imported[key])
       }</span><button data-revert="${key}">revert</button></div>`
    : '';
  return `<div class="field${overridden ? ' is-overridden' : ''}">`
    + `<label>${escapeHTML(label)}${overridden ? '<span class="tag-edited">edited</span>' : ''}</label>`
    + control + note + '</div>';
}

function renderItemDetail() {
  const d = state.detail;
  const eff = d.effective, imported = d.imported, over = d.overrides;

  const bits = [eff.media_kind === 'movie' ? 'Movie' : 'TV episode'];
  if (eff.year) bits.push(eff.year);
  if (eff.genre) bits.push(eff.genre);
  if (eff.duration) bits.push(duration(eff.duration));

  const groups = allEditFields();
  const primary = groups.priority
    .map(([k, label, type]) => fieldControl(k, label, type, eff, imported, over)).join('');
  const secondary = groups.rest
    .map(([k, label, type]) => fieldControl(k, label, type, eff, imported, over)).join('');

  const raw = Object.entries(imported)
    .filter(([, v]) => v !== null && v !== '')
    .map(([k, v]) => `<dt>${escapeHTML(k)}</dt><dd>${escapeHTML(v)}</dd>`)
    .join('');

  const playedNote = !eff.played_date && eff.date_added
    ? `<p class="fallback-note">No play date recorded. The list shows the date added `
      + `(${escapeHTML(showDate(eff.date_added) || '')}) in its place.</p>`
    : '';

  $('detailBody').innerHTML =
    `<img class="hero" src="art/full/${d.id}.jpg" alt="" onerror="this.remove()">`
    + `<div class="detail-inner">`
    + `<h2>${escapeHTML(eff.name ?? '')}</h2>`
    + `<p class="lede">${escapeHTML(bits.join(' \u00b7 '))}</p>`
    + starPicker(eff.rating)
    + lookupLinks(eff)
    + (eff.review
        ? `<div class="review"><h3 class="review-head">Your review</h3>`
          + `<div class="md">${renderMarkdown(eff.review)}</div></div>`
        : '')
    + (eff.long_description ? `<p class="blurb">${escapeHTML(eff.long_description)}</p>` : '')
    + playedNote
    + `<div class="section-head"><h3>Edit</h3>`
    + `<span class="save-note" id="saveNote">edits are stored separately from the import</span></div>`
    + `<div class="edit">${primary}`
    + `<div class="edit-actions">`
    + `<button class="btn btn--primary" id="saveEdits" disabled>Save</button>`
    + `<button class="btn" id="revertAll"${Object.keys(over).length ? '' : ' disabled'}>Remove all edits</button>`
    + `</div></div>`
    + `<details class="raw"><summary>Every other field (${groups.rest.length}) — all editable</summary>`
    + `<div class="edit" style="margin-top:12px">${secondary}</div></details>`
    + `<details class="raw"><summary>Imported values as stored (${Object.keys(imported).length})</summary>`
    + `<dl>${raw}</dl></details></div>`;
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
async function rate(row, stars) {
  if (!row) return;
  const current = Math.round((row.rating || 0) / 20);
  // Clicking the star you are already on clears the rating.
  const next = (stars === current || stars === 0) ? 0 : stars * 20;
  const response = await fetch(`api/item/${row.id}/override`, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ fields: { rating: next } }),
  });
  if (!response.ok) return;
  const payload = await response.json();
  patchRow(payload.item);
  if (state.detail && state.detail.id === payload.item.id) {
    state.detail = payload.item;
    renderItemDetail();
  }
  renderRows();
}

async function saveEdits() {
  if (!state.detail || !state.dirty.size) return;
  const fields = {};
  for (const [k, v] of state.dirty) fields[k] = v === '' ? null : v;
  const response = await fetch(`api/item/${state.detail.id}/override`, {
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
  const url = field
    ? `api/item/${state.detail.id}/override?field=${encodeURIComponent(field)}`
    : `api/item/${state.detail.id}/override`;
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
  const row = state.byId.get(detail.id);
  if (!row) return;
  for (const key of ['name', 'genre', 'director', 'year', 'show', 'season_number',
                     'episode_number', 'media_kind', 'played_count', 'played_date',
                     'date_added', 'duration', 'long_description', 'rating']) {
    if (key in detail.effective) row[key] = detail.effective[key];
  }
  row.edited = Object.keys(detail.overrides).length > 0 ? 1 : 0;
  row.reviewed = detail.effective.review ? 1 : 0;
  state.shows = buildShows(state.items);
  state.directors = buildDirectors(state.items);
  applyFilters();
}

function closeDetail({ redraw = true } = {}) {
  state.selectedId = null;
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

  // Remember each segment's label once; renderPlayedToggle rewrites the
  // button contents to append a count.
  for (const button of $('playedToggle').children) {
    button.dataset.label = button.textContent.trim();
  }
  $('playedToggle').addEventListener('click', (e) => {
    const button = e.target.closest('.seg');
    if (!button) return;
    state.played = button.dataset.played;
    render();
  });
  $('playedDetail').addEventListener('change', (e) => {
    state.playedDetail = e.target.value;
    render();
  });

  $('reset').addEventListener('click', () => {
    state.search = state.genre = state.decade = state.played = '';
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
    requestAnimationFrame(() => { renderRows(); ticking = false; });
  }, { passive: true });

  $('rows').addEventListener('click', (e) => {
    const star = e.target.closest('.star');
    if (star) {
      // Rating from the list should not also open the panel.
      e.stopPropagation();
      const holder = star.closest('[data-rate]');
      const row = state.byId.get(Number(holder.dataset.rate));
      return void rate(row, Number(star.dataset.star));
    }
    const row = e.target.closest('.row');
    if (row) openDetail(state.visible[Number(row.dataset.index)]);
  });

  const body = $('detailBody');
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
    if (e.target.id === 'saveEdits') return void saveEdits();
    if (e.target.id === 'revertAll') return void revertField(null);
    const star = e.target.closest('.star, .clear-stars');
    if (star && state.detail) {
      return void rate(state.byId.get(state.detail.id), Number(star.dataset.star));
    }
    const revert = e.target.dataset?.revert;
    if (revert) revertField(revert);
  });

  $('detailClose').addEventListener('click', () => closeDetail());
  $('scrim').addEventListener('click', () => closeDetail());
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

function populateFilters(facets) {
  for (const g of facets.genres) $('genre').add(new Option(`${g.value} (${g.count})`, g.value));
  for (const d of facets.decades) $('decade').add(new Option(`${d.value}s (${d.count})`, String(d.value)));
}

async function boot() {
  const response = await fetch('api/library');
  if (!response.ok) throw new Error(`the server returned ${response.status}`);
  const data = await response.json();

  const { columns, rows } = data;
  state.items = rows.map((row) => {
    const item = {};
    for (let i = 0; i < columns.length; i++) item[columns[i]] = row[i];
    return item;
  });
  state.byId = new Map(state.items.map((i) => [i.id, i]));
  state.shows = buildShows(state.items);
  state.directors = buildDirectors(state.items);
  state.stats = data.stats;
  state.editableFields = data.editable_fields || [];
  state.fieldTypes = data.field_types || {};

  const s = data.stats;
  $('subtitle').textContent = `${plural(s.movies, 'movie')} · ${plural(s.shows, 'show')} · `
    + `${plural(s.episodes, 'episode')} · ${plural(state.directors.length, 'director')}`;
  $('footnote').textContent =
    `TV.app records a play date for ${s.with_played_date.toLocaleString()} of the `
    + `${s.played.toLocaleString()} items marked played — it keeps only the most recent `
    + `play, and not always that. The rest show a play count instead.`;

  state.sortChain = VIEWS[state.view].defaultSort.map((l) => ({ ...l }));

  renderChanges(data.changes);
  populateFilters(data.facets);
  wire();
  render();
}

boot().catch((err) => {
  $('subtitle').textContent = 'could not load the library';
  $('empty').hidden = false;
  $('empty').textContent = `${err.message}. Run 'movieslists sync' and reload.`;
});
