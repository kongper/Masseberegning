# Masseberegning – deployment and access control

The app is split in two: a static frontend on **GitHub Pages** and a **FastAPI
container** somewhere that can run Python. Registration is **invite-only** —
anyone may sign in with Google, Microsoft or an email link, but only people
holding a redeemed invitation can use the app.

For the reasoning behind these choices, see the two design docs in the Claude
project: `claude/deployment-and-auth-plan.md` and
`claude/invitation-link-design.md`.

---

## 1. The idea in one paragraph

Authentication and authorization live in different places. The identity
provider (Supabase Auth) answers *"who is this person, and is that really
their email address?"* — and it will happily answer that for anyone on the
internet. Our own Postgres answers *"may they use the app?"* A valid token
with no membership row gets `403` from everything that matters. An invitation
does not create an account; it grants membership to an account that has
already proved an email address. So a leaked invite link is useless on its own.

---

## 2. What runs where

| Piece | Where | Notes |
|---|---|---|
| `static/` | GitHub Pages | Public by design. No secrets, ever. |
| `app.py` + friends | Azure Container Apps or Cloud Run | Scale-to-zero is fine |
| Postgres | Supabase | Also the identity provider |
| Job output (PNG/TIFF/CSV) | container filesystem | See §7 before scaling out |

---

## 3. Set up Supabase

1. Create a project in an EU region (the Kartverket data is open, but user
   email addresses are personal data).
2. **Authentication → Providers**: enable Google and Azure (Microsoft), and
   leave Email on with magic links.
3. **Authentication → URL Configuration**: add both redirect URLs. The
   provider refuses any `redirect_to` that is not listed here, and this is the
   single most common reason a working local setup breaks in production:

   ```
   https://<org>.github.io/<repo>/
   https://masseberegning.prosit.no/
   ```

4. **Authentication → Signing keys**: use asymmetric (RS256/ES256) keys and
   note the JWKS URL.
5. Copy the connection string, the project URL, and the anon key.

The API creates its own tables on startup (`migrations/001_init.sql`, applied
by `db.run_migrations()`), so there is no manual migration step.

## 4. Deploy the API

Set the environment from `.env.example`. The four that matter most:

```
DATABASE_URL=postgresql://...            # Supabase connection string
SUPABASE_JWKS_URL=https://<ref>.supabase.co/auth/v1/.well-known/jwks.json
SUPERADMIN_EMAILS=per@prosit.no          # bootstrap; see §6
FRONTEND_URL=https://masseberegning.prosit.no/
ALLOWED_ORIGINS=https://masseberegning.prosit.no
```

The app **refuses to start** if any of `DATABASE_URL`, JWT verification
material, `ALLOWED_ORIGINS` or `SUPERADMIN_EMAILS` is missing. That is
deliberate: every one of those, left empty, produces an app that looks fine
and is either unusable or unprotected. `ALLOW_INCOMPLETE_CONFIG=1` overrides
it for local work.

Then:

```bash
az acr build --registry <acr> --image masseberegning:latest .
az containerapp update -n <app> -g <rg> \
  --image <acr>.azurecr.io/masseberegning:latest \
  --min-replicas 1 --max-replicas 1
curl https://<fqdn>/healthz
```

`--min-replicas 1` avoids a cold start that has to re-import rasterio and
pyproj. `--max-replicas 1` is load-bearing — see §7.

## 5. Deploy the frontend

Under **Settings → Secrets and variables → Actions → Variables**, set:

| Variable | Value |
|---|---|
| `API_BASE` | `https://api.masseberegning.prosit.no` |
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_ANON_KEY` | the anon / publishable key |

These are *variables*, not secrets. All three are served to every visitor in
`config.js`; treating them as secrets would be theatre. The workflow fails
loudly if any is unset, rather than publishing a site whose sign-in silently
does nothing.

Then enable **Settings → Pages → Source: GitHub Actions**. `pages.yml` runs on
any push that touches `static/`.

---

## 6. First sign-in

`SUPERADMIN_EMAILS` is checked on every sign-in, so it works against an empty
database. Sign in with that address and you are a superadmin with no invite.

**Leave it configured permanently.** It is the only way back in if the last
superadmin loses their account, and the app guards against removing the last
one but cannot conjure a new one.

From `/admin.html` you can then:

- create an invitation — a label, optionally an email binding, a use count, an
  expiry, and the granted role
- copy the link (**shown once**; the raw token is never stored, only its
  SHA-256, so it cannot be shown again — revoke and reissue if it is lost)
- revoke invitations, suspend or reactivate users, and read the audit log

Send the link yourself, in Teams or email. There is no SMTP in the app: no
sender domain to warm up, no bounce handling, and you get to add context in
your own words.

Two invite shapes, chosen per invite:

| Want | Set |
|---|---|
| "Kari, nobody else" | Bundet til e-post = `kari@…`, Antall bruk = 1 |
| "The Veidekke pilot team, ~8 people, 30 days" | leave email empty, Antall bruk = 8 |
| Another superadmin | Rolle = Superadmin, and bind the email |

An email-bound invite is single-use by construction — enforced by a database
constraint, not just a check in the API.

---

## 7. Things that will bite you

**Job storage is why `--max-replicas 1`.** Overlay PNGs, the GeoTIFF and the
CSV are written to the container filesystem, and `/api/jobb/{id}/{name}` reads
them back. A second replica serves 404s for the first replica's jobs. Move job
output to blob storage before raising the replica count. `storage.py` is the
only file that touches the filesystem, and it is written as a seam for exactly
this.

**Job URLs are unauthenticated on purpose.** `L.imageOverlay` renders an
`<img src>` and the export links are plain `<a download>` navigations, and
*neither can carry an `Authorization` header*. So the 12-hex job id is the
credential, and the URL expires with `JOB_TTL_MINUTES`. The exposure is that
anyone holding a link can fetch that job's terrain rendering until it expires.
For open Kartverket elevation data that is an acceptable trade — but it is a
decision, not an oversight, and it is the thing to revisit if the outputs ever
become confidential.

**The invite token leaks through `Referer` if you touch the URL handling.**
The token arrives as `?invitasjon=…`, so without care every Kartverket tile
request carries it. Two mechanisms guard this and both are needed: the
`<meta name="referrer" content="no-referrer">` in `index.html`, and the
`history.replaceState()` in `auth.js` that strips the token on load.

**The OAuth round-trip would otherwise eat the token.** It is stashed in
`sessionStorage` *before* any redirect and read back after. Break this and you
get the worst available failure: the person signs in successfully, lands on
"no access", and their link now looks broken.

**Rate limits are per replica.** `ratelimit.py` is in-process. At one replica
the numbers are exact; scaled out, the effective limit is `limit × replicas`.
Move the counters to Postgres before relying on them at scale.

**`[hidden]` needs `!important` here.** The gate, the account chip and the
superadmin badge are toggled with the `hidden` attribute *and* carry classes
that set `display`, which beats the browser's built-in `[hidden]` rule. The
first line of `auth.css` fixes this. Without it the gate stays painted over
the app after sign-in and swallows every click. `test_gate.py` has a
regression check for it.

**GDPR.** `DELETE /api/brukere/{id}` removes our record. The identity provider
still holds the account; deleting that needs Supabase's admin API with a
service key, which is deliberately *not* wired into this service. Do it from
the Supabase dashboard, or add it behind a separate credential.

---

## 8. Local development

```bash
pip install -r requirements.txt -r requirements-dev.txt
docker run -d -p 5432:5432 -e POSTGRES_PASSWORD=postgres postgres:16

export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres
export SUPABASE_JWT_SECRET=any-long-string-for-local-use
export SUPERADMIN_EMAILS=per@prosit.no
export SERVE_STATIC=1
python app.py            # http://127.0.0.1:8000
```

With `SERVE_STATIC=1` the API serves the UI too, so the frontend runs
same-origin and `config.js` needs no edits. Fill in `supabaseUrl` and
`supabaseAnonKey` in `static/config.js` to exercise real sign-in locally, and
remember to add `http://127.0.0.1:8000/static/index.html` to the provider's
redirect allow-list.

## 9. Tests

```bash
pytest -q test_invites.py test_auth.py   # 71 checks, no network
python test_render.py                    # rendering, no network
python test_engine.py                    # volume engine; calls Kartverket
python test_gate.py                      # browser; needs a server on :8011
python test_ui.py                        # full browser run; needs Kartverket
```

`test_invites.py` and `test_auth.py` are pytest suites and run in CI against a
Postgres service container. The others are script-style — run them as
programs, not under pytest.

The one to keep an eye on is
`test_invites.py::test_concurrent_redeem_never_exceeds_max_uses`. Redemption
is a single atomic `UPDATE` rather than a check-then-increment, because a
shared team link produces exactly the race that would break it, and it is the
only part of this design that fails *intermittently* rather than loudly.
