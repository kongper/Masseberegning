/* Masseberegning - authentication, the access gate, and the API helper.
 *
 * Two sections:
 *   1. A thin client for Supabase's auth REST API (GoTrue).
 *   2. The gate: decide which screen to show, and redeem an invitation.
 *
 * Section 1 exists instead of the Supabase JS SDK because this app vendors its
 * dependencies rather than loading them from a CDN, and we need exactly four
 * endpoints. If you later want more of Supabase than auth, swap this for the
 * real SDK — the surface used by the rest of the app is just MB.api / MB.url.
 *
 * Everything the browser holds here is public: the anon key is meant to be
 * readable. The access token is a bearer credential and lives in localStorage,
 * which is the standard tradeoff for a static frontend with no server of its
 * own to set a cookie.
 */

(function () {
  'use strict';

  const CFG = window.MB_CONFIG || {};
  const API = (CFG.apiBase || '').replace(/\/$/, '');
  const SB = (CFG.supabaseUrl || '').replace(/\/$/, '');
  const AUTH = SB + '/auth/v1';
  const STORE_KEY = 'mb.session';
  const INVITE_KEY = 'mb.invite';

  /* ============================================================ section 1
   * Supabase auth transport
   * ==================================================================== */

  function loadSession() {
    try {
      const raw = localStorage.getItem(STORE_KEY);
      return raw ? JSON.parse(raw) : null;
    } catch { return null; }
  }

  function saveSession(s) {
    try {
      if (s) localStorage.setItem(STORE_KEY, JSON.stringify(s));
      else localStorage.removeItem(STORE_KEY);
    } catch { /* private mode; the session simply will not survive a reload */ }
  }

  let session = loadSession();

  function sbHeaders(extra) {
    return Object.assign({
      'apikey': CFG.supabaseAnonKey || '',
      'Content-Type': 'application/json',
    }, extra || {});
  }

  /* The provider returns the session in the URL fragment. Read it, store it,
   * and strip it — a fragment full of tokens should not stay in the address
   * bar or in the back-button history. */
  function captureFragmentSession() {
    if (!location.hash || location.hash.length < 2) return false;
    const p = new URLSearchParams(location.hash.slice(1));
    const access = p.get('access_token');
    if (!access) {
      if (p.get('error_description') || p.get('error')) {
        history.replaceState(null, '', location.pathname + location.search);
        gateError(decodeURIComponent(
          (p.get('error_description') || p.get('error')).replace(/\+/g, ' ')));
      }
      return false;
    }
    session = {
      access_token: access,
      refresh_token: p.get('refresh_token') || '',
      expires_at: Date.now() + (Number(p.get('expires_in') || 3600) * 1000),
    };
    saveSession(session);
    history.replaceState(null, '', location.pathname + location.search);
    return true;
  }

  async function refresh() {
    if (!session || !session.refresh_token) return false;
    try {
      const r = await fetch(`${AUTH}/token?grant_type=refresh_token`, {
        method: 'POST',
        headers: sbHeaders(),
        body: JSON.stringify({ refresh_token: session.refresh_token }),
      });
      if (!r.ok) { signOutLocal(); return false; }
      const d = await r.json();
      if (!d.access_token) { signOutLocal(); return false; }
      session = {
        access_token: d.access_token,
        refresh_token: d.refresh_token || session.refresh_token,
        expires_at: Date.now() + (Number(d.expires_in || 3600) * 1000),
      };
      saveSession(session);
      return true;
    } catch {
      return false;
    }
  }

  async function validSession() {
    if (!session) return null;
    // Refresh a minute early rather than waiting for the first 401.
    if (session.expires_at && session.expires_at - Date.now() < 60000) {
      if (!await refresh()) return null;
    }
    return session;
  }

  function signInWithProvider(provider) {
    const redirect = location.origin + location.pathname;
    const url = `${AUTH}/authorize?provider=${encodeURIComponent(provider)}` +
      `&redirect_to=${encodeURIComponent(redirect)}`;
    location.assign(url);
  }

  async function signInWithEmailLink(email) {
    const redirect = location.origin + location.pathname;
    const r = await fetch(`${AUTH}/otp`, {
      method: 'POST',
      headers: sbHeaders(),
      body: JSON.stringify({
        email,
        create_user: true,
        options: { email_redirect_to: redirect },
      }),
    });
    if (!r.ok) {
      let msg = 'Kunne ikke sende innloggingslenke.';
      try { const d = await r.json(); msg = d.msg || d.error_description || msg; } catch {}
      throw new Error(msg);
    }
  }

  function signOutLocal() {
    session = null;
    saveSession(null);
  }

  async function signOut() {
    const s = session;
    signOutLocal();
    try {
      if (s) {
        await fetch(`${AUTH}/logout`, {
          method: 'POST',
          headers: sbHeaders({ Authorization: 'Bearer ' + s.access_token }),
        });
      }
    } catch { /* the local session is already gone, which is what matters */ }
    location.replace(location.origin + location.pathname);
  }

  /* ============================================================ section 2
   * API helper
   * ==================================================================== */

  function url(path) {
    if (/^https?:/i.test(path)) return path;
    return API + path;
  }

  async function api(path, opts) {
    opts = opts || {};
    const s = await validSession();
    const headers = Object.assign({}, opts.headers || {});
    if (s) headers.Authorization = 'Bearer ' + s.access_token;
    if (opts.body && !headers['Content-Type']) headers['Content-Type'] = 'application/json';

    let r = await fetch(url(path), Object.assign({}, opts, { headers }));

    // One retry after a refresh: a token can expire mid-request, and a single
    // 401 should not surface to the user as a failure.
    if (r.status === 401 && await refresh()) {
      headers.Authorization = 'Bearer ' + session.access_token;
      r = await fetch(url(path), Object.assign({}, opts, { headers }));
    }
    return r;
  }

  async function apiJson(path, opts) {
    const r = await api(path, opts);
    let data = null;
    try { data = await r.json(); } catch {}
    if (!r.ok) {
      const detail = data && (data.detail || data.message);
      throw new Error(detail || `Forespørselen feilet (${r.status}).`);
    }
    return data;
  }

  /* ============================================================ section 3
   * The gate
   * ==================================================================== */

  /* The invite token must be stashed BEFORE any redirect to a provider, and
   * read back after. Getting this wrong produces the worst available failure:
   * the person signs in successfully, lands on "no access", and their link
   * now looks broken. */
  function captureInviteToken() {
    const p = new URLSearchParams(location.search);
    const token = p.get('invitasjon');
    if (!token) return;
    try { sessionStorage.setItem(INVITE_KEY, token); } catch {}
    // Strip it from the URL immediately. Combined with the no-referrer meta
    // tag in index.html this keeps the token out of the Referer header on
    // every map tile request.
    p.delete('invitasjon');
    const qs = p.toString();
    history.replaceState(null, '', location.pathname + (qs ? '?' + qs : ''));
  }

  function stashedInvite() {
    try { return sessionStorage.getItem(INVITE_KEY); } catch { return null; }
  }

  function clearInvite() {
    try { sessionStorage.removeItem(INVITE_KEY); } catch {}
  }

  const gate = () => document.getElementById('gate');
  const appEl = () => document.querySelector('.app');

  /* The gate is a full-screen opaque overlay rather than a screen that
   * replaces the app. That is deliberate: Leaflet measures its container on
   * construction, so a map built inside a hidden element comes up with a zero
   * size and needs an invalidateSize() dance after every reveal. Letting the
   * map initialise at its real size behind the overlay avoids the whole
   * problem. `inert` keeps keyboard focus out of what is behind the gate. */
  function showGate(html) {
    const g = gate();
    if (!g) return;
    g.innerHTML = html;
    g.hidden = false;
    const a = appEl();
    if (a) { a.inert = true; a.setAttribute('aria-hidden', 'true'); }
  }

  function hideGate() {
    if (gate()) gate().hidden = true;
    const a = appEl();
    if (a) { a.inert = false; a.removeAttribute('aria-hidden'); }
  }

  function gateError(msg) {
    const box = document.getElementById('gate-msg');
    if (box) {
      box.textContent = msg;
      box.hidden = false;
    } else {
      pendingError = msg;
    }
  }

  let pendingError = null;

  const PROVIDER_LABEL = {
    google: 'Fortsett med Google',
    azure: 'Fortsett med Microsoft',
    github: 'Fortsett med GitHub',
  };

  function signInScreen(invited) {
    const buttons = (CFG.providers || [])
      .map(p => `<button class="btn btn-primary gate-provider" data-provider="${p}">${
        PROVIDER_LABEL[p] || p}</button>`)
      .join('');

    const emailForm = CFG.allowEmailLink ? `
      <div class="gate-or"><span>eller</span></div>
      <form id="gate-email-form" class="gate-email">
        <label for="gate-email">E-postadresse</label>
        <input id="gate-email" type="email" required autocomplete="email"
               placeholder="navn@firma.no">
        <button type="submit" class="btn">Send innloggingslenke</button>
      </form>` : '';

    return `
      <div class="gate-card">
        <h1>Masseberegning</h1>
        ${invited
          ? `<p class="gate-lead">Du er invitert. Logg inn for å ta i bruk invitasjonen.</p>`
          : `<p class="gate-lead">Skjæring og fylling beregnet på Kartverkets
             terrengmodell. Tilgang krever invitasjon.</p>`}
        <div id="gate-msg" class="gate-msg" hidden></div>
        <div class="gate-actions">${buttons}</div>
        ${emailForm}
        <p class="gate-foot">Høydedata: © Kartverket (DTM 1 m, CC BY 4.0)</p>
      </div>`;
  }

  function noAccessScreen(email) {
    return `
      <div class="gate-card">
        <h1>Ingen tilgang</h1>
        <p class="gate-lead">Du er logget inn som <strong>${escapeHtml(email)}</strong>,
        men denne kontoen har ikke tilgang til Masseberegning.</p>
        <p class="gate-lead">Tilgang gis med en invitasjonslenke. Har du fått en lenke,
        åpne den på nytt i denne nettleseren.</p>
        <div id="gate-msg" class="gate-msg" hidden></div>
        <div class="gate-actions">
          <button class="btn" id="gate-signout">Logg ut</button>
        </div>
      </div>`;
  }

  function checkEmailScreen(email) {
    return `
      <div class="gate-card">
        <h1>Sjekk e-posten</h1>
        <p class="gate-lead">Vi har sendt en innloggingslenke til
        <strong>${escapeHtml(email)}</strong>. Åpne lenken i denne nettleseren.</p>
        <div id="gate-msg" class="gate-msg" hidden></div>
        <div class="gate-actions">
          <button class="btn" id="gate-back">Tilbake</button>
        </div>
      </div>`;
  }

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, c => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function wireSignIn(invited) {
    showGate(signInScreen(invited));
    if (pendingError) { gateError(pendingError); pendingError = null; }

    document.querySelectorAll('.gate-provider').forEach(b => {
      b.addEventListener('click', () => signInWithProvider(b.dataset.provider));
    });

    const form = document.getElementById('gate-email-form');
    if (form) {
      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const email = document.getElementById('gate-email').value.trim();
        if (!email) return;
        const btn = form.querySelector('button');
        btn.disabled = true;
        btn.textContent = 'Sender …';
        try {
          await signInWithEmailLink(email);
          showGate(checkEmailScreen(email));
          document.getElementById('gate-back')
            .addEventListener('click', () => wireSignIn(invited));
        } catch (err) {
          gateError(err.message);
          btn.disabled = false;
          btn.textContent = 'Send innloggingslenke';
        }
      });
    }
  }

  function wireNoAccess(email) {
    showGate(noAccessScreen(email));
    document.getElementById('gate-signout').addEventListener('click', signOut);
  }

  let me = null;

  async function bootstrap() {
    // Order matters: the invite token has to be captured before anything can
    // navigate away, and the fragment session before anything reads storage.
    captureInviteToken();
    captureFragmentSession();

    /* No provider configured. Two possibilities, and the API can tell us
     * which: either this is `start.bat` running the app in local single-user
     * mode, where there is no sign-in at all, or config.js was never filled
     * in. Probing costs one unauthenticated request and only happens when
     * supabaseUrl is empty - the Pages workflow always fills it, so a
     * published site never takes this path. */
    if (!SB || !CFG.supabaseAnonKey) {
      let local = null;
      try {
        const r = await fetch(url('/api/meg'));
        if (r.ok) local = await r.json();
      } catch { /* fall through to the not-configured screen */ }

      if (local && local.status === 'active') {
        me = local;
        MB.user = local;
        hideGate();
        document.dispatchEvent(new CustomEvent('mb:ready', { detail: local }));
        return;
      }

      showGate(`<div class="gate-card"><h1>Ikke konfigurert</h1>
        <p class="gate-lead">Innlogging er ikke satt opp: <code>supabaseUrl</code> og
        <code>supabaseAnonKey</code> mangler i <code>config.js</code>.</p>
        <p class="gate-lead">Kjører du lokalt uten database, start med
        <code>LOCAL_SINGLE_USER=1</code> (se README-DEPLOY.md).</p></div>`);
      return;
    }

    const invite = stashedInvite();

    if (!await validSession()) {
      wireSignIn(!!invite);
      return;
    }

    let who;
    try {
      who = await apiJson('/api/meg');
    } catch (err) {
      // A 401 here means the token did not survive verification; treat it as
      // signed out rather than leaving the user on a dead screen.
      signOutLocal();
      pendingError = err.message;
      wireSignIn(!!invite);
      return;
    }

    if (who.status === 'no_access' && invite) {
      try {
        who = await apiJson('/api/invitasjon/innloes', {
          method: 'POST',
          body: JSON.stringify({ token: invite }),
        });
        clearInvite();
      } catch (err) {
        clearInvite();
        showGate(noAccessScreen(who.email));
        gateError(err.message);
        document.getElementById('gate-signout').addEventListener('click', signOut);
        return;
      }
    }

    if (who.status !== 'active') {
      wireNoAccess(who.email);
      return;
    }

    clearInvite();
    me = who;
    MB.user = who;
    hideGate();
    document.dispatchEvent(new CustomEvent('mb:ready', { detail: who }));
  }

  /* -------------------------------------------------------------- exports */

  const MB = {
    api, apiJson, url, signOut,
    get session() { return session; },
    get user() { return me; },
    set user(v) { me = v; },
    bootstrap,
    escapeHtml,
  };
  window.MB = MB;

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', bootstrap);
  } else {
    bootstrap();
  }
})();
