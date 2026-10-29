// Go-to box logic: coordinate parsing, text normalisation (Latin + Arabic) and
// the offline gazetteer search. DOM-free so it is unit-tested in plain Node.
//
// Coordinate rule (shown as the input's placeholder/title): LATITUDE FIRST,
// like Google Maps -- "24.7136, 46.6753" -- with an optional third value for
// the zoom ("24.71, 46.67, 12"). Hemisphere letters / DMS are accepted in any
// order ("46.6753E 24.7136N", 24°42'49"N 46°40'31"E). Without letters, a first
// value outside +/-90 cannot be a latitude, so the pair is read as "lon, lat"
// and a note says so.

export const GOTO_HINT = "lat, lon (e.g. 24.7136, 46.6753 [, zoom]) — or a place / saved-area name";

const ARABIC_INDIC = /[\u0660-\u0669\u06F0-\u06F9]/g;

function asciiDigits(text) {
  return text.replace(ARABIC_INDIC, (d) => String((d.charCodeAt(0) & 0xf) % 10));
}

// One coordinate component: optional leading hemisphere, degrees, optional
// minutes/seconds, optional trailing hemisphere, then a separator.
const COMPONENT = new RegExp(
  "\\s*([NSEW])?\\s*" +                                   // 1 leading hemisphere
  "([-+]?\\d+(?:\\.\\d+)?)\\s*([°º]|DEG|D(?![A-Z]))?\\s*" +   // 2 degrees, 3 degree sign
  "(?:(\\d+(?:\\.\\d+)?)\\s*['′’]\\s*)?" +                // 4 minutes
  "(?:(\\d+(?:\\.\\d+)?)\\s*(?:[\"″”]|['′’]{2})\\s*)?" +  // 5 seconds
  "([NSEW])?\\s*(?:[,;]\\s*|(?=\\s|$))",                   // 6 trailing hemisphere
  "dy",
);

/**
 * Parse "lat, lon[, zoom]" (decimal, hemisphere letters or DMS).
 * Returns null when the text is not a coordinate, or
 * {lat, lon, zoom?, swapped?, error?} -- `error` set for out-of-range input.
 */
export function parseCoordinates(input) {
  let text = asciiDigits(String(input ?? "")).trim().toUpperCase().replace(/،/g, ",");
  if (!text || !/\d/.test(text)) return null;
  const parts = [];
  COMPONENT.lastIndex = 0;
  while (COMPONENT.lastIndex < text.length) {
    const start = COMPONENT.lastIndex;
    const match = COMPONENT.exec(text);
    if (!match || COMPONENT.lastIndex === start) return null;
    let [, lead, deg, degSign, min, sec, trail] = match;
    if (lead && trail) {
      // "N 24.71 E 46.67": that trailing letter leads the NEXT value.
      COMPONENT.lastIndex = match.indices[6][0];
      trail = undefined;
    }
    const hemi = lead || trail || null;
    const minutes = min == null ? 0 : Number(min);
    const seconds = sec == null ? 0 : Number(sec);
    if (minutes >= 60 || seconds >= 60) return {error: "minutes and seconds must be below 60"};
    if ((min != null || sec != null) && /\./.test(deg)) return null;
    const negative = deg.startsWith("-");
    let value = Math.abs(Number(deg)) + minutes / 60 + seconds / 3600;
    if (negative || hemi === "S" || hemi === "W") value = -value;
    if (negative && (hemi === "S" || hemi === "W")) return {error: "use either a minus sign or S/W, not both"};
    parts.push({value, hemi, plain: !hemi && !degSign && min == null && sec == null});
  }
  if (parts.length < 2 || parts.length > 3) return null;
  let zoom;
  if (parts.length === 3) {
    if (!parts[2].plain) return null;
    zoom = parts[2].value;
    if (!(zoom >= 0 && zoom <= 22)) return {error: "zoom must be between 0 and 22"};
  }
  const [a, b] = parts;
  let lat, lon, swapped = false;
  const axis = (part) => (part.hemi === "N" || part.hemi === "S" ? "lat" : part.hemi ? "lon" : null);
  if (axis(a) || axis(b)) {
    if (axis(a) && axis(a) === axis(b)) return {error: "both values have the same hemisphere axis"};
    const aIsLat = axis(a) ? axis(a) === "lat" : axis(b) === "lon";
    [lat, lon] = aIsLat ? [a.value, b.value] : [b.value, a.value];
  } else if (Math.abs(a.value) > 90 && Math.abs(b.value) <= 90) {
    [lat, lon, swapped] = [b.value, a.value, true];
  } else {
    [lat, lon] = [a.value, b.value];
  }
  if (!(Math.abs(lat) <= 90)) return {error: `latitude ${round(lat, 5)} is outside -90..90`};
  if (!(Math.abs(lon) <= 180)) return {error: `longitude ${round(lon, 5)} is outside -180..180`};
  const result = {lat, lon};
  if (zoom !== undefined) result.zoom = zoom;
  if (swapped) result.swapped = true;
  return result;
}

function round(value, digits) {
  const m = 10 ** digits;
  return Math.round(value * m) / m;
}

/** "24.68770, 46.72190" (5 decimals ~ 1 m). */
export function formatLatLon(lat, lon, digits = 5) {
  return `${Number(lat).toFixed(digits)}, ${Number(lon).toFixed(digits)}`;
}

// ---- normalisation ----------------------------------------------------------

/**
 * Case/diacritics-insensitive key. Arabic: alef forms -> ا, ة -> ه, ى -> ي,
 * ؤ -> و, ئ -> ي, harakat and tatweel removed. Punctuation, "_" and "-" become
 * spaces; apostrophes (Ta'if, Ta’if) are dropped.
 */
export function normalizeText(value) {
  return asciiDigits(String(value ?? ""))
    .normalize("NFKD")
    .replace(/[\u0300-\u036f\u064B-\u065F\u0670\u0640]/g, "")   // Latin marks, harakat, tatweel
    .toLowerCase()
    .replace(/[\u0622\u0623\u0625\u0671]/g, "\u0627")
    .replace(/\u0629/g, "\u0647")
    .replace(/\u0649/g, "\u064A")
    .replace(/\u0624/g, "\u0648")
    .replace(/\u0626/g, "\u064A")
    .replace(/[ıİ]/g, "i").replace(/ß/g, "ss").replace(/[øØ]/g, "o").replace(/[łŁ]/g, "l").replace(/đ/g, "d")
    .replace(/['’ʼʻʿʾ`‘]/g, "")
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .trim();
}

// ---- catalog (saved areas) --------------------------------------------------

/**
 * Find a saved area by name: exact (normalised, "_" == " ") first, then a
 * unique prefix. `namesOf(item)` returns the candidate names of an item.
 */
export function findCatalogItem(items, query, namesOf) {
  const q = normalizeText(query);
  if (!q || !Array.isArray(items)) return null;
  const keyed = items.map((item) => ({item, keys: namesOf(item).map(normalizeText).filter(Boolean)}));
  const exact = keyed.find(({keys}) => keys.includes(q));
  if (exact) return exact.item;
  const prefixed = keyed.filter(({keys}) => keys.some((key) => key.startsWith(q)));
  return prefixed.length === 1 ? prefixed[0].item : null;
}

// ---- offline gazetteer ----------------------------------------------------------

/** Parse public/places.tsv (see scripts/build_places.py) into searchable records. */
export function parsePlaces(text) {
  const places = [];
  for (const line of String(text).split("\n")) {
    if (!line || line.startsWith("#")) continue;
    const [name, ascii, arabic, cc, lat, lon, pop, aliases] = line.replace(/\r$/, "").split("\t");
    const latN = Number(lat), lonN = Number(lon);
    if (!name || !Number.isFinite(latN) || !Number.isFinite(lonN)) continue;
    const arabicNames = arabic ? arabic.split("|").filter(Boolean) : [];
    const aliasNames = aliases ? aliases.split("|").filter(Boolean) : [];
    const names = [name, ascii || null, ...arabicNames, ...aliasNames].filter(Boolean);
    const keys = [];
    const labels = [];
    for (const label of names) {
      const key = normalizeText(label);
      if (key && !keys.includes(key)) { keys.push(key); labels.push(label); }
    }
    places.push({name, arabic: arabicNames[0] || "", cc: cc || "", lat: latN, lon: lonN, population: Number(pop) || 0, keys, labels});
  }
  return places;
}

const RANK_EXACT = 0, RANK_PREFIX = 1, RANK_WORD = 2, RANK_SUBSTRING = 3;

function rankKey(key, q) {
  if (key === q) return RANK_EXACT;
  if (key.startsWith(q)) return RANK_PREFIX;
  if (key.includes(` ${q}`)) return RANK_WORD;
  if (key.includes(q)) return RANK_SUBSTRING;
  return -1;
}

/**
 * Search the gazetteer. Ranking: exact > prefix > word-prefix > substring,
 * ties broken by population. "Name, CC" restricts to a country code.
 * Returns [{place, rank, matched}] where `matched` is the name that matched.
 */
export function searchPlaces(places, query, limit = 8) {
  let text = String(query ?? "").trim();
  let country = null;
  const cc = text.match(/^(.*?)[,،]\s*([A-Za-z]{2})$/);
  if (cc) { text = cc[1]; country = cc[2].toUpperCase(); }
  const q = normalizeText(text);
  if (!q) return [];
  const minRank = q.length < 3 ? RANK_PREFIX : RANK_SUBSTRING;   // 1-2 letters: no substring noise
  const hits = [];
  for (const place of places) {
    if (country && place.cc !== country) continue;
    let best = -1, matched = place.name;
    for (let i = 0; i < place.keys.length; i++) {
      const rank = rankKey(place.keys[i], q);
      if (rank >= 0 && rank <= minRank && (best < 0 || rank < best)) { best = rank; matched = place.labels[i]; if (rank === RANK_EXACT) break; }
    }
    if (best >= 0) hits.push({place, rank: best, matched});
  }
  hits.sort((a, b) => a.rank - b.rank || b.place.population - a.place.population);
  return hits.slice(0, limit);
}

/** Display label: "Riyadh (الرياض), SA" or, for an alias match, "Mecca → Makkah, SA". */
export function placeLabel(hit) {
  const {place, matched} = hit.place ? hit : {place: hit, matched: hit.name};
  const arabicMatch = /[\u0600-\u06FF]/.test(matched);
  let label = place.name;
  if (arabicMatch) label = `${matched} (${place.name})`;
  else if (matched && normalizeText(matched) !== normalizeText(place.name)) label = `${matched} → ${place.name}`;
  else if (place.arabic) label = `${place.name} (${place.arabic})`;
  return place.cc ? `${label}, ${place.cc}` : label;
}

/** MapLibre zoom for a place when the user did not give one: big city ~10, town ~13. */
export function zoomForPopulation(population) {
  const p = Number(population) || 0;
  if (p >= 3_000_000) return 10;
  if (p >= 1_000_000) return 10.5;
  if (p >= 300_000) return 11.5;
  if (p >= 100_000) return 12;
  return 13;
}
