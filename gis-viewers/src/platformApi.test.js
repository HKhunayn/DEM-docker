import test from "node:test";
import assert from "node:assert/strict";
import {
  areaItems,
  catalogItems,
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
  // a duplicate id collapses to one entry; catalog metadata remains valid
  const deduped = mergeItems({areas: [{id: "x", name: "a", bbox: [1, 2, 3, 4]}]}, {items: [{id: "x", name: "b", bbox: [5, 6, 7, 8]}]});
  assert.equal(deduped.length, 1);
  assert.deepEqual(deduped[0].bbox, [5, 6, 7, 8]);
});

test("tileUrls resolves TileJSON templates against the platform endpoint", () => {
  assert.deepEqual(
    tileUrls({tiles: ["/tiles/terrain/{z}/{x}/{y}.png"]}, "http://platform.test/tiles/terrain.json"),
    ["http://platform.test/tiles/terrain/{z}/{x}/{y}.png"],
  );
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
