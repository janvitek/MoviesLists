#!/usr/bin/env osascript -l JavaScript
//
// Bulk-export the Apple TV.app library to JSON.
//
// Every scriptable property of a track is fetched for the whole collection in
// a single Apple Event (`tracks.genre()` -> array) rather than per track: the
// full ~7k-item library exports in a few seconds that way, versus many
// minutes element-by-element.
//
// Usage: osascript -l JavaScript extract.js <output.json>
//
ObjC.import('Foundation');

// Every property on the `track` class, plus `id`, `name`, `index` and
// `persistent ID` inherited from `item`.
var FIELDS = [
  'persistentID', 'databaseID', 'index', 'name',
  'album', 'albumRating', 'albumRatingKind', 'bitRate', 'bookmark',
  'bookmarkable', 'category', 'comment', 'dateAdded', 'description',
  'director', 'discCount', 'discNumber', 'downloaderAccount',
  'downloaderName', 'duration', 'enabled', 'episodeID', 'episodeNumber',
  'finish', 'genre', 'grouping', 'kind', 'longDescription', 'mediaKind',
  'modificationDate', 'playedCount', 'playedDate', 'purchaserAccount',
  'purchaserName', 'rating', 'ratingKind', 'releaseDate', 'sampleRate',
  'seasonNumber', 'show', 'skippedCount', 'skippedDate', 'size',
  'sortAlbum', 'sortDirector', 'sortName', 'sortShow', 'start', 'time',
  'trackCount', 'trackNumber', 'unplayed', 'volumeAdjustment', 'year'
];

// Defined only on particular track subclasses, so asking for them across a
// mixed collection can fail outright. Fetched separately and allowed to fail.
var SUBCLASS_FIELDS = ['location', 'address'];

function run(argv) {
  if (!argv || argv.length < 1) throw new Error('usage: extract.js <output.json>');
  var outPath = argv[0];

  var TV = Application('TV');
  var tracks = TV.libraryPlaylists[0].tracks;

  var started = new Date();
  var columns = {};
  var failed = {};

  FIELDS.concat(SUBCLASS_FIELDS).forEach(function (field) {
    try {
      columns[field] = tracks[field]();
    } catch (e) {
      failed[field] = String(e);
    }
  });

  var present = Object.keys(columns);
  if (present.length === 0) throw new Error('no properties could be read from TV.app');

  var count = columns[present[0]].length;
  var items = [];
  for (var i = 0; i < count; i++) {
    var row = {};
    for (var j = 0; j < present.length; j++) {
      var key = present[j];
      var value = columns[key][i];
      if (value === undefined) value = null;
      // `location` arrives as a Path object, which JSON.stringify empties.
      if (value !== null && typeof value === 'object' && !(value instanceof Date)) {
        value = String(value);
      }
      row[key] = value;
    }
    // Position in the library playlist, 1-based. Artwork is fetched by
    // position, so this is what ties an image back to its track.
    row.position = i + 1;
    items.push(row);
  }

  var payload = {
    exportedAt: new Date().toISOString(),
    elapsedMs: new Date() - started,
    count: count,
    unavailableFields: failed,
    items: items
  };

  // console.log goes to stderr under osascript and a large return value gets
  // mangled by AppleScript text coercion, so write the file directly.
  var str = $.NSString.alloc.initWithUTF8String(JSON.stringify(payload));
  var ok = str.writeToFileAtomicallyEncodingError(
    outPath, true, $.NSUTF8StringEncoding, $()
  );
  if (!ok) throw new Error('could not write ' + outPath);
  return 'wrote ' + count + ' items to ' + outPath;
}
