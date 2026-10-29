import test from "node:test";
import assert from "node:assert/strict";
import {buildProgress, describeTiles, pollDelay, tilesSignature} from "./tilesStatus.js";

const building = {
  published: {ok: false, reason: "nothing published (no tiles in the volume)"},
  autobuild: {state: "running", message: "building", done: [],
    current: {name: "Saudi_Arabia", mode: "full", step: "build", elapsed: 754, index: 1, total: 3}},
};

test("empty volume being built says so with progress", () => {
  const banner = describeTiles(building);
  assert.equal(banner.kind, "info");
  assert.match(banner.text, /Building the elevation tiles/);
  assert.match(banner.text, /Saudi_Arabia \(elevation tiles \+ ArcGIS service\) · build step · 12m 34s · build 1 of 3/);
  assert.equal(pollDelay(building), 5000);
});

test("nothing published and no automatic build points to the fetch page", () => {
  const banner = describeTiles({published: {ok: false, reason: "nothing published"}, autobuild: {state: "idle"}});
  assert.equal(banner.kind, "warning");
  assert.match(banner.text, /nothing published.*fetch page/);
});

test("low disk and failures are warnings", () => {
  assert.match(describeTiles({published: {ok: false}, autobuild: {state: "blocked", message: "stopped: only 3.0 GB free"}}).text, /3\.0 GB free/);
  assert.match(describeTiles({published: {ok: false}, autobuild: {state: "error", message: "1 build(s) failed"}}).text, /failed/);
});

test("ready platform: no banner, slow polling; LERC-only update is shown", () => {
  const ready = {published: {ok: true, version: "v1"}, autobuild: {state: "done", done: []}};
  assert.equal(describeTiles(ready), null);
  assert.equal(pollDelay(ready), 60000);
  const lerc = {published: {ok: true}, autobuild: {state: "running", current: {name: "Riyadh", mode: "lerc", step: "build", elapsed: 5, index: 2, total: 2}}};
  assert.match(describeTiles(lerc).text, /^Updating tiles: Riyadh \(ArcGIS elevation service\) · build step · 5s · build 2 of 2$/);
});

test("signature changes when a version goes live or a build finishes", () => {
  const a = tilesSignature({published: {version: "v1"}, autobuild: {done: []}});
  assert.notEqual(a, tilesSignature({published: {version: "v2"}, autobuild: {done: []}}));
  assert.notEqual(a, tilesSignature({published: {version: "v1"}, autobuild: {done: ["Riyadh (lerc)"]}}));
  assert.equal(buildProgress(null), "");
});
