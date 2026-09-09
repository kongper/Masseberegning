-- Masseberegning: membership, invitations and audit.
--
-- Deliberately no foreign key to Supabase's auth.users. Two reasons: the API
-- connects with its own least-privilege role and would otherwise need rights
-- in the auth schema, and a cross-schema FK makes local development and tests
-- require a shim for a table we do not own. app_user.id holds the provider's
-- user id; deletion ordering is the API's job (see DELETE /api/brukere/{id}).

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- app_user

create table if not exists app_user (
  id            uuid primary key,
  email         text not null unique,
  role          text not null default 'user'
                  check (role in ('user', 'superadmin')),
  status        text not null default 'active'
                  check (status in ('active', 'suspended')),
  invited_by    uuid references app_user(id) on delete set null,
  invite_id     uuid,
  created_at    timestamptz not null default now(),
  last_seen_at  timestamptz
);

comment on column app_user.id is 'Identity provider user id (Supabase auth.users.id)';
comment on column app_user.invite_id is 'Invite redeemed to gain access; null for bootstrapped superadmins';

-- Case-insensitive lookup, since invite binding compares addresses.
create unique index if not exists app_user_email_lower_idx on app_user (lower(email));

-- ------------------------------------------------------------------ invite

create table if not exists invite (
  id            uuid primary key default gen_random_uuid(),
  token_hash    bytea not null unique,
  label         text,
  email         text,
  role_granted  text not null default 'user'
                  check (role_granted in ('user', 'superadmin')),
  max_uses      int  not null default 1 check (max_uses between 1 and 100),
  uses          int  not null default 0 check (uses >= 0),
  expires_at    timestamptz not null,
  created_by    uuid not null references app_user(id) on delete cascade,
  revoked_at    timestamptz,
  created_at    timestamptz not null default now(),

  constraint uses_within_max check (uses <= max_uses),
  -- An address-bound invite is for one person, so it is single-use by
  -- construction. Enforced here rather than in the API so it cannot drift.
  constraint email_invite_is_single_use check (email is null or max_uses = 1)
);

comment on column invite.token_hash is 'sha256 of the raw token, hashed in the application. The raw token is never stored and cannot be recovered.';

create index if not exists invite_created_by_idx on invite (created_by);
create index if not exists invite_open_idx on invite (expires_at) where revoked_at is null;

-- ------------------------------------------------------- invite_redemption

create table if not exists invite_redemption (
  id          uuid primary key default gen_random_uuid(),
  invite_id   uuid not null references invite(id) on delete cascade,
  user_id     uuid not null references app_user(id) on delete cascade,
  redeemed_at timestamptz not null default now(),
  ip          inet,
  user_agent  text
);

create index if not exists invite_redemption_invite_idx on invite_redemption (invite_id);

-- ------------------------------------------------------------- admin_audit

create table if not exists admin_audit (
  id         bigserial primary key,
  actor_id   uuid references app_user(id) on delete set null,
  actor_email text,
  action     text not null,
  target     text,
  detail     jsonb,
  created_at timestamptz not null default now()
);

create index if not exists admin_audit_created_idx on admin_audit (created_at desc);

-- ---------------------------------------------------------------------- RLS
--
-- The API is the only client and connects with a role that bypasses RLS.
-- Enabling it with no policies means that if the project's anon key ever ends
-- up in the frontend bundle, it grants access to nothing here.

alter table app_user          enable row level security;
alter table invite            enable row level security;
alter table invite_redemption enable row level security;
alter table admin_audit       enable row level security;
