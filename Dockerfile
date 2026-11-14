# SpaceApps 2026 Data API image.
#
#   docker build -t spaceapps-data .
#   docker run --rm -p 8091:8091 spaceapps-data
#
# Swagger UI: http://localhost:8091/docs
#
# Without a mounted database the API serves the committed Orillia preview.
# For indexed coverage, mount a directory holding fields.sqlite and set FIELDS_DB,
# e.g. -v /path/to/fields:/var/lib/spaceapps-fields
#      -e FIELDS_DB=/var/lib/spaceapps-fields/fields.sqlite
# Mount the whole directory, not just the file, so SQLite WAL/SHM files are shared.
# Credentials are never baked in: mount .env at /app/.env for /daily-point.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8091

WORKDIR /app

COPY requirements.txt .
# rasterio's bundled GDAL needs libexpat, which the slim image does not include.
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends libexpat1 >/dev/null \
 && rm -rf /var/lib/apt/lists/* \
 && pip install -r requirements.txt \
 && useradd --system --uid 1000 --user-group --no-create-home spaceapps

COPY *.py ./
COPY environmental ./environmental
COPY data/fields.geojson ./data/

RUN mkdir -p /app/data/smap && chown spaceapps:spaceapps /app/data/smap

USER 1000:1000
EXPOSE 8091

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/ready', timeout=2)"

CMD ["sh", "-c", "exec uvicorn server:app --host 0.0.0.0 --port \"$PORT\""]
