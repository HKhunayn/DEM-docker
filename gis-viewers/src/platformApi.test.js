import test from "node:test";
import assert from "node:assert/strict";
import {
  PlatformApi,
  areaItems,
  catalogItems,
  formatError,
  imagerySets,
  imageryTemplate,
  itemBounds,
  mergeItems,
  resolveApiUrl,
  tileUrls,
} from "./platformApi.js";

test("catalogItems accepts the platform catalog and GeoJSON item shapes", () => {
  assert.deepEqual(catalogItems({items: [{id: "a"}]}), [{id: "a"}]);
  assert.deepEqual(catalogItems({features: [{id: "b"}]}), [{id: "b"}]);
  assert.deepEqual(catalogItems({}), []);
});

test("itemBounds reads a saved area's bbox from common platform shapes", () => {
  assert.deepEqual(itemBounds({bbox: [1, 2, 3, 4]}), [1, 2, 3, 4]);
  assert.deepEqual(itemBounds({geometry: {bbox: [5, 6, 7, 8]}}), [5, 6, 7, 8]);
  assert.deepEqual(itemBounds({properties: {extent: {bbox: [9, 10, 11, 12]}}}), [9, 10, 11, 12]);
  assert.equal(itemBounds({}), null);
  assert.equal(itemBounds({bbox: [1, "x", 3, 4]}), null);
});

test("resolveApiUrl preserves absolute URLs and resolves platform paths", () => {
  assert.equal(resolveApiUrl("https://example.test/api/items", "http://platform.test"), "https://example.test/api/items");
  assert.equal(resolveApiUrl("/tiles/terrain.json", "http://platform.test/"), "http://platform.test/tiles/terrain.json");
});

test("mergeItems surfaces saved areas from the fetch page alongside catalog items", () => {
  const areas = {areas: [{id: "area-1", name: "verify_glo30", bbox: [46, 24, 47, 25]}]};
  const catalog = {items: [{id: "ingest-1", title: "manifest"}]};
  const merged = mergeItems(areas, catalog);
  assert.equal(merged.length, 2);
  assert.equal(areaItems(areas).length, 1);
  assert.equal(merged[0].id, "area-1", "areas come first");
});

test("mergeItems: on a duplicate id the saved area wins, catalog-only fields are kept", () => {
  const deduped = mergeItems(
    {areas: [{id: "x", name: "a", bbox: [1, 2, 3, 4], dataset: {state: "done"}}]},
    {items: [{id: "x", name: "b", bbox: [5, 6, 7, 8], links: [{rel: "self", href: "/x"}]}]},
  );
  assert.equal(deduped.length, 1);
  assert.deepEqual(deduped[0].bbox, [1, 2, 3, 4]);
  assert.equal(deduped[0].name, "a");
  assert.equal(deduped[0].dataset.state, "done");
  assert.deepEqual(deduped[0].links, [{rel: "self", href: "/x"}]);
});

test("tileUrls resolves TileJSON templates against the platform endpoint", () => {
  assert.deepEqual(
    tileUrls({tiles: ["/tiles/terrain/{z}/{x}/{y}.png"]}, "http://platform.test/tiles/terrain.json"),
    ["http://platform.test/tiles/terrain/{z}/{x}/{y}.png"],
  );
  assert.deepEqual(tileUrls(null, "http://platform.test/tiles/terrain.json"), []);
});

test("imageryTemplate resolves XYZ and ArcGIS WebTileLayer templates", () => {
  const set = {name: "w", tiles: ["/tiles/imagery-sets/w/{z}/{x}/{y}?v=1"]};
  assert.equal(imageryTemplate(set, "http://p.test"), "http://p.test/tiles/imagery-sets/w/{z}/{x}/{y}?v=1");
  assert.equal(imageryTemplate(set, "http://p.test", "arcgis"), "http://p.test/tiles/imagery-sets/w/{level}/{col}/{row}?v=1");
  assert.equal(imageryTemplate({}, "http://p.test"), null);
});

test("imagerySets keeps only usable sets", () => {
  assert.deepEqual(imagerySets({sets: [{name: "a", tiles: ["x"]}, {name: "b"}]}), [{name: "a", tiles: ["x"]}]);
  assert.deepEqual(imagerySets(null), []);
});

test("formatError handles Error, plain objects and primitives", () => {
  assert.equal(formatError(new Error("boom")), "boom");
  assert.equal(formatError({message: "esri"}), "esri");
  assert.equal(formatError("x"), "x");
});

// ---- loadState: Promise.allSettled, required vs optional parts, timeouts ----

const OK_PAYLOADS = {
  "/api/catalog": {items: [{id: "c1", title: "cat"}]},
  "/api/areas": {areas: [{id: "a1", name: "area", bbox: [1, 2, 3, 4]}]},
  "/api/services": {services: [{name: "s", url: "/arcgis/rest/services/s/ImageServer"}]},
  "/tiles/terrain.json": {tiles: ["/tiles/terrain/{z}/{x}/{y}.png"]},
  "/api/imagery": {sets: [{name: "w", tiles: ["/t/{z}/{x}/{y}"]}]},
};

function fakeFetch(overrides = {}) {
  const calls = [];
  const impl = async (url, init) => {
    const path = new URL(url).pathname;
    calls.push({path, init});
    const override = overrides[path];
    if (typeof override === "function") return override(url, init);
    if (typeof override === "number") return {ok: false, status: override, json: async () => ({})};
    return {ok: true, status: 200, json: async () => OK_PAYLOADS[path]};
  };
  return {impl, calls};
}

test("loadState returns every part and no warnings when all succeed", async () => {
  const {impl, calls} = fakeFetch();
  const state = await new PlatformApi("http://p.test", impl).loadState();
  assert.equal(calls.length, 5);
  assert.ok(calls.every((call) => call.init.signal), "every request carries an abort signal");
  assert.deepEqual(state.warnings, []);
  assert.equal(state.items.length, 2);
  assert.equal(state.imagery.length, 1);
  assert.deepEqual(state.terrain, OK_PAYLOADS["/tiles/terrain.json"]);
});

test("loadState degrades optional parts to warnings instead of failing", async () => {
  const {impl} = fakeFetch({"/api/services": 500, "/tiles/terrain.json": 500, "/api/imagery": 503});
  const state = await new PlatformApi("http://p.test", impl).loadState();
  assert.deepEqual(state.services, {services: []});
  assert.equal(state.terrain, null);
  assert.deepEqual(state.imagery, []);
  assert.deepEqual(state.warnings.map((warning) => warning.part), ["services", "terrain", "imagery"]);
  assert.match(state.warnings[1].message, /Terrain TileJSON request failed \(500\)/);
  assert.equal(state.items.length, 2, "catalog + areas still loaded");
});

test("loadState stays quiet about an older platform without /api/imagery (404)", async () => {
  const {impl} = fakeFetch({"/api/imagery": 404});
  const state = await new PlatformApi("http://p.test", impl).loadState();
  assert.deepEqual(state.warnings, []);
  assert.deepEqual(state.imagery, []);
});

test("loadState rejects only when a required part fails", async () => {
  const {impl} = fakeFetch({"/api/areas": 500, "/api/services": 500});
  await assert.rejects(new PlatformApi("http://p.test", impl).loadState(), /Saved areas request failed \(500\)/);
  const {impl: catalogDown} = fakeFetch({"/api/catalog": () => Promise.reject(new TypeError("Failed to fetch"))});
  await assert.rejects(new PlatformApi("http://p.test", catalogDown).loadState(), /Catalog request failed: Failed to fetch/);
});

test("requests time out instead of hanging forever", async () => {
  const hang = (_url, init) => new Promise((_resolve, reject) => {
    init.signal.addEventListener("abort", () => reject(init.signal.reason));
  });
  // AbortSignal.timeout() uses an unref'd timer in Node; with the fake request
  // hanging, nothing else keeps the event loop alive (Node 22 then cancels the
  // test). Hold a ref'd timer for the duration, as a real page would be alive.
  const keepAlive = setInterval(() => {}, 1000);
  try {
    const {impl} = fakeFetch({"/tiles/terrain.json": hang});
    const api = new PlatformApi("http://p.test", impl, {timeoutMs: 30});
    const started = Date.now();
    const state = await api.loadState();
    assert.ok(Date.now() - started < 2000);
    assert.equal(state.terrain, null);
    assert.match(state.warnings[0].message, /Terrain TileJSON request timed out/);

    const {impl: hangRequired} = fakeFetch({"/api/catalog": hang});
    await assert.rejects(new PlatformApi("http://p.test", hangRequired, {timeoutMs: 30}).loadState(), /Catalog request timed out/);
  } finally {
    clearInterval(keepAlive);
  }
});

test("getJson attaches the HTTP status to failures", async () => {
  const {impl} = fakeFetch({"/api/catalog": 503});
  await assert.rejects(new PlatformApi("http://p.test", impl).getJson("/api/catalog", "Catalog request"), (error) => error.status === 503);
});
