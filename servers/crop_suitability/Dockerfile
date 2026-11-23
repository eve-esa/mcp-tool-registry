# Crop Suitability MCP image: same layout as ../../shared/Dockerfile, plus libexpat1,
# which the rasterio wheel (GDAL) needs at runtime and python:3.13-slim no longer ships.
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "server.py"]
