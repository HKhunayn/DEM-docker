// Small, DOM-free helpers shared by the viewer UI (unit-tested in node).

const HTML_ESCAPES = {"&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"};

/** Escape any value for safe interpolation into HTML text or a quoted attribute. */
export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => HTML_ESCAPES[char]);
}

/**
 * Retry delay after `attempt` consecutive failures (0-based):
 * 1.5 s, 3 s, 6 s, 12 s, 24 s, then capped at 30 s.
 */
export function backoffDelay(attempt, {base = 1500, max = 30000} = {}) {
  const n = Math.max(0, Math.floor(Number(attempt) || 0));
  return Math.min(max, base * 2 ** Math.min(n, 30));
}

/** Human-readable elapsed time: "7s", "2m 05s", "1h 02m". */
export function formatElapsed(ms) {
  const total = Math.max(0, Math.floor((Number(ms) || 0) / 1000));
  if (total < 60) return `${total}s`;
  const minutes = Math.floor(total / 60); const seconds = total % 60;
  if (minutes < 60) return `${minutes}m ${String(seconds).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/**
 * Resolve with the promise's outcome, or with `{timedOut: true}` after `ms`.
 * Used so a navigation that never reports completion (e.g. a hidden viewer)
 * cannot leave the UI busy forever. Rejections propagate unchanged.
 */
export function withTimeout(promise, ms) {
  let timer;
  const timeout = new Promise((resolve) => { timer = setTimeout(() => resolve({timedOut: true}), ms); });
  return Promise.race([Promise.resolve(promise), timeout]).finally(() => clearTimeout(timer));
}

/** True when an error is an expected abort (superseded navigation etc.). */
export function isAbortError(error) {
  if (!error) return false;
  if (error.name === "AbortError" || error.name === "view:goto-interrupted") return true;
  return /abort|interrupt/i.test(String(error.message || error));
}

/** Stable comparison key for a list of tile URL templates. */
export function templatesKey(templates) {
  return JSON.stringify(Array.isArray(templates) ? templates : []);
}
