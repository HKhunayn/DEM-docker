function asUrl(value, baseUrl) {
  if (!value) return null;
  try {
    return new URL(value, baseUrl).href;
  } catch {
    return null;
  }
}

export const REQUEST_TIMEOUT_MS = 15000;

export function resolveApiUrl(value, platformUrl) {
  return asUrl(value, `${platformUrl.replace(/\/$/, "")}/`);
}

export function catalogItems(payload) {
  if (Array.isArray(payload?.items)) return payload.items;
  if (Array.isArray(payload?.features)) return payload.features;
  return [];
}

export function areaItems(payload) {
  if (Array.isArray(payload?.areas)) return payload.areas;
  return [];
}

function itemKey(item) {
  return item?.id || item?.name || JSON.stringify(item?.bbox ?? item?.title ?? "");
}

// The viewer is read-only against the platform: saved areas from the fetch page
// and catalog items are one selection list, deduplicated by id.
//
// Merge rule for a duplicate id: the SAVED AREA wins field-by-field, layered
// over the catalog entry ({...catalogItem, ...area}). The area is what the
// fetch page last wrote for a built dataset, so its bbox, zooms, kind and
// dataset.{state,service} are the freshest and drive selection, framing and
// the ArcGIS service lookup; catalog-only fields (links, assets, properties)
// are kept because the area does not carry them. Order: areas first, then
// catalog-only items.
export function mergeItems(areas, catalog) {
  const merged = new Map();
  for (const area of areaItems(areas)) merged.set(itemKey(area), area);
  for (const item of catalogItems(catalog)) {
    const key = itemKey(item);
    const area = merged.get(key);
    merged.set(key, area ? {...item, ...area} : item);
  }
  return [...merged.values()];
}

export function itemBounds(item) {
  const candidates = [
    item?.bbox,
    item?.geometry?.bbox,
    item?.properties?.bbox,
    item?.properties?.extent?.bbox,
  ];
  return candidates.find((bbox) => Array.isArray(bbox) && bbox.length >= 4 && bbox.every(Number.isFinite)) || null;
}

export function tileUrls(tileJson, sourceUrl) {
  const templates = Array.isArray(tileJson?.tiles) ? tileJson.tiles : [];
  return templates.map((template) => asUrl(template, sourceUrl)?.replaceAll("%7B", "{").replaceAll("%7D", "}")).filter(Boolean);
}

// Platform imagery sets publish an XYZ template ({z}/{x}/{y}). ArcGIS
// WebTileLayer wants {level}/{col}/{row}; MapLibre/Cesium take it as-is.
export function imageryTemplate(set, platformUrl, flavor = "xyz") {
  const raw = Array.isArray(set?.tiles) ? set.tiles[0] : null;
  if (!raw) return null;
  const url = resolveApiUrl(raw, platformUrl)?.replaceAll("%7B", "{").replaceAll("%7D", "}");
  if (!url || flavor !== "arcgis") return url;
  return url.replace("{z}", "{level}").replace("{x}", "{col}").replace("{y}", "{row}");
}

export function imagerySets(payload) {
  return Array.isArray(payload?.sets) ? payload.sets.filter((set) => set?.name && Array.isArray(set.tiles)) : [];
}

export function formatError(error) {
  if (error instanceof Error) return error.message;
  if (error && typeof error === "object" && typeof error.message === "string") return error.message;
  return String(error);
}

function timeoutSignal(ms) {
  return typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function" ? AbortSignal.timeout(ms) : undefined;
}

// Parts of the platform state. Only catalog + areas are required: without them
// there is nothing to select. The others degrade to a per-part warning.
const STATE_PARTS = [
  {part: "catalog", path: "/api/catalog", label: "Catalog request", required: true},
  {part: "areas", path: "/api/areas", label: "Saved areas request", required: true},
  {part: "services", path: "/api/services", label: "ArcGIS services request", fallback: {services: []}},
  {part: "terrain", path: "/tiles/terrain.json", label: "Terrain TileJSON request", fallback: null},
  // An older platform without /api/imagery (404) is not worth a warning.
  {part: "imagery", path: "/api/imagery", label: "Imagery sets request", fallback: {sets: []}, quietStatus: 404},
];

export class PlatformApi {
  constructor(platformUrl, fetchImpl = globalThis.fetch?.bind(globalThis), {timeoutMs = REQUEST_TIMEOUT_MS} = {}) {
    this.platformUrl = platformUrl.replace(/\/$/, "");
    this.timeoutMs = timeoutMs;
    // window.fetch rejects a non-Window `this`, so bind it (or wrap the caller)
    // instead of storing the raw reference and calling it as a method.
    this.fetch = typeof fetchImpl === "function"
      ? (...args) => fetchImpl(...args)
      : globalThis.fetch.bind(globalThis);
  }

  url(path) {
    return resolveApiUrl(path, this.platformUrl);
  }

  async getJson(path, label, {timeoutMs = this.timeoutMs} = {}) {
    let response;
    try {
      response = await this.fetch(this.url(path), {headers: {accept: "application/json"}, cache: "no-store", signal: timeoutSignal(timeoutMs)});
    } catch (error) {
      if (error?.name === "TimeoutError" || error?.name === "AbortError") {
        const timeout = new Error(`${label} timed out after ${Math.round(timeoutMs / 1000)} s`);
        timeout.name = "TimeoutError";
        throw timeout;
      }
      throw new Error(`${label} failed: ${formatError(error)}`);
    }
    if (!response.ok) {
      const failure = new Error(`${label} failed (${response.status})`);
      failure.status = response.status;
      throw failure;
    }
    return response.json();
  }

  /**
   * One snapshot of the platform's catalog, saved areas and delivery metadata.
   * Rejects only when a required part (catalog, areas) fails; optional parts
   * fall back to an empty value and are reported in `warnings`
   * ([{part, message}]).
   */
  async loadState() {
    const results = await Promise.allSettled(STATE_PARTS.map(({path, label}) => this.getJson(path, label)));
    const values = {}; const warnings = []; const failures = [];
    results.forEach((result, index) => {
      const spec = STATE_PARTS[index];
      if (result.status === "fulfilled") { values[spec.part] = result.value; return; }
      const message = formatError(result.reason);
      if (spec.required) failures.push(message);
      else {
        values[spec.part] = spec.fallback;
        const quiet = spec.quietStatus !== undefined && result.reason?.status === spec.quietStatus;
        if (!quiet) warnings.push({part: spec.part, message});
      }
    });
    if (failures.length) throw new Error(failures.join("; "));
    const {catalog, areas, services, terrain, imagery} = values;
    return {catalog, areas, services, terrain, imagery: imagerySets(imagery), items: mergeItems(areas, catalog), warnings};
  }
}
