# syntax=docker/dockerfile:1
FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Named context "runtime" is the shared/ directory. The launcher is available
# in every image. A server starts it with python -m eve_mcp_runtime server.py.
COPY --from=runtime eve_mcp_runtime ./eve_mcp_runtime

EXPOSE 8000

CMD ["python", "server.py"]
