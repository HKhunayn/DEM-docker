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

// A production container serves the viewer and reverse-proxies the platform
// under the same origin. VITE_PLATFORM_URL remains available for local dev or
// a separately hosted viewer.
const platform = (import.meta.env.VITE_PLATFORM_URL || window.location.origin || "http://127.0.0.1:8787").replace(/\/$/, "");
const api = new PlatformApi(platform);
const app = document.querySelector("#app");

app.innerHTML = `
  <header>
    <div class="brand"><h1>Open GIS Viewers</h1><p>Platform-backed saved areas</p></div>
    <nav aria-label="Viewer selection">
      <button data-view="maplibre" class="active">MapLibre</button>
      <button data-view="arcgis">ArcGIS</button>
      <button data-view="cesium">CesiumJS</button>
    </nav>
  </header>
  <main>
    <aside aria-label="Platform catalog">
      <div class="panel-heading"><h2>Saved areas</h2><button id="reload" class="quiet" title="Reload platform state">↻</button></div>
      <div id="status" class="status" role="status" aria-live="polite">Loading platform state…</div>
      <div id="items" aria-live="polite"></div>
      <div id="details" class="details hidden"></div>
      <div class="panel-heading imagery-heading"><h2>Imagery</h2></div>
      <select id="imagery-select" aria-label="Imagery set"><option value="">— none —</option></select>
      <div id="imagery-info" class="imagery-info">No imagery sets yet — fetch one on the platform's /fetch/ page (Imagery tab).</div>
    </aside>
    <section id="map" aria-label="Geospatial viewers">
      <div id="maplibre" class="viewer"></div>
      <div id="arcgis" class="viewer hidden"></div>
      <div id="cesium" class="viewer hidden"></div>
      <div id="map-message" class="map-message hidden" role="alert"></div>
    </section>
  </main>`;

const status = document.querySelector("#status");
const itemsEl = document.querySelector("#items");
const detailsEl = document.querySelector("#details");
const mapMessage = document.querySelector("#map-message");
let state = null;
let selectedItem = null;
let maplibre;
let arcgisView;
let arcgisMap;
let cesiumViewer;
let selectedImagery = null;
let arcgisPendingGoTo = false;
let mapLibreReady = false;
let arcgisImageryLayer = null;
let cesiumImageryLayer = null;
const imageryInfo = document.querySelector("#imagery-info");
const imagerySelect = document.querySelector("#imagery-select");

function setStatus(message, kind = "info") {
  status.textContent = message;
  status.className = `status ${kind}`;
}

// Viewer messages are scoped: a MapLibre tile error must not cover the Cesium
// tab. "global" messages (platform state) show on every tab. Transient
// messages (individual tile failures) clear themselves.
let activeView = "maplibre";
const viewMessages = {global: "", maplibre: "", arcgis: "", cesium: ""};
const messageTimers = {};

function renderMapMessage() {
  const message = viewMessages.global || viewMessages[activeView] || "";
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

function itemName(item) {
  return item?.title || item?.name || item?.properties?.title || item?.filename || item?.id || "Untitled area";
}

function itemKind(item) {
  return item?.kind || item?.properties?.kind || item?.type || "area";
}

function itemCenter(item) {
  const bbox = itemBounds(item);
  if (bbox) return [(bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2];
  const center = state?.terrain?.center;
  return Array.isArray(center) ? center.slice(0, 2) : [46.76, 24.74];
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
    itemsEl.innerHTML = `<div class="notice">The platform catalog has no saved areas yet.</div>`;
    detailsEl.classList.add("hidden");
    return;
  }
  itemsEl.innerHTML = items.map((item, index) => `
    <button class="item ${item.id === selectedItem?.id ? "selected" : ""}" data-index="${index}">
      <span>${itemName(item)}</span><small>${itemKind(item)}${item.id ? ` · ${item.id}` : ""}</small>
    </button>`).join("");
  itemsEl.querySelectorAll(".item").forEach((button) => button.addEventListener("click", () => selectItem(items[Number(button.dataset.index)])));
}

function renderDetails(item) {
  if (!item) {
    detailsEl.classList.add("hidden");
    return;
  }
  const bbox = itemBounds(item);
  detailsEl.innerHTML = `<strong>${itemName(item)}</strong><span>${bbox ? `Extent: ${bbox.map((value) => Number(value).toFixed(4)).join(", ")}` : "No extent supplied by platform"}</span>`;
  detailsEl.classList.remove("hidden");
}

function selectItem(item) {
  selectedItem = item;
  // Picking an imagery area also switches every viewer to that imagery set.
  if (item?.kind === "imagery") {
    const set = (state?.imagery || []).find((candidate) => candidate.name === item.name);
    if (set && set !== selectedImagery) setImagery(set);
  }
  renderCatalog(allItems());
  renderDetails(item);
  setStatus(`${itemName(item)} selected · updating viewers`, "busy");
  setMapMessage("");
  updateViews();
  setStatus(`${itemName(item)} selected · viewers synchronized`, "success");
}

function terrainTemplates() {
  return tileUrls(state.terrain, api.url("/tiles/terrain.json"));
}

function imageryTemplates() {
  // Prefer the platform's versioned hillshade URL (cache-busted per build).
  const versioned = tileUrls({tiles: state.terrain?.imagery}, api.url("/tiles/terrain.json"));
  if (versioned.length) return versioned;
  const configured = state.catalog?.links?.find((link) => link.rel === "imagery")?.href;
  // Hillshade is only a fallback visualization, not aerial texture. Keep it
  // transparent where coverage is absent; terrain remains the DEM source.
  return [resolveApiUrl(configured || "/tiles/imagery/{z}/{x}/{y}.png", platform)
    .replaceAll("%7B", "{").replaceAll("%7D", "}")];
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
  imagerySelect.innerHTML = `<option value="">— none —</option>` + sets.map((set) =>
    `<option value="${set.name}">${set.name} · ${set.provider} z${set.minzoom}–${set.maxzoom}</option>`).join("");
  imagerySelect.value = selectedImagery?.name || "";
  imagerySelect.disabled = !sets.length;
  imageryInfo.textContent = !sets.length
    ? "No imagery sets yet — fetch one on the platform's /fetch/ page (Imagery tab)."
    : selectedImagery
      ? `${selectedImagery.tileCount ?? "?"} tiles · ${((selectedImagery.bytes || 0) / 1e6).toFixed(2)} MB · over-zoomed above z${selectedImagery.maxzoom} · ${selectedImagery.attribution || ""}`
      : `${sets.length} imagery set(s) available`;
}

function setImagery(set) {
  selectedImagery = set || null;
  renderImageryPicker();
  applyMapLibreImagery();
  applyArcGISImagery();
  applyCesiumImagery();
  setStatus(selectedImagery ? `Imagery “${selectedImagery.name}” shown in MapLibre, ArcGIS and CesiumJS` : "Imagery hidden in all viewers", "success");
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
  if (!url) return;
  const bounds = Array.isArray(set.bounds) && set.bounds.length === 4 ? set.bounds : null;
  // Index 0: under the hillshade drape.
  cesiumImageryLayer = cesiumViewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
    url, maximumLevel: set.maxzoom ?? 18,
    rectangle: bounds ? Cesium.Rectangle.fromDegrees(...bounds) : undefined,
    credit: set.attribution || undefined,
  }), 0);
}

function createMapLibre() {
  const terrain = terrainTemplates();
  const imagery = imageryTemplates();
  maplibre = new maplibregl.Map({
    container: "maplibre",
    center: itemCenter(selectedItem), zoom: itemZoom(selectedItem), pitch: 42,
    style: {version: 8, sources: {
      terrain: {type: "raster-dem", tiles: terrain, tileSize: state.terrain?.tileSize || 256, encoding: state.terrain?.encoding || "terrarium", maxzoom: state.terrain?.maxzoom || 13},
      // Separate DEM source for shading (MapLibre recommends not sharing the
      // terrain source with a hillshade layer).
      relief: {type: "raster-dem", tiles: terrain, tileSize: state.terrain?.tileSize || 256, encoding: state.terrain?.encoding || "terrarium", maxzoom: state.terrain?.maxzoom || 13},
      imagery: {type: "raster", tiles: imagery, tileSize: 256, maxzoom: state.terrain?.maxzoom || 13},
    }, layers: [
      {id: "background", type: "background", paint: {"background-color": "#d9d2c3"}},
      // Client-side hillshade only where it means something. Below ~z6 the
      // DEM is one flat fill per tile, and MapLibre shades each tile against
      // its own back-filled borders, so the whole world shows a patchwork grid
      // of tones with banding. Fade it in from z6 to z8 instead.
      {id: "relief", type: "hillshade", source: "relief", minzoom: 6, paint: {
        "hillshade-exaggeration": ["interpolate", ["linear"], ["zoom"], 6, 0, 8, 0.6],
        "hillshade-shadow-color": "#473b2b",
        "hillshade-highlight-color": "#fffaf0", "hillshade-accent-color": "#8a7a62",
        // Light from a fixed map direction, so tones don't shift when rotating.
        "hillshade-illumination-anchor": "map",
      }},
      {id: "imagery", type: "raster", source: "imagery", paint: {"raster-opacity": 0.28}},
    ]},
  });
  maplibre.addControl(new maplibregl.NavigationControl(), "top-right");
  if (import.meta.env.DEV) window.__viewers = Object.assign(window.__viewers || {}, {maplibre});
  maplibre.on("load", () => {
    maplibre.setTerrain({source: "terrain", exaggeration: 1});
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
  const serviceUrl = serviceForItem(selectedItem);
  // Create the SceneView first. Loading a local/custom ImageServer directly in
  // Ground's constructor can abort SceneView initialization on some SDK builds.
  arcgisMap = new ArcGISMap({basemap: "gray-vector", ground: new Ground()});
  arcgisView = new SceneView({container: "arcgis", map: arcgisMap, center: itemCenter(selectedItem), zoom: itemZoom(selectedItem), qualityProfile: "high"});
  if (import.meta.env.DEV) window.__viewers = Object.assign(window.__viewers || {}, {arcgis: arcgisView});
  applyArcGISImagery();
  try {
    await arcgisView.when();
    if (serviceUrl) {
      arcgisMap.ground.layers.add(new ElevationLayer({url: serviceUrl, title: "Platform elevation"}));
    } else {
      setMapMessage("The platform has no ArcGIS ImageServer URL for this selection.", "arcgis");
    }
  } catch (error) {
    setMapMessage(`ArcGIS viewer error: ${formatError(error)}`, "arcgis");
  }
}

async function createCesium() {
  const serviceUrl = serviceForItem(selectedItem);
  // Always create the viewer. If the native ArcGIS terrain adapter rejects a
  // local service, retain a working globe and use the local Terrarium adapter.
  cesiumViewer = new Cesium.Viewer("cesium", {
    animation: false, timeline: false, baseLayerPicker: false, geocoder: false,
    homeButton: false, sceneModePicker: true, baseLayer: false,
    terrainProvider: new Cesium.EllipsoidTerrainProvider(),
    // Report render errors in the app banner instead of Cesium's modal, which
    // stops rendering for good after one bad tile.
    showRenderLoopErrors: false,
  });
  let renderRestarts = 0;
  cesiumViewer.scene.renderError.addEventListener((_scene, error) => {
    const message = formatError(error);
    if (renderRestarts < 3) {
      renderRestarts += 1;
      setMapMessage(`Cesium recovered from a render error: ${message}`, "cesium", {timeout: 8000});
      setTimeout(() => { cesiumViewer.useDefaultRenderLoop = true; }, 250);
    } else {
      setMapMessage(`Cesium rendering stopped after repeated errors: ${message}. Reload the page.`, "cesium");
    }
  });
  if (import.meta.env.DEV) window.__viewers = Object.assign(window.__viewers || {}, {cesium: cesiumViewer, Cesium});
  // No remote basemap: the ground outside the DEM uses the hillshade's
  // flat-terrain tone (sun at 45 deg -> ~179/255), so the drape edge blends.
  cesiumViewer.scene.globe.baseColor = Cesium.Color.fromBytes(179, 179, 179, 255);
  cesiumViewer.scene.globe.depthTestAgainstTerrain = true;
  const bounds = state.terrain?.bounds;
  cesiumViewer.imageryLayers.addImageryProvider(new Cesium.UrlTemplateImageryProvider({
    url: imageryTemplates()[0],
    // Only request the hillshade drape where the platform has DEM coverage.
    rectangle: Array.isArray(bounds) && bounds.length === 4 ? Cesium.Rectangle.fromDegrees(...bounds) : undefined,
    // Hillshade exists up to the terrain maxzoom; Cesium over-zooms beyond.
    maximumLevel: state.terrain?.maxzoom || 13,
  }));
  applyCesiumImagery();
  // Use the platform Terrarium pyramid: the platform fills no-data and serves
  // flat fill tiles everywhere outside coverage, so the globe has continuous
  // ground. The per-dataset Lerc ImageServers have no level-0 root tile and
  // end at the service extent, which leaves CesiumJS with holes/cliffs.
  try {
    cesiumViewer.terrainProvider = makePlatformTerrainProvider(terrainTemplates(), {credit: "Platform terrain", maxzoom: state.terrain?.maxzoom ?? 13});
  } catch (error) {
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
  cesiumFrame(selectedItem, 0);
}

// Oblique framing of the selected area (a 1500 km nadir view showed only a
// flat colour).
function cesiumFrame(item, duration = 1.5) {
  if (!cesiumViewer) return;
  const bbox = itemBounds(item);
  if (bbox && Math.abs(bbox[2] - bbox[0]) > 90) {
    // Continental/world extents: a bounding sphere of the corners collapses
    // (±180° share a meridian) and a world Rectangle destination lands at
    // ground level, so look straight down from a height that fits the span.
    const [cx, cy] = itemCenter(item);
    const span = Math.max(Math.abs(bbox[2] - bbox[0]), Math.abs(bbox[3] - bbox[1]) * 2);
    const height = Math.min(2.2e7, span * 111_320 * 0.9);
    cesiumViewer.camera.flyTo({destination: Cesium.Cartesian3.fromDegrees(cx, cy, height), duration});
    return;
  }
  const [lon, lat] = itemCenter(item);
  const base = Number(state?.terrain?.statistics?.min) || 0;
  const corners = bbox
    ? [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]]
    : [[lon - 0.1, lat - 0.1], [lon + 0.1, lat + 0.1]];
  const sphere = Cesium.BoundingSphere.fromPoints(corners.map(([x, y]) => Cesium.Cartesian3.fromDegrees(x, y, base)));
  cesiumViewer.camera.flyToBoundingSphere(sphere, {
    duration,
    offset: new Cesium.HeadingPitchRange(0, Cesium.Math.toRadians(-35), sphere.radius * 2.2),
    // A flight cancelled by a newer selection or by user input is expected.
  });
}

function updateViews() {
  if (!selectedItem) return;
  const center = itemCenter(selectedItem); const zoom = itemZoom(selectedItem);
  if (maplibre) maplibre.flyTo({center, zoom});
  if (arcgisView) {
    const serviceUrl = serviceForItem(selectedItem);
    const elevation = arcgisMap?.ground?.layers?.getItemAt(0);
    if (elevation && serviceUrl && elevation.url !== serviceUrl) elevation.url = serviceUrl;
    // goTo rejects with AbortError when superseded by a newer navigation or
    // while the view is hidden in another tab; that is not a failure.
    // A hidden SceneView aborts goTo; replay it when the ArcGIS tab is shown.
    arcgisPendingGoTo = activeView !== "arcgis";
    arcgisView.goTo({center, zoom}).catch((error) => {
      if (error?.name !== "AbortError" && !/abort/i.test(formatError(error))) setMapMessage(`ArcGIS navigation error: ${formatError(error)}`, "arcgis");
    });
  }
  if (cesiumViewer) cesiumFrame(selectedItem);
}

function setViewer(view) {
  activeView = view;
  renderMapMessage();
  document.querySelectorAll("header nav button").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
  document.querySelectorAll(".viewer").forEach((element) => element.classList.toggle("hidden", element.id !== view));
  requestAnimationFrame(() => {
    if (view === "maplibre") maplibre?.resize();
    if (view === "arcgis") {
      // SceneView has no resize(); it tracks its container size itself.
      if (arcgisPendingGoTo && arcgisView && selectedItem) {
        arcgisPendingGoTo = false;
        // The first time the view becomes visible it may still be starting up
        // and abort the navigation; retry a few times until it lands.
        const target = {center: itemCenter(selectedItem), zoom: itemZoom(selectedItem)};
        const attempt = (left) => arcgisView.when()
          .then(() => arcgisView.goTo(target))
          .catch(() => { if (left > 0 && activeView === "arcgis") setTimeout(() => attempt(left - 1), 400); });
        attempt(5);
      }
    }
    if (view === "cesium") cesiumViewer?.resize();
  });
}

async function loadPlatformState() {
  setStatus("Loading catalog, saved areas, and delivery metadata…", "busy");
  itemsEl.innerHTML = `<div class="loading">Contacting the GIS data platform…</div>`;
  setMapMessage("");
  try {
    state = await api.loadState();
    const items = allItems();
    // Default to a built elevation area; imagery areas are selectable too.
    selectedItem = items.find((item) => item?.kind !== "imagery" && item?.dataset?.state === "done")
      || items.find((item) => item?.dataset?.service) || items[0] || null;
    // Keep the current imagery choice across reloads when it still exists.
    const sets = state.imagery || [];
    selectedImagery = sets.find((set) => set.name === selectedImagery?.name) || sets[0] || null;
    renderCatalog(items); renderDetails(selectedItem); renderImageryPicker();
    if (!selectedItem) { setStatus("Platform connected · no saved areas", "warning"); setMapMessage("Select a saved area after it is added to the platform catalog."); return; }
    setStatus(`Platform connected · ${items.length} saved area(s)`, "success");
    if (!maplibre) {
      createMapLibre();
      // ArcGIS may take time to resolve its SceneView, but it must not block
      // Cesium initialization while the ArcGIS view is hidden in a tab.
      createArcGIS();
      await createCesium();
    } else { updateViews(); setImagery(selectedImagery); }
  } catch (error) {
    state = null; selectedItem = null;
    itemsEl.innerHTML = `<div class="notice error">${formatError(error)}<br><button id="retry">Retry</button></div>`;
    document.querySelector("#retry")?.addEventListener("click", loadPlatformState);
    setStatus(`Platform unavailable · ${formatError(error)}`, "error");
    setMapMessage(`Viewer initialization failed: ${formatError(error)}`);
  }
}

document.querySelectorAll("header nav button").forEach((button) => button.addEventListener("click", () => setViewer(button.dataset.view)));
document.querySelector("#reload").addEventListener("click", loadPlatformState);
await loadPlatformState();
