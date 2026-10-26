# GIS Viewers

One open-source browser application with three viewer adapters:

- **MapLibre GL JS**: raster DEM terrain from the platform Terrarium TileJSON (`/tiles/terrain.json`) and platform imagery tiles.
- **ArcGIS Maps SDK for JavaScript**: native `ElevationLayer` against the platform ArcGIS ImageServer URL from `/api/services`.
- **CesiumJS**: platform imagery plus a Terrarium adapter driven by the same platform TileJSON.

The UI is intentionally not a data owner: one `PlatformApi.loadState()` fetch combines `/api/catalog`, `/api/services`, and `/tiles/terrain.json`, then saved-area selection coordinates MapLibre, ArcGIS, and CesiumJS. Add saved areas through the first project's catalog/API; the viewer only renders the returned catalog state.

## Run

The recommended deployment is from the repository root with Docker Compose:

```bash
./Run.sh up
# Windows: Run.bat
```

The viewer is served at `http://127.0.0.1:5173/` and proxies API, tile,
ArcGIS, fetch-page, and Leaflet requests to the data platform. Runtime data is
kept in Compose's named `gis-data` volume and is not included in the image.

For local Vite development:

```bash
npm install
set VITE_PLATFORM_URL=http://127.0.0.1:8787
npm run dev
```

`VITE_PLATFORM_URL` can point at a production reverse proxy. If it is empty in
the composed image, the viewer uses its own origin. The app intentionally does
not assume a cloud provider or a token.

## Production notes

- Use platform TileJSON for MapLibre raster-dem.
- Use a published ArcGIS ImageServer URL for ArcGIS `ElevationLayer`.
- Publish Cesium quantized-mesh or heightmap terrain and configure a `CesiumTerrainProvider`; imagery can be XYZ/TMS/WMTS.
- Serve all immutable tiles through a CDN or reverse proxy with long-lived caching.
