# Masseberegning – setup runbook

Follow this top to bottom. Every step ends with something you can check, so a
mistake surfaces at the step that caused it rather than three steps later.

**What you end up with:**

| | |
|---|---|
| UI | `https://kongper.github.io/Masseberegning/` |
| API | `https://masseberegning-api-<hash>.europe-north1.run.app` (Cloud Run) |
| Repo | `https://github.com/kongper/Masseberegning` (public — Pages needs Pro for a private repo) |
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
> earlier Azure-targeted version. Ignore them — `setup.py` installs the
> current Cloud Run ones. `Claude outputs/` is gitignored.

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
| 3 | Session-pooler connection string | Secret Manager `masseberegning-db-url` |
| 4 | JWKS URL | `env.cloudrun.yaml` -> `SUPABASE_JWKS_URL` |
| 5 | Issuer | `env.cloudrun.yaml` -> `SUPABASE_JWT_ISSUER` |

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

## 3. Google Cloud Run

The API used to run on Fly.io. It moved on 2026-09-11, when the Fly trial
credit ended and the app was parked with

```
failed to list active VMs: trial has ended, please add a credit card
```

Cloud Run was chosen for its **permanent** free allowance — 180 000 vCPU-s,
360 000 GiB-s and 2 M requests per billing account per month, which at 1 vCPU /
2 GiB works out to 50 hours of request-processing time, or roughly 9 000
calculations. Fly's realistic bill for this app was under a dollar a month, so
the win is the shape of the bill, not its size: this version stays at zero
without anyone maintaining a payment relationship. `claude/cloud-run-migration.md`
in the Claude project has the full cost working.

Region **`europe-north1`** (Finland): a Tier 1 region, inside the EEA, closest
to Norway. A billing account with a card is still required — the free tier is
per billing account, not per project — but nothing here bills against it at
this volume.

### 3.1 One-time setup

Roughly an hour, most of it clicking. Set `PROJECT` once and paste the rest.

```bash
PROJECT=masseberegning          # or your own; must be globally unique
REGION=europe-north1

gcloud projects create "$PROJECT"
gcloud config set project "$PROJECT"
# Link billing in the console (Billing -> Link a billing account) - the free
# tier is per billing account and a project without one cannot deploy.

gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  iamcredentials.googleapis.com
```

**Set a budget alert at €1 before anything else.** The free tier has no hard
cap; an alert is the only thing between a runaway loop and a surprise.
*Billing → Budgets & alerts → Create budget.*

Container registry, in the same region as the service so the pull is free:

```bash
gcloud artifacts repositories create masseberegning \
  --repository-format=docker --location="$REGION" \
  --description="Masseberegning API images"

# The free storage allowance is 0.5 GiB and this image is larger than that on
# its own, so keep only the newest few versions.
gcloud artifacts repositories set-cleanup-policies masseberegning \
  --location="$REGION" --policy=- <<'EOF'
[{"name":"keep-3-newest","action":{"type":"Keep"},"mostRecentVersions":{"keepCount":3}}]
EOF
```

The one secret. **Copy the connection string from the Supabase dashboard's
Connect button**, not from here — the same three project-specific parts as
before (region in the hostname, `postgres.<ref>` as the username, the
password), and the same misleading failures if any of them is guessed. Session
pooler, IPv4, port 5432:

```bash
printf '%s' 'postgresql://postgres.<ref>:<password>@aws-N-YOUR-REGION.pooler.supabase.com:5432/postgres' \
  | gcloud secrets create masseberegning-db-url --data-file=-
```

Six active secret versions are free, so rotating the password later costs
nothing:

```bash
printf '%s' '<new url>' | gcloud secrets versions add masseberegning-db-url --data-file=-
```

### 3.2 Deploy identity, without a downloadable key

```bash
PROJECT_NUM=$(gcloud projects describe "$PROJECT" --format='value(projectNumber)')
SA=masseberegning-deploy@"$PROJECT".iam.gserviceaccount.com

gcloud iam service-accounts create masseberegning-deploy \
  --display-name="GitHub Actions deployer"

for role in roles/run.admin roles/iam.serviceAccountUser roles/artifactregistry.writer; do
  gcloud projects add-iam-policy-binding "$PROJECT" \
    --member="serviceAccount:$SA" --role="$role" --condition=None
done

# Scoped to the one secret rather than project-wide.
gcloud secrets add-iam-policy-binding masseberegning-db-url \
  --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
```

Then Workload Identity Federation, so no JSON key ever lands in a repository
secret. A downloaded key in `secrets.GCP_SA_KEY` also works and is quicker —
it is also a long-lived credential in a public repo's settings, which is the
reason not to:

```bash
gcloud iam workload-identity-pools create github \
  --location=global --display-name="GitHub"

gcloud iam workload-identity-pools providers create-oidc github \
  --location=global --workload-identity-pool=github \
  --display-name="GitHub OIDC" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == 'kongper/Masseberegning'"

gcloud iam service-accounts add-iam-policy-binding "$SA" \
  --role=roles/iam.workloadIdentityUser \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUM/locations/global/workloadIdentityPools/github/attributes/repository/kongper/Masseberegning"

echo "GCP_PROJECT      = $PROJECT"
echo "GCP_DEPLOY_SA    = $SA"
echo "GCP_WIF_PROVIDER = projects/$PROJECT_NUM/locations/global/workloadIdentityPools/github/providers/github"
```

> **`--attribute-condition` is not optional.** Without it the pool will mint a
> token for *any* GitHub repository on the internet that asks, and that token
> can deploy to your project. The condition pins it to this repo.

Put those three printed values in the repo as **variables** (not secrets):
*Settings → Secrets and variables → Actions → Variables* —
`GCP_PROJECT`, `GCP_DEPLOY_SA`, `GCP_WIF_PROVIDER`. They are identifiers, not
credentials.

### 3.3 The deploy itself

`.github/workflows/api.yml` does it on every push to `main`: build, push to
Artifact Registry, `gcloud run deploy`, then verify `/healthz` and `/readyz`.
Everything non-secret is in **`env.cloudrun.yaml`**, committed, which replaces
what used to be `fly.toml`'s `[env]` block.

To deploy by hand the first time, or from a machine, from the repo root:

```bash
IMAGE="$REGION-docker.pkg.dev/$PROJECT/masseberegning/api"
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
docker build --platform linux/amd64 -t "$IMAGE:manual" .
docker push "$IMAGE:manual"

gcloud run deploy masseberegning-api \
  --image "$IMAGE:manual" --region "$REGION" --allow-unauthenticated \
  --cpu 1 --memory 2Gi --min-instances 0 --max-instances 1 \
  --concurrency 8 --timeout 180 \
  --startup-probe httpGet.path=/healthz,initialDelaySeconds=10,periodSeconds=5,failureThreshold=12,timeoutSeconds=4 \
  --set-secrets DATABASE_URL=masseberegning-db-url:latest \
  --env-vars-file env.cloudrun.yaml
```

Four of those flags carry reasoning that is expensive to rediscover, and the
workflow repeats it in comments:

- **`--max-instances 1`** is the one-machine invariant inherited from Fly. Job
  output is written to the instance's filesystem and read back through
  `/api/jobb/{id}/{name}`, so a second instance serves 404s for the first one's
  downloads. Raising it needs blob storage first; `storage.py` is the seam.
- **`--startup-probe` on `/healthz`, never `/readyz`.** `/healthz` is liveness
  only and touches nothing outside the process. A database-backed probe fails a
  cold start, which takes the whole revision down and returns the platform's
  bodiless 503 — which browsers report as a CORS error. Twelve failures × 5 s
  is a 60-second budget for importing rasterio and pyproj.
- **No `--no-cpu-throttling`.** It looks like the fix for idle connections, but
  it switches the service to instance-based billing where an idle instance is
  charged for its whole lifetime, and the free allowance stops covering the
  month. The `check=check_connection` argument in `db.py` is the actual fix,
  and `test_pool.py` proves it.
- **`--concurrency 8`**, not the default 80, which would let eighty CPU-bound
  calculations pile onto one vCPU.

**Check:**

```bash
URL=$(gcloud run services describe masseberegning-api --region "$REGION" --format='value(status.url)')
curl -s "$URL/healthz"     # {"status":"ok",...}
curl -s "$URL/readyz"      # {"status":"ready",...} - migrations ran
```

`/readyz` not ready is almost always the pooler hostname or the password in
Secret Manager. `gcloud run services logs read masseberegning-api --region "$REGION" --limit 50`
shows the real error; the app names what is wrong rather than coming up
half-working.

Then the check that no unit test can do — **idle, then twice**:

```bash
sleep 1200
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' "$URL/readyz"   # cold start
curl -s -o /dev/null -w '%{http_code} %{time_total}s\n' "$URL/readyz"   # warm
```

The first proves the cold start fits inside the startup probe; the second
proves the connection-pool fix survives a frozen instance. This is the pair
that would otherwise fail in front of a user, on whichever endpoint they
happened to open first.

---

## 4. GitHub Pages

**Settings → Secrets and variables → Actions → Variables.** Three, as
*variables*, not secrets — all three are served to every visitor in
`config.js`, so treating them as secrets would be theatre:

| Variable | Value |
|---|---|
| `API_BASE` | the Cloud Run service URL, no trailing slash — `gcloud run services describe masseberegning-api --region europe-north1 --format='value(status.url)'` |
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | the `sb_publishable_…` key (Project Settings → API Keys) |

The workflow fails loudly if any is unset, rather than publishing a site whose
sign-in silently does nothing.

Then **Settings → Pages → Source: GitHub Actions**.

Re-run both workflows: **Actions →** pick each →
**Re-run all jobs**.

**Check:** open <https://kongper.github.io/Masseberegning/>. You should get the
sign-in card with a "Fortsett med Google" button. If you instead see **"Ikke
konfigurert"**, the three variables did not reach `config.js` — check the Pages
workflow log.

---

## 5. First sign-in

Sign in with Google as `per@prosit.no`.

You land straight in the app, with **superadmin** next to your address in the
bottom-left corner and an **Administrasjon** link. No invitation needed:
`SUPERADMIN_EMAILS` in `env.cloudrun.yaml` is checked on every sign-in, which is what
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

**Cold starts are worse on Cloud Run than they were on Fly.** Fly's proxy held
the request while a suspended machine resumed; Cloud Run starts a container
from cold, and this image imports rasterio, pyproj and shapely before uvicorn
binds. Expect 10–20 seconds on the first request after an idle spell — which
users read as "the app is slow" the first time each morning.
`--min-instances 1` fixes it and costs the free tier entirely (an always-on
instance burns about 2.6 M GiB-seconds a month against a 360 k allowance), so
don't, unless someone complains — and then price it first.

**Set a budget alert** on the Google billing account and a spend limit on
Supabase. Cloud Run's free tier has no hard cap: exceeding it bills rather
than throttles, and the only line that grows with real use is egress (overlay
PNGs and GeoTIFFs, around $0.10/GiB from `europe-north1` — the free egress
allowance is North America only).

**Rate limits are per instance.** `ratelimit.py` is in-process. At
`--max-instances 1` the numbers are exact; scaled out, the effective limit is
`limit × instances`. That cannot happen before job output moves to blob
storage anyway — the two constraints are the same constraint.

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
