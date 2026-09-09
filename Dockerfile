# Masseberegning API.
#
# No GDAL system packages: the rasterio wheels on PyPI bundle their own GDAL
# for linux/amd64. Adding gdal-bin here triples the image size and invites
# version conflicts between the system library and the one inside the wheel.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Requirements first, so a code change does not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py ./
COPY migrations/ ./migrations/

# Only used when SERVE_STATIC=1. In production the UI is on GitHub Pages, but
# copying it keeps the image able to run the whole app standalone, which is
# useful for smoke-testing the container before pointing Pages at it.
COPY static/ ./static/

# Job output. Mount a volume here if you want jobs to survive a restart;
# otherwise they expire with the container, which the TTL already assumes.
ENV JOB_ROOT=/var/lib/masseberegning/jobs
RUN mkdir -p /var/lib/masseberegning/jobs && \
    useradd --create-home --uid 10001 app && \
    chown -R app:app /var/lib/masseberegning
USER app

EXPOSE 8000
ENV HOST=0.0.0.0 PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4).status==200 else 1)"

# Exec form so uvicorn is PID 1 and receives SIGTERM directly, which matters
# for graceful shutdown when the platform scales the revision down.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 65"]
