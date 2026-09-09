# Supabase setup for Masseberegning

Written against the Supabase dashboard as of September 2026. The dashboard has
been reorganised more than once — if a page has moved, the value you need is
still one of the five in §1, so search the dashboard for that rather than for
the menu path.

Supabase does two jobs here:

- **Sign-in.** It answers *"who is this person, and is that really their email
  address?"* — for anyone on the internet.
- **Postgres.** Which is where our own answer to *"may they use the app?"*
  lives: the `app_user`, `invite`, `invite_redemption` and `admin_audit`
  tables, created automatically on first API start.

---

## 1. The five values you are collecting

Everything below exists to fill in this table. Write them down as you go — you
will paste them into Fly and GitHub in §8.

| | Value | Looks like | Goes to |
|---|---|---|---|
| 1 | Project URL | `https://abcdefghijkl.supabase.co` | GitHub variable `SUPABASE_URL` |
| 2 | Publishable key | `sb_publishable_xxxxxxxx` | GitHub variable `SUPABASE_PUBLISHABLE_KEY` |
| 3 | Connection string | `postgresql://postgres.abcdefghijkl:PASSWORD@aws-1-eu-north-1.pooler.supabase.com:5432/postgres` | Fly secret `DATABASE_URL` |
| 4 | JWKS URL | `https://abcdefghijkl.supabase.co/auth/v1/.well-known/jwks.json` | Fly secret `SUPABASE_JWKS_URL` |
| 5 | Issuer | `https://abcdefghijkl.supabase.co/auth/v1` | Fly secret `SUPABASE_JWT_ISSUER` |

4 and 5 are just the project ref with a suffix, so really you are collecting
the ref, the publishable key, and the database password.

The `abcdefghijkl` part is your **project ref**. It appears in the dashboard URL
(`supabase.com/dashboard/project/<ref>`), so that is the quickest place to read
it off.

---

## 2. Create the project

<https://supabase.com/dashboard> → **New project**.

- **Region:** an EU one — *North EU (Stockholm)* or *Central EU (Frankfurt)*.
  Kartverket's elevation data is open, but the email addresses of invited
  users are personal data and belong in the EEA.
- **Database password:** generate one and put it in a password manager now. It
  is shown once, and you need it in §5. If you lose it you can reset it under
  **Project Settings → Database**, but that means updating the Fly secret too.

Wait for provisioning to finish before continuing — some pages show
placeholder values while the project is still building.

---

## 3. The publishable key (value 2)

**Project Settings → API Keys.**

Supabase now issues four kinds of key:

| Key | Format | Status |
|---|---|---|
| Publishable | `sb_publishable_…` | current — **use this** |
| Secret | `sb_secret_…` | current, server-side only — we do not use it |
| `anon` | long JWT, starts `eyJ` | legacy |
| `service_role` | long JWT, starts `eyJ` | legacy |

Copy the **publishable** key. It is safe to publish: it ends up in
`config.js`, which every visitor downloads. That is by design — it identifies
the project, it does not authorise anything.

> **Any tutorial showing a long key beginning with `eyJ` is describing the old
> system.** `anon` and `service_role` are being retired by the end of 2026. If
> your project only offers the legacy keys, the `anon` key still works — set the
> GitHub variable `SUPABASE_ANON_KEY` instead of `SUPABASE_PUBLISHABLE_KEY` and
> the Pages workflow will pick it up.

**Never put the `secret` or `service_role` key anywhere in this project.** The
API does not need it: it verifies tokens against public keys and talks to
Postgres directly.

---

## 4. JWT signing keys (values 4 and 5)

**Project Settings → JWT** (`/dashboard/project/_/settings/jwt`).

The API verifies every access token's signature against Supabase's *public*
keys, so it never holds a shared secret. That needs asymmetric keys.

1. Click **Migrate JWT secret**. Your existing secret is imported and a new
   asymmetric key is created as **standby**. Nothing changes yet, and stopping
   here is safe.
2. Click **Rotate keys**. The asymmetric key becomes current.

ES256 (NIST P-256) is Supabase's preferred algorithm; the API accepts ES256,
RS256 and RS512, so either is fine.

Values 4 and 5 are then:

```
https://<ref>.supabase.co/auth/v1/.well-known/jwks.json
https://<ref>.supabase.co/auth/v1
```

**Check it:**

```
curl -s https://<ref>.supabase.co/auth/v1/.well-known/jwks.json
```

You should get a `keys` array with at least one entry whose `alg` is `ES256`
or `RS256`. An empty array means the rotation in step 2 has not happened —
the API will reject every token until it has.

> If you skip this section entirely, the API can still work using the legacy
> shared secret: set the Fly secret `SUPABASE_JWT_SECRET` to the JWT secret
> from this page and leave `SUPABASE_JWKS_URL` unset. It is a worse setup —
> the same secret both signs and verifies — so only use it if the migration
> gives you trouble.

---

## 5. The connection string (value 3)

Click **Connect** at the top of the dashboard, then the **Postgres** /
connection-string tab. You are offered several options, and the differences
matter:

| Option | Host | Port | Network |
|---|---|---|---|
| Direct connection | `db.<ref>.supabase.co` | 5432 | **IPv6 only** unless you buy the IPv4 add-on |
| Shared pooler — **Session** | `aws-N-<region>.pooler.supabase.com` | 5432 | IPv4 |
| Shared pooler — Transaction | `aws-N-<region>.pooler.supabase.com` | 6543 | IPv4 |
| Dedicated pooler | `db.<ref>.supabase.co` | 6543 | paid tiers |

**Take the Session pooler string.**

The direct connection is what Supabase recommends for a long-lived server like
ours, and it would be the right answer — except that it is IPv6-only on new
projects. The session pooler is IPv4, behaves the same way from the
application's point of view (one connection held open per pool slot), and
removes a whole networking question from the deployment.

The username differs between them — `postgres.<ref>` for the pooler,
plain `postgres` for the direct connection — so you cannot swap the host and
keep the rest.

**Do not use the transaction pooler (6543).** It is PgBouncer in transaction
mode, where consecutive queries can land on different backend sessions. Our
pool already sets `prepare_threshold: None` so it would survive that, but
session mode is simply the right fit.

Replace `[YOUR-PASSWORD]` with the password from §2. If the password contains
`@`, `/`, `:` or `#`, percent-encode it (`@` → `%40`) or the URL will parse
wrongly.

**Check it**, if you have `psql`:

```
psql "postgresql://postgres.<ref>:<password>@aws-1-eu-north-1.pooler.supabase.com:5432/postgres" -c "select now()"
```

No `psql`? Skip it — the API's `/healthz` reports `"db":"ok"` and is a better
check anyway, because it proves the connection works *from Fly*.

---

## 6. Redirect URLs

**Authentication → URL Configuration.**

Supabase refuses any `redirect_to` that is not allow-listed here. **This is
the most common reason a working local setup breaks in production**, and the
symptom is unhelpful — a redirect to an error page, or a blank screen.

**Site URL:**

```
https://kongper.github.io/masseberegning/
```

**Redirect URLs** — add all three:

```
https://kongper.github.io/masseberegning/**
http://localhost:8000/static/**
http://127.0.0.1:8000/static/**
```

Both loopback forms are listed deliberately: `127.0.0.1` is what `start.bat`
opens, but **Microsoft/Entra rejects `127.0.0.1` as a redirect host** and
requires `localhost`. Having both means local sign-in works whichever provider
you test with.

Wildcards are glob-style, and `.` and `/` count as separators:

- `*` matches anything except a separator
- `**` matches anything **including** separators — which is why the paths above
  use `**`, so `/masseberegning/index.html` matches
- `?` matches one non-separator character

There is a pattern tester on that dashboard page. Use it — it is faster than
finding out from a failed sign-in.

---

## 7. Google sign-in

Do this one first and get the whole chain working before adding others. Google
has no rate limit and needs no SMTP.

### 7a. In Supabase

**Authentication → Providers → Google.** Enable it, and note the **callback
URL** shown there. It will be:

```
https://<ref>.supabase.co/auth/v1/callback
```

Leave the tab open; you come back to paste two values.

### 7b. In Google Cloud

<https://console.cloud.google.com/auth/clients> — this is the **Google Auth
Platform** console. Older guides send you to *APIs & Services → Credentials*;
same thing, reorganised.

1. Create a project if you have none.
2. **Branding** — app name and support email. This is what the consent screen
   shows, so put something recognisable: people will see it when they sign in.
3. **Audience** — *External*. While the app is in *Testing* only listed test
   users can sign in, which is a second allow-list on top of our invitations
   and a confusing way to be locked out. Either add your invitees as test
   users, or publish the app. For an invite-only tool, publishing is fine:
   requesting only `openid`, `email` and `profile` avoids Google's
   verification review.
4. **Data Access** → add scopes: `openid`, `.../auth/userinfo.email`,
   `.../auth/userinfo.profile`. `openid` sometimes has to be typed in manually.
5. **Clients → Create client → Web application:**

   **Authorised JavaScript origins** — the *origin* only, no path:
   ```
   https://kongper.github.io
   http://localhost:8000
   ```

   **Authorised redirect URIs** — the Supabase callback, exactly, no trailing
   slash:
   ```
   https://<ref>.supabase.co/auth/v1/callback
   ```

   Note the asymmetry: the redirect URI points at **Supabase**, not at our
   site. Google sends the user to Supabase, which then sends them on to us
   using §6's list. Putting our own URL here is the classic mistake, and it
   produces `redirect_uri_mismatch`.

6. Copy the **Client ID** and **Client secret** into the Supabase tab from 7a
   and save.

### Check it

```
curl -s -H "apikey: sb_publishable_xxxx" https://<ref>.supabase.co/auth/v1/settings
```

That returns the project's public auth settings, including which providers are
on. Look for `"google": true` under `external`. This one command validates
value 1, value 2 and the provider wiring at once, before the app is involved
at all.

---

## 8. Where the values go

**Fly secrets** — carry a password or reveal the project ref:

```
fly secrets set \
  DATABASE_URL="postgresql://postgres.<ref>:<password>@aws-1-eu-north-1.pooler.supabase.com:5432/postgres" \
  SUPABASE_JWKS_URL="https://<ref>.supabase.co/auth/v1/.well-known/jwks.json" \
  SUPABASE_JWT_ISSUER="https://<ref>.supabase.co/auth/v1"
```

**GitHub repository variables** — *Settings → Secrets and variables → Actions
→ Variables*. Variables, not secrets: all three are served to every visitor in
`config.js`.

| Variable | Value |
|---|---|
| `API_BASE` | `https://masseberegning-api.fly.dev` |
| `SUPABASE_URL` | `https://<ref>.supabase.co` |
| `SUPABASE_PUBLISHABLE_KEY` | `sb_publishable_…` |

Optional, once you add more sign-in methods:

| Variable | Value | Effect |
|---|---|---|
| `PROVIDERS` | `'google','azure'` | which buttons the gate shows — quoted, comma-separated |
| `ALLOW_EMAIL_LINK` | `true` | shows the email form |

Only list a provider you have actually enabled, or its button will fail on
click.

---

## 9. Adding Microsoft later

**Authentication → Providers → Azure** in Supabase; the app registration goes
in <https://entra.microsoft.com> → **App registrations → New registration**.

- **Redirect URI**, platform *Web*: `https://<ref>.supabase.co/auth/v1/callback`
- **Account types:** *Any organizational directory and personal Microsoft
  accounts* to invite people outside Prosit; *My organization only* to
  restrict it.
- **Certificates & secrets → New client secret.** Copy the **Value**, not the
  Secret ID — they look similar and only one works. **Note the expiry date**:
  when it lapses, Microsoft sign-in stops working with no warning. Put it in a
  calendar.
- **Azure Tenant URL** in Supabase: `https://login.microsoftonline.com/<tenant-id>`
  for your tenant, or `https://login.microsoftonline.com/consumers` for
  personal accounts only. Leave it blank for the multi-tenant default.
- Supabase requires Azure to return an email address, so the `email` scope is
  mandatory. `offline_access` is optional here — Supabase issues its own
  refresh tokens, so the app does not need Microsoft's.

**One security note that matters for our design.** Add the `xms_edov` optional
claim in the app registration's manifest. It tells Supabase whether the email
Azure returned is actually verified. Our invitations can be *bound* to an email
address, and that binding is compared against the address in the token — so an
unverified email from an unverified domain would weaken exactly that check. It
costs one manifest edit.

Then set the `PROVIDERS` variable to `'google','azure'` and re-run the Pages
workflow.

---

## 10. Adding the email link later

The email link needs **custom SMTP first**. Supabase's built-in email service
is capped at **2 messages per hour** and is explicitly not for production, so
it cannot carry real invitations — and worse, it fails quietly from the user's
point of view.

**Project Settings → Authentication → SMTP Settings**, pointed at Resend,
Postmark, Brevo, SendGrid or similar. Then enable **Authentication → Providers
→ Email** with magic links, and set `ALLOW_EMAIL_LINK` to `true`.

Bear in mind that our invitations are already sent by hand, so the email link
is a *sign-in* convenience, not part of the invitation flow. It is genuinely
optional — Google and Microsoft cover most people.

---

## 11. Troubleshooting

| Symptom | Cause |
|---|---|
| Gate says **"Ikke konfigurert"** | `SUPABASE_URL` / `SUPABASE_PUBLISHABLE_KEY` did not reach `config.js`. Open `config.js` on the live site — the Pages workflow writes it and validates it with `node --check`, so check that run's log. |
| Google error **`redirect_uri_mismatch`** | The authorised redirect URI in Google Cloud must be the **Supabase** callback (`https://<ref>.supabase.co/auth/v1/callback`), not our site. |
| Sign-in returns to an error page or blank screen | The URL is not in §6's redirect list. Test the pattern with the dashboard's tester. |
| Google says the app is not verified, or "access blocked" | Consent screen is in *Testing* and the account is not a listed test user. Add them, or publish. |
| API returns 401 **"Ugyldig innlogging"** with a valid session | `SUPABASE_JWT_ISSUER` does not match the token's `iss`, or the JWKS rotation in §4 never happened. `fly logs` shows the rejection reason. |
| API returns 401 **"Innloggingen er utløpt"** immediately | Clock skew, or a token minted before a key rotation. Sign out and in again. |
| Browser console shows a **CORS** error | `ALLOWED_ORIGINS` in `fly.toml` must be the bare origin `https://kongper.github.io` — no path, no trailing slash. |
| `/healthz` reports `"db":"error"` | Connection string. Wrong password, an unencoded special character in it, or the IPv6-only direct connection instead of the session pooler. |
| App refuses to start at all | Deliberate: it will not run with an incomplete configuration. `fly logs` names exactly which variable is missing. |
| Invitation link says **"ugyldig eller ikke lenger gyldig"** | One generic message covers unknown, expired, revoked and used-up — by design, so a bad token reveals nothing. Check the invitation's row in **Administrasjon**. |

### Reading your own token

The most useful debugging tool here. Signed in on the site, open the browser
console and run:

```js
JSON.parse(atob(JSON.parse(localStorage['mb.session']).access_token.split('.')[1]))
```

You get the claims the API sees. Check `iss` matches `SUPABASE_JWT_ISSUER`
exactly, `aud` is `authenticated`, `email` is the address you expect, and `exp`
is in the future. A mismatch between `iss` here and the Fly secret explains
most otherwise-baffling 401s.
