# python:3.12-slim — stable, has prebuilt wheels for every dependency.
# (The host runs 3.14, which is too new for some libs; the container pins 3.12.)
FROM python:3.12-slim

WORKDIR /app

# M5 fault-injection tooling: `stress` (CPU spike) and `iproute2` (`tc` netem
# for network-latency injection). Only used by scripts/induce_failure.sh, never
# by the service itself.
#
# The `|| echo` keeps the build green on networks that can't reach
# deb.debian.org (this dev box is one — it only has egress to PyPI). Where apt
# works (CI, EC2) the tools land in the image and induce_failure.sh uses them
# directly; where it doesn't, the script falls back to a host-side equivalent
# (a Python busy-loop for CPU, `tc` on the container's veth from the host).
RUN apt-get update \
    && apt-get install -y --no-install-recommends stress iproute2 \
    && rm -rf /var/lib/apt/lists/* \
    || echo "WARNING: could not install stress/iproute2 — induce_failure.sh will use host-side fallbacks"

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
