import "maplibre-gl/dist/maplibre-gl.css";
import "cesium/Build/Cesium/Widgets/widgets.css";
import "@arcgis/core/assets/esri/themes/light/main.css";
import "./style.css";

import maplibregl from "maplibre-gl";
import ArcGISMap from "@arcgis/core/Map.js";
import SceneView from "@arcgis/core/views/SceneView.js";
import ElevationLayer from "@arcgis/core/layers/ElevationLayer.js";
import Ground from "@arcgis/core/Ground.js";
import WebTileLayer from "@arcgis/core/layers/WebTileLayer.js";
import * as Cesium from "cesium";
import {PlatformApi, formatError, imageryTemplate, itemBounds, mergeItems, resolveApiUrl, tileUrls} from "./platformApi.js";
import {makePlatformTerrainProvider} from "./cesiumTerrain.js";
import {ReliefImageryProvider} from "./cesiumRelief.js";
import {COLOR_RELIEF_OPACITY, HILLSHADE, MAP_BACKGROUND, colorReliefExpression, hillshadeExaggerationExpression} from "./relief.js";
import {formatElapsed, isAbortError, templatesKey, withTimeout} from "./uiHelpers.js";
import {describeTiles, pollDelay, tilesSignature} from "./tilesStatus.js";
import {GOTO_HINT, findCatalogItem, formatLatLon, normalizeText, parseCoordinates, parsePlaces, placeLabel, searchPlaces, zoomForPopulation} from "./goto.js";
import {cleanCamera, fromArcgisZoom, fromCesiumPitch, parseCameraHash, rangeForZoom, throttle, toArcgisZoom, toCesiumPitch, verticalFov, withCameraHash, zoomForRange} from "./camera.js";

// A production container serves the viewer and reverse-proxies the platform
// under the same origin. VITE_PLATFORM_URL remains available for local dev or
// a separately hosted viewer.
const platform = (import.meta.env.VITE_PLATFORM_URL || window.location.origin || "http://127.0.0.1:8787").replace(/\/$/, "");
const api = new PlatformApi(platform);
const app = document.querySelector("#app");

// Static shell only: no server data is ever interpolated into HTML. All
// catalog/area/imagery strings are inserted with textContent via el().
app.innerHTML = `
  <header>
    <div class="brand"><h1>Open GIS Viewers</h1><p>Platform-backed saved areas</p></div>
    <nav aria-label="Viewer selection">
      <button data-view="maplibre" class="active">MapLibre</button>
      <button data-view="arcgis">ArcGIS</button>
      <button data-view="cesium">CesiumJS</button>
    </nav>
    <form id="goto" class="goto" role="search" autocomplete="off">
      <input id="goto-input" type="search" aria-label="Go to a place or coordinates" spellcheck="false">
      <button type="submit">Go</button>
      <div id="goto-suggestions" class="goto-suggestions hidden" role="listbox"></div>
    </form>
  </header>
  <main>
    <aside aria-label="Platform catalog">
      <div class="panel-heading"><h2>Saved areas</h2><button id="reload" class="quiet" title="Reload platform state">↻</button></div>
      <div id="status" class="status" role="status" aria-live="polite">Loading platform state…</div>
      <div id="warnings" class="warnings hidden" role="alert"></div>
      <div id="items" aria-live="polite"></div>
      <div id="details" class="details hidden"></div>
      <div class="panel-heading imagery-heading"><h2>Imagery</h2></div>
      <select id="imagery-select" aria-label="Imagery set"><option value="">— none —</option></select>
      <div id="imagery-info" class="imagery-info">No imagery sets yet — fetch one on the platform's /fetch/ page (Imagery tab).</div>
      <p class="attribution-note">Place search: offline <a href="https://www.geonames.org/" target="_blank" rel="noopener">GeoNames</a> cities (CC-BY 4.0).</p>
    </aside>
    <section id="map" aria-label="Geospatial viewers">
      <div id="maplibre" class="viewer"></div>
      <div id="arcgis" class="viewer hidden"></div>
      <div id="cesium" class="viewer hidden"></div>
      <div id="map-message" class="map-message hidden" role="alert"></div>
      <div id="tiles-banner" class="tiles-banner hidden" role="status"></div>
      <button id="coords" class="coords hidden" type="button" title="Cursor position (lat, lon) - click to copy"></button>
    </section>
  </main>`;

const status = document.querySelector("#status");
const warningsEl = document.querySelector("#warnings");
const itemsEl = document.querySelector("#items");
const detailsEl = document.querySelector("#details");
const mapMessage = document.querySelector("#map-message");
const reloadButton = document.querySelector("#reload");
const imageryInfo = document.querySelector("#imagery-info");
const imagerySelect = document.querySelector("#imagery-select");
let state = null;
let selectedItem = null;
let maplibre = null;
let arcgisView = null;
let arcgisMap = null;
let cesiumViewer = null;
let selectedImagery = null;
let mapLibreReady = false;
let arcgisImageryLayer = null;
let arcgisElevationLayer = null;
let arcgisElevationUrl = null;
let cesiumImageryLayer = null;
let cesiumReliefLayer = null;
let cesiumReliefColour = null;
let appliedTerrainKey = null;
let loading = false;
let selectionToken = 0;

const VIEWER_LABELS = {maplibre: "MapLibre", arcgis: "ArcGIS", cesium: "CesiumJS"};
const NAVIGATION_TIMEOUT_MS = 10000;

/** Build a DOM node; strings become text nodes, never HTML. */
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value == null || value === false) continue;
    if (key === "className") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    node.append(child instanceof Node ? child : String(child));
  }
  return node;
}

// ---- status line: busy states show an elapsed-seconds counter -------------

let busyTicker = null;

function stopBusyTicker() {
  clearInterval(busyTicker);
  busyTicker = null;
}

function setStatus(message, kind = "info") {
  stopBusyTicker();
  status.textContent = message;
  status.className = `status ${kind}`;
}

function setBusyStatus(message) {
  stopBusyTicker();
  const started = performance.now();
  const render = () => { status.textContent = `${message} · ${formatElapsed(performance.now() - started)}`; };
  status.className = "status busy";
  render();
  busyTicker = setInterval(render, 1000);
}

function renderWarnings(warnings = [], {retry = false} = {}) {
  warningsEl.replaceChildren();
  if (!warnings.length) { warningsEl.classList.add("hidden"); return; }
  warningsEl.append(el("strong", {text: retry ? "Reload failed" : "Partially loaded"}));
  warningsEl.append(el("ul", {}, warnings.map(({part, message}) => el("li", {text: part ? `${part}: ${message}` : message}))));
  if (retry) warningsEl.append(el("button", {type: "button", text: "Retry", onclick: loadPlatformState}));
  warningsEl.classList.remove("hidden");
}

// Viewer messages are scoped: a MapLibre tile error must not cover the Cesium
// tab. "global" messages (platform state) show on every tab. Transient
// messages (individual tile failures) clear themselves. Start-up failures of
// a viewer are kept separately so clearing banners never hides them.
let activeView = "maplibre";
const viewMessages = {global: "", maplibre: "", arcgis: "", cesium: ""};
const viewerFailures = {maplibre: "", arcgis: "", cesium: ""};
const messageTimers = {};

function renderMapMessage() {
  const message = viewMessages.global || viewMessages[activeView] || viewerFailures[activeView] || "";
  mapMessage.textContent = message;
  mapMessage.classList.toggle("hidden", !message);
}

function setMapMessage(message, view = "global", {timeout = 0} = {}) {
  if (view === "global" && !message) Object.keys(viewMessages).forEach((key) => { viewMessages[key] = ""; });
  else viewMessages[view] = message;
  clearTimeout(messageTimers[view]);
  if (message && timeout) messageTimers[view] = setTimeout(() => { viewMessages[view] = ""; renderMapMessage(); }, timeout);
  renderMapMessage();
}

function setViewerFailure(view, message) {
  viewerFailures[view] = message;
  renderMapMessage();
}

function itemName(item) {
  return String(item?.title || item?.name || item?.properties?.title || item?.filename || item?.id || "Untitled area");
}

function itemKind(item) {
  return String(item?.kind || item?.properties?.kind || item?.type || "area");
}

function sameItem(a, b) {
  if (!a || !b) return false;
  return (a.id && a.id === b.id) || (!a.id && !b.id && a.name && a.name === b.name);
}

function itemCenter(item) {
  const bbox = itemBounds(item);
  if (bbox) return [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2];
  const center = state?.terrain?.center;
  return Array.isArray(center) && center.slice(0, 2).every(Number.isFinite) ? center.slice(0, 2) : [46.76, 24.74];
}

function itemZoom(item) {
  const bbox = itemBounds(item);
  if (!bbox) return 7;
  const span = Math.max(Math.abs(bbox[2] - bbox[0]), Math.abs(bbox[3] - bbox[1]));
  return Math.max(1, Math.min(14, Math.round(Math.log2(360 / Math.max(span, 0.01)) - 1)));
}

function allItems() {
  return state?.items?.length ? state.items : mergeItems(state?.areas, state?.catalog);
}

function renderCatalog(items) {
  if (!items.length) {
    itemsEl.replaceChildren(el("div", {className: "notice", text: "The platform catalog has no saved areas yet."}));
    detailsEl.classList.add("hidden");
    return;
  }
  itemsEl.replaceChildren(...items.map((item) => el("button", {
    type: "button",
    className: `item ${sameItem(item, selectedItem) ? "selected" : ""}`,
    onclick: () => selectItem(item),
  }, el("span", {text: itemName(item)}), el("small", {text: `${itemKind(item)}${item?.id ? ` · ${item.id}` : ""}`}))));
}

function renderDetails(item) {
  if (!item) {
    detailsEl.classList.add("hidden");
    return;
  }
  const bbox = itemBounds(item);
  detailsEl.replaceChildren(
    el("strong", {text: itemName(item)}),
    el("span", {text: bbox ? `Extent: ${bbox.map((value) => Number(value).toFixed(4)).join(", ")}` : "No extent supplied by platform"}),
  );
  detailsEl.classList.remove("hidden");
}

async function selectItem(item) {
  const token = ++selectionToken;
  selectedItem = item;
  // Picking an imagery area also switches every viewer to that imagery set.
  if (item?.kind === "imagery") {
    const set = (state?.imagery || []).find((candidate) => candidate.name === item.name);
    if (set && set !== selectedImagery) setImagery(set, {announce: false});
  }
  renderCatalog(allItems());
  renderDetails(item);
  setMapMessage("");
  // MapLibre/Cesium show the CURRENT published terrain build only (ArcGIS has
  // one ImageServer per dataset): say so instead of showing an empty map.
  const b = itemBounds(item), t = terrainBounds();
  const outside = item?.kind !== "imagery" && b && t && (b[2] < t[0] || b[0] > t[2] || b[3] < t[1] || b[1] > t[3]);
  for (const view of ["maplibre", "cesium"]) {
    setMapMessage(outside ? `${itemName(item)} is outside the current terrain build - MapLibre/Cesium show only the last built area. Rebuild it on the /fetch/ page to view it here; the ArcGIS view has it.` : "", view);
  }
  setBusyStatus(`${itemName(item)} selected · moving viewers`);
  const outcome = await syncViews();
  if (token !== selectionToken) return;   // superseded by a newer selection
  setStatus(`${itemName(item)} selected · ${outcome.text}`, outcome.kind);
}

function terrainTemplates() {
  const templates = tileUrls(state?.terrain, api.url("/tiles/terrain.json"));
  if (templates.length) return templates;
  // terrain.json unavailable (reported as a warning): the platform's fixed
  // tile route still serves the pyramid.
  return [api.url("/tiles/terrain/{z}/{x}/{y}.png").replaceAll("%7B", "{").replaceAll("%7D", "}")];
}

function terrainMaxzoom() {
  const value = Number(state?.terrain?.maxzoom);
  return Number.isFinite(value) && value > 0 ? value : 13;
}

function terrainBounds() {
  const bounds = state?.terrain?.bounds;
  return Array.isArray(bounds) && bounds.length === 4 && bounds.every(Number.isFinite) ? bounds : null;
}

function serviceForItem(item) {
  const links = item?.links || item?.assets?.service || item?.assets?.elevation;
  const candidate = Array.isArray(links) ? links.find((link) => /imageserver/i.test(link?.href || "")) : links;
  const href = candidate?.href || (typeof candidate === "string" ? candidate : null);
  if (href) return resolveApiUrl(href, platform);
  // Fetch jobs persist the selected dataset service under area.dataset.service.
  const nested = item?.dataset?.service || item?.properties?.dataset?.service;
  if (nested) return resolveApiUrl(nested, platform);
  const services = state?.services?.services || [];
  const requested = item?.service || item?.properties?.service;
  const match = services.find((service) => service.name === requested) || services[0];
  return match?.url ? resolveApiUrl(match.url, platform) : null;
}

// ---- imagery sets: one selection drives all three viewers -----------------

function renderImageryPicker() {
  const sets = state?.imagery || [];
  imagerySelect.replaceChildren(
    new Option("— none —", ""),
    ...sets.map((set) => new Option(`${set.name} · ${set.provider ?? "?"} z${set.minzoom ?? "?"}–${set.maxzoom ?? "?"}`, set.name)),
  );
  imagerySelect.value = selectedImagery?.name || "";
  imagerySelect.disabled = !sets.length;
  imageryInfo.textContent = !sets.length
    ? "No imagery sets yet — fetch one on the platform's /fetch/ page (Imagery tab)."
    : selectedImagery
      ? `${selectedImagery.tileCount ?? "?"} tiles · ${((Number(selectedImagery.bytes) || 0) / 1e6).toFixed(2)} MB · over-zoomed above z${selectedImagery.maxzoom ?? "?"} · ${selectedImagery.attribution || ""}`
      : `${sets.length} imagery set(s) available`;
}

function setImagery(set, {announce = true} = {}) {
  selectedImagery = set || null;
  renderImageryPicker();
  for (const [view, apply] of [["maplibre", applyMapLibreImagery], ["arcgis", applyArcGISImagery], ["cesium", applyCesiumImagery]]) {
    try { apply(); } catch (error) { setMapMessage(`${VIEWER_LABELS[view]} imagery error: ${formatError(error)}`, view); }
  }
  if (announce) setStatus(selectedImagery ? `Imagery “${selectedImagery.name}” shown in MapLibre, ArcGIS and CesiumJS` : "Imagery hidden in all viewers", "success");
}

imagerySelect.addEventListener("change", () => {
  setImagery((state?.imagery || []).find((set) => set.name === imagerySelect.value));
});

function applyMapLibreImagery() {
  if (!maplibre || !mapLibreReady) return;
  if (maplibre.getLayer("satellite")) maplibre.removeLayer("satellite");
  if (maplibre.getSource("satellite")) maplibre.removeSource("satellite");
  const set = selectedImagery;
  const url = imageryTemplate(set, platform);
  // An imagery set may cover more than the elevation area: don't mask it.
  if (maplibre.getLayer("outside-mask")) maplibre.setLayoutProperty("outside-mask", "visibility", url ? "none" : "visible");
  if (!url) return;
  maplibre.addSource("satellite", {
    type: "raster", tiles: [url], tileSize: 256,
    minzoom: set.minzoom ?? 0, maxzoom: set.maxzoom ?? 22,   // client over-zooms past maxzoom
    bounds: Array.isArray(set.bounds) ? set.bounds : undefined,
    attribution: set.attribution || "",
  });
  // Below the hillshade so relief still reads over the imagery.
  maplibre.addLayer({id: "satellite", type: "raster", source: "satellite"}, "relief");
}

function applyArcGISImagery() {
  if (!arcgisMap) return;
  if (arcgisImageryLayer) { arcgisMap.layers.remove(arcgisImageryLayer); arcgisImageryLayer.destroy?.(); arcgisImageryLayer = null; }
  const url = imageryTemplate(selectedImagery, platform, "arcgis");
  if (!url) return;
  // WebTileLayer has no client over-zoom; the platform over-zooms server-side.
  arcgisImageryLayer = new WebTileLayer({urlTemplate: url, title: `Imagery · ${selectedImagery.name}`, copyright: selectedImagery.attribution || ""});
  arcgisMap.layers.add(arcgisImageryLayer, 0);
  arcgisImageryLayer.load().catch((error) => setMapMessage(`ArcGIS imagery error: ${formatError(error)}`, "arcgis"));
}

function applyCesiumImagery() {
  if (!cesiumViewer) return;
  if (cesiumImageryLayer) { cesiumViewer.imageryLayers.remove(cesiumImageryLayer, true); cesiumImageryLayer = null; }
  const set = selectedImagery;
  const url = imageryTemplate(set, platform);
  if (url) {
    const bounds = Array.isArray(set.bounds) && set.bounds.length === 4 ? set.bounds : null;
    // Index 0: under the client-side relief drape.
    cesiumImageryLayer = cesiumViewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
      url, maximumLevel: set.maxzoom ?? 18,
      rectangle: bounds ? Cesium.Rectangle.fromDegrees(...bounds) : undefined,
      credit: set.attribution || undefined,
    }), 0);
  }
  // With imagery the relief drape switches to shade-only (like MapLibre,
  // where the imagery covers the colour ramp but not the hillshade).
  if (cesiumReliefColour !== !url) setCesiumRelief();
  cesiumViewer.scene.requestRender();
}

// ---- ArcGIS elevation: a loaded ElevationLayer ignores a new .url, so the
// layer is replaced in map.ground.layers whenever the service changes. -------

function setArcGISElevation(url, {force = false} = {}) {
  if (!arcgisMap) return;
  if (!url) {
    if (arcgisElevationLayer) { arcgisMap.ground.layers.remove(arcgisElevationLayer); arcgisElevationLayer.destroy?.(); }
    arcgisElevationLayer = null; arcgisElevationUrl = null;
    setMapMessage("The platform has no ArcGIS ImageServer URL for this selection.", "arcgis");
    return;
  }
  if (arcgisElevationLayer && arcgisElevationUrl === url && !force) return;
  if (arcgisElevationLayer) { arcgisMap.ground.layers.remove(arcgisElevationLayer); arcgisElevationLayer.destroy?.(); }
  arcgisElevationUrl = url;
  arcgisElevationLayer = new ElevationLayer({url, title: "Platform elevation"});
  arcgisMap.ground.layers.add(arcgisElevationLayer);
  const layer = arcgisElevationLayer;
  layer.load().catch((error) => {
    if (layer === arcgisElevationLayer && !isAbortError(error)) setMapMessage(`ArcGIS elevation error: ${formatError(error)}`, "arcgis");
  });
}

// ---- viewer construction (each isolated: one failing viewer never blocks
// the others or masquerades as "platform unavailable") ----------------------

// ---- MapLibre relief styling ------------------------------------------------
// Hypsometric tint computed on the GPU from the DEM itself (no extra tiles).
// Absolute metres, so it works for any area. 0 m is open sea / water (the
// published tiles put no-data sea at sea level); land starts just above it.
// Ramp + hillshade values are shared with the Cesium relief (relief.js).
const COLOR_RELIEF_LAYER = {id: "color-relief", type: "color-relief", source: "relief", paint: {
  "color-relief-opacity": COLOR_RELIEF_OPACITY,
  "color-relief-color": colorReliefExpression(),
}};
// Client-side multidirectional hillshade computed from the DEM.
const HILLSHADE_LAYER = {id: "relief", type: "hillshade", source: "relief", paint: {
  "hillshade-method": "multidirectional",
  // Light from fixed map directions, so tones don't shift when rotating.
  "hillshade-illumination-anchor": "map",
  "hillshade-illumination-direction": [...HILLSHADE.directions],
  "hillshade-illumination-altitude": [...HILLSHADE.altitudes],
  "hillshade-highlight-color": [...HILLSHADE.highlightColors],
  "hillshade-shadow-color": [...HILLSHADE.shadowColors],
  "hillshade-exaggeration": hillshadeExaggerationExpression(),
}};

// Colour + shading only inside the build's coverage: outside it the published
// terrain is a flat base (kept for the 3D edge fade) that would otherwise be
// painted as a huge plain of land/sea colour.
function reliefSource(terrain, maxzoom) {
  const bounds = terrainBounds();
  return {type: "raster-dem", tiles: terrain, tileSize: state.terrain?.tileSize || 256,
          encoding: state.terrain?.encoding || "terrarium", maxzoom, ...(bounds ? {bounds} : {})};
}

// Everything outside the coverage rectangle is painted with the page
// background: MapLibre loads whole tiles, so without this mask the flat base
// around the area shows as a large sea/plain apron at low zoom.
function outsideMask() {
  const b = terrainBounds();
  const world = [[-180, -85.06], [180, -85.06], [180, 85.06], [-180, 85.06], [-180, -85.06]];
  const rings = [world];
  if (b) rings.push([[b[0], b[1]], [b[0], b[3]], [b[2], b[3]], [b[2], b[1]], [b[0], b[1]]]);
  return {type: "Feature", properties: {}, geometry: {type: "Polygon", coordinates: rings}};
}
const OUTSIDE_LAYER = {id: "outside-mask", type: "fill", source: "outside",
  paint: {"fill-color": MAP_BACKGROUND, "fill-opacity": 1, "fill-antialias": false}};

function resetMapLibreRelief(terrain) {
  for (const id of [HILLSHADE_LAYER.id, COLOR_RELIEF_LAYER.id]) if (maplibre.getLayer(id)) maplibre.removeLayer(id);
  if (maplibre.getSource("relief")) maplibre.removeSource("relief");
  maplibre.addSource("relief", reliefSource(terrain, terrainMaxzoom()));
  maplibre.addLayer(COLOR_RELIEF_LAYER, maplibre.getLayer("satellite") ? "satellite" : undefined);
  maplibre.addLayer(HILLSHADE_LAYER, OUTSIDE_LAYER.id);
  maplibre.getSource("outside")?.setData(outsideMask());
}

function createMapLibre() {
  const terrain = terrainTemplates();
  const maxzoom = terrainMaxzoom();
  const demSource = () => ({type: "raster-dem", tiles: terrain, tileSize: state.terrain?.tileSize || 256, encoding: state.terrain?.encoding || "terrarium", maxzoom});
  maplibre = new maplibregl.Map({
    container: "maplibre",
    center: itemCenter(selectedItem), zoom: itemZoom(selectedItem), pitch: 55, maxPitch: 80,
    // Sharper on HiDPI screens without over-fetching on 1x displays.
    pixelRatio: Math.min(window.devicePixelRatio || 1, 2),
    style: {version: 8, sources: {
      terrain: demSource(),
      // Separate DEM source for shading/colour (MapLibre recommends not
      // sharing the 3D terrain source with hillshade/colour-relief layers).
      relief: reliefSource(terrain, maxzoom),
      outside: {type: "geojson", data: outsideMask()},
    },
    // Atmosphere for pitched 3D views: horizon haze instead of a hard edge.
    sky: {
      "sky-color": "#9cc3e4", "horizon-color": "#e9e2d3", "fog-color": "#e9e2d3",
      "sky-horizon-blend": 0.6, "horizon-fog-blend": 0.7,
      "fog-ground-blend": ["interpolate", ["linear"], ["zoom"], 6, 0.9, 12, 0.3],
      "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 3, 1, 9, 0],
    },
    layers: [
      {id: "background", type: "background", paint: {"background-color": MAP_BACKGROUND}},
      COLOR_RELIEF_LAYER,
      HILLSHADE_LAYER,
      OUTSIDE_LAYER,
    ]},
  });
  maplibre.addControl(new maplibregl.NavigationControl(), "top-right");
  if (import.meta.env.DEV) window.__viewers = Object.assign(window.__viewers || {}, {maplibre});
  watchMapLibre();
  maplibre.on("load", () => {
    // Gentle vertical exaggeration: most of the data is low-relief desert.
    maplibre.setTerrain({source: "terrain", exaggeration: 1.4});
    mapLibreReady = true;
    applyMapLibreImagery();
  });
  maplibre.on("error", (event) => {
    if (!event.error) return;
    // Tile-level failures (one aborted/undecodable tile) are transient: MapLibre
    // retries on the next view change, so do not pin a permanent banner.
    const tileError = Boolean(event.tile || event.sourceId);
    setMapMessage(`MapLibre ${tileError ? "tile" : "delivery"} error: ${event.error.message || "tile request failed"}`, "maplibre", {timeout: tileError ? 6000 : 0});
  });
}

async function createArcGIS() {
  // Create the SceneView first. Loading a local/custom ImageServer directly in
  // Ground's constructor can abort SceneView initialization on some SDK builds.
  arcgisMap = new ArcGISMap({basemap: "gray-vector", ground: new Ground()});
  arcgisView = new SceneView({container: "arcgis", map: arcgisMap, center: itemCenter(selectedItem), zoom: itemZoom(selectedItem), qualityProfile: "high"});
  if (import.meta.env.DEV) window.__viewers = Object.assign(window.__viewers || {}, {arcgis: arcgisView});
  applyArcGISImagery();
  await arcgisView.when();
  watchArcGIS();
  setArcGISElevation(serviceForItem(selectedItem));
}

// Hypsometric colour + multidirectional hillshade computed in the browser
// from the Terrarium elevation tiles (same ramp/lights as MapLibre). Colour is
// dropped (shade only) while an imagery set is shown underneath.
function setCesiumRelief() {
  if (!cesiumViewer) return;
  if (cesiumReliefLayer) { cesiumViewer.imageryLayers.remove(cesiumReliefLayer, true); cesiumReliefLayer = null; }
  const colour = !imageryTemplate(selectedImagery, platform);
  cesiumReliefColour = colour;
  try {
    // Added on top, so an imagery set (inserted at index 0) stays underneath.
    cesiumReliefLayer = cesiumViewer.imageryLayers.addImageryProvider(new ReliefImageryProvider({
      template: terrainTemplates()[0],
      // Only shade where the platform has DEM coverage (outside it the
      // terrain is a flat base; the globe shows the background colour).
      bounds: terrainBounds(),
      // Cesium over-zooms the maxzoom relief beyond it.
      maxzoom: terrainMaxzoom(),
      tileSize: state?.terrain?.tileSize || 256,
      colour,
    }));
  } catch (error) {
    setMapMessage(`Cesium relief error: ${formatError(error)}`, "cesium");
  }
  cesiumViewer.scene.requestRender();
}

// Use the platform Terrarium pyramid: the platform fills no-data and serves
// flat fill tiles everywhere outside coverage, so the globe has continuous
// ground. The per-dataset Lerc ImageServers have no level-0 root tile and
// end at the service extent, which leaves CesiumJS with holes/cliffs.
async function setCesiumTerrain() {
  if (!cesiumViewer) return;
  try {
    cesiumViewer.terrainProvider = makePlatformTerrainProvider(terrainTemplates(), {credit: "Platform terrain", maxzoom: terrainMaxzoom()});
  } catch (error) {
    const serviceUrl = serviceForItem(selectedItem);
    if (serviceUrl) {
      try {
        cesiumViewer.terrainProvider = await Cesium.ArcGISTiledElevationTerrainProvider.fromUrl(serviceUrl);
      } catch (fallbackError) {
        setMapMessage(`Cesium terrain error: ${formatError(fallbackError)}`, "cesium");
      }
    } else {
      setMapMessage(`Cesium terrain error: ${formatError(error)}`, "cesium");
    }
  }
  cesiumViewer?.scene.requestRender();
}

async function createCesium() {
  // Always create the viewer with an ellipsoid; the platform terrain is set
  // afterwards so a terrain failure still leaves a working globe.
  cesiumViewer = new Cesium.Viewer("cesium", {
    animation: false, timeline: false, baseLayerPicker: false, geocoder: false,
    homeButton: false, sceneModePicker: true, baseLayer: false,
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    // Render only when something changes (camera, tiles, layers) instead of
    // 60 fps forever -- matters most while the Cesium tab is hidden.
    requestRenderMode: true,
    maximumRenderTimeChange: Number.POSITIVE_INFINITY,
    // Report render errors in the app banner instead of Cesium's modal, which
    // stops rendering for good after one bad tile.
    showRenderLoopErrors: false,
  });
  const viewer = cesiumViewer;
  let renderRestarts = 0;
  viewer.scene.renderError.addEventListener((_scene, error) => {
    const message = formatError(error);
    if (renderRestarts < 3) {
      renderRestarts += 1;
      setMapMessage(`Cesium recovered from a render error: ${message}`, "cesium", {timeout: 8000});
      setTimeout(() => { viewer.useDefaultRenderLoop = true; viewer.scene.requestRender(); }, 250);
    } else {
      setMapMessage(`Cesium rendering stopped after repeated errors: ${message}. Reload the page.`, "cesium");
    }
  });
  if (import.meta.env.DEV) window.__viewers = Object.assign(window.__viewers || {}, {cesium: viewer, Cesium});
  // No remote basemap: the ground outside the DEM uses the same background
  // colour as MapLibre (the relief drape only covers the DEM coverage).
  viewer.scene.globe.baseColor = Cesium.Color.fromCssColorString(MAP_BACKGROUND);
  viewer.scene.globe.depthTestAgainstTerrain = true;
  setCesiumRelief();
  applyCesiumImagery();
  await setCesiumTerrain();
  watchCesium();
  if (lastCamera) applyCamera("cesium", lastCamera); else cesiumFrame(selectedItem, 0);
}

function destroyQuietly(object) {
  try { object?.destroy?.(); } catch { /* already broken */ }
}

/** Create every viewer that does not exist yet; each failure is reported per viewer. */
async function startViewers() {
  const failures = [];
  if (!maplibre) {
    try {
      createMapLibre();
      setViewerFailure("maplibre", "");
    } catch (error) {
      destroyQuietly(maplibre); maplibre = null; mapLibreReady = false;
      setViewerFailure("maplibre", `MapLibre failed to start: ${formatError(error)}`);
      failures.push("MapLibre");
    }
  }
  // ArcGIS may take a long time to resolve its SceneView; it must not block
  // CesiumJS, so it runs in the background with its own error handling.
  if (!arcgisView) {
    createArcGIS()
      .then(() => setViewerFailure("arcgis", ""))
      .catch((error) => {
        destroyQuietly(arcgisView); arcgisView = null; arcgisMap = null;
        arcgisElevationLayer = null; arcgisElevationUrl = null; arcgisImageryLayer = null;
        setViewerFailure("arcgis", `ArcGIS viewer failed to start: ${formatError(error)}`);
        if (!loading) setStatus(`ArcGIS viewer failed to start: ${formatError(error)}`, "warning");
      });
  }
  if (!cesiumViewer) {
    try {
      await createCesium();
      setViewerFailure("cesium", "");
    } catch (error) {
      destroyQuietly(cesiumViewer); cesiumViewer = null; cesiumImageryLayer = null; cesiumReliefLayer = null; cesiumReliefColour = null;
      setViewerFailure("cesium", `CesiumJS failed to start: ${formatError(error)}`);
      failures.push("CesiumJS");
    }
  }
  return failures;
}

/**
 * After a reload: if the platform's terrain tile template changed (it is
 * versioned per build), point every viewer at the new tiles. All relief
 * shading is computed on the client from those tiles.
 */
function refreshTileSources() {
  const terrain = terrainTemplates();
  const terrainKey = templatesKey(terrain);
  const terrainChanged = appliedTerrainKey !== null && terrainKey !== appliedTerrainKey;
  appliedTerrainKey = terrainKey;
  if (maplibre && terrainChanged) {
    try {
      maplibre.getSource("terrain")?.setTiles(terrain);
      // New build/publish: tiles AND coverage bounds change (bounds can't
      // be updated in place).
      if (mapLibreReady) resetMapLibreRelief(terrain);
    } catch (error) {
      setMapMessage(`MapLibre could not refresh tile sources: ${formatError(error)}`, "maplibre");
    }
  }
  if (cesiumViewer && terrainChanged) {
    setCesiumRelief();
    setCesiumTerrain();
  }
  // A rebuilt dataset keeps its ImageServer URL; force a fresh layer so the
  // SceneView drops its cached elevation tiles.
  if (arcgisMap && arcgisView) setArcGISElevation(serviceForItem(selectedItem), {force: terrainChanged});
  return terrainChanged;
}

// ---- navigation: each viewer reports when it has actually arrived ---------

// Oblique framing of the selected area (a 1500 km nadir view showed only a
// flat colour). Resolves when the flight completes or is cancelled.
function cesiumFrame(item, duration = 1.5) {
  if (!cesiumViewer) return Promise.resolve("skipped");
  return new Promise((resolve) => {
    const done = {complete: () => resolve("arrived"), cancel: () => resolve("cancelled")};
    const bbox = itemBounds(item);
    if (bbox && Math.abs(bbox[2] - bbox[0]) > 90) {
      // Continental/world extents: a bounding sphere of the corners collapses
      // (±180° share a meridian) and a world Rectangle destination lands at
      // ground level, so look straight down from a height that fits the span.
      const [cx, cy] = itemCenter(item);
      const span = Math.max(Math.abs(bbox[2] - bbox[0]), Math.abs(bbox[3] - bbox[1]) * 2);
      const height = Math.min(2.2e7, span * 111_320 * 0.9);
      cesiumViewer.camera.flyTo({destination: Cesium.Cartesian3.fromDegrees(cx, cy, height), duration, ...done});
      return;
    }
    const [lon, lat] = itemCenter(item);
    const base = Number(state?.terrain?.statistics?.min) || 0;
    const corners = bbox
      ? [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]]
      : [[lon - 0.1, lat - 0.1], [lon + 0.1, lat + 0.1]];
    const sphere = Cesium.BoundingSphere.fromPoints(corners.map(([x, y]) => Cesium.Cartesian3.fromDegrees(x, y, base)));
    // A flight cancelled by a newer selection or by user input is expected.
    cesiumViewer.camera.flyToBoundingSphere(sphere, {
      duration,
      offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-35), sphere.radius * 2.2),
      ...done,
    });
    cesiumViewer.scene.requestRender();
  });
}

function mapLibreNavigate(center, zoom) {
  if (!maplibre) return Promise.resolve("skipped");
  // Hidden tab: no point animating; jump so the view is ready when shown.
  if (activeView !== "maplibre") {
    maplibre.jumpTo({center, zoom});
    return Promise.resolve("arrived");
  }
  return new Promise((resolve) => {
    maplibre.once("moveend", () => resolve("arrived"));
    maplibre.flyTo({center, zoom});
  });
}

function arcgisNavigate(center, zoom) {
  if (!arcgisView) return Promise.resolve("skipped");
  setArcGISElevation(serviceForItem(selectedItem));
  // A hidden SceneView is suspended: its goTo aborts or never settles. Record
  // the target and replay it when the ArcGIS tab is shown (setViewer).
  // The camera is handed over when the tab is shown (setViewer).
  if (activeView !== "arcgis") return Promise.resolve("deferred");
  return arcgisView.goTo({center, zoom}).then(() => "arrived", (error) => {
    // goTo rejects with AbortError when superseded by a newer navigation or
    // while the view is hidden in another tab; that is not a failure.
    if (isAbortError(error)) return "deferred";
    setMapMessage(`ArcGIS navigation error: ${formatError(error)}`, "arcgis");
    throw error;
  });
}

// ---- shared camera: go-to box, URL hash, tab hand-over, cursor readout ----
// The common camera is MapLibre's {lon, lat, zoom, bearing, pitch} (camera.js
// converts it for ArcGIS and CesiumJS).

let lastCamera = parseCameraHash(window.location.hash);   // restored on reload
const startCamera = lastCamera;
let arcgisPendingCamera = null;     // applied when the ArcGIS tab is shown
let suppressCameraTracking = 0;     // ignore our own programmatic moves' events
const writeHash = throttle((camera) => {
  const hash = withCameraHash(window.location.hash, camera);
  if (hash !== window.location.hash) history.replaceState(null, "", `${window.location.pathname}${window.location.search}${hash}`);
}, 400);

const rad = (deg) => (deg * Math.PI) / 180;
const deg = (radians) => (radians * 180) / Math.PI;

function cesiumFovy() {
  const frustum = cesiumViewer?.camera?.frustum;
  const canvas = cesiumViewer?.scene?.canvas;
  return verticalFov(frustum?.fov ?? rad(60), canvas?.clientWidth || 1, canvas?.clientHeight || 1);
}

function cesiumCenterPick() {
  const scene = cesiumViewer.scene; const canvas = scene.canvas;
  const ray = cesiumViewer.camera.getPickRay(new Cesium.Cartesian2(canvas.clientWidth / 2, canvas.clientHeight / 2));
  return ray ? scene.globe.pick(ray, scene) : undefined;
}

/** Current camera of a viewer in the common format (null if unavailable). */
function readCamera(view) {
  try {
    if (view === "maplibre" && maplibre) {
      const c = maplibre.getCenter();
      return cleanCamera({lon: c.lng, lat: c.lat, zoom: maplibre.getZoom(), bearing: maplibre.getBearing(), pitch: maplibre.getPitch()});
    }
    if (view === "arcgis" && arcgisView?.ready && arcgisView.center) {
      const cam = arcgisView.camera;
      return cleanCamera({lon: arcgisView.center.longitude, lat: arcgisView.center.latitude,
        zoom: fromArcgisZoom(arcgisView.zoom), bearing: cam?.heading ?? 0, pitch: cam?.tilt ?? 0});
    }
    if (view === "cesium" && cesiumViewer) {
      const camera = cesiumViewer.camera;
      const target = cesiumCenterPick();
      const canvas = cesiumViewer.scene.canvas;
      let lon, lat, range;
      if (target) {
        const carto = Cesium.Cartographic.fromCartesian(target);
        lon = deg(carto.longitude); lat = deg(carto.latitude);
        range = Cesium.Cartesian3.distance(camera.positionWC, target);
      } else {           // looking at the sky/horizon: use the camera position
        const carto = camera.positionCartographic;
        lon = deg(carto.longitude); lat = deg(carto.latitude); range = carto.height;
      }
      return cleanCamera({lon, lat, zoom: zoomForRange(range, lat, canvas.clientHeight, cesiumFovy()),
        bearing: deg(camera.heading), pitch: fromCesiumPitch(deg(camera.pitch))});
    }
  } catch { /* viewer not ready */ }
  return null;
}

/** Record the active viewer's camera (URL hash + hand-over to other tabs). */
function trackCamera(view) {
  if (view !== activeView || suppressCameraTracking > 0) return;
  const camera = readCamera(view);
  if (!camera) return;
  lastCamera = camera;
  writeHash(camera);
}

/** Put one viewer at `camera`; animate only when it is the visible one. */
function applyCamera(view, camera, {animate = false} = {}) {
  const c = cleanCamera(camera);
  if (!c) return Promise.resolve("skipped");
  const visible = view === activeView;
  if (view === "maplibre" && maplibre) {
    const options = {center: [c.lon, c.lat], zoom: c.zoom, bearing: c.bearing, pitch: c.pitch};
    if (!(animate && visible)) { maplibre.jumpTo(options); return Promise.resolve("arrived"); }
    return new Promise((resolve) => { maplibre.once("moveend", () => resolve("arrived")); maplibre.flyTo(options); });
  }
  if (view === "arcgis" && arcgisView) {
    // A hidden SceneView is suspended (goTo aborts): apply when shown.
    if (!visible) { arcgisPendingCamera = c; return Promise.resolve("deferred"); }
    arcgisPendingCamera = null;
    const target = {center: [c.lon, c.lat], zoom: toArcgisZoom(c.zoom), heading: c.bearing, tilt: c.pitch};
    return arcgisView.when().then(() => arcgisView.goTo(target, {animate: animate && visible}))
      .then(() => "arrived", (error) => { if (isAbortError(error)) return "deferred"; throw error; });
  }
  if (view === "cesium" && cesiumViewer) {
    return new Promise((resolve) => {
      const carto = Cesium.Cartographic.fromDegrees(c.lon, c.lat);
      const ground = cesiumViewer.scene.globe.getHeight(carto) ?? (Number(state?.terrain?.statistics?.min) || 0);
      const centre = Cesium.Cartesian3.fromDegrees(c.lon, c.lat, Math.max(0, ground));
      const canvas = cesiumViewer.scene.canvas;
      const range = rangeForZoom(c.zoom, c.lat, canvas.clientHeight, cesiumFovy());
      cesiumViewer.camera.flyToBoundingSphere(new Cesium.BoundingSphere(centre, 0), {
        // A hidden widget never advances a flight: duration 0 is synchronous.
        duration: animate && visible ? 1.5 : 0,
        offset: new Cesium.HeadingPitchRange(rad(c.bearing), rad(toCesiumPitch(c.pitch)), range),
        complete: () => resolve("arrived"), cancel: () => resolve("cancelled"),
      });
      cesiumViewer.scene.requestRender();
    });
  }
  return Promise.resolve("skipped");
}

/** Smallest built elevation area whose bounds contain lon/lat (or null). */
function areaCovering(lon, lat) {
  const hits = allItems().filter((item) => {
    const b = itemBounds(item);
    return item?.kind !== "imagery" && b && lon >= b[0] && lon <= b[2] && lat >= b[1] && lat <= b[3] && serviceForItem(item);
  });
  const size = (item) => { const b = itemBounds(item); return (b[2] - b[0]) * (b[3] - b[1]); };
  return hits.sort((a, b) => size(a) - size(b))[0] || null;
}

/** Make the area under the target the selection (ArcGIS elevation is per area). */
function selectAreaAt(lon, lat) {
  const item = areaCovering(lon, lat);
  if (!item || sameItem(item, selectedItem)) return;
  selectedItem = item;
  renderCatalog(allItems());
  renderDetails(item);
  for (const view of ["maplibre", "cesium"]) setMapMessage("", view);
  setArcGISElevation(serviceForItem(item));
}

/** Move every viewer to `camera` (the visible one animates). */
async function teleport(camera, {animate = true} = {}) {
  const c = cleanCamera(camera);
  if (!c) return;
  selectAreaAt(c.lon, c.lat);
  lastCamera = c; writeHash(c);
  suppressCameraTracking += 1;
  try {
    await Promise.allSettled(["maplibre", "arcgis", "cesium"].map((view) =>
      withTimeout(applyCamera(view, c, {animate}), NAVIGATION_TIMEOUT_MS)));
  } finally {
    suppressCameraTracking -= 1;
  }
  trackCamera(activeView);
}

// Called once per created viewer (camera tracking + cursor readout).
function watchMapLibre() {
  maplibre.on("moveend", () => trackCamera("maplibre"));
  maplibre.on("mousemove", (event) => {
    const terrain = maplibre.getTerrain?.();
    let elevation = terrain ? maplibre.queryTerrainElevation(event.lngLat) : null;
    // queryTerrainElevation includes the vertical exaggeration.
    if (Number.isFinite(elevation) && terrain?.exaggeration) elevation /= terrain.exaggeration;
    showReadout("maplibre", event.lngLat.lat, event.lngLat.lng, elevation);
  });
}

function watchCesium() {
  cesiumViewer.camera.moveEnd.addEventListener(() => trackCamera("cesium"));
  const handler = new Cesium.ScreenSpaceEventHandler(cesiumViewer.scene.canvas);
  let pending = null;
  handler.setInputAction((movement) => {
    if (pending) { pending.position = movement.endPosition; return; }
    pending = {position: movement.endPosition};
    requestAnimationFrame(() => {
      const ray = cesiumViewer?.camera.getPickRay(pending.position); pending = null;
      const hit = ray && cesiumViewer.scene.globe.pick(ray, cesiumViewer.scene);
      if (!hit) return;
      const carto = Cesium.Cartographic.fromCartesian(hit);
      showReadout("cesium", deg(carto.latitude), deg(carto.longitude), carto.height);
    });
  }, Cesium.ScreenSpaceEventType.MOUSE_MOVE);
}

function watchArcGIS() {
  arcgisView.watch("stationary", (stationary) => { if (stationary) trackCamera("arcgis"); });
  let busy = false;
  arcgisView.on("pointer-move", (event) => {
    if (busy) return;
    busy = true;
    requestAnimationFrame(() => {
      busy = false;
      const point = arcgisView?.toMap({x: event.x, y: event.y});
      if (point) showReadout("arcgis", point.latitude, point.longitude, point.z);
    });
  });
}

// ---- go-to box -------------------------------------------------------------

const gotoForm = document.querySelector("#goto");
const gotoInput = document.querySelector("#goto-input");
const gotoList = document.querySelector("#goto-suggestions");
let places = null;          // lazily loaded offline gazetteer
let placesLoading = null;
let suggestionHits = [];

function loadPlaces() {
  if (places) return Promise.resolve(places);
  placesLoading ||= fetch(`${import.meta.env.BASE_URL}places.tsv`)
    .then((response) => { if (!response.ok) throw new Error(`gazetteer HTTP ${response.status}`); return response.text(); })
    .then((text) => (places = parsePlaces(text)))
    .catch((error) => { placesLoading = null; throw error; });
  return placesLoading;
}

function catalogNames(item) {
  return [itemName(item), item?.name, item?.title, item?.dataset?.name, item?.properties?.name].filter(Boolean);
}

function hideSuggestions() {
  suggestionHits = [];
  gotoList.replaceChildren();
  gotoList.classList.add("hidden");
}

async function showSuggestions() {
  const query = gotoInput.value.trim();
  if (!query || parseCoordinates(query)) { hideSuggestions(); return; }
  const rows = allItems().filter((item) => catalogNames(item).some((name) => normalizeText(name).includes(normalizeText(query))))
    .slice(0, 3).map((item) => ({label: `${itemName(item)} · saved area`, run: () => goToItem(item)}));
  try {
    const hits = searchPlaces(await loadPlaces(), query, 8 - rows.length);
    if (gotoInput.value.trim() !== query) return;   // the user kept typing
    suggestionHits = hits;
    rows.push(...hits.map((hit) => ({label: placeLabel(hit), run: () => goToPlace(hit)})));
  } catch { /* gazetteer unavailable: saved areas only */ }
  gotoList.replaceChildren(...rows.map((row) => el("button", {type: "button", text: row.label,
    onmousedown: (event) => { event.preventDefault(); hideSuggestions(); row.run(); }})));
  gotoList.classList.toggle("hidden", rows.length === 0);
}

function goToItem(item) {
  gotoInput.value = itemName(item);
  return selectItem(item);
}

function goToPlace(hit) {
  const {place} = hit;
  gotoInput.value = place.name;
  const zoom = zoomForPopulation(place.population);
  setStatus(`Going to ${placeLabel(hit)} · ${formatLatLon(place.lat, place.lon)}`, "success");
  return teleport({...(readCamera(activeView) || lastCamera || {}), lat: place.lat, lon: place.lon, zoom});
}

async function goTo(query) {
  const text = String(query || "").trim();
  if (!text) return;
  hideSuggestions();
  const coords = parseCoordinates(text);
  if (coords?.error) { setStatus(`Go to: ${coords.error}`, "error"); return; }
  if (coords) {
    const zoom = coords.zoom ?? Math.max(lastCamera?.zoom ?? 0, 12);
    const note = coords.swapped ? " (first value > 90 read as longitude)" : "";
    setStatus(`Going to ${formatLatLon(coords.lat, coords.lon)}${note}`, "success");
    await teleport({...(readCamera(activeView) || lastCamera || {}), lat: coords.lat, lon: coords.lon, zoom});
    return;
  }
  const item = findCatalogItem(allItems(), text, catalogNames);
  if (item) { await goToItem(item); return; }
  setBusyStatus(`Searching places for "${text}"`);
  try {
    const [best] = searchPlaces(await loadPlaces(), text, 1);
    if (!best) { setStatus(`No place or coordinates matched "${text}" · try "lat, lon" (e.g. 24.7136, 46.6753)`, "warning"); return; }
    await goToPlace(best);
  } catch (error) {
    setStatus(`Place search unavailable (${formatError(error)}) · coordinates still work: "lat, lon"`, "error");
  }
}

gotoInput.placeholder = "Go to: place or lat, lon";
gotoInput.title = GOTO_HINT;
gotoForm.addEventListener("submit", (event) => { event.preventDefault(); goTo(gotoInput.value); });
let suggestTimer = null;
gotoInput.addEventListener("input", () => { clearTimeout(suggestTimer); suggestTimer = setTimeout(showSuggestions, 150); });
gotoInput.addEventListener("focus", () => { loadPlaces().catch(() => {}); });
gotoInput.addEventListener("blur", () => setTimeout(hideSuggestions, 150));
gotoInput.addEventListener("keydown", (event) => { if (event.key === "Escape") { hideSuggestions(); gotoInput.blur(); } });

// ---- cursor readout ----------------------------------------------------------

const readout = document.querySelector("#coords");
let readoutText = "";

function showReadout(view, lat, lon, elevation) {
  if (view !== activeView || !Number.isFinite(lat) || !Number.isFinite(lon)) return;
  readoutText = formatLatLon(lat, lon);
  readout.textContent = `${readoutText}${Number.isFinite(elevation) ? ` · ${Math.round(elevation)} m` : ""}`;
  readout.classList.remove("hidden");
}

readout.addEventListener("click", async () => {
  if (!readoutText) return;
  try { await navigator.clipboard.writeText(readoutText); setStatus(`Copied ${readoutText}`, "success"); }
  catch { setStatus(`Copy failed · ${readoutText}`, "warning"); }
});

/** Move every viewer to the selection; resolves once all navigations settled. */
async function syncViews() {
  if (!selectedItem) return {text: "nothing selected", kind: "warning"};
  const center = itemCenter(selectedItem); const zoom = itemZoom(selectedItem);
  const tasks = [
    ["MapLibre", () => mapLibreNavigate(center, zoom)],
    ["ArcGIS", () => arcgisNavigate(center, zoom)],
    // A hidden Cesium widget has a 0x0 canvas and never advances a flight, so
    // frame it instantly (duration 0 completes synchronously).
    ["CesiumJS", () => cesiumFrame(selectedItem, activeView === "cesium" ? 1.5 : 0)],
  ];
  const results = await Promise.allSettled(tasks.map(([, run]) => {
    try { return withTimeout(run(), NAVIGATION_TIMEOUT_MS); } catch (error) { return Promise.reject(error); }
  }));
  const failed = []; const slow = [];
  results.forEach((result, index) => {
    if (result.status === "rejected") failed.push(`${tasks[index][0]} (${formatError(result.reason)})`);
    else if (result.value?.timedOut) slow.push(tasks[index][0]);
  });
  if (failed.length) return {text: `navigation failed in ${failed.join(", ")}`, kind: "warning"};
  if (slow.length) return {text: `viewers synchronized (${slow.join(", ")} still moving)`, kind: "success"};
  return {text: "viewers synchronized", kind: "success"};
}

function setViewer(view) {
  // Hand the current position over to the viewer being shown.
  const handOver = (view !== activeView && readCamera(activeView)) || lastCamera;
  if (handOver) lastCamera = handOver;
  activeView = view;
  renderMapMessage();
  document.querySelectorAll("header nav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".viewer").forEach((element) => element.classList.toggle("hidden", element.id !== view));
  requestAnimationFrame(() => {
    if (view === "maplibre") maplibre?.resize();
    if (view === "cesium" && cesiumViewer) { cesiumViewer.resize(); cesiumViewer.scene.requestRender(); }
    const target = handOver || arcgisPendingCamera;
    if (!target || activeView !== view) return;
    if (view === "arcgis" && arcgisView) {
      // SceneView has no resize(); it tracks its container size itself. The
      // first time it becomes visible it may still be starting up and abort
      // the navigation; retry a few times until it lands.
      arcgisPendingCamera = null;
      suppressCameraTracking += 1;
      const attempt = (left) => applyCamera("arcgis", target)
        .then((result) => { if (result !== "arrived") throw new Error(result); suppressCameraTracking -= 1; })
        .catch(() => {
          if (left > 0 && activeView === "arcgis") setTimeout(() => attempt(left - 1), 400);
          else suppressCameraTracking -= 1;
        });
      attempt(5);
    } else {
      suppressCameraTracking += 1;
      applyCamera(view, target).finally(() => { suppressCameraTracking -= 1; });
    }
  });
}

async function loadPlatformState() {
  if (loading) return;
  loading = true;
  reloadButton.disabled = true;
  setBusyStatus("Loading catalog, saved areas, and delivery metadata");
  if (!state) itemsEl.replaceChildren(el("div", {className: "loading", text: "Contacting the GIS data platform…"}));
  let next;
  try {
    next = await api.loadState();
  } catch (error) {
    // Only a failure of the platform's required state is "unavailable".
    const message = formatError(error);
    setStatus(`Platform unavailable · ${message}`, "error");
    if (state) {
      // Keep the working viewers and the last catalog; offer a retry.
      renderWarnings([{part: "", message}], {retry: true});
    } else {
      selectedItem = null;
      itemsEl.replaceChildren(el("div", {className: "notice error"},
        el("div", {text: message}),
        el("button", {type: "button", text: "Retry", onclick: loadPlatformState})));
      setMapMessage(`Platform unavailable: ${message}`);
    }
    loading = false;
    reloadButton.disabled = false;
    return;
  }

  try {
    const firstLoad = !state;
    state = next;
    setMapMessage("");
    const items = allItems();
    // Keep the current selection across reloads when it still exists;
    // otherwise default to a built elevation area (imagery areas are
    // selectable too).
    selectedItem = items.find((item) => sameItem(item, selectedItem))
      || items.find((item) => item?.kind !== "imagery" && item?.dataset?.state === "done")
      || items.find((item) => item?.dataset?.service) || items[0] || null;
    // Keep the current imagery choice across reloads when it still exists.
    const sets = state.imagery || [];
    selectedImagery = sets.find((set) => set.name === selectedImagery?.name) || (firstLoad ? sets[0] : null) || null;
    renderCatalog(items); renderDetails(selectedItem); renderImageryPicker();
    renderWarnings(state.warnings || []);
    if (!selectedItem) {
      setStatus("Platform connected · no saved areas", "warning");
      setMapMessage("Select a saved area after it is added to the platform catalog.");
      return;
    }
    let navigation = {text: "viewers ready", kind: "success"};
    const anyViewer = maplibre || arcgisView || cesiumViewer;
    if (anyViewer) {
      setBusyStatus("Platform reloaded · refreshing viewers");
      refreshTileSources();
      setImagery(selectedImagery, {announce: false});
    } else {
      setBusyStatus("Platform connected · starting viewers");
    }
    // Creates missing viewers (first load, or a retry after a start-up failure).
    const failures = await startViewers();
    if (!anyViewer) {
      appliedTerrainKey = templatesKey(terrainTemplates());
      // A position in the URL (#map=zoom/lat/lon/bearing/pitch) wins over
      // the default framing of the selected area.
      if (startCamera) {
        await teleport(startCamera, {animate: false});
        navigation = {text: "restored the position from the URL", kind: "success"};
      }
    } else {
      setBusyStatus("Platform reloaded · moving viewers");
      navigation = await syncViews();
    }
    const parts = [`Platform connected · ${items.length} saved area(s)`];
    const warningCount = (state.warnings || []).length;
    if (warningCount) parts.push(`${warningCount} optional part(s) unavailable`);
    if (failures.length) parts.push(`${failures.join(", ")} failed to start`);
    parts.push(navigation.text);
    setStatus(parts.join(" · "), warningCount || failures.length || navigation.kind !== "success" ? "warning" : "success");
  } catch (error) {
    // A bug in rendering/refreshing is not a platform outage.
    setStatus(`Viewer update failed · ${formatError(error)}`, "error");
  } finally {
    loading = false;
    reloadButton.disabled = false;
  }
}

// ---- tile status: the platform builds missing tiles by itself (e.g. after a
// volume import that carried only the DEMs); say so instead of an empty map,
// and reload the viewers when new tiles go live.
const tilesBanner = document.querySelector("#tiles-banner");
let tilesSeen = null;
let tilesTimer = null;

async function pollTiles() {
  clearTimeout(tilesTimer);
  let status = null;
  try {
    status = await api.getJson("/api/tiles/status", "Tile status");
  } catch { /* older platform or offline: no banner */ }
  const banner = describeTiles(status);
  tilesBanner.textContent = banner?.text || "";
  tilesBanner.className = `tiles-banner ${banner?.kind || ""}${banner ? "" : " hidden"}`;
  if (status) {
    const signature = tilesSignature(status);
    if (tilesSeen !== null && signature !== tilesSeen && !loading) loadPlatformState();
    tilesSeen = signature;
  }
  tilesTimer = setTimeout(pollTiles, pollDelay(status));
}

document.querySelectorAll("header nav button").forEach((button) => button.addEventListener("click", () => setViewer(button.dataset.view)));
reloadButton.addEventListener("click", loadPlatformState);
await loadPlatformState();
pollTiles();
