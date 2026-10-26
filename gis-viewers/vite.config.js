import {defineConfig} from "vite";
import {createReadStream, cpSync, existsSync, statSync} from "node:fs";
import {extname, join, normalize, resolve, sep} from "node:path";

// CesiumJS loads Workers, Assets (skybox, textures, icons), ThirdParty and
// Widgets at runtime from CESIUM_BASE_URL. Without serving them, Vite's SPA
// fallback returns index.html and Cesium fails with "The source image could
// not be decoded". Serve them at /cesium/ in dev and copy them on build.
const CESIUM_BUILD = resolve("node_modules/cesium/Build/Cesium");
const CESIUM_BASE = "/cesium/";
const MIME = {
  ".js": "text/javascript", ".json": "application/json", ".css": "text/css",
  ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml",
  ".wasm": "application/wasm", ".xml": "application/xml", ".glb": "model/gltf-binary",
};

function cesiumAssets() {
  return {
    name: "local-cesium-assets",
    configureServer(server) {
      server.middlewares.use(CESIUM_BASE, (req, res, next) => {
        const relative = decodeURIComponent((req.url || "/").split("?")[0]);
        const file = normalize(join(CESIUM_BUILD, relative));
        if (!file.startsWith(CESIUM_BUILD + sep) || !existsSync(file) || !statSync(file).isFile()) {
          res.statusCode = 404;
          res.end("Not found");
          return;
        }
        res.setHeader("Content-Type", MIME[extname(file).toLowerCase()] || "application/octet-stream");
        createReadStream(file).pipe(res);
      });
    },
    writeBundle(options) {
      const outDir = options.dir || resolve("dist");
      for (const folder of ["Workers", "Assets", "ThirdParty", "Widgets"]) {
        cpSync(join(CESIUM_BUILD, folder), join(outDir, "cesium", folder), {recursive: true});
      }
    },
  };
}

export default defineConfig({
  plugins: [cesiumAssets()],
  server: {host: "127.0.0.1", port: 5173},
  optimizeDeps: {include: ["cesium", "maplibre-gl"]},
});
