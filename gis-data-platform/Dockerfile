# ---- stage 1: compile the Lerc1 encoder used for ArcGIS elevation tiles ----
# Source is GDAL's frmts/mrf/LERCV1 (Esri Lerc1, Apache-2.0), bundled in
# engine/scripts/ so the image builds with no engine checkout and no MSVC.
FROM python:3.13-slim AS lerc
RUN apt-get update \
    && apt-get install -y --no-install-recommends g++ \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY engine/scripts/lerc1enc.cpp engine/scripts/lercv1/Lerc1Image.cpp engine/scripts/lercv1/Lerc1Image.h ./
RUN g++ -O2 -std=c++17 lerc1enc.cpp Lerc1Image.cpp -o lerc1enc

# ---- stage 2: API + heightmap-30m DEM engine ----
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && python - <<'PY'
import urllib.request
from pathlib import Path

version = "1.9.4"
base = f"https://unpkg.com/leaflet@{version}/dist"
target = Path("/opt/leaflet/dist")
target.mkdir(parents=True, exist_ok=True)
for name in ("leaflet.js", "leaflet.css", "images/layers.png", "images/layers-2x.png", "images/marker-icon.png", "images/marker-icon-2x.png", "images/marker-shadow.png"):
    destination = target / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(urllib.request.urlopen(f"{base}/{name}", timeout=30).read())
PY

# heightmap-30m engine (fetch/build/verify/serve) + its prebuilt Lerc1 encoder
COPY engine /opt/heightmap-30m
COPY --from=lerc /build/lerc1enc /opt/heightmap-30m/scripts/lerc1enc

COPY app ./app

RUN mkdir -p /app/data

EXPOSE 8787

HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=10 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=3)"

CMD ["python", "-m", "app", "--host", "0.0.0.0", "--port", "8787"]
