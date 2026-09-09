"""
Database access: connection pool, migrations, and every query the app makes.

All SQL lives here so the routers stay readable and so the interesting
statements — above all the invite claim in `redeem_invite` — sit next to each
other where they can be reviewed as a group.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

log = logging.getLogger(__name__)


class DatabaseUnavailable(RuntimeError):
    """The database is configured but not reachable right now."""

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_pool: ConnectionPool | None = None

# Why the retry state below exists: an unreachable database used to raise out
# of the app's lifespan, which killed the process. The platform then had no
# healthy machine, answered every request with its own bodiless 503 — no CORS
# headers, so the browser reported it as a CORS failure — and there was no way
# to ask the app what was wrong. A dependency being down should degrade the
# app, not delete it.
_url: str = ""
_sizes: tuple[int, int] = (1, 8)
last_error: str = ""
_last_attempt: float = 0.0
_RETRY_AFTER = 20.0


# --------------------------------------------------------------------- pool


def init_pool(database_url: str, *, min_size: int = 1, max_size: int = 8,
              required: bool = True) -> ConnectionPool | None:
    """Open the pool. With required=False, log and return None on failure.

    The caller can then start anyway and let `ensure_pool` retry in the
    background, so a database that comes back does not need a redeploy.
    """
    global _pool, _url, _sizes, last_error, _last_attempt
    _url, _sizes = database_url, (min_size, max_size)
    if _pool is not None:
        return _pool
    _last_attempt = time.monotonic()
    try:
        return _open_pool(database_url, min_size, max_size)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        last_error = _describe(exc)
        log.error("Database unavailable: %s", last_error)
        if required:
            raise
        return None


def _describe(exc: BaseException) -> str:
    """A DatabaseUnavailable already carries a diagnosed message; don't re-prefix it."""
    return str(exc) if isinstance(exc, DatabaseUnavailable) else f"{type(exc).__name__}: {exc}"


def _diagnose(database_url: str, original: BaseException) -> str:
    """Turn a bare PoolTimeout into something that names the actual problem."""
    import urllib.parse

    parts = urllib.parse.urlsplit(database_url)
    where = f"{parts.hostname}:{parts.port or 5432}"
    try:
        import psycopg

        with psycopg.connect(database_url, connect_timeout=8):
            # Connecting works now, so the pool merely lost a race.
            return f"{type(original).__name__}: {original} (a direct connect to {where} succeeded)"
    except Exception as exc:  # noqa: BLE001 - this IS the diagnostic
        detail = " ".join(str(exc).split())
        return f"{type(exc).__name__} connecting to {where}: {detail}"


def _open_pool(database_url: str, min_size: int, max_size: int) -> ConnectionPool:
    """Open and verify a pool, publishing it only once it actually works.

    The publish has to come after wait(). Assigning the global first left a
    half-open pool behind when wait() timed out, and the consequences were
    worse than the original outage: /healthz cheerfully reported
    "connected": true against a database that was refusing every connection,
    and later queries failed with "PoolClosed" instead of the real error. A
    health endpoint that lies is worse than no health endpoint.
    """
    global _pool, last_error
    candidate = ConnectionPool(
        database_url,
        min_size=min_size,
        max_size=max_size,
        kwargs={
            "row_factory": dict_row,
            # psycopg3 starts using server-side prepared statements after a
            # query has run a few times. Behind a transaction-mode pooler
            # (Supabase's port 6543, PgBouncer) a later execution can land on a
            # different backend session and fail with "prepared statement
            # already exists" or "does not exist" - intermittently, under load,
            # which is the worst way to find out.
            #
            # Turning them off removes that whole class of failure and costs
            # nothing here: every query in this app is tiny, and each request is
            # dominated by a multi-second Kartverket fetch, not by SQL parsing.
            "prepare_threshold": None,
            # Fail an individual attempt fast. Without this a socket to an
            # unroutable host hangs until the pool's own wait() gives up, so
            # every diagnosis costs the full 15 seconds.
            "connect_timeout": 8,
        },
        open=True,
    )
    try:
        candidate.wait(timeout=15)
    except BaseException as exc:
        candidate.close()
        # "PoolTimeout: pool initialization incomplete after 15 sec" says only
        # that connecting did not finish - not why. The pool logs the real
        # reason per attempt, but that is buried in the platform log rather
        # than visible on /readyz. So make one direct attempt with a short
        # timeout purely to capture a usable message: "Network is unreachable"
        # (an IPv6-only host from an IPv4 network), "password authentication
        # failed", "Name or service not known" all point somewhere different.
        raise DatabaseUnavailable(_diagnose(database_url, exc)) from exc
    _pool = candidate
    last_error = ""
    log.info("Database pool ready")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_pool() -> ConnectionPool | None:
    """Return the pool, retrying a failed open at most every _RETRY_AFTER.

    Rate-limited so a request storm against a down database does not turn into
    a connection storm, and so the failure stays cheap.
    """
    global _last_attempt, last_error
    if _pool is not None:
        return _pool
    if not _url:
        return None
    if time.monotonic() - _last_attempt < _RETRY_AFTER:
        return None
    _last_attempt = time.monotonic()
    try:
        log.info("Retrying the database connection")
        return _open_pool(_url, *_sizes)
    except Exception as exc:  # noqa: BLE001
        last_error = _describe(exc)
        log.error("Database still unavailable: %s", last_error)
        return None


def available() -> bool:
    return ensure_pool() is not None


def status() -> dict:
    """For /healthz and /readyz. Never raises, never blocks on a dead socket."""
    if not _url:
        return {"configured": False}
    if _pool is not None:
        return {"configured": True, "connected": True}
    return {"configured": True, "connected": False, "error": last_error or "not connected"}


def pool() -> ConnectionPool:
    p = ensure_pool()
    if p is None:
        raise DatabaseUnavailable(last_error or "database not connected")
    return p


def run_migrations() -> None:
    """Apply every .sql file in migrations/ in name order.

    The files are written to be idempotent (create table if not exists), which
    is enough for a schema this small and avoids a migration-state table.
    """
    files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        raise RuntimeError(f"No migrations found in {MIGRATIONS_DIR}")
    with pool().connection() as conn:
        for path in files:
            conn.execute(path.read_text(encoding="utf-8"))
        conn.commit()
    log.info("Migrations applied: %s", ", ".join(p.name for p in files))


# ------------------------------------------------------------------- tokens


def new_token() -> str:
    """A raw invite token. 32 bytes of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


def as_inet(value: str | None) -> str | None:
    """Coerce a client address to something the `inet` column will accept.

    X-Forwarded-For is client-supplied, so it can hold anything at all. It is
    fine as a rate-limit key, but handing a non-address straight to an inet
    column makes the INSERT fail — which would turn a junk header into a 500
    on the redemption endpoint. An unparseable address is simply not recorded.
    """
    if not value:
        return None
    import ipaddress

    candidate = value.strip()
    # Strip a :port suffix, and the brackets of an [IPv6]:port form.
    if candidate.startswith("["):
        candidate = candidate[1:].split("]", 1)[0]
    elif candidate.count(":") == 1:
        candidate = candidate.split(":", 1)[0]
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        return None


def hash_token(raw: str) -> bytes:
    """Hash an invite token for storage.

    Hashed in Python rather than with pgcrypto's digest() so the raw token
    never appears in a SQL statement, and therefore never in slow-query logs
    or pg_stat_statements.
    """
    return hashlib.sha256(raw.encode("utf-8")).digest()


# -------------------------------------------------------------------- users


@dataclass
class User:
    id: str
    email: str
    role: str
    status: str

    @property
    def is_superadmin(self) -> bool:
        return self.role == "superadmin"

    @property
    def is_active(self) -> bool:
        return self.status == "active"


def get_user(user_id: str) -> User | None:
    with pool().connection() as conn:
        row = conn.execute(
            "select id, email, role, status from app_user where id = %s", (user_id,)
        ).fetchone()
    if not row:
        return None
    return User(id=str(row["id"]), email=row["email"], role=row["role"], status=row["status"])


def touch_user(user_id: str) -> None:
    """Record activity. Coarse on purpose — one write per request is plenty."""
    with pool().connection() as conn:
        conn.execute(
            "update app_user set last_seen_at = now() where id = %s "
            "and (last_seen_at is null or last_seen_at < now() - interval '5 minutes')",
            (user_id,),
        )
        conn.commit()


def ensure_bootstrap_superadmin(user_id: str, email: str) -> User:
    """Create or promote a configured bootstrap superadmin.

    Idempotent, and safe to call on every sign-in. Note the email update: if a
    bootstrap admin's provider email changes case or the row predates the
    config, this converges rather than conflicting.
    """
    with pool().connection() as conn:
        row = conn.execute(
            """
            insert into app_user (id, email, role, status)
                 values (%s, %s, 'superadmin', 'active')
            on conflict (id) do update
                set role   = 'superadmin',
                    status = 'active',
                    email  = excluded.email
            returning id, email, role, status
            """,
            (user_id, email),
        ).fetchone()
        conn.commit()
    return User(id=str(row["id"]), email=row["email"], role=row["role"], status=row["status"])


def list_users() -> list[dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            """
            select u.id, u.email, u.role, u.status, u.created_at, u.last_seen_at,
                   inv.email as invited_by_email
              from app_user u
              left join app_user inv on inv.id = u.invited_by
             order by u.created_at
            """
        ).fetchall()
    return [dict(r) for r in rows]


def count_active_superadmins(exclude_id: str | None = None) -> int:
    sql = "select count(*) as n from app_user where role = 'superadmin' and status = 'active'"
    params: tuple = ()
    if exclude_id:
        sql += " and id <> %s"
        params = (exclude_id,)
    with pool().connection() as conn:
        return int(conn.execute(sql, params).fetchone()["n"])


def update_user(user_id: str, *, role: str | None = None, status: str | None = None) -> dict | None:
    sets, params = [], []
    if role is not None:
        sets.append("role = %s")
        params.append(role)
    if status is not None:
        sets.append("status = %s")
        params.append(status)
    if not sets:
        return None
    params.append(user_id)
    with pool().connection() as conn:
        row = conn.execute(
            f"update app_user set {', '.join(sets)} where id = %s "
            "returning id, email, role, status",
            tuple(params),
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


def delete_user(user_id: str) -> bool:
    with pool().connection() as conn:
        cur = conn.execute("delete from app_user where id = %s", (user_id,))
        conn.commit()
    return cur.rowcount > 0


# ------------------------------------------------------------------ invites


def create_invite(
    *,
    created_by: str,
    label: str | None,
    email: str | None,
    role_granted: str,
    max_uses: int,
    expires_in_days: int,
) -> tuple[str, dict]:
    """Create an invite. Returns (raw_token, row).

    The raw token is returned exactly once, here. Only its hash is stored, so
    there is no way to show it again later.
    """
    raw = new_token()
    expires_at = datetime.now(timezone.utc) + timedelta(days=expires_in_days)
    with pool().connection() as conn:
        row = conn.execute(
            """
            insert into invite (token_hash, label, email, role_granted,
                                max_uses, expires_at, created_by)
                 values (%s, %s, %s, %s, %s, %s, %s)
            returning id, label, email, role_granted, max_uses, uses,
                      expires_at, created_at, revoked_at
            """,
            (hash_token(raw), label, email, role_granted, max_uses, expires_at, created_by),
        ).fetchone()
        conn.commit()
    return raw, dict(row)


def list_invites() -> list[dict[str, Any]]:
    """Invites with a computed status and the addresses that redeemed them."""
    with pool().connection() as conn:
        rows = conn.execute(
            """
            select i.id, i.label, i.email, i.role_granted, i.max_uses, i.uses,
                   i.expires_at, i.created_at, i.revoked_at,
                   c.email as created_by_email,
                   case
                     when i.revoked_at is not null then 'revoked'
                     when i.expires_at <= now()    then 'expired'
                     when i.uses >= i.max_uses     then 'used_up'
                     else 'active'
                   end as status,
                   coalesce(
                     (select array_agg(u.email order by r.redeemed_at)
                        from invite_redemption r
                        join app_user u on u.id = r.user_id
                       where r.invite_id = i.id),
                     '{}'
                   ) as redeemed_by
              from invite i
              join app_user c on c.id = i.created_by
             order by i.created_at desc
             limit 500
            """
        ).fetchall()
    return [dict(r) for r in rows]


def revoke_invite(invite_id: str) -> dict | None:
    with pool().connection() as conn:
        row = conn.execute(
            "update invite set revoked_at = now() "
            "where id = %s and revoked_at is null returning id, label",
            (invite_id,),
        ).fetchone()
        conn.commit()
    return dict(row) if row else None


class RedeemResult:
    """Outcome of a redemption attempt."""

    OK = "ok"
    ALREADY_MEMBER = "already_member"
    INVALID = "invalid"          # unknown, expired, revoked, or used up
    WRONG_EMAIL = "wrong_email"  # bound to a different address


@dataclass
class Redeemed:
    outcome: str
    user: User | None = None
    bound_email: str | None = None


def redeem_invite(*, raw_token: str, user_id: str, email: str,
                  ip: str | None = None, user_agent: str | None = None) -> Redeemed:
    """Claim an invite and create the membership, atomically.

    The claim is a single UPDATE. A check-then-increment would be a race, and
    with a multi-use link shared to a team, several people clicking within the
    same second is the expected case rather than an edge case.
    """
    token_hash = hash_token(raw_token)

    with pool().connection() as conn:
        # Already a member? Idempotent success without burning a use, so that
        # clicking your own link twice does not cost a seat.
        existing = conn.execute(
            "select id, email, role, status from app_user where id = %s", (user_id,)
        ).fetchone()
        if existing and existing["status"] == "active":
            return Redeemed(
                RedeemResult.ALREADY_MEMBER,
                User(id=str(existing["id"]), email=existing["email"],
                     role=existing["role"], status=existing["status"]),
            )

        with conn.transaction():
            claimed = conn.execute(
                """
                update invite
                   set uses = uses + 1
                 where token_hash = %s
                   and revoked_at is null
                   and expires_at > now()
                   and uses < max_uses
                   and (email is null or lower(email) = lower(%s))
                returning id, role_granted, created_by
                """,
                (token_hash, email),
            ).fetchone()

            if claimed is None:
                # Distinguish "bound to someone else" from every other failure.
                # Holding the link already implies the sender told them who it
                # was for, and the vague version of this error is genuinely
                # hard for a recipient to recover from.
                bound = conn.execute(
                    """
                    select email from invite
                     where token_hash = %s and email is not null
                       and revoked_at is null and expires_at > now() and uses < max_uses
                    """,
                    (token_hash,),
                ).fetchone()
                if bound:
                    return Redeemed(RedeemResult.WRONG_EMAIL, bound_email=bound["email"])
                return Redeemed(RedeemResult.INVALID)

            row = conn.execute(
                """
                insert into app_user (id, email, role, status, invited_by, invite_id)
                     values (%s, %s, %s, 'active', %s, %s)
                on conflict (id) do update
                    set status     = 'active',
                        role       = excluded.role,
                        invited_by = excluded.invited_by,
                        invite_id  = excluded.invite_id
                returning id, email, role, status
                """,
                (user_id, email, claimed["role_granted"],
                 claimed["created_by"], claimed["id"]),
            ).fetchone()

            conn.execute(
                "insert into invite_redemption (invite_id, user_id, ip, user_agent) "
                "values (%s, %s, %s, %s)",
                (claimed["id"], user_id, as_inet(ip), user_agent),
            )

    return Redeemed(
        RedeemResult.OK,
        User(id=str(row["id"]), email=row["email"], role=row["role"], status=row["status"]),
    )


# -------------------------------------------------------------------- audit


def audit(actor_id: str | None, actor_email: str | None, action: str,
          target: str | None = None, detail: dict | None = None) -> None:
    import json

    with pool().connection() as conn:
        conn.execute(
            "insert into admin_audit (actor_id, actor_email, action, target, detail) "
            "values (%s, %s, %s, %s, %s)",
            (actor_id, actor_email, action, target,
             json.dumps(detail) if detail is not None else None),
        )
        conn.commit()


def list_audit(limit: int = 200) -> list[dict[str, Any]]:
    with pool().connection() as conn:
        rows = conn.execute(
            "select actor_email, action, target, detail, created_at "
            "from admin_audit order by created_at desc limit %s",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
