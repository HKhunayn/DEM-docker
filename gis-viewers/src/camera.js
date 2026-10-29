// Shared camera model for the three viewers + URL-hash persistence.
// DOM-free (unit-tested in Node).
//
// The common camera is MapLibre's: {lon, lat, zoom, bearing, pitch} where
// zoom is a 512 px web-mercator zoom, bearing is degrees clockwise from north
// and pitch is degrees from nadir (0 = looking straight down).
//
//   - ArcGIS SceneView: zoom levels follow the 256 px web-mercator LODs, so
//     ArcGIS zoom = MapLibre zoom + ARCGIS_ZOOM_OFFSET (1; verified in the
//     browser by comparing visible extents). heading = bearing, tilt = pitch.
//   - CesiumJS: no zoom; the camera sits `range` metres from the centre point
//     along the view ray. MapLibre shows H * metresPerPixel of ground over the
//     viewport height H, so range = H * mpp / (2 tan(fovy / 2)).
//     heading = bearing, Cesium pitch = pitch - 90 (degrees, -90 = nadir).

export const EARTH_CIRCUMFERENCE = 40075016.686;   // WGS84 equator, metres
export const TILE_SIZE = 512;                      // MapLibre zoom tiles
export const ARCGIS_ZOOM_OFFSET = 1;
export const MAX_PITCH = 80;

const clamp = (value, min, max) => Math.min(max, Math.max(min, value));
const rad = (deg) => (deg * Math.PI) / 180;

/** Wrap a bearing into (-180, 180]. */
export function normalizeBearing(deg) {
  const value = ((((Number(deg) || 0) + 180) % 360) + 360) % 360 - 180;
  return value === -180 ? 180 : value;
}

/** Wrap a longitude into [-180, 180]. */
export function wrapLon(lon) {
  const value = Number(lon);
  if (value >= -180 && value <= 180) return value;
  return ((((value + 180) % 360) + 360) % 360) - 180;
}

/** Validate/clean a camera; returns null when centre/zoom are unusable. */
export function cleanCamera(camera) {
  if (!camera) return null;
  const lat = Number(camera.lat), lon = Number(camera.lon), zoom = Number(camera.zoom);
  if (![lat, lon, zoom].every(Number.isFinite) || Math.abs(lat) > 90) return null;
  return {
    lon: wrapLon(lon),
    lat: clamp(lat, -85.051129, 85.051129),
    zoom: clamp(zoom, 0, 22),
    bearing: normalizeBearing(camera.bearing),
    pitch: clamp(Number(camera.pitch) || 0, 0, MAX_PITCH),
  };
}

// ---- URL hash: MapLibre's format "#map=zoom/lat/lon/bearing/pitch" ---------

/** Same rounding as maplibre-gl's Hash: precision follows the zoom. */
export function formatCameraHash(camera) {
  const c = cleanCamera(camera);
  if (!c) return "";
  const precision = Math.max(0, Math.ceil((c.zoom * Math.LN2 + Math.log(512 / 360 / 0.5)) / Math.LN10));
  const m = 10 ** precision;
  const fixed = (value) => Math.round(value * m) / m;
  let text = `${Math.round(c.zoom * 100) / 100}/${fixed(c.lat)}/${fixed(c.lon)}`;
  const bearing = Math.round(c.bearing * 10) / 10;
  const pitch = Math.round(c.pitch);
  if (bearing || pitch) text += `/${bearing}`;
  if (pitch) text += `/${pitch}`;
  return text;
}

/** Read the camera from a location.hash ("#map=..." or bare "#z/lat/lon"). */
export function parseCameraHash(hash) {
  const raw = String(hash ?? "").replace(/^#/, "");
  if (!raw) return null;
  let value = null;
  for (const part of raw.split("&")) {
    const [key, ...rest] = part.split("=");
    if (key === "map") value = rest.join("=");
  }
  if (value === null && /^[-\d.]+\/[-\d.]+\/[-\d.]+/.test(raw)) value = raw;
  if (!value) return null;
  const numbers = decodeURIComponent(value).split("/").map(Number);
  if (numbers.length < 3 || numbers.length > 5 || numbers.some((n) => !Number.isFinite(n))) return null;
  const [zoom, lat, lon, bearing = 0, pitch = 0] = numbers;
  return cleanCamera({zoom, lat, lon, bearing, pitch});
}

/** Replace (or add) the map= parameter of a hash, keeping other parameters. */
export function withCameraHash(hash, camera) {
  const value = formatCameraHash(camera);
  const parts = String(hash ?? "").replace(/^#/, "").split("&")
    .filter((part) => part && !part.startsWith("map=") && !/^[-\d.]+\/[-\d.]+\/[-\d.]+/.test(part));
  if (value) parts.unshift(`map=${value}`);
  return parts.length ? `#${parts.join("&")}` : "";
}

// ---- zoom <-> ground resolution ---------------------------------------------

/** Metres per CSS pixel at `lat` for a MapLibre (512 px tile) zoom. */
export function metersPerPixel(lat, zoom) {
  return (EARTH_CIRCUMFERENCE * Math.cos(rad(lat))) / (TILE_SIZE * 2 ** zoom);
}

/**
 * Cesium's frustum `fov` is the horizontal angle when the viewport is wider
 * than tall, otherwise the vertical one; return the vertical angle (radians).
 */
export function verticalFov(fov, width, height) {
  const aspect = width > 0 && height > 0 ? width / height : 1;
  return aspect > 1 ? 2 * Math.atan(Math.tan(fov / 2) / aspect) : fov;
}

/** Camera-to-centre distance (m) showing the same ground as MapLibre at `zoom`. */
export function rangeForZoom(zoom, lat, viewportHeight, fovy) {
  const height = viewportHeight > 0 ? viewportHeight : 800;
  return (height * metersPerPixel(lat, zoom)) / (2 * Math.tan(fovy / 2));
}

/** Inverse of rangeForZoom. */
export function zoomForRange(range, lat, viewportHeight, fovy) {
  const height = viewportHeight > 0 ? viewportHeight : 800;
  const mpp = (Math.max(range, 1) * 2 * Math.tan(fovy / 2)) / height;
  return clamp(Math.log2((EARTH_CIRCUMFERENCE * Math.cos(rad(lat))) / (TILE_SIZE * mpp)), 0, 22);
}

export const toArcgisZoom = (zoom) => zoom + ARCGIS_ZOOM_OFFSET;
export const fromArcgisZoom = (zoom) => zoom - ARCGIS_ZOOM_OFFSET;
/** MapLibre pitch (0 = nadir) -> Cesium pitch (-90 = nadir), degrees. */
export const toCesiumPitch = (pitch) => clamp(pitch, 0, MAX_PITCH) - 90;
export const fromCesiumPitch = (pitch) => clamp(pitch + 90, 0, MAX_PITCH);

/** True when two cameras are visually the same (for skipping no-op moves). */
export function sameCamera(a, b, {zoomTolerance = 0.05} = {}) {
  if (!a || !b) return false;
  const mpp = metersPerPixel(a.lat, a.zoom);
  const dy = (a.lat - b.lat) * 111_320, dx = (a.lon - b.lon) * 111_320 * Math.cos(rad(a.lat));
  return Math.hypot(dx, dy) < mpp * 2
    && Math.abs(a.zoom - b.zoom) < zoomTolerance
    && Math.abs(normalizeBearing(a.bearing - b.bearing)) < 0.5
    && Math.abs((a.pitch || 0) - (b.pitch || 0)) < 0.5;
}

/** Simple trailing-edge throttle (last call wins) for hash writes. */
export function throttle(fn, ms) {
  let timer = null, last = 0, pending = null;
  return (...args) => {
    pending = args;
    const wait = ms - (Date.now() - last);
    if (wait <= 0 && !timer) { last = Date.now(); fn(...pending); pending = null; return; }
    if (!timer) timer = setTimeout(() => { timer = null; last = Date.now(); const call = pending; pending = null; if (call) fn(...call); }, Math.max(wait, 0));
  };
}
