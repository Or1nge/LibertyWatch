FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=5048 \
    REFRESH_INTERVAL_MS=60000 \
    WATCHLIST_FILE=/app/config/watchlist.json \
    DEMO_WATCHLIST_FILE=/app/config/demo-watchlist.json \
    SNAPSHOT_FILE=/app/runtime/latest_snapshot.json \
    PUBLIC_DIR=/app/public

WORKDIR /app

COPY requirements.txt /app/requirements.txt
COPY vendor/wheels /app/wheels
RUN pip install --no-cache-dir --no-compile --no-index \
      --find-links=/app/wheels -r /app/requirements.txt \
    && rm -rf /app/wheels \
    && groupadd --gid 10001 liberty \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin liberty

COPY --chown=10001:10001 app /app/app
COPY --chown=10001:10001 liberty_v2 /app/liberty_v2
COPY --chown=10001:10001 public /app/public
COPY --chown=10001:10001 config /app/config
RUN mkdir -p /app/runtime && chown 10001:10001 /app/runtime

USER 10001:10001

EXPOSE 5048

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5048/healthz', timeout=3).read()"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "5048", "--no-access-log", "--no-server-header"]
