# python:3.12-slim — stable, has prebuilt wheels for every dependency.
# (The host runs 3.14, which is too new for some libs; the container pins 3.12.)
FROM python:3.12-slim

WORKDIR /app

# NOTE: the M5 fault-injection tools (`stress` for a CPU spike, `iproute2` for
# `tc` network delay) are intentionally NOT installed here yet. They need an
# apt-get against deb.debian.org, which the current build environment can't
# reach. They get added back in M5 when they're actually used. Nothing in M1–M4
# depends on them.

# Install deps first so Docker layer-caches them when only app code changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Logs are written here; docker-compose bind-mounts ./logs from the host onto
# this path so the host log-watcher can tail the file.
RUN mkdir -p /app/logs
EXPOSE 8000

# Container-level health check. Coarser than the app's /health: it hits `/`,
# which is 200 whenever the ASGI server is answering at all. The real dependency
# check is GET /health, polled from the host by the M4 watchdog. Uses stdlib
# urllib so no extra package is needed in the image.
HEALTHCHECK --interval=10s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/').status==200 else 1)"

# One worker: the in-process tick generator thread and the metric objects must
# live in a single process, otherwise each worker would have its own counters.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
