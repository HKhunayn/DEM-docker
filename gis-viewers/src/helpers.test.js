import test from "node:test";
import assert from "node:assert/strict";
import {backoffDelay, escapeHtml, formatElapsed, isAbortError, templatesKey, withTimeout} from "./uiHelpers.js";
import {LruCache, decodeTerrarium} from "./terrarium.js";

test("escapeHtml neutralises markup in server/user strings", () => {
  assert.equal(escapeHtml(`<img src=x onerror="alert('1')">&`), "&lt;img src=x onerror=&quot;alert(&#39;1&#39;)&quot;&gt;&amp;");
  assert.equal(escapeHtml(null), "");
  assert.equal(escapeHtml(42), "42");
});

test("backoffDelay doubles from 1.5 s and caps at 30 s", () => {
  assert.deepEqual([0, 1, 2, 3, 4, 5, 6, 50].map((n) => backoffDelay(n)), [1500, 3000, 6000, 12000, 24000, 30000, 30000, 30000]);
  assert.equal(backoffDelay(-3), 1500);
  assert.equal(backoffDelay(undefined), 1500);
});

test("formatElapsed renders seconds, minutes and hours", () => {
  assert.equal(formatElapsed(7400), "7s");
  assert.equal(formatElapsed(125000), "2m 05s");
  assert.equal(formatElapsed(3720000), "1h 02m");
  assert.equal(formatElapsed(-5), "0s");
});

test("withTimeout resolves with the value or a timeout marker", async () => {
  assert.equal(await withTimeout(Promise.resolve("ok"), 100), "ok");
  assert.deepEqual(await withTimeout(new Promise(() => {}), 10), {timedOut: true});
  await assert.rejects(withTimeout(Promise.reject(new Error("no")), 100), /no/);
});

test("isAbortError recognises superseded navigations", () => {
  assert.ok(isAbortError({name: "AbortError"}));
  assert.ok(isAbortError({name: "view:goto-interrupted", message: "Goto was interrupted"}));
  assert.ok(!isAbortError(new Error("404")));
  assert.ok(!isAbortError(null));
});

test("templatesKey detects changed tile templates", () => {
  assert.equal(templatesKey(["a?v=1"]), templatesKey(["a?v=1"]));
  assert.notEqual(templatesKey(["a?v=1"]), templatesKey(["a?v=2"]));
  assert.equal(templatesKey(undefined), "[]");
});

function rgba(...pixels) {
  return Uint8ClampedArray.from(pixels.flat());
}

test("decodeTerrarium decodes opaque pixels with the Terrarium formula", () => {
  // 0 m = (128, 0, 0); 1000.5 m = 32768 + 1000.5 -> R=131 G=232 B=128
  const values = decodeTerrarium(rgba([128, 0, 0, 255], [131, 232, 128, 255]));
  assert.deepEqual([...values], [0, 1000.5]);
});

test("decodeTerrarium fills alpha=0 pixels with the tile's valid mean, never -32768", () => {
  const values = decodeTerrarium(rgba([128, 100, 0, 255], [0, 0, 0, 0], [128, 200, 0, 255]));
  assert.deepEqual([...values], [100, 150, 200]);
  assert.ok([...values].every((value) => value > -1000));
});

test("decodeTerrarium uses the fallback height for a fully transparent tile", () => {
  assert.deepEqual([...decodeTerrarium(rgba([0, 0, 0, 0], [9, 9, 9, 0]), 2, 12)], [12, 12]);
  assert.deepEqual([...decodeTerrarium(rgba([0, 0, 0, 0]))], [0]);
});

test("LruCache evicts the least recently used entry and refreshes on hit", () => {
  const cache = new LruCache(2);
  cache.set("a", 1).set("b", 2);
  assert.equal(cache.get("a"), 1);   // a is now most recent
  cache.set("c", 3);                 // evicts b
  assert.ok(cache.has("a") && cache.has("c") && !cache.has("b"));
  assert.equal(cache.size, 2);
  cache.set("a", 10); cache.set("d", 4);   // re-set refreshes a; evicts c
  assert.ok(cache.has("a") && cache.has("d") && !cache.has("c"));
  assert.equal(cache.get("missing"), undefined);
});
