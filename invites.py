"""
Invitation and user administration endpoints.

Paths are Norwegian to match the existing API (/api/beregn, /api/hoyde).
"""

from __future__ import annotations

import logging
from datetime import datetime
from urllib.parse import urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, EmailStr, Field

import db
from auth import Principal, client_ip, load_membership, require_member, require_superadmin, require_user
from config import settings
from ratelimit import SlidingWindow

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["tilgang"])

# Brute-forcing a 256-bit token is not the concern; log noise and probing are.
_redeem_limit = SlidingWindow(settings.redeem_per_hour, 3600)


def requires_database() -> None:
    """Invitations are a database feature; local single-user mode has none.

    Without this, every invite endpoint would reach `db.pool()` and fail with a
    500 in local mode. A 503 with an explanation is the honest answer.
    """
    if settings.local_single_user:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Invitasjoner krever database. Appen kjører i lokal enbrukermodus "
            "(LOCAL_SINGLE_USER=1), der tilgangsstyring er slått av.",
        )


def invite_url(raw_token: str) -> str:
    """Build the link an admin copies.

    The token goes in the query string rather than the fragment because the
    fragment is where the auth provider puts its own return parameters. The
    frontend strips it from the URL immediately on load; see auth.js.
    """
    parts = urlsplit(settings.frontend_url)
    query = urlencode({"invitasjon": raw_token})
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))


# ------------------------------------------------------------------- models


class InviteCreate(BaseModel):
    label: str | None = Field(None, max_length=120)
    email: EmailStr | None = None
    role_granted: str = Field("user", pattern="^(user|superadmin)$")
    max_uses: int = Field(1, ge=1, le=100)
    expires_in_days: int | None = None


class Redeem(BaseModel):
    token: str = Field(..., min_length=10, max_length=200)


class UserPatch(BaseModel):
    role: str | None = Field(None, pattern="^(user|superadmin)$")
    status: str | None = Field(None, pattern="^(active|suspended)$")


# ---------------------------------------------------------------- who am I


@router.get("/meg")
def meg(principal: Principal = Depends(require_user)):
    """The caller's own access state. Drives which screen the frontend shows."""
    if settings.local_single_user:
        from auth import LOCAL_USER

        return {"id": LOCAL_USER.id, "email": LOCAL_USER.email,
                "role": LOCAL_USER.role, "status": LOCAL_USER.status,
                "local_single_user": True}

    user = load_membership(principal)
    if user is None:
        return {"email": principal.email, "status": "no_access"}
    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "status": user.status,
    }


# ----------------------------------------------------------------- redeem


@router.post("/invitasjon/innloes")
def innloes(body: Redeem, request: Request, principal: Principal = Depends(require_user),
            _db: None = Depends(requires_database)):
    """Redeem an invitation.

    The one endpoint that does not require an existing membership — it is how
    membership is granted. Note that it still requires a verified identity, so
    a leaked link cannot be used without also passing Google, Microsoft, or an
    email round-trip.
    """
    ok, retry = _redeem_limit.check(client_ip(request))
    if not ok:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "For mange forsøk. Prøv igjen senere.",
            headers={"Retry-After": str(int(retry) + 1)},
        )

    result = db.redeem_invite(
        raw_token=body.token,
        user_id=principal.id,
        email=principal.email,
        ip=client_ip(request),
        user_agent=request.headers.get("user-agent", "")[:400],
    )

    if result.outcome == db.RedeemResult.WRONG_EMAIL:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"Invitasjonen gjelder {result.bound_email}. "
            f"Du er logget inn som {principal.email}. "
            "Logg inn med den inviterte adressen.",
        )
    if result.outcome == db.RedeemResult.INVALID:
        # One generic message for unknown, expired, revoked and used-up. There
        # is no reason to let the holder of a bad token learn which it was.
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Invitasjonslenken er ugyldig eller ikke lenger gyldig. "
            "Be om en ny lenke.",
        )

    user = result.user
    if result.outcome == db.RedeemResult.OK:
        db.audit(user.id, user.email, "invite.redeem", target=user.email,
                 detail={"role": user.role})
        log.info("Invite redeemed by %s as %s", user.email, user.role)

    return {
        "id": user.id,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "already_member": result.outcome == db.RedeemResult.ALREADY_MEMBER,
    }


# ------------------------------------------------------------ invite admin


@router.post("/invitasjon", status_code=status.HTTP_201_CREATED)
def opprett_invitasjon(body: InviteCreate, admin: db.User = Depends(require_superadmin),
                       _db: None = Depends(requires_database)):
    """Create an invitation. The link is returned once and cannot be recovered."""
    days = body.expires_in_days or settings.invite_default_days
    if not 1 <= days <= settings.invite_max_days:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Gyldighet må være mellom 1 og {settings.invite_max_days} dager.",
        )

    email = str(body.email).strip().lower() if body.email else None

    # Mirrors the email_invite_is_single_use constraint, so the caller gets a
    # clear message instead of a database error.
    if email and body.max_uses != 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "En invitasjon som er bundet til en e-postadresse kan bare brukes én gang.",
        )

    raw, row = db.create_invite(
        created_by=admin.id,
        label=body.label,
        email=email,
        role_granted=body.role_granted,
        max_uses=body.max_uses,
        expires_in_days=days,
    )
    db.audit(admin.id, admin.email, "invite.create", target=email or body.label,
             detail={"role": body.role_granted, "max_uses": body.max_uses, "days": days})

    return {
        "id": str(row["id"]),
        "url": invite_url(raw),
        "label": row["label"],
        "email": row["email"],
        "role_granted": row["role_granted"],
        "max_uses": row["max_uses"],
        "expires_at": row["expires_at"],
    }


@router.get("/invitasjon")
def list_invitasjoner(_: db.User = Depends(require_superadmin),
                      _db: None = Depends(requires_database)):
    return {"invitasjoner": db.list_invites()}


@router.delete("/invitasjon/{invite_id}")
def trekk_tilbake(invite_id: str, admin: db.User = Depends(require_superadmin),
                  _db: None = Depends(requires_database)):
    row = db.revoke_invite(invite_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fant ikke en aktiv invitasjon.")
    db.audit(admin.id, admin.email, "invite.revoke", target=invite_id)
    return {"ok": True, "id": str(row["id"])}


# -------------------------------------------------------------- user admin


@router.get("/brukere")
def list_brukere(_: db.User = Depends(require_superadmin),
                 _db: None = Depends(requires_database)):
    return {"brukere": db.list_users()}


@router.patch("/brukere/{user_id}")
def endre_bruker(user_id: str, body: UserPatch, admin: db.User = Depends(require_superadmin),
                 _db: None = Depends(requires_database)):
    if body.role is None and body.status is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Ingen endring angitt.")

    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fant ikke brukeren.")

    # You cannot lock yourself out.
    if target.id == admin.id and (body.status == "suspended" or body.role == "user"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Du kan ikke sperre eller degradere din egen konto.",
        )

    # And you cannot remove the last way in.
    losing_admin = target.is_superadmin and (body.role == "user" or body.status == "suspended")
    if losing_admin and db.count_active_superadmins(exclude_id=target.id) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Dette er den siste aktive superadmin. Utpek en ny først.",
        )

    row = db.update_user(user_id, role=body.role, status=body.status)
    db.audit(admin.id, admin.email, "user.update", target=target.email,
             detail={"role": body.role, "status": body.status})
    return row


@router.delete("/brukere/{user_id}")
def slett_bruker(user_id: str, admin: db.User = Depends(require_superadmin),
                 _db: None = Depends(requires_database)):
    """Remove membership. The GDPR path.

    This deletes our record. The identity provider still holds the account —
    deleting that requires the provider's admin API with a service key, which
    is deliberately not wired into this service. See README-DEPLOY.md.
    """
    target = db.get_user(user_id)
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fant ikke brukeren.")
    if target.id == admin.id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Du kan ikke slette din egen konto.")
    if target.is_superadmin and db.count_active_superadmins(exclude_id=target.id) == 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Dette er den siste aktive superadmin. Utpek en ny først.",
        )

    db.delete_user(user_id)
    db.audit(admin.id, admin.email, "user.delete", target=target.email)
    return {"ok": True}


@router.get("/revisjon")
def revisjon(_: db.User = Depends(require_superadmin),
             _db: None = Depends(requires_database)):
    return {"hendelser": db.list_audit()}
