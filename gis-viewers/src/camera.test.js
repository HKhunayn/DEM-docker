import test from "node:test";
import assert from "node:assert/strict";
import {
  ARCGIS_ZOOM_OFFSET, cleanCamera, formatCameraHash, fromArcgisZoom, fromCesiumPitch, metersPerPixel, normalizeBearing,
  parseCameraHash, rangeForZoom, sameCamera, throttle, toArcgisZoom, toCesiumPitch, verticalFov, withCameraHash, wrapLon, zoomForRange,
} from "./camera.js";

const near = (actual, expected, eps = 1e-6) => assert.ok(Math.abs(actual - expected) < eps, `${actual} != ${expected}`);

test("normalizeBearing / wrapLon", () => {
  assert.equal(normalizeBearing(0), 0);
  assert.equal(normalizeBearing(190), -170);
  assert.equal(normalizeBearing(-180), 180);
  assert.equal(normalizeBearing(540), 180);
  assert.equal(normalizeBearing(-725), -5);
  assert.equal(wrapLon(46.7), 46.7);
  assert.equal(wrapLon(190), -170);
  assert.equal(wrapLon(-200), 160);
});

test("cleanCamera clamps and rejects bad input", () => {
  assert.deepEqual(cleanCamera({lon: 46, lat: 24, zoom: 30, bearing: 370, pitch: 95}), {lon: 46, lat: 24, zoom: 22, bearing: 10, pitch: 80});
  assert.equal(cleanCamera({lon: 46, lat: 95, zoom: 3}), null);
  assert.equal(cleanCamera({lon: "x", lat: 24, zoom: 3}), null);
  assert.equal(cleanCamera(null), null);
});

test("formatCameraHash matches maplibre-gl's Hash format", () => {
  // maplibre: zoom 2 decimals, precision from zoom, bearing/pitch omitted when 0
  assert.equal(formatCameraHash({zoom: 10, lat: 24.687712345, lon: 46.721912345, bearing: 0, pitch: 0}), "10/24.6877/46.7219");
  assert.equal(formatCameraHash({zoom: 10.123, lat: 24.6877, lon: 46.7219, bearing: 12.34, pitch: 0}), "10.12/24.6877/46.7219/12.3");
  assert.equal(formatCameraHash({zoom: 3, lat: 24.6877, lon: 46.7219, bearing: 0, pitch: 55.4}), "3/24.69/46.72/0/55");
  assert.equal(formatCameraHash({zoom: 16, lat: 24.68771234, lon: 46.72191234}), "16/24.687712/46.721912");
  assert.equal(formatCameraHash(null), "");
});

test("parseCameraHash reads #map=, bare MapLibre hashes, and rejects junk", () => {
  assert.deepEqual(parseCameraHash("#map=10.5/24.6877/46.7219/-20/45"), {zoom: 10.5, lat: 24.6877, lon: 46.7219, bearing: -20, pitch: 45});
  assert.deepEqual(parseCameraHash("#foo=1&map=8/21.4/39.8"), {zoom: 8, lat: 21.4, lon: 39.8, bearing: 0, pitch: 0});
  assert.deepEqual(parseCameraHash("#7/24/46"), {zoom: 7, lat: 24, lon: 46, bearing: 0, pitch: 0});
  for (const hash of ["", "#", "#map=", "#map=10/abc/46", "#map=10/24", "#map=1/2/3/4/5/6", "#map=10/100/46", "#other=1"]) {
    assert.equal(parseCameraHash(hash), null, hash);
  }
});

test("hash round trip and parameter preservation", () => {
  const camera = {zoom: 12.25, lat: 21.4225, lon: 39.8262, bearing: 30, pitch: 60};
  const hash = withCameraHash("", camera);
  assert.equal(hash, "#map=12.25/21.4225/39.8262/30/60");
  assert.deepEqual(parseCameraHash(hash), camera);
  assert.equal(withCameraHash("#map=1/2/3&view=cesium", camera), "#map=12.25/21.4225/39.8262/30/60&view=cesium");
  assert.equal(withCameraHash("#1/2/3", camera), hash);
  assert.equal(withCameraHash("#a=b", null), "#a=b");
});

test("metersPerPixel: 512 px tiles", () => {
  near(metersPerPixel(0, 0), 40075016.686 / 512);
  near(metersPerPixel(60, 1), 40075016.686 * 0.5 / 1024, 1e-6);
});

test("verticalFov follows Cesium's frustum convention", () => {
  const fov = Math.PI / 3;
  near(verticalFov(fov, 1000, 500), 2 * Math.atan(Math.tan(fov / 2) / 2));
  near(verticalFov(fov, 500, 1000), fov);
  near(verticalFov(fov, 0, 0), fov);
});

test("Cesium range <-> zoom round trip and MapLibre equivalence", () => {
  const fovy = verticalFov(Math.PI / 3, 1200, 800);
  for (const [zoom, lat] of [[3, 0], [10, 24.7], [14.5, 60], [18, -33]]) {
    const range = rangeForZoom(zoom, lat, 800, fovy);
    near(zoomForRange(range, lat, 800, fovy), zoom, 1e-9);
  }
  // With MapLibre's own vertical fov (36.87 deg) the range is MapLibre's
  // camera-to-centre distance: 1.5 viewport heights of pixels.
  const mlFovy = 2 * Math.atan(1 / 3);
  near(rangeForZoom(10, 24.7, 800, mlFovy), 1.5 * 800 * metersPerPixel(24.7, 10), 1e-6);
  // Zooming in one level halves the distance.
  near(rangeForZoom(11, 24.7, 800, fovy) * 2, rangeForZoom(10, 24.7, 800, fovy), 1e-6);
});

test("ArcGIS zoom offset and Cesium pitch mapping", () => {
  assert.equal(ARCGIS_ZOOM_OFFSET, 1);
  assert.equal(toArcgisZoom(10), 11);
  assert.equal(fromArcgisZoom(11), 10);
  assert.equal(toCesiumPitch(0), -90);
  assert.equal(toCesiumPitch(55), -35);
  assert.equal(toCesiumPitch(120), -10);
  assert.equal(fromCesiumPitch(-90), 0);
  assert.equal(fromCesiumPitch(-35), 55);
  assert.equal(fromCesiumPitch(10), 80);
});

test("sameCamera tolerates sub-pixel differences only", () => {
  const a = {lon: 46.7, lat: 24.7, zoom: 10, bearing: 0, pitch: 40};
  assert.ok(sameCamera(a, {...a, lon: 46.70001}));
  assert.ok(sameCamera(a, {...a, bearing: 359.8}));
  assert.ok(!sameCamera(a, {...a, lon: 46.8}));
  assert.ok(!sameCamera(a, {...a, zoom: 10.5}));
  assert.ok(!sameCamera(a, {...a, pitch: 45}));
  assert.ok(!sameCamera(a, null));
});

test("throttle runs the first call now and the last one later", async () => {
  const calls = [];
  const run = throttle((value) => calls.push(value), 30);
  run(1); run(2); run(3);
  assert.deepEqual(calls, [1]);
  await new Promise((resolve) => setTimeout(resolve, 60));
  assert.deepEqual(calls, [1, 3]);
});
