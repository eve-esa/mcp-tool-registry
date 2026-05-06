# EFFIS MCP image: same layout as ../../shared/Dockerfile, plus the EFFIS
# ``modis.ba.poly`` shapefile fetched at *build* time into ``effis_layer/``
# (historical date queries in ``server.py`` use this cache; no runtime download).
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates unzip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# ~200 MiB compressed from EFFIS WFS; allow up to 30 min on slow links / emulated ARM builds.
# Prefer HTTPS; fall back to HTTP only when HTTPS download fails (e.g. temporary cert issue).
ARG EFFIS_SHAPEZIP_URL="https://maps.effis.emergency.copernicus.eu/effis?service=WFS&request=GetFeature&typename=ms:modis.ba.poly&version=1.1.0&outputformat=SHAPEZIP"
ARG EFFIS_SHAPEZIP_FALLBACK_URL="http://maps.effis.emergency.copernicus.eu/effis?service=WFS&request=GetFeature&typename=ms:modis.ba.poly&version=1.1.0&outputformat=SHAPEZIP"
RUN mkdir -p effis_layer \
    && (curl -fSL --connect-timeout 60 --max-time 1800 \
        "${EFFIS_SHAPEZIP_URL}" -o /tmp/effis_modis.poly.zip \
        || (echo "HTTPS download failed, retrying over HTTP fallback..." >&2 \
            && curl -fSL --connect-timeout 60 --max-time 1800 \
            "${EFFIS_SHAPEZIP_FALLBACK_URL}" -o /tmp/effis_modis.poly.zip)) \
    && unzip -oq /tmp/effis_modis.poly.zip -d effis_layer \
    && rm -f /tmp/effis_modis.poly.zip \
    && test -f effis_layer/modis.ba.poly.shp

EXPOSE 8000

CMD ["python", "server.py"]
