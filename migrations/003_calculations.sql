-- Saved calculations - "Mine beregninger".
--
-- Stores the *inputs* and the headline figures, not the full result. Two
-- reasons: a re-run always reflects the current terrain model rather than a
-- frozen copy of it, and the overlay PNGs and GeoTIFF are job files that
-- expire after JOB_TTL_MINUTES anyway, so a stored result could never bring
-- the map back on its own.
--
-- Private per user. `user_id` is not merely a column to filter on - every
-- query in db.py that touches this table takes the caller's id, so there is no
-- code path that can read a row without naming its owner.

create table if not exists calculation (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references app_user(id) on delete cascade,
  name        text not null check (length(btrim(name)) between 1 and 120),

  -- [[lon, lat], ...] in WGS84, exactly as the browser sent it, so reopening
  -- redraws the same ring rather than a reprojected approximation of it.
  polygon     jsonb not null,

  -- mode, fixed_level, soil_depth, swell_*, shrinkage, truck_capacity,
  -- resolution. Kept whole rather than as columns: these are the request body,
  -- and adding a parameter should not need a migration.
  params      jsonb not null,

  -- level, area_m2, cut/fill/net, resolution_m, coverage. Enough to render a
  -- useful list without recomputing anything.
  summary     jsonb not null,

  created_at  timestamptz not null default now(),
  updated_at  timestamptz not null default now()
);

-- The list query is always "mine, newest first".
create index if not exists calculation_user_idx
  on calculation (user_id, created_at desc);

-- Deleting a membership takes their saved work with it, which is what the
-- GDPR deletion path in DELETE /api/brukere/{id} needs.
alter table calculation enable row level security;
