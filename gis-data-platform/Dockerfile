# Base pinned to the Python patch release the image was validated with.
# Bump deliberately (and re-run the tests) rather than floating on 3.13-slim.
ARG PYTHON_IMAGE=python:3.13.18-slim-trixie

# ---- stage 1: compile the Lerc1 encoder used for ArcGIS elevation tiles ----
# Source is GDAL's frmts/mrf/LERCV1 (Esri Lerc1, Apache-2.0), bundled in
# engine/scripts/ so the image builds with no engine checkout and no MSVC.
FROM ${PYTHON_IMAGE} AS lerc
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY engine/scripts/lerc1enc.cpp engine/scripts/lercv1/Lerc1Image.cpp engine/scripts/lercv1/Lerc1Image.h ./
RUN g++ -O2 -std=c++17 lerc1enc.cpp Lerc1Image.cpp -o lerc1enc

# ---- stage 2: API + heightmap-30m DEM engine ----
FROM ${PYTHON_IMAGE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    GIS_DATA_ROOT=/app/data \
    HEIGHTMAP_ENGINE_ROOT=/opt/heightmap-30m \
    HEIGHTMAP_PYTHON=/usr/local/bin/python \
    LEAFLET_ROOT=/opt/leaflet

WORKDIR /app

# rasterio's bundled GDAL libraries need libexpat.so.1 at runtime; the
# python:slim base image does not ship it and the wheels do not bundle it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

# Leaflet for the /fetch page, served locally from /vendor/leaflet. Own layer
# (before requirements) so dependency changes don't re-download it; every file
# is SHA-256 verified so a tampered or changed CDN response fails the build.
RUN python - <<'PY'
import hashlib
import urllib.request
from pathlib import Path

version = "1.9.4"
base = f"https://unpkg.com/leaflet@{version}/dist"
target = Path("/opt/leaflet/dist")
files = {
    "leaflet.js": "db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a",
    "leaflet.css": "a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6",
    "images/layers.png": "1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6",
    "images/layers-2x.png": "066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf",
    "images/marker-icon.png": "574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437",
    "images/marker-icon-2x.png": "00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528",
    "images/marker-shadow.png": "264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da",
}
for name, expected in files.items():
    data = urllib.request.urlopen(f"{base}/{name}", timeout=30).read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(f"leaflet {name}: sha256 {actual} != expected {expected}")
    destination = target / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
print("leaflet", version, "verified:", len(files), "files")
PY

# Runtime dependencies only (pinned). Test tooling is in requirements-dev.txt.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# heightmap-30m engine (fetch/build/verify/serve) + its prebuilt Lerc1 encoder
COPY engine /opt/heightmap-30m
COPY --from=lerc /build/lerc1enc /opt/heightmap-30m/scripts/lerc1enc

COPY app ./app

# NOTE: runs as root on purpose for now: the existing gis-data volume (tens of
# GB of tiles) is root-owned. Moving to a non-root user needs a one-off
# chown of the volume first.
RUN mkdir -p /app/data

EXPOSE 8787

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"

CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8787"]
