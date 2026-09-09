"""
Job output storage.

The original version kept an in-process OrderedDict of job directories under
the system temp dir. That breaks in two ways once this runs in a container:
a second replica serves a request for a job it has never heard of, and a cold
start wipes every previous job — so every overlay PNG and every download 404s.

The fix here is to stop keeping an index at all. A job's existence is a
directory on disk, and expiry is its mtime. That survives restarts as long as
the volume does, and works unchanged across replicas if the volume is shared.

`/api/jobb/{job_id}/{name}` stays unauthenticated: the job id is 12 hex
characters of `uuid4`, so the URL is the credential. This is deliberate, and
the reason is mechanical rather than lazy — Leaflet's `L.imageOverlay` renders
an `<img src>` and the export links are plain `<a download>` navigations, and
neither can carry an Authorization header. The exposure is that anyone holding
a link can fetch that job's terrain rendering until it expires, which for open
Kartverket elevation data is acceptable. See claude/invitation-link-design.md.

To move to object storage, replace the three functions below; nothing else in
the app touches the filesystem.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import time
import uuid
from pathlib import Path

log = logging.getLogger(__name__)

JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")

_root: Path | None = None
_ttl_seconds: int = 7200


def init(root: str | None, ttl_minutes: int) -> Path:
    global _root, _ttl_seconds
    _ttl_seconds = max(60, ttl_minutes * 60)
    _root = Path(root) if root else Path(tempfile.gettempdir()) / "masseberegning-jobs"
    _root.mkdir(parents=True, exist_ok=True)
    log.info("Job storage at %s (TTL %d min)", _root, _ttl_seconds // 60)
    return _root


def root() -> Path:
    if _root is None:
        raise RuntimeError("Job storage not initialised")
    return _root


def new_job() -> tuple[str, Path]:
    cleanup()
    job_id = uuid.uuid4().hex[:12]
    path = root() / job_id
    path.mkdir(parents=True, exist_ok=True)
    return job_id, path


def job_file(job_id: str, name: str) -> Path | None:
    """Resolve a job file, or None if it is missing, expired or a bad name.

    Both components are validated against a strict allow-list, and the result
    is checked to be inside the job root, so no combination of input can walk
    out of it.
    """
    if not JOB_ID_RE.match(job_id) or not NAME_RE.match(name):
        return None

    path = (root() / job_id / name).resolve()
    try:
        path.relative_to(root().resolve())
    except ValueError:
        return None

    if not path.is_file():
        return None
    if time.time() - path.stat().st_mtime > _ttl_seconds:
        return None
    return path


def cleanup() -> int:
    """Delete expired job directories. Cheap enough to call per job."""
    if _root is None or not _root.exists():
        return 0
    cutoff = time.time() - _ttl_seconds
    removed = 0
    for child in _root.iterdir():
        if not child.is_dir():
            continue
        try:
            if child.stat().st_mtime < cutoff:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    if removed:
        log.info("Removed %d expired job(s)", removed)
    return removed
