"""
Connection pool behaviour when the server hangs up on idle connections.

This exists because of how Cloud Run runs the process: CPU is allocated only
while a request is being served, so between requests the container is frozen -
the pool's maintenance thread does not run, while Supabase's pooler goes on
closing idle connections at the other end. The pool then holds sockets that
look fine and are not.

A closed socket cannot be detected without touching it, so without a check on
checkout the failure surfaces as an OperationalError on the *first* query after
an idle spell, on whichever endpoint the user happened to open first.

`check=check_connection` fixes that, and introduces a second problem worth
testing: on a failed check the pool sleeps with exponential backoff (1s, 2s,
4s ...) before trying the next connection, so the recovery time scales with how
many stale connections it holds. Measured against a real PostgreSQL with every
backend terminated: 1 stale connection costs 0.01s, two cost 1s, four cost 6.4s
and eight exceed the 30-second pool timeout outright - an outage strictly worse
than the bug the check was added to fix. Hence a small max_size, and hence the
bound asserted below.

`pg_terminate_backend` is the honest local stand-in for the pooler's idle
timeout: same outcome, same lack of warning.

These tests deliberately do NOT use the session pool from conftest, which is
opened wide (max_size=12) so the concurrency tests in test_invites.py can
actually be concurrent. They open a pool with the production defaults instead,
because the production defaults are the thing under test.
"""

from __future__ import annotations

import time

import psycopg
import pytest

from conftest import TEST_DB, needs_db

pytestmark = needs_db

# The worst case the production configuration is allowed to cost. One backoff
# sleep is 1s; two would mean the pool is holding more connections than
# init_pool's default permits.
RECOVERY_BUDGET_S = 2.5


@pytest.fixture
def prod_pool(database):
    """A pool with the production defaults, replacing the session pool after.

    init_pool writes a module global, so the session-scoped pool the other test
    files use has to be put back - otherwise this file's position in the run
    order changes their behaviour.

    It is REPLACED rather than restored, because these tests terminate every
    backend on the database and the session pool's connections are among them.
    Handing it back with a dozen dead sockets in it made conftest's truncate
    fixture hang for the full 30-second pool timeout and error out - which is
    the same failure this file is about, arriving from the other direction.
    """
    db = database
    saved_url, saved_sizes = db._url, db._sizes
    db.close_pool()
    db.init_pool(TEST_DB)          # min_size=1, max_size=2 by default
    try:
        yield db
    finally:
        db.close_pool()
        db.init_pool(saved_url, min_size=saved_sizes[0], max_size=saved_sizes[1])


def _kill_every_backend(exclude_own: bool = True) -> int:
    """Terminate every client backend on the test database.

    Runs on a direct connection so the pool never sees the statement and
    cannot repair itself as a side effect of it.
    """
    with psycopg.connect(TEST_DB, autocommit=True) as killer:
        rows = killer.execute(
            """
            select pg_terminate_backend(pid) as killed
              from pg_stat_activity
             where datname = current_database()
               and pid <> pg_backend_pid()
               and backend_type = 'client backend'
            """
        ).fetchall()
    return len(rows)


def _fill_pool(db) -> int:
    """Grow the pool to its maximum by holding every connection at once."""
    held = []
    try:
        while True:
            try:
                held.append(db.pool().getconn(timeout=0.5))
            except Exception:
                break
    finally:
        for conn in held:
            db.pool().putconn(conn)
    return len(held)


def test_a_connection_the_server_closed_is_never_handed_to_a_caller(prod_pool):
    db = prod_pool
    n = _fill_pool(db)
    assert n >= 1

    killed = _kill_every_backend()
    assert killed >= 1, "nothing was killed, so this test proves nothing"

    # Without check=check_connection the first of these raises
    # OperationalError: consuming input failed / server closed the connection.
    for i in range(5):
        with db.pool().connection() as conn:
            assert conn.execute("select 1 as n").fetchone()["n"] == 1, f"checkout {i}"


def test_recovery_after_every_connection_dies_stays_within_a_second_or_so(prod_pool):
    """The bound that justifies max_size=2.

    This is the test that fails if someone raises the pool size for an imagined
    throughput win: at max_size=4 the same scenario takes 6.4 seconds, and at
    max_size=8 it exceeds the pool timeout and the request fails outright.
    """
    db = prod_pool
    filled = _fill_pool(db)
    _kill_every_backend()

    started = time.perf_counter()
    with db.pool().connection() as conn:
        conn.execute("select 1")
    recovery = time.perf_counter() - started

    assert recovery < RECOVERY_BUDGET_S, (
        f"recovering from {filled} dead connections took {recovery:.1f}s "
        f"(budget {RECOVERY_BUDGET_S}s). The pool is probably larger than "
        f"init_pool's default; read the measured table there before raising it."
    )

    # And the next request pays nothing.
    started = time.perf_counter()
    with db.pool().connection() as conn:
        conn.execute("select 1")
    assert time.perf_counter() - started < 0.5


def test_a_real_query_survives_the_database_going_away(prod_pool):
    """The same thing from the application's point of view: no visible blip."""
    db = prod_pool

    user = db.ensure_bootstrap_superadmin(
        "00000000-0000-0000-0000-0000000000aa", "pool@prosit.no")
    _kill_every_backend()

    # A real query, not select 1 - this is the path a request takes.
    again = db.get_user(user.id)
    assert again is not None and again.email == "pool@prosit.no"


def test_the_pool_is_configured_to_check_and_to_stay_small(prod_pool):
    """Guards the configuration itself.

    The tests above fail loudly if `check` is dropped, but `max_idle` has no
    observable effect inside a test run - it only matters across the minutes an
    idle instance sits frozen, and on Cloud Run the thread that enforces it is
    frozen too. So assert it is set, and say here that it narrows the window
    rather than closing it.
    """
    from psycopg_pool import ConnectionPool

    p = prod_pool.pool()
    assert p._check is ConnectionPool.check_connection
    assert p.max_idle <= 60.0
    assert p.max_size <= 2, (
        "max_size above 2 makes recovery from a frozen instance cost seconds; "
        "see the measured table in db.init_pool"
    )
