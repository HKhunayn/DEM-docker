function asUrl(value, baseUrl) {
  if (!value) return null;
  try {
    return new URL(value, baseUrl).href;
  } catch {
    return null;
  }
}

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

// The viewer is read-only against the platform: saved areas from the fetch page
// and catalog items are one selection list, deduplicated by id (areas win, since
// they carry the freshest bbox/zoom for a built dataset).
export function mergeItems(areas, catalog) {
  const merged = new Map();
  for (const item of [...areaItems(areas), ...catalogItems(catalog)]) {
    const key = item?.id || item?.name || JSON.stringify(item?.bbox ?? item?.title ?? "");
    merged.set(key, item);
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
  return error instanceof Error ? error.message : String(error);
}

export class PlatformApi {
  constructor(platformUrl, fetchImpl = globalThis.fetch?.bind(globalThis)) {
    this.platformUrl = platformUrl.replace(/\/$/, "");
    // window.fetch rejects a non-Window `this`, so bind it (or wrap the caller)
    // instead of storing the raw reference and calling it as a method.
    this.fetch = typeof fetchImpl === "function"
      ? (...args) => fetchImpl(...args)
      : globalThis.fetch.bind(globalThis);
  }

  url(path) {
    return resolveApiUrl(path, this.platformUrl);
  }

  async getJson(path, label) {
    const response = await this.fetch(this.url(path), {headers: {accept: "application/json"}, cache: "no-store"});
    if (!response.ok) throw new Error(`${label} failed (${response.status})`);
    return response.json();
  }

  async loadState() {
    // The viewer has no data store of its own: this object is one snapshot of
    // the platform's catalog, saved areas, and delivery URL metadata.
    const [catalog, areas, services, terrain, imagery] = await Promise.all([
      this.getJson("/api/catalog", "Catalog request"),
      this.getJson("/api/areas", "Saved areas request"),
      this.getJson("/api/services", "ArcGIS services request"),
      this.getJson("/tiles/terrain.json", "Terrain TileJSON request"),
      // Imagery is optional: an older platform without /api/imagery still
      // yields a working terrain viewer.
      this.getJson("/api/imagery", "Imagery sets request").catch(() => ({sets: []})),
    ]);
    return {catalog, areas, services, terrain, imagery: imagerySets(imagery), items: mergeItems(areas, catalog)};
  }
}
