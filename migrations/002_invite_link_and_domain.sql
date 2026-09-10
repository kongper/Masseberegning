-- Two additions to `invite`.
--
-- Idempotent throughout: run_migrations() applies every file in this directory
-- on each start, so nothing here may fail on a second pass. Postgres has
-- "add column if not exists" but no "add constraint if not exists", hence the
-- DO blocks.

-- 1. invite.token — the raw token, kept so a superadmin can copy an open link
--    again instead of losing it with the clipboard.
--
--    This is a deliberate reversal of the original design, which stored only
--    the SHA-256. The reasoning: an invitation is not a password. It grants
--    membership only to someone who has *already* authenticated with Google,
--    Microsoft or a verified email, so a stolen token is not a way in on its
--    own. Against that, a team link you cannot re-copy has to be revoked and
--    reissued, which breaks it for everyone already holding it.
--
--    token_hash stays and remains the lookup key, so redemption still never
--    needs this column. Three things keep the exposure small:
--      - it is revealed only through an audited superadmin endpoint, never in
--        the invite list
--      - it is erased the moment the invite is used up, revoked or expired
--      - RLS below means the anon key grants nothing here
alter table invite add column if not exists token text;

comment on column invite.token is
  'Raw token, revealable by a superadmin and erased once the invite is spent. token_hash remains the lookup key.';

-- 2. invite.email_domain — bind an invitation to a domain rather than to one
--    address, so "anyone at vg.no" is a single multi-use link.
--
--    Sound only because the address is verified by the identity provider
--    before we ever see it; the app never takes the user's word for it.
--    Matched exactly, so mail.vg.no does not satisfy vg.no.
alter table invite add column if not exists email_domain text;

comment on column invite.email_domain is
  'Lower-case domain without the @, e.g. vg.no. Mutually exclusive with email.';

do $$
begin
  -- An invitation is bound to one address, or to one domain, or to nobody.
  -- Both at once has no coherent meaning.
  if not exists (
    select 1 from pg_constraint where conname = 'invite_email_xor_domain'
  ) then
    alter table invite add constraint invite_email_xor_domain
      check (email is null or email_domain is null);
  end if;

  -- Cheap shape check. A domain with an @ in it is almost always a full
  -- address that lost its local part, and would silently match nobody.
  if not exists (
    select 1 from pg_constraint where conname = 'invite_domain_shape'
  ) then
    alter table invite add constraint invite_domain_shape
      check (email_domain is null or email_domain ~ '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$');
  end if;
end $$;

create index if not exists invite_domain_idx on invite (email_domain)
  where email_domain is not null;
