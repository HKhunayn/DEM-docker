import * as Cesium from "cesium";

// Decode one Terrarium PNG into a Float32 height grid (row-major, north first).
async function fetchTerrarium(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`Terrain tile failed (${response.status})`);
  const bitmap = await createImageBitmap(await response.blob());
  const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
  const context = canvas.getContext("2d", {willReadFrequently: true});
  context.drawImage(bitmap, 0, 0);
  const rgba = context.getImageData(0, 0, bitmap.width, bitmap.height).data;
  const size = bitmap.width;
  const values = new Float32Array(size * size);
  for (let i = 0; i < values.length; i += 1) {
    const p = i * 4;
    values[i] = rgba[p] * 256 + rgba[p + 1] + rgba[p + 2] / 256 - 32768;
  }
  return {values, size};
}

/**
 * Bilinear resample of a square window of a parent grid into an n x n grid.
 * `edgeX/edgeY` are the window's west/north edge in parent pixel units and
 * `span` its width; samples run edge-to-edge (pixel centres sit at +0.5).
 */
export function resampleWindow(parent, size, edgeX, edgeY, span, n) {
  const out = new Float32Array(n * n);
  const clamp = (v) => Math.min(size - 1, Math.max(0, v));
  for (let row = 0; row < n; row += 1) {
    const fy = clamp(edgeY + (row / (n - 1)) * span - 0.5);
    const y0 = Math.floor(fy); const y1 = Math.min(size - 1, y0 + 1); const ty = fy - y0;
    for (let col = 0; col < n; col += 1) {
      const fx = clamp(edgeX + (col / (n - 1)) * span - 0.5);
      const x0 = Math.floor(fx); const x1 = Math.min(size - 1, x0 + 1); const tx = fx - x0;
      const top = parent[y0 * size + x0] * (1 - tx) + parent[y0 * size + x1] * tx;
      const bottom = parent[y1 * size + x0] * (1 - tx) + parent[y1 * size + x1] * tx;
      out[row * n + col] = top * (1 - ty) + bottom * ty;
    }
  }
  return out;
}

/**
 * CesiumJS terrain from the platform's Terrarium XYZ pyramid.
 *
 * Levels above `maxzoom` are never requested (the server has no such tiles);
 * they are bilinearly over-zoomed from the maxzoom ancestor, as MapLibre does
 * for `raster-dem` maxzoom. Every tile is resampled to an edge-inclusive grid
 * so neighbouring Cesium tiles share border samples (no cracks at seams).
 */
export function makePlatformTerrainProvider(tileTemplates, options = {}) {
  const templates = Array.isArray(tileTemplates) ? tileTemplates.filter(Boolean) : [];
  if (!templates.length) throw new Error("Platform returned no terrain tile URL");
  const maxzoom = Number.isFinite(options.maxzoom) ? options.maxzoom : 13;
  const grid = options.size || 65;
  const cache = new Map();

  const tileUrl = (z, x, y) => templates[0].replace("{z}", z).replace("{x}", x).replace("{y}", y);
  const load = (z, x, y) => {
    const key = `${z}/${x}/${y}`;
    if (!cache.has(key)) {
      const promise = fetchTerrarium(tileUrl(z, x, y)).catch((error) => { cache.delete(key); throw error; });
      cache.set(key, promise);
      if (cache.size > 512) cache.delete(cache.keys().next().value);
    }
    return cache.get(key);
  };

  return new Cesium.CustomHeightmapTerrainProvider({
    width: grid,
    height: grid,
    tilingScheme: new Cesium.WebMercatorTilingScheme(),
    credit: options.credit || "Platform elevation",
    callback: async (x, y, level) => {
      const z = Math.min(level, maxzoom);
      const scale = 2 ** (level - z);
      const px = Math.floor(x / scale); const py = Math.floor(y / scale);
      const {values, size} = await load(z, px, py);
      const span = size / scale;
      return resampleWindow(values, size, (x - px * scale) * span, (y - py * scale) * span, span, grid);
    },
  });
}

export const makeLocalHeightmapProvider = makePlatformTerrainProvider;
