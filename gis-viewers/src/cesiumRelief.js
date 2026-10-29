// CesiumJS imagery provider that colours + shades the platform's Terrarium
// elevation tiles in the browser (same look as the MapLibre relief layers).
// The maths lives in relief.js (pure, unit-tested); this file only does
// fetching, decoding, caching and the Cesium ImageryProvider plumbing.
import * as Cesium from "cesium";
import {LruCache, decodeTerrarium} from "./terrarium.js";
import {padHeights, shadeRelief, tileUrl} from "./relief.js";

const TILE_TIMEOUT_MS = 20000;
// Decoded height grids, shared by every provider instance (a relief rebuild,
// e.g. toggling colour for an imagery set, reuses them). Key = tile URL.
// ~256 KB of Float32 per 256 px tile -> ~48 MB max.
const tileCache = new LruCache(192);

function timeoutSignal(ms) {
  return typeof AbortSignal !== "undefined" && typeof AbortSignal.timeout === "function" ? AbortSignal.timeout(ms) : undefined;
}

function makeCanvas(width, height) {
  if (typeof OffscreenCanvas !== "undefined") return new OffscreenCanvas(width, height);
  const canvas = document.createElement("canvas");
  canvas.width = width; canvas.height = height;
  return canvas;
}

/** Terrarium WebP/PNG blob -> {values: Float32Array, mask: Uint8Array|null, size}. */
async function decodeBlob(blob) {
  // Raw bytes are data, not colour: no colour conversion / premultiplication.
  const bitmap = await createImageBitmap(blob, {colorSpaceConversion: "none", premultiplyAlpha: "none"});
  try {
    const size = bitmap.width;
    const context = makeCanvas(size, bitmap.height).getContext("2d", {willReadFrequently: true});
    context.drawImage(bitmap, 0, 0);
    const rgba = context.getImageData(0, 0, size, bitmap.height).data;
    const count = size * bitmap.height;
    let mask = null;
    for (let i = 0; i < count; i += 1) {
      if (rgba[i * 4 + 3] === 0) {
        mask ||= new Uint8Array(count).fill(1);
        mask[i] = 0;
      }
    }
    return {values: decodeTerrarium(rgba, count, 0), mask, size};
  } finally {
    bitmap.close();
  }
}

async function plainFetchBlob(url) {
  const response = await fetch(url, {signal: timeoutSignal(TILE_TIMEOUT_MS)});
  if (!response.ok) throw new Error(`Terrain tile failed (${response.status})`);
  return response.blob();
}

/**
 * Cached decoded tile. With a Cesium `request` the fetch goes through
 * Cesium's RequestScheduler (Resource) and may be throttled -> undefined.
 */
function loadTile(url, request) {
  const hit = tileCache.get(url);
  if (hit) return hit;
  let blob;
  if (request) {
    blob = new Cesium.Resource({url, request}).fetchBlob();
    if (!blob) return undefined;   // throttled: Cesium retries later
  } else {
    blob = plainFetchBlob(url);
  }
  const promise = Promise.resolve(blob).then(decodeBlob);
  promise.request = request;
  promise.catch(() => { if (tileCache.map.get(url) === promise) tileCache.delete(url); });
  tileCache.set(url, promise);
  return promise;
}

function isCancelled(request) {
  return request?.state === Cesium.RequestState.CANCELLED;
}

function transparentTile() {
  return makeCanvas(1, 1);
}

/**
 * ImageryProvider (Cesium interface) rendering hypsometric colour +
 * multidirectional hillshade from Terrarium tiles.
 *
 * Options: template (XYZ Terrarium URL), bounds [w,s,e,n] (coverage; no tile
 * is requested outside it), maxzoom, tileSize (256), colour (true: opaque
 * colour + shade; false: semi-transparent shade only, for draping over
 * imagery), neighbours (true: fetch the 8 neighbours for seam-free edge
 * gradients, falling back to edge clamping), credit.
 */
export class ReliefImageryProvider {
  constructor(options = {}) {
    if (!options.template) throw new Error("Relief needs a terrain tile URL");
    this._template = options.template;
    this._colour = options.colour !== false;
    this._neighbours = options.neighbours !== false;
    this._tilingScheme = new Cesium.WebMercatorTilingScheme();
    const b = options.bounds;
    this._rectangle = Array.isArray(b) && b.length === 4 ? Cesium.Rectangle.fromDegrees(...b) : this._tilingScheme.rectangle;
    this._tileSize = options.tileSize || 256;
    this._maximumLevel = Number.isFinite(options.maxzoom) ? options.maxzoom : 13;
    this._errorEvent = new Cesium.Event();
    this._credit = options.credit ? new Cesium.Credit(options.credit) : undefined;
  }

  get tileWidth() { return this._tileSize; }
  get tileHeight() { return this._tileSize; }
  get maximumLevel() { return this._maximumLevel; }
  get minimumLevel() { return 0; }
  get tilingScheme() { return this._tilingScheme; }
  get rectangle() { return this._rectangle; }
  get tileDiscardPolicy() { return undefined; }
  get errorEvent() { return this._errorEvent; }
  get credit() { return this._credit; }
  get proxy() { return undefined; }
  get hasAlphaChannel() { return true; }

  getTileCredits() { return undefined; }
  pickFeatures() { return undefined; }

  async _neighbourGrids(z, x, y) {
    if (!this._neighbours) return {};
    const offsets = {nw: [-1, -1], n: [0, -1], ne: [1, -1], w: [-1, 0], e: [1, 0], sw: [-1, 1], s: [0, 1], se: [1, 1]};
    const entries = await Promise.all(Object.entries(offsets).map(async ([key, [dx, dy]]) => {
      const url = tileUrl(this._template, z, x + dx, y + dy);
      if (!url) return [key, null];
      try {
        return [key, (await loadTile(url)).values];
      } catch {
        return [key, null];   // best effort: clamp at that edge instead
      }
    }));
    return Object.fromEntries(entries);
  }

  async _render(tile, z, x, y) {
    const neighbours = await this._neighbourGrids(z, x, y);
    for (const key of Object.keys(neighbours)) {
      if (neighbours[key] && neighbours[key].length !== tile.values.length) neighbours[key] = null;
    }
    const rgba = shadeRelief(padHeights(tile.values, tile.size, neighbours), tile.size,
      {z, y, colour: this._colour, mask: tile.mask});
    const canvas = makeCanvas(tile.size, tile.size);
    canvas.getContext("2d").putImageData(new ImageData(rgba, tile.size, tile.size), 0, 0);
    return canvas;
  }

  requestImage(x, y, level, request) {
    const url = tileUrl(this._template, level, x, y);
    if (!url) return Promise.resolve(transparentTile());
    const centre = loadTile(url, request);
    if (!centre) return undefined;   // throttled by Cesium's RequestScheduler
    return centre.then(
      (tile) => this._render(tile, level, x, y),
      async (error) => {
        // Our own request was cancelled (tile no longer needed): reject so
        // Cesium marks it UNLOADED and requests it again when needed.
        if (isCancelled(request)) throw error;
        // A shared load started by ANOTHER tile's request got cancelled:
        // load it again ourselves instead of leaving a hole.
        if (centre.request && centre.request !== request && isCancelled(centre.request)) {
          try { return await this._render(await loadTile(url), level, x, y); } catch { /* fall through */ }
        }
        // Missing/broken tile: transparent, never an endless error loop.
        return transparentTile();
      },
    );
  }
}
