"""
Authentication and authorization.

The split that makes this simple: the identity provider answers "who is this
person, and is that really their email address?", and this module's database
answers "may they use the app?". Anyone in the world may hold a valid token.
Only invited people hold a membership.

Three guards, each building on the last:

    require_user        valid token, nothing more
    require_member      valid token + an active app_user row
    require_superadmin  the above + role = 'superadmin'
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt import PyJWKClient

import db
from config import settings

log = logging.getLogger(__name__)

# auto_error=False so a missing header produces our own Norwegian 401 rather
# than FastAPI's default English one.
bearer = HTTPBearer(auto_error=False)

_jwk_client: PyJWKClient | None = None


def _jwks() -> PyJWKClient:
    global _jwk_client
    if _jwk_client is None:
        # PyJWKClient caches keys and refetches on an unknown kid, which is
        # exactly the behaviour needed across a provider key rotation.
        _jwk_client = PyJWKClient(settings.jwks_url, cache_keys=True, lifespan=3600)
    return _jwk_client


@dataclass
class Principal:
    """An authenticated identity. Says nothing about authorization."""

    id: str
    email: str
    claims: dict


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status.HTTP_401_UNAUTHORIZED, detail, headers={"WWW-Authenticate": "Bearer"}
    )


def decode_token(token: str) -> dict:
    """Verify an access token's signature and claims, or raise 401."""
    options = {"require": ["exp", "sub"]}
    kwargs: dict = {
        "algorithms": [],
        "audience": settings.jwt_audience or None,
        "options": options,
    }
    if settings.jwt_issuer:
        kwargs["issuer"] = settings.jwt_issuer

    try:
        if settings.jwks_url:
            key = _jwks().get_signing_key_from_jwt(token).key
            kwargs["algorithms"] = ["RS256", "ES256", "RS512"]
        elif settings.jwt_secret:
            key = settings.jwt_secret
            kwargs["algorithms"] = ["HS256"]
        else:
            raise RuntimeError("No JWT verification material configured")
        return jwt.decode(token, key, **kwargs)
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Innloggingen er utløpt. Logg inn på nytt.")
    except jwt.PyJWTError as exc:
        log.warning("Token rejected: %s", exc)
        raise _unauthorized("Ugyldig innlogging.")


def _email_from_claims(claims: dict) -> str:
    email = claims.get("email")
    if not email:
        # Supabase puts email at the top level; be defensive about providers
        # that only fill user_metadata.
        meta = claims.get("user_metadata") or {}
        email = meta.get("email")
    if not email:
        raise _unauthorized("Kontoen mangler e-postadresse.")
    return str(email).strip()


def _email_is_verified(claims: dict) -> bool:
    """True unless the token explicitly says the address is unverified.

    Absent claim means "the provider did not tell us", which we accept — OAuth
    providers have already verified, and a magic link verifies by construction.
    A present-and-false claim is the case worth refusing, because invite
    binding compares against this address.
    """
    for src in (claims, claims.get("user_metadata") or {}, claims.get("app_metadata") or {}):
        if "email_verified" in src:
            return bool(src["email_verified"])
    return True


LOCAL_USER = db.User(
    id="00000000-0000-0000-0000-000000000000",
    email="lokal@localhost",
    role="superadmin",
    status="active",
)


def require_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal:
    if settings.local_single_user:
        return Principal(id=LOCAL_USER.id, email=LOCAL_USER.email, claims={})
    if creds is None or not creds.credentials:
        raise _unauthorized("Innlogging kreves.")
    claims = decode_token(creds.credentials)
    email = _email_from_claims(claims)
    if not _email_is_verified(claims):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "E-postadressen er ikke bekreftet. Bekreft adressen og prøv igjen.",
        )
    return Principal(id=str(claims["sub"]), email=email, claims=claims)


def load_membership(principal: Principal) -> db.User | None:
    """Look up membership, applying the bootstrap-superadmin rule.

    SUPERADMIN_EMAILS is checked on every sign-in so that it works against an
    empty database, survives a restore, and stays available as the way back in
    if the last superadmin loses their account.
    """
    user = db.get_user(principal.id)
    if principal.email.lower() in settings.superadmin_emails:
        if user is None or not user.is_superadmin or not user.is_active:
            log.info("Bootstrapping superadmin %s", principal.email)
            user = db.ensure_bootstrap_superadmin(principal.id, principal.email)
    return user


def require_member(principal: Principal = Depends(require_user)) -> db.User:
    if settings.local_single_user:
        return LOCAL_USER
    user = load_membership(principal)
    if user is None:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Kontoen din har ikke tilgang. Tilgang krever en invitasjon.",
        )
    if not user.is_active:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Kontoen din er sperret.")
    db.touch_user(user.id)
    return user


def require_superadmin(user: db.User = Depends(require_member)) -> db.User:
    if not user.is_superadmin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Krever superadmin.")
    return user


def client_ip(request: Request) -> str:
    """Best-effort client address.

    Both Azure Container Apps and Cloud Run put the real address first in
    X-Forwarded-For. Trusting a client-supplied header is only acceptable
    because this is used for rate-limit keys and audit rows, never for access
    decisions.
    """
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
