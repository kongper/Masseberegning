"""
Saved calculations - "Mine beregninger".

Private per user. The access rule is not enforced here but in db.py, where
every query takes the caller's id and puts it in the WHERE clause; this module
only ever passes `user.id` from `require_member`. A missing row and someone
else's row are therefore indistinguishable from the outside, which is the
behaviour you want: a 404 either way, revealing nothing about what exists.

What is stored is the *input* - the polygon and the parameters - plus the
headline figures for the list. Opening a saved calculation re-runs it, so the
numbers always reflect the current terrain model, and the map overlay and
downloads come back (they are job files and expire on their own).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

import db
from auth import require_member
from invites import requires_database

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/beregninger", tags=["beregninger"])

# A ceiling, so one person cannot fill the table by accident. Generous enough
# that nobody doing real work will meet it.
MAX_PER_USER = 200


class Summary(BaseModel):
    """The figures the list shows. Bounded so a client cannot store anything."""

    level: float
    area_m2: float
    cut_bank_m3: float
    fill_void_m3: float
    net_bank_m3: float
    resolution_m: float | None = None
    coverage: float | None = None
    mode: str | None = None


class SaveRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    polygon: list[list[float]] = Field(..., min_length=3, max_length=2000)
    params: dict = Field(default_factory=dict)
    summary: Summary


class RenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)


# Only the request fields, so a client cannot smuggle arbitrary keys into a
# jsonb column that later gets fed back to the calculator.
ALLOWED_PARAMS = {
    "mode", "fixed_level", "soil_depth", "swell_soil", "swell_rock",
    "shrinkage", "truck_capacity", "resolution",
}


@router.post("", status_code=status.HTTP_201_CREATED)
def lagre(body: SaveRequest, user: db.User = Depends(require_member),
          _db: None = Depends(requires_database)):
    if db.count_calculations(user.id) >= MAX_PER_USER:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Du har lagret {MAX_PER_USER} beregninger, som er grensen. "
            "Slett en før du lagrer en ny.",
        )

    params = {k: v for k, v in body.params.items() if k in ALLOWED_PARAMS}
    row = db.save_calculation(
        user_id=user.id,
        name=body.name,
        polygon=body.polygon,
        params=params,
        summary=body.summary.model_dump(),
    )
    log.info("Beregning lagret av %s: %s", user.email, body.name)
    return row


@router.get("")
def liste(user: db.User = Depends(require_member),
          _db: None = Depends(requires_database)):
    return {"beregninger": db.list_calculations(user.id)}


@router.get("/{calc_id}")
def hent(calc_id: str, user: db.User = Depends(require_member),
         _db: None = Depends(requires_database)):
    row = db.get_calculation(user.id, calc_id)
    if row is None:
        # Also the answer when the row belongs to someone else. Saying
        # "forbidden" would confirm that it exists.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fant ikke beregningen.")
    return row


@router.patch("/{calc_id}")
def gi_nytt_navn(calc_id: str, body: RenameRequest,
                 user: db.User = Depends(require_member),
                 _db: None = Depends(requires_database)):
    row = db.rename_calculation(user.id, calc_id, body.name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fant ikke beregningen.")
    return row


@router.delete("/{calc_id}")
def slett(calc_id: str, user: db.User = Depends(require_member),
          _db: None = Depends(requires_database)):
    if not db.delete_calculation(user.id, calc_id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Fant ikke beregningen.")
    return {"ok": True}
