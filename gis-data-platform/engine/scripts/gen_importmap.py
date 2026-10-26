"""Generate a browser-correct importmap for the vendored @arcgis/core graph.

Walks installed packages for bare import specifiers (tslib, luxon,
@esri/calcite-components/..., lit, es-toolkit, ...), resolves each through its
package `exports` map using the conditions a browser honors (import > browser >
module > development > default — never require/node), and injects the result
into public/viewer/index.html's <script type="importmap"> block.

One prefix fallback (`@arcgis/core/`) covers the entry modules the viewer
imports directly. Re-run after `npm install` / `npm update`:

    .venv/Scripts/python.exe scripts/gen_importmap.py
"""
import json
import os
import re

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NM = os.path.join(HERE, "node_modules")
VIEWER = os.path.join(HERE, "public", "viewer", "index.html")

SCAN_PKGS = [
    "@arcgis/core", "@arcgis/toolkit", "@esri/arcgis-html-sanitizer",
    "@esri/calcite-components", "@zip.js/zip.js", "luxon", "tslib",
    "@amcharts/amcharts5", "lit", "es-toolkit",
]
PAT = re.compile(r"""(?:from|import|import\()\s*["']([^"'\s]+)["']""")
NOISE = re.compile(r"[\w@][\w\-.@/]*")

# Packages whose `exports`/`module` default to a Node or CJS build; pin the
# browser ESM file instead. (Keys are exact import specifiers.)
OVERRIDES = {
    "tslib": "tslib/tslib.es6.mjs",
    "luxon": "luxon/build/es6/luxon.mjs",
    "@zip.js/zip.js": "@zip.js/zip.js/index.js",
    "@esri/arcgis-html-sanitizer": "@esri/arcgis-html-sanitizer/dist/esm/index.js",
    "@arcgis/toolkit/dom": "@arcgis/toolkit/dist/dom/index.js",
    "@arcgis/toolkit/intl": "@arcgis/toolkit/dist/intl/index.js",
    "@arcgis/toolkit/string": "@arcgis/toolkit/dist/string/index.js",
    "@floating-ui/dom": "@floating-ui/dom/dist/floating-ui.dom.mjs",
    "composed-offset-position":
        "composed-offset-position/dist/composed-offset-position.browser.min.mjs",
    "svg-arc-to-cubic-bezier": "svg-arc-to-cubic-bezier/modules/index.js",
    "focus-trap": "focus-trap/dist/focus-trap.esm.js",
    "sortablejs": "sortablejs/modular/sortable.esm.js",
    "flatpickr": "flatpickr/dist/esm/index.js",
    "d3-sankey": "d3-sankey/src/index.js",
}

# Trailing-slash prefix fallbacks: specifier-subpath -> file-subpath (1:1).
PREFIXES = {"@arcgis/core/": "/vendor/@arcgis/core/"}

CONDITIONS = ["import", "browser", "module", "development", "default"]


def pkg_of(spec):
    parts = spec.split("/")
    if spec.startswith("@"):
        return "/".join(parts[:2]), "/".join(parts[2:])
    return parts[0], "/".join(parts[1:])


def pick(node):
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        for cond in CONDITIONS:
            if cond in node:
                got = pick(node[cond])
                if got:
                    return got
    return None


def match_exports(exports, sub):
    s = "./" + sub if sub else "."
    if isinstance(exports, str):
        return pick(exports)
    keys = list(exports)
    if not any(k.startswith(".") for k in keys):  # condition-only map for "."
        return pick(exports) if not sub else None
    for k in keys:
        v = exports[k]
        if k == "." and not sub:
            got = pick(v)
            if got:
                return got
        elif k.endswith("/*") and s.startswith(k[:-1]):
            got = pick(v)
            if got:
                return got.replace("*", s[len(k) - 1:])
        elif k == "./" and sub:
            got = pick(v)
            if got:
                return got + sub
        elif k in (s, s + ".js"):
            got = pick(v)
            if got:
                return got
    return None


def resolve(spec):
    if spec in OVERRIDES:
        return OVERRIDES[spec]
    pkg, sub = pkg_of(spec)
    pj_path = os.path.join(NM, *pkg.split("/"), "package.json")
    if not os.path.exists(pj_path):
        return None
    pj = json.load(open(pj_path, encoding="utf-8"))
    if pj.get("exports") is not None:
        got = match_exports(pj["exports"], sub)
        return os.path.join(pkg, got.lstrip("./")) if got else None
    if sub:
        return f"{pkg}/{sub}"
    return os.path.join(pkg, pj.get("module") or pj.get("main") or "")


def main():
    specs = set()
    to_scan = list(SCAN_PKGS)
    seen = set()
    while to_scan:  # transitive closure: scan every package we import
        pkg = to_scan.pop()
        if pkg in seen:
            continue
        seen.add(pkg)
        root = os.path.join(NM, *pkg.split("/"))
        if not os.path.isdir(root):
            continue
        for dp, _dirs, files in os.walk(root):
            for fn in files:
                if not fn.endswith((".js", ".mjs")):
                    continue
                try:
                    text = open(os.path.join(dp, fn), encoding="utf-8",
                                errors="replace").read()
                except OSError:
                    continue
                for m in PAT.finditer(text):
                    s = m.group(1)
                    if s.startswith((".", "/", "data:", "http")) or not NOISE.fullmatch(s):
                        continue
                    specs.add(s)
                    owner = pkg_of(s)[0]
                    if owner not in seen:
                        to_scan.append(owner)
    print(f"scanned {len(seen)} packages, {len(specs)} bare specifiers")

    lines, skipped = dict(PREFIXES), []
    for spec in sorted(specs):
        rel = resolve(spec)
        if rel is None:
            skipped.append(spec)
            continue
        if not os.path.exists(os.path.join(NM, *rel.split("/"))):
            if os.path.exists(os.path.join(NM, *(rel + ".js").split("/"))):
                rel += ".js"  # extensionless subpath like "@amcharts/amcharts5/xy"
            else:
                skipped.append(f"{spec} -> {rel} (missing)")
                continue
        lines[spec] = "/vendor/" + rel.replace(os.sep, "/")

    for s in skipped:
        print("  SKIPPED", s)
    print(f"{len(lines)} importmap entries")

    json_blob = json.dumps(lines, indent=6, sort_keys=True).replace("\n", "\n    ")
    block = f'<script type="importmap">\n    {{ "imports": {json_blob} }}\n  </script>'
    html = open(VIEWER, encoding="utf-8").read()
    html, n = re.subn(r'<script type="importmap">.*?</script>', block, html, count=1,
                      flags=re.S)
    if n != 1:
        raise RuntimeError("importmap block not found in viewer page")
    open(VIEWER, "w", encoding="utf-8").write(html)
    json.dump(lines, open(os.path.join(HERE, "data", "importmap.json"), "w"), indent=1)
    print("injected into", VIEWER)


if __name__ == "__main__":
    main()
