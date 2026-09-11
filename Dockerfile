# Masseberegning API.
#
# No GDAL *toolchain* here: the rasterio wheels on PyPI bundle their own GDAL,
# GEOS, PROJ and friends for linux/amd64, so installing gdal-bin would triple
# the image and invite conflicts between the system library and the one inside
# the wheel.
#
# But "bundles GDAL" is not the same as "needs nothing". The wheel ships its
# own copies of everything hash-suffixed (libgeos-*.so, libproj-*.so, ...) and
# links the rest against the base image. On python:*-slim one of those is
# missing, and the failure is brutal: `import rasterio` dies with
#
#   ImportError: libexpat.so.1: cannot open shared object file
#
# at module-import time, so uvicorn never binds, the platform has no healthy
# machine, every request gets a bodiless 503, and the browser reports it as a
# CORS error. Hours of debugging for one absent package.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Runtime shared libraries the rasterio/pyproj wheels expect from the image.
# libexpat1 is the one slim omits (GDAL uses it for its XML-based drivers);
# libstdc++6 is listed because the bundled GEOS and GDAL are C++ and it is
# cheap insurance rather than a second round of this same bug.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libexpat1 libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Requirements first, so a code change does not invalidate the dependency layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fail the BUILD if the geospatial stack cannot import.
#
# This is the check that was missing. CI runs the test suite on ubuntu-latest,
# where libexpat is present, so it passed happily while the image was broken -
# an image-specific problem can only be caught by importing inside the image.
# A build that fails here costs a minute; the alternative cost an evening.
RUN python -c "\
import numpy, shapely, pyproj, rasterio, PIL;\
from rasterio.features import rasterize;\
from pyproj import Transformer;\
Transformer.from_crs('EPSG:4326', 'EPSG:25833', always_xy=True);\
print('geospatial stack imports OK:', rasterio.__version__, pyproj.__version__)"

COPY *.py ./
COPY migrations/ ./migrations/

# And fail the build if the app module itself cannot be imported. Everything
# above only proves the third-party stack loads; this proves app.py does, which
# is exactly what uvicorn does first at startup. No environment is needed -
# nothing at module scope reads configuration that has no default.
RUN python -c "import app; print('app imports OK')"

# Only used when SERVE_STATIC=1. In production the UI is on GitHub Pages, but
# copying it keeps the image able to run the whole app standalone, which is
# useful for smoke-testing the container before pointing Pages at it.
COPY static/ ./static/

# Job output: overlay PNGs, the GeoTIFF and the CSV, read back by
# /api/jobb/{id}/{name} and expired by their mtime.
#
# /tmp, not /var/lib/..., because Cloud Run's first-generation execution
# environment makes only /tmp writable. Second generation makes the whole
# filesystem writable, but on BOTH generations every write goes into memory and
# counts against the memory limit - there is no disk. /tmp works on both, so
# use it and stay off that decision entirely.
#
# The memory accounting is why JOB_TTL_MINUTES is set low in production: a job
# is tens of megabytes and it now shares the instance's RAM with a 64 MB DEM
# and a 128 MB difference array. storage.cleanup() runs on every new job.
#
# storage.init() creates the root itself, so there is nothing to mkdir here -
# which is just as well, since /tmp on Cloud Run is a fresh tmpfs at runtime
# and anything built into the image at that path would be invisible anyway.
ENV JOB_ROOT=/tmp/mb-jobs
RUN useradd --create-home --uid 10001 app
USER app

EXPOSE 8000
ENV HOST=0.0.0.0 PORT=8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,os,sys; \
    sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz', timeout=4).status==200 else 1)"

# Exec form so uvicorn is PID 1 and receives SIGTERM directly, which matters
# for graceful shutdown when the platform scales the revision down.
CMD ["sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 65"]
