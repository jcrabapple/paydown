// Subscriptions: detected suggestions, active list with renewals, cancelled list.
// Every active subscription feeds the monthly plan at its monthly-equivalent cost;
// the server computes all figures (monthly_cents, annual_cents, next_renewal).
//
// The panel is reusable: `renderSubscriptionsInto` renders it inside the Budget
// page, and `subscriptionsView` wraps the same panel as a standalone `#/subscriptions`
// route so existing bookmarks keep working.

import { api, errText } from '../api.js';
import {
  usd, esc, el, field, moneyInput, openFormDialog, confirmDialog,
  parseCents, errorBox, todayISO, errorBanner,
} from '../util.js';

const CYCLE_LABEL = { weekly: 'week', monthly: 'month', quarterly: 'quarter', yearly: 'year' };

function fmtDate(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
}

function untilText(days) {
  if (days === 0) return 'today';
  if (days === 1) return 'tomorrow';
  return `in ${days} days`;
}

/** Standalone route: `#/subscriptions` renders the same section under its own heading. */
export async function subscriptionsView(main) {
  main.innerHTML = '';
  main.appendChild(el('<div class="page-head"><h1>Subscriptions</h1></div>'));
  const refresh = () => window.dispatchEvent(new HashChangeEvent('hashchange'));
  const onError = (e) => errorBanner(main, errText(e));
  await renderSubscriptionsInto(main, refresh, onError);
}

/**
 * Fetch subscriptions data + suggestions and render the section into `container`.
 * Used by the Budget page (embedded) and by `subscriptionsView` (standalone).
 */
export async function renderSubscriptionsInto(container, refresh, onError) {
  let data, suggestions;
  try {
    [data, suggestions] = await Promise.all([
      api.get('/api/subscriptions'),
      api.get('/api/subscriptions/suggestions').catch(() => []),
    ]);
  } catch (err) {
    onError(err);
    return;
  }
  const fail = (e) => onError(e);
  container.appendChild(buildSubscriptionsPanel(data, suggestions, refresh, fail));
}

/** Build the Subscriptions section (panel) from already-fetched data. */
export function buildSubscriptionsPanel(data, suggestions, refresh, fail) {
  const t = data.totals;
  const panel = el(`<section class="panel" data-section="subscriptions">
    <div class="section-head">
      <h2>Subscriptions</h2>
      <span class="head-pills">
        <span class="pill bill num">${usd(t.monthly_cents)}/mo</span>
        <span class="pill off num">${usd(t.annual_cents)}/yr · ${t.active_count} active</span>
      </span>
      <button class="btn primary small" data-add>Add subscription</button>
    </div>
    <p class="small muted">Each active subscription is counted in your monthly plan at its monthly cost
      (a yearly charge counts 1/12 each month). Cancel one here and your debt budget grows by that amount.</p>
    <ul class="list sub-list"></ul>
  </section>`);

  // Active subscriptions
  const ul = panel.querySelector('.sub-list');
  if (!data.active.length) {
    ul.appendChild(el('<li><span class="muted small">None yet — add one, or connect a bank and Paydown will look for them.</span></li>'));
  }
  for (const s of data.active) {
    const soon = s.days_until <= 7;
    const li = el(`<li>
      <span class="grow"><span class="name">${esc(s.name)}</span>
        <span class="pill bill">${esc(s.cycle)}</span>
        ${s.source === 'converted' ? '<span class="pill off">was a bill</span>' : ''}
        <span class="small ${soon ? '' : 'muted'}">renews ${esc(fmtDate(s.next_renewal))} (${esc(untilText(s.days_until))})</span>
        ${s.note ? `<span class="small muted">· ${esc(s.note)}</span>` : ''}
      </span>
      <span class="num">${usd(s.amount_cents)}<span class="small muted">/${CYCLE_LABEL[s.cycle]}</span>
        ${s.cycle !== 'monthly' ? `<span class="small muted"> · ${usd(s.monthly_cents)}/mo</span>` : ''}</span>
      <span class="row-actions">
        <button class="linklike" data-edit>edit</button>
        <button class="linklike" data-cancel>cancel</button>
      </span>
    </li>`);
    li.querySelector('[data-edit]').addEventListener('click', () => subDialog(s, refresh));
    li.querySelector('[data-cancel]').addEventListener('click', async () => {
      const msg = `Mark "${s.name}" cancelled? It leaves your plan and frees ${usd(s.monthly_cents)}/month. `
        + 'This only updates Paydown; cancel the real subscription with the company.';
      if (!(await confirmDialog(msg, false))) return;
      try { await api.post(`/api/subscriptions/${s.id}/cancel`, { cancelled_on: todayISO() }); }
      catch (e) { fail(e); return; }
      refresh();
    });
    ul.appendChild(li);
  }
  panel.querySelector('[data-add]').addEventListener('click', () => subDialog(null, refresh));

  // Detected in transactions — collapsed into a disclosure so it never dominates.
  if (suggestions.length) {
    panel.appendChild(suggestionsDisclosure(suggestions, refresh, fail));
  }

  // Cancelled subscriptions
  if (data.cancelled.length) {
    const det = el(`<details class="sub-disclosure">
      <summary><span>Cancelled <span class="pill off num">(${data.cancelled.length})</span></span></summary>
      <ul class="list cancelled-list"></ul>
    </details>`);
    const cul = det.querySelector('.cancelled-list');
    for (const s of data.cancelled) {
      const li = el(`<li>
        <span class="grow"><span class="name">${esc(s.name)}</span>
          <span class="pill off">cancelled${s.cancelled_on ? ` ${esc(fmtDate(s.cancelled_on))}` : ''}</span></span>
        <span class="num muted">${usd(s.amount_cents)}/${CYCLE_LABEL[s.cycle]}</span>
        <span class="row-actions">
          <button class="linklike" data-react>reactivate</button>
          <button class="linklike" data-del>delete</button>
        </span>
      </li>`);
      li.querySelector('[data-react]').addEventListener('click', async () => {
        try { await api.post(`/api/subscriptions/${s.id}/reactivate`); } catch (e) { fail(e); return; }
        refresh();
      });
      li.querySelector('[data-del]').addEventListener('click', async () => {
        if (!(await confirmDialog(`Delete "${s.name}" for good?`))) return;
        try { await api.del(`/api/subscriptions/${s.id}`); } catch (e) { fail(e); return; }
        refresh();
      });
      cul.appendChild(li);
    }
    panel.appendChild(det);
  }

  return panel;
}

/** "Found in your transactions" suggestions, collapsed by default. */
function suggestionsDisclosure(suggestions, refresh, fail) {
  const det = el(`<details class="sub-disclosure" data-suggestions>
    <summary>
      <span>Found in your transactions <span class="pill warn num">${suggestions.length}</span></span>
      <span class="small muted">Confirm recurring charges</span>
    </summary>
    <p class="small muted">Charges that repeat on a steady schedule. Confirm the ones that are real subscriptions.</p>
    <ul class="list sug-list"></ul>
  </details>`);
  const ul = det.querySelector('.sug-list');
  for (const s of suggestions) {
    const li = el(`<li>
      <span class="grow"><span class="name">${esc(s.name)}</span>
        <span class="pill bill">${esc(s.cycle)}</span>
        <span class="small muted">${s.occurrences} charges, last ${esc(fmtDate(s.last_date))}</span>
        ${s.bill_id ? `<span class="small muted">· matches your bill "${esc(s.bill_name)}"</span>` : ''}
      </span>
      <span class="num">${usd(s.amount_cents)}<span class="small muted">/${CYCLE_LABEL[s.cycle]}</span></span>
      <span class="row-actions">
        ${s.bill_id
          ? '<button class="btn small primary" data-convert>Convert bill</button>'
          : '<button class="btn small primary" data-add>Add</button>'}
        <button class="linklike" data-dismiss>dismiss</button>
      </span>
    </li>`);
    const addBtn = li.querySelector('[data-add]');
    if (addBtn) {
      addBtn.addEventListener('click', () => subDialog({
        name: s.name, amount_cents: s.amount_cents, cycle: s.cycle,
        next_date: s.next_date, match_text: s.match_text,
      }, refresh, true));
    }
    const convBtn = li.querySelector('[data-convert]');
    if (convBtn) {
      convBtn.addEventListener('click', async () => {
        const msg = `Convert the bill "${s.bill_name}" into a subscription? The bill is switched off so it isn't counted twice.`;
        if (!(await confirmDialog(msg, false))) return;
        try { await api.post(`/api/subscriptions/from-expense/${s.bill_id}`); } catch (e) { fail(e); return; }
        refresh();
      });
    }
    li.querySelector('[data-dismiss]').addEventListener('click', async () => {
      try { await api.post('/api/subscriptions/suggestions/dismiss', { key: s.key }); }
      catch (e) { fail(e); return; }
      refresh();
    });
    ul.appendChild(li);
  }
  return det;
}

/** Add/edit dialog. `isNew` forces POST even when prefilled from a suggestion. */
function subDialog(row, done, isNew = false) {
  const r = row || {};
  const creating = isNew || !row;
  openFormDialog({
    title: creating ? 'Add subscription' : `Edit ${r.name}`,
    body: `
      ${field('Name', `<input name="name" maxlength="80" required value="${esc(r.name || '')}" autocomplete="off">`)}
      ${field('Amount per charge ($)', moneyInput('amount', `value="${((r.amount_cents || 0) / 100).toFixed(2)}"`))}
      ${field('Billed', `<select name="cycle">${['weekly', 'monthly', 'quarterly', 'yearly']
        .map((c) => `<option value="${c}"${(r.cycle || 'monthly') === c ? ' selected' : ''}>${c}</option>`).join('')}</select>`)}
      ${field('Next charge', `<input type="date" name="next_date" required value="${esc(r.next_date || todayISO())}">`,
        'Any past charge date works too; Paydown rolls it forward.')}
      ${field('Bank description contains', `<input name="match_text" maxlength="80" value="${esc(r.match_text || '')}" autocomplete="off">`,
        'Optional. Keeps this out of the "found in your transactions" list.')}
      ${field('Note', `<input name="note" maxlength="200" value="${esc(r.note || '')}" autocomplete="off">`)}`,
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      const cents = parseCents(form.elements.amount.value);
      if (cents === null || cents < 0) throw new Error('Amount must be zero or more.');
      const next = form.elements.next_date.value;
      if (!next) throw new Error('Pick the next charge date.');
      const body = {
        name, amount_cents: cents, cycle: form.elements.cycle.value, next_date: next,
        match_text: form.elements.match_text.value.trim() || null,
        note: form.elements.note.value.trim() || null,
      };
      if (creating) await api.post('/api/subscriptions', body);
      else await api.put(`/api/subscriptions/${r.id}`, body);
    },
  }).then((ok) => { if (ok) done(); });
}
