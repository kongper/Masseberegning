# Masseberegning – setup runbook

Follow this top to bottom. Every step ends with something you can check, so a
mistake surfaces at the step that caused it rather than three steps later.

**What you end up with:**

| | |
|---|---|
| UI | `https://kongper.github.io/masseberegning/` |
| API | `https://masseberegning-api.fly.dev` |
| Repo | `https://github.com/kongper/masseberegning` (private) |
| Database + sign-in | Supabase (EU region) |

Registration is invite-only. Anyone may sign in with Google, Microsoft or an
email link, but only people holding a redeemed invitation can use the app.

---

## 0. The idea, so the rest makes sense

Authentication and authorization live in different places. Supabase answers
*"who is this person, and is that really their email?"* — for anyone on the
internet. Our own Postgres answers *"may they use the app?"* A valid token with
no membership row gets `403` from everything that matters.

An invitation does not create an account. It grants membership to an account
that has **already** proved an email address. So a leaked invite link is
useless on its own.

---

## 1. Git and GitHub

In the project folder:

```
python setup.py
```

It installs the two CI workflows into `.github/workflows/`, makes the folder a
git repository, and commits. It does not talk to GitHub and does not push.

> The `api.yml` and `pages.yml` sitting in your `Claude outputs` folder are an
> earlier Azure-targeted version. Ignore them — `setup.py` installs the Fly.io
> ones. `Claude outputs/` is gitignored.

Then create the repo and push. With the GitHub CLI:

```
gh repo create masseberegning --private --source=. --remote=origin --push
```

Without it, create an **empty** private repo named `masseberegning` at
<https://github.com/new> — no README, no .gitignore, no licence — then:

```
git remote add origin https://github.com/kongper/masseberegning.git
git push -u origin main
```

**Check:** the code is on GitHub, and the Actions tab shows two workflows that
have **failed**. That is expected — they need variables and secrets that do not
exist yet. You will fix that in steps 3 and 4.

---

## 2. Supabase

This is the fiddliest part and the dashboard has been reorganised more than
once, so it has its own guide: **[README-SUPABASE.md](README-SUPABASE.md)**.

Work through it to the end of section 7 and come back with five values:

| | Value | Goes to |
|---|---|---|
| 1 | Project URL | GitHub variable `SUPABASE_URL` |
| 2 | Publishable key (`sb_publishable_…`) | GitHub variable `SUPABASE_PUBLISHABLE_KEY` |
| 3 | Session-pooler connection string | Fly secret `DATABASE_URL` |
| 4 | JWKS URL | Fly secret `SUPABASE_JWKS_URL` |
| 5 | Issuer | Fly secret `SUPABASE_JWT_ISSUER` |

Three things there are easy to get wrong and unpleasant to debug:

- **The publishable key, not the `anon` key.** `anon` and `service_role` are
  legacy and retired by the end of 2026. Any guide showing a long key starting
  `eyJ` is describing the old system.
- **The session pooler, not the direct connection.** Direct is IPv6-only on new
  projects; the session pooler is IPv4 and behaves the same for a long-lived
  server. The username differs between them, so you cannot just swap the host.
- **Redirect URLs must be allow-listed before sign-in works at all**, and the
  failure looks like a blank screen rather than an error.

**Check:** the single command below validates the project URL, the publishable
key and the provider wiring at once, before any of our code is involved.

```
curl -s -H "apikey: sb_publishable_xxxx" https://<ref>.supabase.co/auth/v1/settings
```

Look for `"google": true` under `external`.

---

## 3. Fly.io

```
# install flyctl, then
fly auth signup        # or: fly auth login
```

The app name in `fly.toml` is `masseberegning-api`. Fly app names are globally
unique, so if it is taken, change the `app =` line and remember the new
hostname.

Create the app **without deploying**, so it does not start before it has any
configuration:

```
fly launch --no-deploy --copy-config --name masseberegning-api --region arn
```

Then set the three secrets — the values that either carry a password or reveal
the Supabase project ref. Everything else is already in `fly.toml` under
`[env]`, where it is version-controlled and visible.

```
fly secrets set \
  DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-1-eu-north-1.pooler.supabase.com:5432/postgres" \
  SUPABASE_JWKS_URL="https://<ref>.supabase.co/auth/v1/.well-known/jwks.json" \
  SUPABASE_JWT_ISSUER="https://<ref>.supabase.co/auth/v1"
```

Deploy, then pin it to one machine:

```
fly deploy
fly scale count 1
fly status
```

**`fly scale count 1` is not optional.** `fly launch` may create two machines
for high availability. Job output — the overlay PNGs, the GeoTIFF, the CSV — is
written to the machine's own filesystem and read back through
`/api/jobb/{id}/{name}`, so a second machine serves 404s for the first one's
downloads. The deploy workflow asserts the count and fails if it is not 1.
Raising it needs blob storage first; `storage.py` is the seam.

**Check:**

```
curl https://masseberegning-api.fly.dev/healthz
```

Expect `{"status":"ok", ..., "db":"ok"}`. The app creates its own tables on
startup, so `"db":"ok"` means the migrations ran.

If it says `"status":"degraded"` with a database error, the connection string
is wrong — `fly logs` will show it. If the app refuses to start at all, read the
message: it deliberately refuses an incomplete configuration rather than coming
up half-working, and it names exactly what is missing.

Finally, the deploy token for CI:

```
fly tokens create deploy -x 999999h
```

Copy the **whole** value, including the `FlyV1` prefix, into the repo:
**Settings → Secrets and variables → Actions → Secrets → New repository
secret**, named `FLY_API_TOKEN`.

---

## 4. GitHub Pages

**Settings → Secrets and variables → Actions → Variables.** Three, as
*variables*, not secrets — all three are served to every visitor in
`config.js`, so treating them as secrets would be theatre:

| Variable | Value |
|---|---|
| `API_BASE` | `https://masseberegning-api.fly.dev` |
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | the `sb_publishable_…` key (Project Settings → API Keys) |

The workflow fails loudly if any is unset, rather than publishing a site whose
sign-in silently does nothing.

Then **Settings → Pages → Source: GitHub Actions**.

Re-run both workflows: **Actions →** pick each →
**Re-run all jobs**.

**Check:** open <https://kongper.github.io/masseberegning/>. You should get the
sign-in card with a "Fortsett med Google" button. If you instead see **"Ikke
konfigurert"**, the three variables did not reach `config.js` — check the Pages
workflow log.

---

## 5. First sign-in

Sign in with Google as `per@prosit.no`.

You land straight in the app, with **superadmin** next to your address in the
bottom-left corner and an **Administrasjon** link. No invitation needed:
`SUPERADMIN_EMAILS` in `fly.toml` is checked on every sign-in, which is what
makes this work against an empty database.

**Leave that setting in place permanently.** It is the only way back in if the
last superadmin ever loses their account. The app guards against removing the
last one, but it cannot conjure a new one.

**Check:** sign in from a private window with a different Google account. You
should get **"Ingen tilgang"** naming that address — that is the authorization
wall doing its job.

---

## 6. Invite someone

**Administrasjon → Ny invitasjon.**

| Want | Set |
|---|---|
| One named person | *Bundet til e-post* = their address, *Antall bruk* = 1 |
| A client team, ~8 people, 30 days | leave the email empty, *Antall bruk* = 8 |
| Another superadmin | *Rolle* = Superadmin, and bind the address |

Press **Opprett lenke**, then **Kopier**. The link is shown **once** — only its
SHA-256 is stored, so it cannot be shown again. If you lose it, revoke and
reissue.

Send it yourself, in Teams or email. There is no SMTP in the app: no sender
domain to warm up, no bounce handling, and you get to add context in your own
words.

An email-bound invitation is single-use by construction — enforced by a
database constraint, not just a check in the API.

**Check:** open the link in a private window, sign in with a different account,
and you should land in the app. The invitation's row should then read `1 / 1`
and **Oppbrukt**, with that address under *Innløst av*, and the event should
appear in **Hendelseslogg**.

---

## 7. Adding the other sign-in methods

Once the above works end to end. Both are covered in
[README-SUPABASE.md](README-SUPABASE.md) §9 and §10:

- **Microsoft** needs an Entra app registration. Note the client-secret expiry
  in a calendar — when it lapses, Microsoft sign-in stops with no warning. And
  add the `xms_edov` optional claim: it tells Supabase whether the email Azure
  returned is verified, which matters because email-bound invitations are
  checked against exactly that address.
- **The email link** needs custom SMTP first. Supabase's built-in service
  allows 2 messages per hour and is not for production.

Then set the repository variables `PROVIDERS` (e.g. `'google','azure'`) and
`ALLOW_EMAIL_LINK`, and re-run the Pages workflow. Only list a provider you
have actually enabled, or its button will fail on click.

## 8. Things that will bite you

**One machine only.** See §3. This is the constraint to remember if you ever
scale up.

**Job URLs are unauthenticated on purpose.** `L.imageOverlay` renders an
`<img src>` and the export links are plain `<a download>` navigations, and
*neither can carry an `Authorization` header*. So the 12-hex job id is the
credential, and the URL expires with `JOB_TTL_MINUTES`. Anyone holding a link
can fetch that job's terrain rendering until it expires. For open Kartverket
elevation data that is an acceptable trade — but it is a decision, not an
oversight, and it is the thing to revisit if the outputs ever become
confidential.

**Cold starts.** `min_machines_running = 0` means the machine stops when idle,
and the first request afterwards has to re-import rasterio and pyproj — a few
seconds. Set it to 1 in `fly.toml` to trade a little cost for always-warm.

**Set a spend limit** on both Fly and Supabase. Both scale to near-nothing at
idle, but neither caps spending by default.

**Rate limits are per machine.** `ratelimit.py` is in-process. At one machine
the numbers are exact; scaled out, the effective limit is `limit × machines`.

**The invite token leaks through `Referer` if you touch the URL handling.** The
token arrives as `?invitasjon=…`, so without care every Kartverket tile request
carries it. Two mechanisms guard this and both are needed: the
`<meta name="referrer" content="no-referrer">` in `index.html`, and the
`history.replaceState()` in `auth.js` that strips the token on load.

**The OAuth round-trip would otherwise eat the token.** It is stashed in
`sessionStorage` *before* any redirect and read back after. Break this and you
get the worst available failure: the person signs in successfully, lands on
"no access", and their link now looks broken.

**`[hidden]` needs `!important` here.** The gate, the account chip and the
superadmin badge are toggled with the `hidden` attribute *and* carry classes
that set `display`, which beats the browser's built-in `[hidden]` rule. The
first line of `auth.css` fixes this. Without it the gate stays painted over the
app after sign-in and swallows every click. `test_gate.py` has a regression
check for it.

**GDPR.** `DELETE /api/brukere/{id}` removes our record. The Supabase account
survives; deleting that needs the admin API with a service key, which is
deliberately *not* wired into this service. Do it from the Supabase dashboard.
Storing invited users' addresses also needs a short privacy statement.

---

## 9. Local development

`start.bat` still works as a double-click: local single-user mode, no database,
no sign-in, the app exactly as it was before access control existed.

```
LOCAL_SINGLE_USER=1 SERVE_STATIC=1 python app.py    # http://127.0.0.1:8000
```

It is an auth bypass, so it is fenced: the app refuses the flag on any
non-loopback bind, and refuses it alongside `DATABASE_URL` or
`ALLOWED_ORIGINS`.

To exercise the real sign-in locally, run a Postgres and point at your Supabase
project:

```
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16

export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
export SUPABASE_JWKS_URL=https://<ref>.supabase.co/auth/v1/.well-known/jwks.json
export SUPABASE_JWT_ISSUER=https://<ref>.supabase.co/auth/v1
export SUPERADMIN_EMAILS=per@prosit.no
export SERVE_STATIC=1
python app.py
```

Fill in `supabaseUrl` and `supabasePublishableKey` in `static/config.js` — and note
that `config.js` is overwritten by the Pages workflow on deploy, so a local
edit never reaches production.

## 10. Tests

```
pytest -q test_invites.py test_auth.py   # 71 checks, no network
python test_render.py                    # rendering, no network
python test_engine.py                    # volume engine; calls Kartverket
python test_gate.py                      # browser; needs a server on :8011
python test_ui.py                        # full browser run; needs Kartverket
```

The first two run in CI against a Postgres service container. The others are
script-style — run them as programs, not under pytest.

The one to keep an eye on is
`test_invites.py::test_concurrent_redeem_never_exceeds_max_uses`. Redemption is
a single atomic `UPDATE` rather than a check-then-increment, because a shared
team link produces exactly the race that would break it, and it is the only
part of this design that fails *intermittently* rather than loudly.
