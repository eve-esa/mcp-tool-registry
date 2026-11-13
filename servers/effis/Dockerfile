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

# Includes .env when the deploy workflow has replaced the .env.template
# placeholders. .dockerignore must not exclude that file.
COPY . .

# ~200 MiB compressed from EFFIS WFS; allow up to 30 min on slow links / emulated ARM builds.
# Prefer verified HTTPS. The wildcard cert (*.effis.emergency.copernicus.eu,
# Sectigo, notAfter 2026-11-11) is expired, and the HTTP URL only 301s back to
# HTTPS, so a plain fallback still dies with curl exit 60. Retry the same URL
# without expiry checks, pinned to the key that served the layer on 2026-11-12
# (curl --pinnedpubkey, https://curl.se/docs/manpage.html). A renewed valid cert
# takes the first path and never uses the pin. Drop the pin once EFFIS renews it.
ARG EFFIS_SHAPEZIP_URL="https://maps.effis.emergency.copernicus.eu/effis?service=WFS&request=GetFeature&typename=ms:modis.ba.poly&version=1.1.0&outputformat=SHAPEZIP"
ARG EFFIS_PINNED_PUBKEY="sha256//Jezz2pXi7RwTS8b8GWWfkaFNRStOvwK+4d2tfSlDp0E="
RUN mkdir -p effis_layer \
    && (curl -fSL --connect-timeout 60 --max-time 1800 \
        "${EFFIS_SHAPEZIP_URL}" -o /tmp/effis_modis.poly.zip \
        || (echo "Verified HTTPS failed, retrying with the pinned EFFIS key..." >&2 \
            && curl -kfSL --connect-timeout 60 --max-time 1800 \
            --pinnedpubkey "${EFFIS_PINNED_PUBKEY}" \
            "${EFFIS_SHAPEZIP_URL}" -o /tmp/effis_modis.poly.zip)) \
    && unzip -oq /tmp/effis_modis.poly.zip -d effis_layer \
    && rm -f /tmp/effis_modis.poly.zip \
    && test -f effis_layer/modis.ba.poly.shp

EXPOSE 8000

ENV PYTHONPATH=/app

CMD ["python", "-m", "eve_mcp_runtime", "server.py"]
