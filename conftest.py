"""
Test configuration.

Environment variables are set here, before anything imports `config`, because
`config.settings` is built at import time. Tests that need a database are
skipped rather than failed when TEST_DATABASE_URL is not set, so the pure
tests (test_engine, test_render) still run on a bare checkout.
"""

from __future__ import annotations

import os
import tempfile
import uuid

import pytest

TEST_DB = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql://postgres@127.0.0.1:5433/masseberegning",
)

JWT_SECRET = "test-secret-not-used-anywhere-real"
ISSUER = "https://test.supabase.co/auth/v1"

os.environ.setdefault("DATABASE_URL", TEST_DB)
os.environ["SUPABASE_JWT_SECRET"] = JWT_SECRET
os.environ["SUPABASE_JWKS_URL"] = ""
os.environ["SUPABASE_JWT_ISSUER"] = ISSUER
os.environ["SUPABASE_JWT_AUDIENCE"] = "authenticated"
os.environ["SUPERADMIN_EMAILS"] = "boss@prosit.no"
os.environ["ALLOWED_ORIGINS"] = "https://example.test"
os.environ["FRONTEND_URL"] = "https://masseberegning.example.test/"
os.environ["SERVE_STATIC"] = "0"
os.environ["JOB_ROOT"] = os.path.join(tempfile.gettempdir(), "mb-test-jobs")
os.environ["CALC_PER_MINUTE"] = "3"
os.environ["REDEEM_PER_HOUR"] = "1000"


def _database_available() -> tuple[bool, str]:
    """(usable, why not).

    The two reasons this returns False are worth telling apart, because they
    have completely different fixes and the wrong message sends you looking in
    the wrong place. The original version caught both in one `except
    Exception` and reported "No test database at ...", so a virtualenv missing
    psycopg produced a skip message about the database - and a whole suite
    that silently tested nothing while appearing to be fine.
    """
    try:
        import psycopg
    except ImportError:
        return False, (
            "psycopg is not installed in the interpreter running pytest. "
            "Install the dependencies into it: "
            "`python -m pip install -r requirements.txt -r requirements-dev.txt`, "
            "and check you are using the project's virtualenv - a bare `pytest` "
            "may be a different Python entirely."
        )

    try:
        with psycopg.connect(TEST_DB, connect_timeout=3):
            return True, ""
    except Exception as exc:
        return False, (
            f"No test database at {TEST_DB} ({exc.__class__.__name__}: {exc}). "
            "Start one with: docker run --rm -d --name mb-test -p 5433:5432 "
            "-e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=masseberegning postgres:16"
        )


DB_UP, DB_SKIP_REASON = _database_available()
needs_db = pytest.mark.skipif(not DB_UP, reason=DB_SKIP_REASON)


@pytest.fixture(scope="session")
def database():
    if not DB_UP:
        pytest.skip(DB_SKIP_REASON)
    import db as dbmod

    # Room for the concurrency test to actually be concurrent.
    dbmod.init_pool(TEST_DB, min_size=2, max_size=12)
    dbmod.run_migrations()
    yield dbmod
    dbmod.close_pool()


@pytest.fixture(autouse=True)
def clean_tables(request):
    """Truncate between tests so ordering never matters."""
    if "database" not in request.fixturenames:
        yield
        return
    dbmod = request.getfixturevalue("database")
    with dbmod.pool().connection() as conn:
        conn.execute(
            "truncate admin_audit, invite_redemption, invite, app_user restart identity cascade"
        )
        conn.commit()
    yield


@pytest.fixture
def token():
    """Mint an access token the API will accept, as the given identity."""
    import time

    import jwt

    def make(user_id: str, email: str, *, expired: bool = False,
             email_verified: bool | None = None, audience: str = "authenticated"):
        now = int(time.time())
        claims = {
            "sub": user_id,
            "email": email,
            "aud": audience,
            "iss": ISSUER,
            "iat": now - 10,
            "exp": now - 5 if expired else now + 3600,
        }
        if email_verified is not None:
            claims["user_metadata"] = {"email_verified": email_verified}
        return jwt.encode(claims, JWT_SECRET, algorithm="HS256")

    return make


@pytest.fixture
def new_id():
    return lambda: str(uuid.uuid4())


@pytest.fixture(scope="session")
def client(database):
    """A TestClient with lifespan run, so the pool and storage are initialised.

    Session-scoped deliberately. The app's lifespan closes the connection pool
    on shutdown — correct in production, where the lifespan owns it — so a
    per-test client would tear the pool out from under the next test.
    """
    from fastapi.testclient import TestClient

    import app as appmod

    with TestClient(appmod.app) as c:
        yield c


@pytest.fixture
def bearer(token):
    return lambda user_id, email, **kw: {
        "Authorization": "Bearer " + token(user_id, email, **kw)
    }
