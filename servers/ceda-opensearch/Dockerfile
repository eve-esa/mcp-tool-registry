# Mirrors shared/Dockerfile of eve-esa/mcp-tool-registry.
# Build from this directory: docker build -t ceda-opensearch-mcp .
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "server.py"]
