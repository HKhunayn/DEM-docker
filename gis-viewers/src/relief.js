// Shared relief styling + pure shading maths (no DOM, no Cesium, unit-tested).
//
// MapLibre renders the relief on the GPU from these constants (color-relief +
// multidirectional hillshade layers); CesiumJS renders the same look on the
// CPU per imagery tile with shadeRelief(), which reimplements MapLibre 5.x's
// hillshade_prepare + multidirectional hillshade shaders. Change the look
// here only, so both viewers stay identical.

/** Page / globe background behind the relief. */
export const MAP_BACKGROUND = "#e9e2d3";

/** Hypsometric ramp, absolute metres -> colour (0 m = sea / water). */
export const RELIEF_RAMP = Object.freeze([
  [-500, "#5d8fb5"], [0, "#a9c8dc"], [0.5, "#b9cf9f"], [150, "#cfd8a6"], [400, "#e6dcae"],
  [800, "#e2c48f"], [1200, "#cfa271"], [1800, "#b07f55"], [2500, "#8f6a4f"],
  [3500, "#9b8f86"], [4500, "#dedad5"], [6000, "#ffffff"],
]);

export const COLOR_RELIEF_OPACITY = 0.85;

/** Multidirectional hillshade: light from fixed map directions. */
export const HILLSHADE = Object.freeze({
  directions: [270, 315, 0, 45],
  altitudes: [30, 30, 30, 30],
  highlightColors: ["#fffaf0", "#fffaf0", "#fffaf0", "#fffaf0"],
  shadowColors: ["#3d3022", "#3d3022", "#5a4a36", "#5a4a36"],
  /** [zoom, exaggeration] stops, linear in between, clamped outside. */
  exaggeration: [[3, 0.25], [8, 0.45], [12, 0.6]],
});

const EARTH_CIRCUMFERENCE = 2 * Math.PI * 6378137;

// ---- MapLibre style expressions built from the shared constants ----------

export function colorReliefExpression(ramp = RELIEF_RAMP) {
  return ["interpolate", ["linear"], ["elevation"], ...ramp.flat()];
}

export function hillshadeExaggerationExpression(stops = HILLSHADE.exaggeration) {
  return ["interpolate", ["linear"], ["zoom"], ...stops.flat()];
}

// ---- pure maths -----------------------------------------------------------

/** Terrarium: height_m = R*256 + G + B/256 - 32768. */
export function terrariumHeight(r, g, b) {
  return r * 256 + g + b / 256 - 32768;
}

/** Tile URL from an XYZ template; x wraps around the antimeridian; null for rows outside the world. */
export function tileUrl(template, z, x, y) {
  const n = 2 ** z;
  const col = ((x % n) + n) % n;
  if (y < 0 || y >= n) return null;
  return template.replace("{z}", z).replace("{x}", col).replace("{y}", y);
}

/** "#rrggbb" -> [r, g, b] (0-255). */
export function hexToRgb(hex) {
  const value = String(hex).replace("#", "");
  const full = value.length === 3 ? [...value].map((c) => c + c).join("") : value;
  const n = Number.parseInt(full, 16);
  if (full.length !== 6 || !Number.isFinite(n)) throw new Error(`Bad colour: ${hex}`);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** Parse a [metres, "#hex"] ramp once into {heights, colors} for rampColor(). */
export function parseRamp(ramp = RELIEF_RAMP) {
  return {heights: ramp.map(([h]) => h), colors: ramp.map(([, c]) => hexToRgb(c))};
}

const DEFAULT_RAMP = parseRamp();

/**
 * Linear interpolation of the ramp at `height` (clamped to the end stops),
 * like MapLibre's ["interpolate", ["linear"], ["elevation"], ...]. Writes
 * r,g,b (floats 0-255) into `out` and returns it.
 */
export function rampColor(height, parsed = DEFAULT_RAMP, out = [0, 0, 0]) {
  const {heights, colors} = parsed;
  const last = heights.length - 1;
  if (!(height > heights[0])) { out[0] = colors[0][0]; out[1] = colors[0][1]; out[2] = colors[0][2]; return out; }
  if (height >= heights[last]) { out[0] = colors[last][0]; out[1] = colors[last][1]; out[2] = colors[last][2]; return out; }
  let i = 1;
  while (heights[i] < height) i += 1;
  const t = (height - heights[i - 1]) / (heights[i] - heights[i - 1]);
  const a = colors[i - 1]; const b = colors[i];
  out[0] = a[0] + (b[0] - a[0]) * t; out[1] = a[1] + (b[1] - a[1]) * t; out[2] = a[2] + (b[2] - a[2]) * t;
  return out;
}

/** Piecewise-linear exaggeration for a (map) zoom, clamped to the end stops. */
export function exaggerationAtZoom(zoom, stops = HILLSHADE.exaggeration) {
  if (zoom <= stops[0][0]) return stops[0][1];
  for (let i = 1; i < stops.length; i += 1) {
    if (zoom <= stops[i][0]) {
      const [z0, e0] = stops[i - 1]; const [z1, e1] = stops[i];
      return e0 + (e1 - e0) * (zoom - z0) / (z1 - z0);
    }
  }
  return stops[stops.length - 1][1];
}

/** Latitude (degrees) of a Web-Mercator position given as a 0..1 fraction from the north edge. */
export function mercatorLatitude(yFraction) {
  return (Math.atan(Math.sinh(Math.PI * (1 - 2 * yFraction))) * 180) / Math.PI;
}

/** Ground size (m) of one pixel of a `tileSize` px tile at zoom z and latitude (deg). */
export function pixelSizeMeters(latitude, z, tileSize = 256) {
  return (EARTH_CIRCUMFERENCE * Math.cos((latitude * Math.PI) / 180)) / (tileSize * 2 ** z);
}

/**
 * Build a (size+2)^2 grid: the centre tile plus a 1 px border taken from the
 * 8 neighbour tiles, so Sobel gradients on the tile edge use real data and
 * adjacent tiles shade identically across the seam. A missing neighbour
 * (null/undefined) is replaced by clamping to the centre tile's edge.
 * `n` = {n, s, e, w, nw, ne, sw, se}: Float32Array(size*size) or null.
 */
export function padHeights(center, size, n = {}) {
  const w = size + 2;
  const out = new Float32Array(w * w);
  const at = (grid, row, col) => grid[row * size + col];
  for (let row = 0; row < size; row += 1) {
    out.set(center.subarray(row * size, row * size + size), (row + 1) * w + 1);
    out[(row + 1) * w] = n.w ? at(n.w, row, size - 1) : at(center, row, 0);
    out[(row + 1) * w + size + 1] = n.e ? at(n.e, row, 0) : at(center, row, size - 1);
  }
  for (let col = 0; col < size; col += 1) {
    out[col + 1] = n.n ? at(n.n, size - 1, col) : at(center, 0, col);
    out[(size + 1) * w + col + 1] = n.s ? at(n.s, 0, col) : at(center, size - 1, col);
  }
  // Corners: diagonal neighbour, else the adjacent padded border cell.
  out[0] = n.nw ? at(n.nw, size - 1, size - 1) : (n.n ? out[1] : n.w ? out[w] : at(center, 0, 0));
  out[size + 1] = n.ne ? at(n.ne, size - 1, 0) : (n.n ? out[size] : n.e ? out[w + size + 1] : at(center, 0, size - 1));
  out[(size + 1) * w] = n.sw ? at(n.sw, 0, size - 1) : (n.s ? out[(size + 1) * w + 1] : n.w ? out[size * w] : at(center, size - 1, 0));
  out[(size + 1) * w + size + 1] = n.se ? at(n.se, 0, 0)
    : (n.s ? out[(size + 1) * w + size] : n.e ? out[size * w + size + 1] : at(center, size - 1, size - 1));
  return out;
}

function lights(style) {
  const rad = Math.PI / 180;
  return style.directions.map((direction, i) => {
    const altitude = style.altitudes[i] * rad; const azimuth = direction * rad;
    return {
      sinAlt: Math.sin(altitude), cosAlt: Math.cos(altitude),
      // MapLibre multidirectional: cos_az = -cos(az), sin_az = -sin(az).
      cosAz: -Math.cos(azimuth), sinAz: -Math.sin(azimuth),
      highlight: hexToRgb(style.highlightColors[i]).map((c) => c / 255),
      shadow: hexToRgb(style.shadowColors[i]).map((c) => c / 255),
    };
  });
}

/** MapLibre's hillshade_prepare zoom boost: shallow terrain stays visible at low zoom. */
export function prepareZoomBoost(z) {
  const factor = z < 2 ? 0.4 : z < 4.5 ? 0.35 : 0.3;
  return z < 15 ? 2 ** ((15 - z) * factor) : 1;
}

/**
 * Colour + multidirectional hillshade for one tile.
 *
 * padded: (size+2)^2 heights from padHeights(). Options:
 *   z, y        tile zoom and row (Web Mercator, y=0 north) -> latitude per row
 *   exaggeration  hillshade exaggeration (default exaggerationAtZoom(z - 1):
 *               a 256 px tile at level z is drawn at about MapLibre zoom z-1)
 *   colour      true: opaque hypsometric colour (over MAP_BACKGROUND, at
 *               COLOR_RELIEF_OPACITY) with the shade composited on top;
 *               false: shade only, semi-transparent (for draping over imagery)
 *   mask        optional Uint8Array(size*size), 0 = no data -> transparent
 * Returns RGBA Uint8ClampedArray(size*size*4), non-premultiplied.
 */
export function shadeRelief(padded, size, options = {}) {
  const z = options.z ?? 0; const tileY = options.y ?? 0;
  const style = options.style || HILLSHADE;
  const exaggeration = options.exaggeration ?? exaggerationAtZoom(z - 1, style.exaggeration);
  const colour = options.colour !== false;
  const opacity = options.opacity ?? COLOR_RELIEF_OPACITY;
  const ramp = options.ramp ? parseRamp(options.ramp) : DEFAULT_RAMP;
  const background = hexToRgb(options.background || MAP_BACKGROUND);
  const mask = options.mask;
  const sources = lights(style);
  const count = sources.length;
  const w = size + 2;
  // Sobel sum = 8 * slope * pixel size. MapLibre divides by the EQUATORIAL
  // pixel size (and its zoom boost), clamps to +-4 (texture encoding), then
  // divides by cos(latitude) in the hillshade pass -> true ground pixel size.
  const scale = prepareZoomBoost(z) / (8 * (EARTH_CIRCUMFERENCE / (size * 2 ** z)));
  const out = new Uint8ClampedArray(size * size * 4);
  const rgb = [0, 0, 0];
  for (let row = 0; row < size; row += 1) {
    const latitude = mercatorLatitude((tileY * size + row + 0.5) / (size * 2 ** z));
    const cosLat = Math.max(1e-6, Math.cos((latitude * Math.PI) / 180));
    const top = row * w; const mid = top + w; const bottom = mid + w;
    for (let col = 0; col < size; col += 1) {
      const index = row * size + col; const p = index * 4;
      if (mask && mask[index] === 0) { out[p + 3] = 0; continue; }
      const a = padded[top + col]; const b = padded[top + col + 1]; const c = padded[top + col + 2];
      const d = padded[mid + col]; const f = padded[mid + col + 2];
      const g = padded[bottom + col]; const h = padded[bottom + col + 1]; const i = padded[bottom + col + 2];
      // x: east positive; y: SOUTH positive (row order), as in MapLibre.
      let dx = ((c + f + f + i) - (a + d + d + g)) * scale;
      let dy = ((g + h + h + i) - (a + b + b + c)) * scale;
      dx = Math.max(-4, Math.min(4, dx)) / cosLat * exaggeration * 2;
      dy = Math.max(-4, Math.min(4, dy)) / cosLat * exaggeration * 2;
      const norm = Math.sqrt(1 + dx * dx + dy * dy);
      let sr = 0; let sg = 0; let sb = 0; let sa = 0;   // premultiplied shade
      for (let k = 0; k < count; k += 1) {
        const light = sources[k];
        const cang = (light.sinAlt - (dy * light.cosAz * light.cosAlt - dx * light.sinAz * light.cosAlt)) / norm;
        const shade = Math.max(0, Math.min(1, cang));
        if (shade > 0.5) {
          const weight = (2 * shade - 1) / count;
          sr += light.highlight[0] * weight; sg += light.highlight[1] * weight; sb += light.highlight[2] * weight; sa += weight;
        } else {
          const weight = (1 - 2 * shade) / count;
          sr += light.shadow[0] * weight; sg += light.shadow[1] * weight; sb += light.shadow[2] * weight; sa += weight;
        }
      }
      if (colour) {
        rampColor(padded[mid + col + 1], ramp, rgb);
        const keep = 1 - sa;
        out[p] = sr * 255 + (rgb[0] * opacity + background[0] * (1 - opacity)) * keep;
        out[p + 1] = sg * 255 + (rgb[1] * opacity + background[1] * (1 - opacity)) * keep;
        out[p + 2] = sb * 255 + (rgb[2] * opacity + background[2] * (1 - opacity)) * keep;
        out[p + 3] = 255;
      } else if (sa * 255 >= 0.5) {   // (flat ground: sa ~ 1e-16 -> transparent)
        out[p] = (sr / sa) * 255; out[p + 1] = (sg / sa) * 255; out[p + 2] = (sb / sa) * 255;
        out[p + 3] = sa * 255;
      }
    }
  }
  return out;
}
