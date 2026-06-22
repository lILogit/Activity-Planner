# KAIROS — single-process FastAPI service.
# One container: uvicorn + in-process APScheduler + a SQLite file on a volume.
# (Golden Rule #1: one process, one datastore. No worker, no broker, no second service.)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# SQLite needs no extra system libs. tzdata keeps APScheduler cron jobs on local time (TZ).
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Deps first → cache layer survives code changes.
COPY requirements.txt .
RUN pip install -r requirements.txt

# App code. schema.sql + seed.sql ship next to db.py and are read via Path(__file__).
COPY app/ ./app/

# Non-root runtime. /data is the SQLite volume mount point (DB_PATH=/data/kairos.db).
RUN useradd --create-home --uid 1000 kairos \
    && mkdir -p /data && chown -R kairos:kairos /app /data
USER kairos

EXPOSE 8000

# Single uvicorn process. APScheduler runs in-process — no separate worker.
# --proxy-headers: trust the host TLS reverse proxy's X-Forwarded-* headers.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
