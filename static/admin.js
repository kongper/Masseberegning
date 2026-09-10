/* Masseberegning - superadmin page.
 *
 * Every endpoint used here is behind require_superadmin, so this file being
 * publicly readable on GitHub Pages gives away nothing but the shape of the
 * API. The `not-admin` branch below is a courtesy for a normal user who
 * follows the link, not a security boundary.
 */

(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const esc = (s) => MB.escapeHtml(s == null ? '' : s);

  const dt = new Intl.DateTimeFormat('nb-NO', {
    day: '2-digit', month: '2-digit', year: 'numeric',
    hour: '2-digit', minute: '2-digit',
  });
  const fmtDate = (iso) => (iso ? dt.format(new Date(iso)) : '–');

  const STATUS_LABEL = {
    active: 'Aktiv', expired: 'Utløpt', used_up: 'Oppbrukt',
    revoked: 'Trukket tilbake', suspended: 'Sperret',
  };

  function message(kind, text, where) {
    const box = document.createElement('div');
    box.className = 'admin-msg ' + kind;
    box.textContent = text;
    where.replaceChildren(box);
  }

  /* ------------------------------------------------------------- invites */

  async function copyText(text, btn, label = 'Kopier') {
    try {
      // The Clipboard API needs a secure context; select+execCommand is the
      // fallback for plain-http local development.
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        const t = document.createElement('textarea');
        t.value = text; t.style.position = 'fixed'; t.style.opacity = '0';
        document.body.appendChild(t); t.select();
        document.execCommand('copy'); t.remove();
      }
      btn.textContent = 'Kopiert';
    } catch {
      btn.textContent = 'Kopier manuelt';
    }
    setTimeout(() => { btn.textContent = label; }, 2500);
  }

  /* Who an invitation is for, in one cell. */
  function boundTo(i) {
    if (i.email) return esc(i.email);
    if (i.email_domain) return `alle på <strong>@${esc(i.email_domain)}</strong>`;
    return '<span class="dim">åpen lenke</span>';
  }

  function showInviteLink(inv) {
    const out = $('invite-out');
    const bound = inv.email ? ` for <strong>${esc(inv.email)}</strong>` : '';
    out.innerHTML = `
      <div class="invite-result">
        <p>Lenken er laget${bound}. Du kan hente den igjen fra tabellen under
        så lenge invitasjonen er aktiv.</p>
        <div class="invite-link">
          <input id="inv-url" type="text" readonly value="${esc(inv.url)}">
          <button type="button" class="btn" id="inv-copy">Kopier</button>
        </div>
      </div>`;

    const field = $('inv-url');
    field.focus();
    field.select();

    $('inv-copy').addEventListener('click',
      () => copyText(inv.url, $('inv-copy')));
  }

  async function loadInvites() {
    const tbody = $('invite-rows');
    let data;
    try {
      data = await MB.apiJson('/api/invitasjon');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="8" class="dim">${esc(err.message)}</td></tr>`;
      return;
    }

    const rows = data.invitasjoner || [];
    $('invite-empty').hidden = rows.length > 0;
    tbody.innerHTML = rows.map(i => `
      <tr>
        <td>${esc(i.label) || '<span class="dim">–</span>'}</td>
        <td>${boundTo(i)}</td>
        <td>${i.role_granted === 'superadmin'
              ? '<span class="badge">superadmin</span>' : 'Bruker'}</td>
        <td class="num">${i.uses} / ${i.max_uses}</td>
        <td><span class="pill ${i.status}">${STATUS_LABEL[i.status] || i.status}</span></td>
        <td class="dim">${fmtDate(i.expires_at)}</td>
        <td class="dim">${(i.redeemed_by || []).map(esc).join('<br>') || '–'}</td>
        <td class="row-actions">${i.status === 'active' && i.has_link
              ? `<button class="btn btn-small" data-copy="${esc(i.id)}">Kopier lenke</button>`
              : ''}${i.status === 'active'
              ? `<button class="btn btn-small btn-danger" data-revoke="${esc(i.id)}">Trekk tilbake</button>`
              : ''}</td>
      </tr>`).join('');

    // Fetched per click rather than read from the list: the list is loaded on
    // every page view, so the token is deliberately not in it.
    tbody.querySelectorAll('[data-copy]').forEach(b => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        const was = b.textContent;
        b.textContent = 'Henter …';
        try {
          const d = await MB.apiJson('/api/invitasjon/' + b.dataset.copy + '/lenke');
          await copyText(d.url, b, was);
        } catch (err) {
          message('error', err.message, $('invite-out'));
          b.textContent = was;
        } finally {
          b.disabled = false;
        }
      });
    });

    tbody.querySelectorAll('[data-revoke]').forEach(b => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          await MB.apiJson('/api/invitasjon/' + b.dataset.revoke, { method: 'DELETE' });
          await Promise.all([loadInvites(), loadAudit()]);
        } catch (err) {
          message('error', err.message, $('invite-out'));
          b.disabled = false;
        }
      });
    });
  }

  /* --------------------------------------------------------------- users */

  async function loadUsers() {
    const tbody = $('user-rows');
    let data;
    try {
      data = await MB.apiJson('/api/brukere');
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="6" class="dim">${esc(err.message)}</td></tr>`;
      return;
    }

    const me = MB.user || {};
    tbody.innerHTML = (data.brukere || []).map(u => {
      const self = u.id === me.id;
      const suspended = u.status === 'suspended';
      return `
      <tr>
        <td>${esc(u.email)}${self ? ' <span class="dim">(deg)</span>' : ''}</td>
        <td>${u.role === 'superadmin'
              ? '<span class="badge">superadmin</span>' : 'Bruker'}</td>
        <td>${suspended
              ? '<span class="pill suspended">Sperret</span>'
              : '<span class="pill active">Aktiv</span>'}</td>
        <td class="dim">${esc(u.invited_by_email) || '–'}</td>
        <td class="dim">${fmtDate(u.last_seen_at)}</td>
        <td>${self ? '' : `
          <button class="btn btn-small" data-toggle="${esc(u.id)}"
                  data-status="${suspended ? 'active' : 'suspended'}">
            ${suspended ? 'Gjenåpne' : 'Sperr'}
          </button>`}</td>
      </tr>`;
    }).join('');

    tbody.querySelectorAll('[data-toggle]').forEach(b => {
      b.addEventListener('click', async () => {
        b.disabled = true;
        try {
          await MB.apiJson('/api/brukere/' + b.dataset.toggle, {
            method: 'PATCH',
            body: JSON.stringify({ status: b.dataset.status }),
          });
          await Promise.all([loadUsers(), loadAudit()]);
        } catch (err) {
          message('error', err.message, $('invite-out'));
          b.disabled = false;
        }
      });
    });
  }

  /* --------------------------------------------------------------- audit */

  async function loadAudit() {
    const tbody = $('audit-rows');
    try {
      const data = await MB.apiJson('/api/revisjon');
      tbody.innerHTML = (data.hendelser || []).map(h => `
        <tr>
          <td class="dim">${fmtDate(h.created_at)}</td>
          <td>${esc(h.actor_email) || '<span class="dim">–</span>'}</td>
          <td><code>${esc(h.action)}</code></td>
          <td class="dim">${esc(h.target) || '–'}</td>
        </tr>`).join('') ||
        '<tr><td colspan="4" class="dim">Ingen hendelser ennå.</td></tr>';
    } catch (err) {
      tbody.innerHTML = `<tr><td colspan="4" class="dim">${esc(err.message)}</td></tr>`;
    }
  }

  /* ---------------------------------------------------------------- init */

  document.addEventListener('mb:ready', async (e) => {
    const me = e.detail;
    $('account').hidden = false;
    $('account-email').textContent = me.email;
    $('account-signout').addEventListener('click', () => MB.signOut());

    /* Reachable only by typing the URL: local mode hides the link. Every
     * endpoint on this page would answer 503, so say why once instead of
     * rendering three failed tables. */
    if (me.local_single_user) {
      $('not-admin').hidden = false;
      $('not-admin').innerHTML =
        '<div class="admin-msg error">Appen kjører i lokal enbrukermodus uten ' +
        'database. Invitasjoner og brukerstyring er ikke tilgjengelig her. ' +
        '<a href="index.html">Gå til kartet</a>.</div>';
      return;
    }

    if (me.role !== 'superadmin') {
      $('not-admin').hidden = false;
      return;
    }
    $('admin-body').hidden = false;

    $('invite-form').addEventListener('submit', async (ev) => {
      ev.preventDefault();
      const btn = ev.target.querySelector('button[type=submit]');
      const email = $('inv-email').value.trim();
      const uses = Number($('inv-uses').value) || 1;

      // Mirrored server-side and in a database constraint; caught here so the
      // admin gets the explanation before a round-trip. A domain rule
      // (@vg.no) is exempt - being multi-use is the whole point of it.
      const isDomain = email.startsWith('@');
      if (email && !isDomain && uses !== 1) {
        message('error',
          'En invitasjon bundet til én e-postadresse kan bare brukes én gang. ' +
          'Sett antall bruk til 1, eller skriv domenet i stedet – f.eks. @vg.no ' +
          'for alle med en verifisert adresse der.', $('invite-out'));
        return;
      }

      btn.disabled = true;
      btn.textContent = 'Oppretter …';
      try {
        const inv = await MB.apiJson('/api/invitasjon', {
          method: 'POST',
          body: JSON.stringify({
            label: $('inv-label').value.trim() || null,
            email: email || null,
            role_granted: $('inv-role').value,
            max_uses: uses,
            expires_in_days: Number($('inv-days').value) || 14,
          }),
        });
        showInviteLink(inv);
        $('invite-form').reset();
        $('inv-uses').value = 1;
        $('inv-days').value = 14;
        await Promise.all([loadInvites(), loadAudit()]);
      } catch (err) {
        message('error', err.message, $('invite-out'));
      } finally {
        btn.disabled = false;
        btn.textContent = 'Opprett lenke';
      }
    });

    await Promise.all([loadInvites(), loadUsers(), loadAudit()]);
  });
})();
