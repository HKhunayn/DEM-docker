// Pure Terrarium decoding + a tiny LRU cache (no Cesium import, unit-tested).

/**
 * Decode Terrarium RGBA pixels into heights (metres).
 *
 * height = R * 256 + G + B / 256 - 32768. Pixels with alpha 0 are "no data":
 * they are never decoded (their RGB is meaningless and would read as
 * -32768 m, i.e. a pit to the Earth's core). Instead they get the mean of the
 * tile's valid pixels, or `fallback` when the whole tile is empty, so a hole
 * stays flush with its surroundings rather than becoming a spike.
 */
export function decodeTerrarium(rgba, count = rgba.length / 4, fallback = 0) {
  const values = new Float32Array(count);
  let sum = 0; let valid = 0; let missing = 0;
  for (let i = 0; i < count; i += 1) {
    const p = i * 4;
    if (rgba[p + 3] === 0) { values[i] = Number.NaN; missing += 1; continue; }
    const height = rgba[p] * 256 + rgba[p + 1] + rgba[p + 2] / 256 - 32768;
    values[i] = height; sum += height; valid += 1;
  }
  if (missing) {
    const fill = valid ? sum / valid : fallback;
    for (let i = 0; i < count; i += 1) if (Number.isNaN(values[i])) values[i] = fill;
  }
  return values;
}

/** Map-backed LRU: a hit is re-inserted so the oldest-used entry is evicted first. */
export class LruCache {
  constructor(maxEntries = 256) {
    this.maxEntries = Math.max(1, maxEntries);
    this.map = new Map();
  }

  get size() { return this.map.size; }

  has(key) { return this.map.has(key); }

  get(key) {
    if (!this.map.has(key)) return undefined;
    const value = this.map.get(key);
    this.map.delete(key);
    this.map.set(key, value);
    return value;
  }

  set(key, value) {
    this.map.delete(key);
    this.map.set(key, value);
    while (this.map.size > this.maxEntries) this.map.delete(this.map.keys().next().value);
    return this;
  }

  delete(key) { return this.map.delete(key); }

  clear() { this.map.clear(); }
}
