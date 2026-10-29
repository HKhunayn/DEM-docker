// Banner text for GET /api/tiles/status: the platform builds missing tiles by
// itself (e.g. after a volume import that carried only the DEMs), and the
// viewer has to say so instead of showing an empty map. DOM-free (node tests).
import {formatElapsed} from "./uiHelpers.js";

const RUNNING = new Set(["checking", "running"]);

export function buildProgress(current) {
  if (!current) return "";
  const what = current.mode === "full" ? "elevation tiles + ArcGIS service" : "ArcGIS elevation service";
  const step = current.step ? `${current.step} step` : "starting";
  const elapsed = Number.isFinite(current.elapsed) ? ` · ${formatElapsed(current.elapsed * 1000)}` : "";
  const count = current.total > 1 ? ` · build ${current.index} of ${current.total}` : "";
  return `${current.name} (${what}) · ${step}${elapsed}${count}`;
}

/** {text, kind: "info" | "warning"} or null when there is nothing to say. */
export function describeTiles(status) {
  if (!status || typeof status !== "object") return null;
  const auto = status.autobuild || {};
  const published = status.published || {};
  const running = RUNNING.has(auto.state);
  const progress = buildProgress(auto.current) || auto.message || "checking what is missing";
  if (!published.ok) {
    if (running) {
      return {kind: "info", text: `Building the elevation tiles (the data has none yet): ${progress}. The map appears by itself when this finishes.`};
    }
    if (auto.state === "blocked") return {kind: "warning", text: `No elevation tiles yet - automatic build ${auto.message}`};
    if (auto.state === "error") return {kind: "warning", text: `No elevation tiles - the automatic build failed (${auto.message}). Check the job log on the fetch page (/fetch/).`};
    return {kind: "warning", text: `No elevation tiles: ${published.reason || "nothing built yet"}. Build an area on the fetch page (/fetch/).`};
  }
  if (running && auto.current) return {kind: "info", text: `Updating tiles: ${progress}`};
  if (auto.state === "blocked") return {kind: "warning", text: `Tile update ${auto.message}`};
  return null;
}

/** Changes whenever new tiles went live (viewers must reload their sources). */
export function tilesSignature(status) {
  const done = status?.autobuild?.done;
  return `${status?.published?.version || ""}|${Array.isArray(done) ? done.length : 0}`;
}

/** Poll faster while something is being built or nothing is published. */
export function pollDelay(status) {
  if (!status) return 60000;
  return RUNNING.has(status.autobuild?.state) || !status.published?.ok ? 5000 : 60000;
}
