import test from "node:test";
import assert from "node:assert/strict";
import {
  COLOR_RELIEF_OPACITY, HILLSHADE, MAP_BACKGROUND, RELIEF_RAMP, colorReliefExpression, exaggerationAtZoom,
  hexToRgb, hillshadeExaggerationExpression, mercatorLatitude, padHeights, parseRamp, pixelSizeMeters,
  prepareZoomBoost, rampColor, shadeRelief, terrariumHeight, tileUrl,
} from "./relief.js";

const close = (actual, expected, eps = 1e-6) => assert.ok(Math.abs(actual - expected) <= eps, `${actual} != ${expected}`);

test("terrariumHeight decodes R*256 + G + B/256 - 32768", () => {
  assert.equal(terrariumHeight(128, 0, 0), 0);
  assert.equal(terrariumHeight(131, 232, 128), 1000.5);
  assert.equal(terrariumHeight(127, 255, 0), -1);
  assert.equal(terrariumHeight(0, 0, 0), -32768);
});

test("shared MapLibre expressions carry the exact ramp and exaggeration stops", () => {
  assert.deepEqual(colorReliefExpression(), ["interpolate", ["linear"], ["elevation"],
    -500, "#5d8fb5", 0, "#a9c8dc", 0.5, "#b9cf9f", 150, "#cfd8a6", 400, "#e6dcae",
    800, "#e2c48f", 1200, "#cfa271", 1800, "#b07f55", 2500, "#8f6a4f",
    3500, "#9b8f86", 4500, "#dedad5", 6000, "#ffffff"]);
  assert.deepEqual(hillshadeExaggerationExpression(), ["interpolate", ["linear"], ["zoom"], 3, 0.25, 8, 0.45, 12, 0.6]);
  assert.deepEqual(HILLSHADE.directions, [270, 315, 0, 45]);
  assert.deepEqual(HILLSHADE.altitudes, [30, 30, 30, 30]);
  assert.equal(COLOR_RELIEF_OPACITY, 0.85);
  assert.equal(MAP_BACKGROUND, "#e9e2d3");
});

test("hexToRgb parses 6 and 3 digit colours and rejects junk", () => {
  assert.deepEqual(hexToRgb("#a9c8dc"), [169, 200, 220]);
  assert.deepEqual(hexToRgb("#fff"), [255, 255, 255]);
  assert.throws(() => hexToRgb("nope"));
});

test("rampColor hits stops exactly, interpolates linearly and clamps", () => {
  const ramp = parseRamp(RELIEF_RAMP);
  assert.deepEqual(rampColor(0, ramp), [169, 200, 220]);
  assert.deepEqual(rampColor(6000, ramp), [255, 255, 255]);
  assert.deepEqual(rampColor(99999, ramp), [255, 255, 255]);
  assert.deepEqual(rampColor(-9999, ramp), hexToRgb("#5d8fb5"));
  assert.deepEqual(rampColor(Number.NaN, ramp), hexToRgb("#5d8fb5"));
  // halfway between 150 #cfd8a6 and 400 #e6dcae
  const mid = rampColor(275, ramp);
  close(mid[0], (0xcf + 0xe6) / 2); close(mid[1], (0xd8 + 0xdc) / 2); close(mid[2], (0xa6 + 0xae) / 2);
  // 0 m is water, 0.5 m already land
  assert.deepEqual(rampColor(0.5, ramp), hexToRgb("#b9cf9f"));
});

test("exaggerationAtZoom interpolates the stops and clamps", () => {
  assert.equal(exaggerationAtZoom(0), 0.25);
  assert.equal(exaggerationAtZoom(3), 0.25);
  close(exaggerationAtZoom(5.5), 0.35);
  assert.equal(exaggerationAtZoom(8), 0.45);
  close(exaggerationAtZoom(10), 0.525);
  assert.equal(exaggerationAtZoom(20), 0.6);
});

test("pixel size and latitude follow Web Mercator", () => {
  close(pixelSizeMeters(0, 0), 2 * Math.PI * 6378137 / 256, 1e-6);
  close(pixelSizeMeters(60, 10), 2 * Math.PI * 6378137 * 0.5 / (256 * 1024), 1e-6);
  close(mercatorLatitude(0.5), 0);
  close(mercatorLatitude(0), 85.0511287798, 1e-6);
  assert.ok(mercatorLatitude(0.25) > 0 && mercatorLatitude(0.75) < 0);
  close(prepareZoomBoost(15), 1); close(prepareZoomBoost(13), 2 ** 0.6); close(prepareZoomBoost(1), 2 ** 5.6);
});

test("tileUrl wraps x, rejects rows outside the world", () => {
  const t = "http://h/t/{z}/{x}/{y}.webp";
  assert.equal(tileUrl(t, 2, 1, 3), "http://h/t/2/1/3.webp");
  assert.equal(tileUrl(t, 2, -1, 0), "http://h/t/2/3/0.webp");
  assert.equal(tileUrl(t, 2, 4, 0), "http://h/t/2/0/0.webp");
  assert.equal(tileUrl(t, 2, 0, -1), null);
  assert.equal(tileUrl(t, 2, 0, 4), null);
});

test("padHeights copies neighbour edges and clamps missing ones", () => {
  const size = 2;
  const c = Float32Array.from([1, 2, 3, 4]);
  const clamped = padHeights(c, size);
  assert.deepEqual([...clamped], [1, 1, 2, 2, 1, 1, 2, 2, 3, 3, 4, 4, 3, 3, 4, 4]);
  const grid = (v) => Float32Array.from([v, v + 1, v + 2, v + 3]);
  const padded = padHeights(c, size, {n: grid(10), s: grid(20), w: grid(30), e: grid(40),
    nw: grid(50), ne: grid(60), sw: grid(70), se: grid(80)});
  assert.deepEqual([...padded], [
    53, 12, 13, 62,
    31, 1, 2, 40,
    33, 3, 4, 42,
    71, 20, 21, 80,
  ]);
});

function flatPadded(size, height) {
  return new Float32Array((size + 2) ** 2).fill(height);
}

test("flat terrain: ramp colour over background, neutral shade", () => {
  const size = 4;
  const rgba = shadeRelief(flatPadded(size, 0), size, {z: 8, y: 100});
  // Flat ground with sun altitude 30 deg: cang = sin(30) = 0.5 -> shade weight 0.
  const base = hexToRgb("#a9c8dc").map((c, i) => c * 0.85 + hexToRgb(MAP_BACKGROUND)[i] * 0.15);
  for (let p = 0; p < size * size * 4; p += 4) {
    assert.deepEqual([rgba[p], rgba[p + 1], rgba[p + 2], rgba[p + 3]], [...base.map((v) => Math.round(v)), 255]);
  }
  const shadeOnly = shadeRelief(flatPadded(size, 500), size, {z: 8, y: 100, colour: false});
  assert.ok([...shadeOnly].every((v) => v === 0), "flat shade-only tile is fully transparent");
});

function slopeTile(size, dzdx, dzdy) {
  // heights rising east (dzdx per pixel) and south (dzdy per pixel)
  const padded = new Float32Array((size + 2) ** 2);
  for (let r = 0; r < size + 2; r += 1) for (let c = 0; c < size + 2; c += 1) padded[r * (size + 2) + c] = 1000 + c * dzdx + r * dzdy;
  return padded;
}

test("slopes facing the NW lights are brighter than SE-facing ones", () => {
  const size = 4; const opts = {z: 10, y: 400, colour: false, exaggeration: 0.6};
  // Rising towards the SE = slope faces NW (towards the 315 deg light).
  const facingNW = shadeRelief(slopeTile(size, 20, 20), size, opts);
  const facingSE = shadeRelief(slopeTile(size, -20, -20), size, opts);
  const lum = (px) => px[0] + px[1] + px[2];
  const nw = [facingNW[20], facingNW[21], facingNW[22]]; const se = [facingSE[20], facingSE[21], facingSE[22]];
  assert.ok(lum(nw) > lum(se), `NW-facing ${nw} should be lighter than SE-facing ${se}`);
  assert.ok(facingNW[23] > 0 && facingSE[23] > 0, "sloped pixels carry shade alpha");
  // Uniform slope -> every pixel identical (no edge artefacts with padding).
  for (let p = 0; p < size * size * 4; p += 4) assert.deepEqual([...facingNW.subarray(p, p + 4)], [...facingNW.subarray(0, 4)]);
});

test("shadeRelief matches MapLibre's multidirectional maths for a pixel", () => {
  const size = 1; const z = 10; const y = 400; const exaggeration = 0.5;
  const padded = slopeTile(size, 5000, 0);   // rising east only (1 px = 39 km at z10)
  const [r, g, b, a] = shadeRelief(padded, size, {z, y, colour: false, exaggeration});
  // Reference: MapLibre hillshade_prepare + hillshade (multidirectional).
  const lat = mercatorLatitude((y + 0.5) / 2 ** z);
  const eq = 2 * Math.PI * 6378137 / 2 ** z;
  let dx = Math.max(-4, Math.min(4, (8 * 5000) * prepareZoomBoost(z) / (8 * eq)));
  dx = dx / Math.cos(lat * Math.PI / 180) * exaggeration * 2;
  const dy = 0;
  let acc = [0, 0, 0, 0];
  HILLSHADE.directions.forEach((dir, i) => {
    const alt = 30 * Math.PI / 180; const az = dir * Math.PI / 180;
    const cang = (Math.sin(alt) - (dy * -Math.cos(az) * Math.cos(alt) - dx * -Math.sin(az) * Math.cos(alt))) / Math.sqrt(1 + dx * dx);
    const shade = Math.max(0, Math.min(1, cang));
    const color = (shade > 0.5 ? hexToRgb(HILLSHADE.highlightColors[i]) : hexToRgb(HILLSHADE.shadowColors[i])).map((c) => c / 255);
    const w = (shade > 0.5 ? 2 * shade - 1 : 1 - 2 * shade) / 4;
    acc = [acc[0] + color[0] * w, acc[1] + color[1] * w, acc[2] + color[2] * w, acc[3] + w];
  });
  assert.ok(a > 10, `pixel should be visibly shaded (alpha ${a})`);
  close(a, Math.round(acc[3] * 255), 1);
  close(r, Math.round(acc[0] / acc[3] * 255), 1);
  close(g, Math.round(acc[1] / acc[3] * 255), 1);
  close(b, Math.round(acc[2] / acc[3] * 255), 1);
});

test("shadeRelief leaves masked (no-data) pixels transparent", () => {
  const size = 2;
  const rgba = shadeRelief(flatPadded(size, 100), size, {z: 5, y: 10, mask: Uint8Array.from([1, 0, 1, 1])});
  assert.equal(rgba[3], 255); assert.equal(rgba[7], 0); assert.equal(rgba[11], 255);
});

test("neighbour padding removes the tile-edge seam of a uniform slope", () => {
  const size = 4;
  const plane = (ox, oy) => {
    const g = new Float32Array(size * size);
    for (let r = 0; r < size; r += 1) for (let c = 0; c < size; c += 1) g[r * size + c] = (ox + c) * 700 + (oy + r) * 300;
    return g;
  };
  const centre = plane(0, 0);
  const n = {n: plane(0, -size), s: plane(0, size), w: plane(-size, 0), e: plane(size, 0),
    nw: plane(-size, -size), ne: plane(size, -size), sw: plane(-size, size), se: plane(size, size)};
  const withNeighbours = shadeRelief(padHeights(centre, size, n), size, {z: 9, y: 200, colour: false});
  const clamped = shadeRelief(padHeights(centre, size), size, {z: 9, y: 200, colour: false});
  // Same row (same latitude): west-edge pixel vs its interior neighbour.
  const edge = [...withNeighbours.subarray(size * 4, size * 4 + 4)]; const inner = [...withNeighbours.subarray((size + 1) * 4, (size + 1) * 4 + 4)];
  assert.deepEqual(edge, inner, "edge pixel shades like an interior pixel");
  assert.notDeepEqual([...clamped.subarray(size * 4, size * 4 + 4)], inner, "clamping alone would leave a seam");
});
