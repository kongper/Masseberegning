"""
Runtime configuration, all from environment variables.

Nothing here has a production-safe default. The app is designed to fail loudly
at startup if something required is missing, rather than to start up in a
half-configured state and hand out 500s later.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [p.strip() for p in raw.replace(";", ",").split(",") if p.strip()]


@dataclass
class Settings:
    # --- server
    host: str = os.environ.get("HOST", "0.0.0.0")
    port: int = _int("PORT", 8000)

    # Serving the frontend from the API is for local development only. In
    # production the static files live on GitHub Pages and this stays off, so
    # there is exactly one copy of the UI in play.
    serve_static: bool = _bool("SERVE_STATIC", False)

    # Single-user local mode: no database, no identity provider, no access
    # control at all. This exists so start.bat still works as a double-click
    # on one engineer's own machine, which was the app's original shape.
    #
    # It is an auth bypass, so it is fenced in: `guard_local_mode` below
    # refuses to let it run on anything but a loopback address, and refuses if
    # ALLOWED_ORIGINS or DATABASE_URL are set, since either means this is a
    # real deployment that has been misconfigured. Never set it in a container.
    local_single_user: bool = _bool("LOCAL_SINGLE_USER", False)

    # --- CORS. An explicit allow-list; never "*", because we send credentials.
    allowed_origins: list[str] = field(default_factory=lambda: _list("ALLOWED_ORIGINS"))

    # --- database
    database_url: str = os.environ.get("DATABASE_URL", "")

    # --- auth
    # Preferred: asymmetric verification against the provider's JWKS.
    jwks_url: str = os.environ.get("SUPABASE_JWKS_URL", "")
    # Fallback for projects still on a shared HS256 secret.
    jwt_secret: str = os.environ.get("SUPABASE_JWT_SECRET", "")
    jwt_issuer: str = os.environ.get("SUPABASE_JWT_ISSUER", "")
    jwt_audience: str = os.environ.get("SUPABASE_JWT_AUDIENCE", "authenticated")

    # Emails that are granted superadmin on sign-in, no invite needed. This is
    # the bootstrap for an empty database and the permanent lockout recovery
    # path — leave it configured.
    superadmin_emails: list[str] = field(
        default_factory=lambda: [e.lower() for e in _list("SUPERADMIN_EMAILS")]
    )

    # --- invites
    invite_default_days: int = _int("INVITE_DEFAULT_DAYS", 14)
    invite_max_days: int = _int("INVITE_MAX_DAYS", 90)
    # Where the invite link should point. The API knows this only so it can
    # build the full URL for the admin to copy.
    frontend_url: str = os.environ.get("FRONTEND_URL", "http://127.0.0.1:8000/")

    # --- abuse limits
    # A generous ceiling for a site plan; a box around a municipality is not a
    # site plan, and the DEM fetch behind it is expensive.
    max_area_m2: float = _float("MAX_AREA_M2", 10_000_000.0)
    calc_per_minute: int = _int("CALC_PER_MINUTE", 6)
    calc_per_day: int = _int("CALC_PER_DAY", 200)
    redeem_per_hour: int = _int("REDEEM_PER_HOUR", 20)

    # --- caching
    # Memory the DEM cache may hold. It competes with the working set of a
    # calculation (a 16 Mpx DEM is 64 MB at float32, and the difference array
    # is 128 MB at float64), so keep this well under the machine's RAM.
    dem_cache_mb: int = _int("DEM_CACHE_MB", 256)

    # --- jobs
    job_ttl_minutes: int = _int("JOB_TTL_MINUTES", 120)
    job_root: str = os.environ.get("JOB_ROOT", "")

    def __post_init__(self) -> None:
        # In local single-user mode the safe default is loopback, so that
        # `python app.py` with no HOST set does the right thing rather than
        # tripping the guard below. An explicit HOST still wins.
        if self.local_single_user and "HOST" not in os.environ:
            self.host = "127.0.0.1"

    def guard_local_mode(self) -> None:
        """Refuse to run the auth bypass anywhere it could matter.

        Raises rather than warning: a silently-ignored guard here would mean an
        open app on the internet.
        """
        if not self.local_single_user:
            return
        loopback = {"127.0.0.1", "::1", "localhost"}
        if self.host not in loopback:
            raise RuntimeError(
                f"LOCAL_SINGLE_USER=1 refuses to bind {self.host}. It disables all "
                "access control, so it may only listen on a loopback address. "
                "Set HOST=127.0.0.1, or unset LOCAL_SINGLE_USER."
            )
        if self.allowed_origins:
            raise RuntimeError(
                "LOCAL_SINGLE_USER=1 together with ALLOWED_ORIGINS looks like a "
                "deployment with access control switched off. Unset one of them."
            )
        if self.database_url:
            raise RuntimeError(
                "LOCAL_SINGLE_USER=1 together with DATABASE_URL is ambiguous: local "
                "mode ignores the database entirely. Unset one of them."
            )

    def require_for_production(self) -> list[str]:
        """Return a list of misconfigurations that should stop startup."""
        if self.local_single_user:
            # Local mode has no database, no provider and no members by design;
            # none of the checks below apply to it.
            return []

        problems: list[str] = []
        if not self.database_url:
            problems.append("DATABASE_URL is not set")
        if not self.jwks_url and not self.jwt_secret:
            problems.append(
                "neither SUPABASE_JWKS_URL nor SUPABASE_JWT_SECRET is set — "
                "the API cannot verify access tokens"
            )
        if not self.allowed_origins and not self.serve_static:
            problems.append(
                "ALLOWED_ORIGINS is empty and SERVE_STATIC is off — no browser "
                "would be able to call this API"
            )
        if not self.superadmin_emails:
            problems.append(
                "SUPERADMIN_EMAILS is empty — nobody would be able to issue invites"
            )
        return problems


settings = Settings()
