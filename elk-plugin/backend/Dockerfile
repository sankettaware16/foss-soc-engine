# =====================================================================
#  tlsoc-parser-ui  ·  headless backend for the Kibana plugin
# ---------------------------------------------------------------------
#  Runs the FOSS SOC Engine Web UI as an API-only service (no login -
#  Kibana authenticates the user, and this service is only reachable on
#  the internal Docker network). The Kibana plugin proxies /api/* here.
#
#  Build from the ENGINE REPO ROOT (not this folder):
#     docker build -f elk-plugin/backend/Dockerfile -t tlsoc-parser-ui:1.0.0 .
#  (a .dockerignore at the repo root keeps the huge sample logs out)
# =====================================================================
FROM python:3.12-slim

WORKDIR /app

# Minimal deps only: Flask + PyYAML. redis / geoip2 / kafka / orjson are all
# optional - the UI degrades gracefully without them, keeping this image small.
COPY webui/requirements-ui.txt /app/webui/requirements-ui.txt
RUN pip install --no-cache-dir -r /app/webui/requirements-ui.txt

# Engine + UI code (the real modules the UI reuses - never a second engine).
COPY core/ /app/core/
COPY utils/ /app/utils/
COPY webui/ /app/webui/
COPY test_config.py preflight.py ecs_helper.py LICENSE /app/

# Default rules/config baked in; in production these are overridden by a
# read-write bind-mount to the REAL engine dir (see docker-compose.snippet.yml)
# so edits/tests in Kibana act on the live engine's rules and config.
COPY rules/ /app/rules/
COPY config.yaml /app/config.yaml

ENV SOC_UI_HOST=0.0.0.0 \
    SOC_UI_PORT=8600 \
    SOC_UI_NO_AUTH=1 \
    SOC_UI_NO_BROWSER=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8600

# Simple healthcheck against the API.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8600/api/whoami', timeout=4)" || exit 1

CMD ["python", "webui/app.py"]
