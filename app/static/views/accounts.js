// Accounts view: KPI strip, compact bank sync bar, credit cards grid, cash accounts.
// Route: #/accounts (#/cards kept as an alias).

import { api, errText, currentExtra } from '../api.js';
import {
  usd, esc, el, field, moneyInput, openFormDialog, confirmDialog,
  parseCents, parseApr, errorBox, emptyState, monthName, errorBanner,
} from '../util.js';
import { sparkline } from '../charts.js';
import {
  relTime, storeLinkContext, clearLinkContext, openPlaidLink, finishLink,
  itemStatusPill,
} from '../plaid.js';

export async function accountsView(main) {
  let cards, cashAccounts;
  try {
    [cards, cashAccounts] = await Promise.all([
      api.get('/api/cards'),
      api.get('/api/cash-accounts'),
    ]);
  } catch (err) {
    main.innerHTML = '';
    errorBox(main, err);
    return;
  }
  main.innerHTML = '';
  // Split cash accounts: counted rows vs not-counted $0 accounts (collapsed, P1).
  const mainRows = cashAccounts.filter((a) => a.include_in_cash || a.balance_cents !== 0);
  const moreRows = cashAccounts.filter((a) => !a.include_in_cash && a.balance_cents === 0);

  main.appendChild(el('<div class="page-head"><h1>Accounts</h1></div>'));

  // KPI strip: total card debt, counted cash, balance-weighted APR.
  const openCards = cards.filter((c) => !c.closed);
  const debtCents = openCards.reduce((s, c) => s + (c.balance_cents || 0), 0);
  const cashCents = cashAccounts.filter((a) => a.include_in_cash)
    .reduce((s, a) => s + (a.balance_cents || 0), 0);
  const balCards = openCards.filter((c) => c.balance_cents > 0);
  const balTotal = balCards.reduce((s, c) => s + (c.balance_cents || 0), 0);
  const weightedApr = balTotal > 0
    ? balCards.reduce((s, c) => s + (c.balance_cents || 0) * Number(c.apr || 0), 0) / balTotal
    : 0;
  main.appendChild(el(`<section class="tiles">
    <div class="tile"><span class="t-label">Total card debt</span><span class="t-value num">${usd(debtCents)}</span><span class="t-note">${openCards.length} open card${openCards.length === 1 ? '' : 's'}</span></div>
    <div class="tile"><span class="t-label">Counted cash</span><span class="t-value num">${usd(cashCents)}</span><span class="t-note">${mainRows.length} active account${mainRows.length === 1 ? '' : 's'}</span></div>
    <div class="tile"><span class="t-label">Weighted APR</span><span class="t-value num">${weightedApr.toFixed(1)}%</span><span class="t-note">across open balances</span></div>
  </section>`));

  // Bank sync panel (hidden entirely when Plaid is not configured / unavailable).
  await plaidPanel(main, cards, cashAccounts);

  // Payoff months under the current strategy (best-effort; view still works without it).
  let payoffById = new Map();
  if (cards.length) {
    try {
      const [settings, budgetBundle] = await Promise.all([
        api.get('/api/settings'),
        api.get(`/api/month/${todayMonth()}`),
      ]);
      const proj = await api.get(`/api/projection?extra=${currentExtra(settings, budgetBundle.budget)}` +
        `&strategy=${encodeURIComponent(settings.strategy)}&lump=0`);
      const sim = proj[settings.strategy] || proj.avalanche;
      for (const c of (sim && sim.cards) || []) payoffById.set(c.id, c);
    } catch { /* projection is decorative here */ }
  }

  // Credit cards section (primary focus — above cash accounts, 2-column grid on desktop).
  const panel = el('<section class="panel"><div class="section-head"><h2>Credit cards</h2>' +
    '<button class="btn primary small" data-add>Add card</button></div><div class="card-list cards-grid"></div></section>');
  main.appendChild(panel);
  const list = panel.querySelector('.card-list');
  if (!cards.length) {
    list.appendChild(el('<p class="small muted">No credit cards yet. <a href="#" data-add-card-here>Add one</a> — or keep going without them; cash accounts work on their own.</p>'));
    list.querySelector('[data-add-card-here]').addEventListener('click', (ev) => {
      ev.preventDefault();
      cardForm(null, refresh);
    });
  }

  // Fetch every card's history in parallel (P1), then render.
  const historyById = new Map();
  if (cards.length) {
    const settled = await Promise.allSettled(cards.map((c) => api.get(`/api/cards/${c.id}/history`)));
    cards.forEach((c, i) => {
      if (settled[i].status === 'fulfilled') historyById.set(c.id, settled[i].value);
    });
  }

  for (const card of cards) {
    const history = historyById.get(card.id) || [];
    const util = card.utilization_pct;
    const info = payoffById.get(card.id) || null;
    let payoffPill = '';
    if (card.balance_cents === 0) {
      payoffPill = '<span class="pill spending">Paid off</span>';
    } else if (info && info.never) {
      payoffPill = '<span class="pill bad">Not on track</span>';
    } else if (info && info.payoff_month) {
      payoffPill = `<span class="pill good">Payoff ${esc(monthName(info.payoff_month))}</span>`;
    }
    const prog = card.progress_pct;
    const progHtml = (prog !== null && prog !== undefined)
      ? `<div class="prog-row">
           <progress max="100" value="${Math.max(0, Math.min(100, Number(prog)))}" aria-label="Percent paid since added for ${esc(card.name)}"></progress>
           <span class="small muted">${Number(prog).toFixed(0)}% paid since added</span>
         </div>`
      : '';
    const syncHtml = card.sync === 'plaid'
      ? `<div class="sync-line small muted">Synced · ${esc(syncedId(card))} · ${esc(relTime(card.last_synced_at))}</div>`
      : '';
    const dueHtml = card.next_due_date
      ? `<span><span class="k">Due</span> ${esc(dueDateLabel(card.next_due_date))}</span>`
      : (card.due_day ? `<span><span class="k">Due day</span> ${card.due_day}</span>` : '');
    const item = el(`<div class="card-item">
      <div class="card-head">
        <span class="cname">${esc(card.name)}</span>
        ${/^(credit card|card)$/i.test(card.name) ? '<button class="linklike rename-link" data-rename>Rename</button>' : ''}
        ${card.closed ? '<span class="pill off">closed</span>' : ''}
        ${card.bank_overdue ? '<span class="pill bad">Overdue</span>' : ''}
        ${payoffPill}
      </div>
      ${syncHtml}
      <div class="card-stats">
        <span><span class="k">Balance</span> <strong>${usd(card.balance_cents)}</strong></span>
        <span><span class="k">APR</span> ${Number(card.apr).toFixed(2)}% ${aprPill(card)}</span>
        <span><span class="k">Min</span> ${usd(card.min_payment_cents)} ${minPill(card)}</span>
        ${card.statement_balance_cents != null ? `<span><span class="k">Statement</span> ${usd(card.statement_balance_cents)}</span>` : ''}
        ${card.credit_limit_cents ? `<span><span class="k">Utilization</span> ${util === null || util === undefined ? '—' : `${Number(util).toFixed(1)}%`}</span>` : ''}
        ${dueHtml}
      </div>
      ${progHtml}
      <div class="spark-wrap"></div>
      <div class="card-actions">
        <button class="btn small" data-edit>Edit</button>
        ${card.sync === 'plaid' ? '' : '<button class="btn small" data-balance>Update balance</button>'}
        <button class="btn small danger" data-delete>Delete</button>
      </div>
    </div>`);
    item.querySelector('.spark-wrap').innerHTML = sparkline(history);
    item.querySelector('[data-edit]').addEventListener('click', () => cardForm(card, refresh));
    const renameBtn = item.querySelector('[data-rename]');
    if (renameBtn) renameBtn.addEventListener('click', () => cardForm(card, refresh));
    const balBtn = item.querySelector('[data-balance]');
    if (balBtn) balBtn.addEventListener('click', () => balanceForm(card, refresh));
    item.querySelector('[data-delete]').addEventListener('click', async () => {
      if (!(await confirmDialog(`Delete "${card.name}" and its payment history? This cannot be undone.`))) return;
      try { await api.del(`/api/cards/${card.id}`); } catch (e) { errorBanner(panel, errText(e)); return; }
      refresh();
    });
    list.appendChild(item);
  }

  panel.querySelector('[data-add]').addEventListener('click', () => cardForm(null, refresh));

  // Cash accounts section
  const cashPanel = el(`<section class="panel">
    <div class="section-head"><h2>Cash accounts</h2>
      <button class="btn primary small" data-add-cash>Add cash account</button></div>
    <p class="small muted">Checking and savings. Balances here show how much spare cash you have in <a href="#/month">Month</a>.</p>
    <div class="cash-list"></div>
  </section>`);
  main.appendChild(cashPanel);
  const cashList = cashPanel.querySelector('.cash-list');

  function cashRow(acct, compact = false) {
    const kindPill = `<span class="pill ${acct.kind === 'savings' ? 'spending' : 'bill'}">${esc(acct.kind)}</span>`;
    const notCounted = acct.include_in_cash ? '' : '<span class="pill off">Not counted</span>';
    const who = syncedId(acct);
    const row = el(`<div class="cash-row">
      <span class="cash-name">${esc(acct.name)}</span>
      ${kindPill}${notCounted}
      <span class="cash-meta">${esc(who)}</span>
      <span class="cash-balance num"><strong>${usd(acct.balance_cents)}</strong></span>
      <span class="cash-actions">
        <button class="btn small" data-edit>Edit</button>
        <button class="btn small danger" data-delete>Delete</button>
      </span>
    </div>`);
    row.querySelector('[data-edit]').addEventListener('click', () => cashForm(acct, refresh));
    row.querySelector('[data-delete]').addEventListener('click', async () => {
      if (!(await confirmDialog(`Delete "${acct.name}"? Its history with the bank sync is removed. This cannot be undone.`))) return;
      try { await api.del(`/api/cash-accounts/${acct.id}`); } catch (e) { errorBanner(cashPanel, errText(e)); return; }
      refresh();
    });
    return row;
  }

  if (!cashAccounts.length) {
    cashList.appendChild(el('<p class="small muted">No cash accounts yet. Add your checking account to see spare cash — savings are optional.</p>'));
  } else {
    if (mainRows.length) {
      const box = el('<div class="cash-rows"></div>');
      for (const acct of mainRows) box.appendChild(cashRow(acct));
      cashList.appendChild(box);
    }
    if (moreRows.length) {
      const det = el(`<details class="more-accounts"><summary>${moreRows.length} more account${moreRows.length === 1 ? '' : 's'} not counted</summary><div class="cash-rows"></div></details>`);
      const box = det.querySelector('.cash-rows');
      for (const acct of moreRows) box.appendChild(cashRow(acct, true));
      cashList.appendChild(det);
    }
  }
  cashPanel.querySelector('[data-add-cash]').addEventListener('click', () => cashForm(null, refresh));

  function refresh() { window.dispatchEvent(new HashChangeEvent('hashchange')); }
}

function todayMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}


/** "Chase ••4321" for a synced account (either part may be missing). */
function syncedId(acct) {
  return [acct.institution_name, acct.mask ? `••${acct.mask}` : ''].filter(Boolean).join(' ');
}

/* ---------- Cash account dialogs ---------- */

const CASH_KINDS = [
  ['checking', 'Checking'],
  ['savings', 'Savings'],
  ['other', 'Other'],
];

function cashForm(acct, done) {
  const a = acct || {};
  const synced = a.sync === 'plaid';
  const dis = synced ? 'disabled' : '';
  const syncHint = 'Updated from your bank daily.';
  const promise = openFormDialog({
    title: acct ? `Edit ${a.name}` : 'Add cash account',
    body: `
      ${field('Name', `<input name="name" maxlength="80" required value="${esc(a.name || '')}" autocomplete="off">`)}
      ${field('Kind', `<select name="kind">${CASH_KINDS.map(([v, l]) =>
        `<option value="${v}"${a.kind === v || (!acct && v === 'checking') ? ' selected' : ''}>${l}</option>`).join('')}</select>`)}
      ${field('Current balance ($)', moneyInput('balance', `value="${((a.balance_cents || 0) / 100).toFixed(2)}" ${dis}`), synced ? syncHint : 'Overdrawn? Enter a negative amount.')}
      <label class="field"><span class="lbl">Count in cash</span>
        <input type="checkbox" name="include"${a.include_in_cash === undefined || a.include_in_cash ? ' checked' : ''}>
        <span class="hint">Uncheck for savings you don’t want swept into card payments.</span>
      </label>
      ${synced ? '<button type="button" class="btn danger" data-unlink>Unlink from bank</button>' : ''}
    `,
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      const body = { name, kind: form.elements.kind.value, include_in_cash: form.elements.include.checked };
      if (!synced) {
        const balance = parseCents(form.elements.balance.value);
        if (balance === null) throw new Error('Balance is required.');
        body.balance_cents = balance;
      }
      try {
        if (acct) await api.put(`/api/cash-accounts/${acct.id}`, body);
        else await api.post('/api/cash-accounts', body);
      } catch (e) {
        throw new Error(errText(e));
      }
    },
  });
  if (synced) {
    // openFormDialog appends the dialog synchronously, so it is in the DOM now.
    const dlg = [...document.querySelectorAll('dialog.dlg')].pop();
    const btn = dlg && dlg.querySelector('[data-unlink]');
    if (btn) btn.addEventListener('click', async () => {
      const errEl = dlg.querySelector('.form-err');
      const showErr = (msg) => { errEl.textContent = msg; errEl.hidden = false; };
      if (!(await confirmDialog(`Unlink “${a.name}” from your bank? Its balance will stop updating automatically.`))) return;
      try {
        const accounts = await api.get('/api/plaid/accounts');
        const link = (accounts || []).find((x) => x.cash_account_id === a.id);
        if (link) await api.post(`/api/plaid/accounts/${link.id}/unlink`);
        else await api.put(`/api/cash-accounts/${a.id}`, { sync: 'manual' });
      } catch (e) {
        showErr(errText(e));
        return;
      }
      dlg.close();
      done();
    });
  }
  promise.then((ok) => { if (ok) done(); });
}

/** 'YYYY-MM-DD' -> { mon: 'Oct', day: 15 } without Date timezone surprises. */
const MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function dueDateLabel(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  return `${MONTH_ABBR[Number(m[2]) - 1]} ${Number(m[3])}`;
}

/** APR pill per apr_effective_source ('' when bank/manual — plain display). */
function aprPill(card) {
  switch (card.apr_effective_source) {
    case 'inferred': return '<span class="pill warn">est. from interest charges</span>';
    case 'unknown': return '<span class="pill warn">APR unknown</span>';
    case 'override': return '<span class="pill bill">your APR</span>';
    default: return '';
  }
}

/** Minimum pill per min_source. */
function minPill(card) {
  if (card.min_source === 'estimated') return '<span class="pill warn">estimated</span>';
  if (card.min_source === 'override') return '<span class="pill bill">your min</span>';
  return '';
}

/* ---------- Card dialogs ---------- */

function cardForm(card, done) {
  const c = card || {};
  const synced = c.sync === 'plaid';
  const dis = synced ? 'disabled' : '';
  const syncHint = 'Updated from your bank daily.';
  // Synced cards: APR / minimum become editable overrides (blank = use the bank).
  const bankAprHint = synced
    ? (c.apr_source === 'unknown'
        ? (c.apr_inferred != null
            ? `Bank reports no APR (estimated ${Number(c.apr_inferred).toFixed(2)}% from interest charges).`
            : 'Bank reports no APR.')
        : `Bank reports ${Number(c.bank_apr).toFixed(2)}%.`)
    : '';
  const bankMinHint = synced ? `Bank reports ${usd(c.bank_min_payment_cents)}.` : '';
  const promise = openFormDialog({
    title: card ? `Edit ${c.name}` : 'Add card',
    body: `
      ${field('Name', `<input name="name" maxlength="80" required value="${esc(c.name || '')}" autocomplete="off">`)}
      ${field('Current balance ($)', moneyInput('balance', `value="${((c.balance_cents || 0) / 100).toFixed(2)}" ${dis}`), synced ? syncHint : 'Statement balance. Updating it later is logged in the card’s history.')}
      ${synced
        ? field('APR override (%)', `<input class="num-input" name="apr_override" inputmode="decimal" value="${c.apr_override ?? ''}" autocomplete="off" placeholder="blank = bank">`, bankAprHint)
        : field('APR (%)', `<input class="num-input" name="apr" inputmode="decimal" value="${c.apr ?? ''}" autocomplete="off">`, 'e.g. 24.99')}
      ${synced
        ? field('Minimum override ($)', moneyInput('min_override', `value="${c.min_override_cents == null ? '' : (c.min_override_cents / 100).toFixed(2)}"`), bankMinHint)
        : field('Minimum payment ($)', moneyInput('min_payment', `value="${((c.min_payment_cents || 0) / 100).toFixed(2)}" ${dis}`), '')}
      ${field('Credit limit ($, optional)', moneyInput('credit_limit', `value="${c.credit_limit_cents == null ? '' : (c.credit_limit_cents / 100).toFixed(2)}" ${dis}`), synced ? syncHint : 'For utilization.')}
      ${field('Due day (optional)', `<input class="num-input" name="due_day" inputmode="numeric" value="${c.due_day ?? ''}" autocomplete="off">`, '1–31')}
      ${field('Priority (custom order)', '<input class="num-input" name="priority" inputmode="numeric" value="' + (c.priority ?? 0) + '">', 'Lower is paid first under the Custom strategy.')}
      ${card ? '<label class="field"><span class="lbl">Closed</span><input type="checkbox" name="closed"' + (c.closed ? ' checked' : '') + '><span class="hint">Closed / paid-off cards are hidden from the plan.</span></label>' : ''}
      ${synced ? '<button type="button" class="btn danger" data-unlink>Unlink from bank</button>' : ''}
    `,
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      const body = { name };
      if (!synced) {
        const balance = parseCents(form.elements.balance.value);
        if (balance === null || balance < 0) throw new Error('Balance must be zero or more.');
        const apr = parseApr(form.elements.apr.value);
        if (apr === null) throw new Error('APR is required.');
        const minPay = parseCents(form.elements.min_payment.value);
        if (minPay === null || minPay < 0) throw new Error('Minimum payment must be zero or more.');
        const limit = parseCents(form.elements.credit_limit.value);
        if (limit !== null && limit < 0) throw new Error('Credit limit must be zero or more.');
        body.balance_cents = balance;
        body.apr = apr;
        body.min_payment_cents = minPay;
        body.credit_limit_cents = limit;
      } else {
        const aprOv = parseApr(form.elements.apr_override.value);
        if (aprOv !== null && aprOv < 0) throw new Error('APR override must be zero or more.');
        const minOv = parseCents(form.elements.min_override.value);
        if (minOv !== null && minOv < 0) throw new Error('Minimum override must be zero or more.');
        body.apr_override = aprOv; // number, or null when blank (= use the bank)
        body.min_override_cents = minOv;
      }
      const due = form.elements.due_day.value.trim();
      if (due !== '' && !(Number(due) >= 1 && Number(due) <= 31 && Number.isInteger(Number(due)))) {
        throw new Error('Due day must be between 1 and 31.');
      }
      const prio = form.elements.priority.value.trim() === '' ? 0 : Number(form.elements.priority.value);
      if (!Number.isInteger(prio) || prio < 0) throw new Error('Priority must be a non-negative whole number.');
      body.due_day = due === '' ? null : Number(due);
      body.priority = prio;
      if (card) body.closed = form.elements.closed.checked;
      try {
        if (card) await api.put(`/api/cards/${card.id}`, body);
        else await api.post('/api/cards', body);
      } catch (e) {
        throw new Error(errText(e));
      }
    },
  });
  if (synced) {
    // openFormDialog appends the dialog synchronously, so it is in the DOM now.
    const dlg = [...document.querySelectorAll('dialog.dlg')].pop();
    const btn = dlg && dlg.querySelector('[data-unlink]');
    if (btn) btn.addEventListener('click', async () => {
      const errEl = dlg.querySelector('.form-err');
      const showErr = (msg) => { errEl.textContent = msg; errEl.hidden = false; };
      if (!(await confirmDialog(`Unlink “${c.name}” from your bank? Its balance will stop updating automatically.`))) return;
      try {
        const accounts = await api.get('/api/plaid/accounts');
        const acct = (accounts || []).find((a) => a.card_id === c.id);
        if (acct) await api.post(`/api/plaid/accounts/${acct.id}/unlink`);
        else await api.put(`/api/cards/${c.id}`, { sync: 'manual' });
      } catch (e) {
        showErr(errText(e));
        return;
      }
      dlg.close();
      done();
    });
  }
  promise.then((ok) => { if (ok) done(); });
}

function balanceForm(card, done) {
  openFormDialog({
    title: `Update balance — ${card.name}`,
    submitLabel: 'Update',
    body: field('New statement balance ($)', moneyInput('balance', `value="${(card.balance_cents / 100).toFixed(2)}"`), 'Recorded in this card’s balance history.'),
    onSubmit: async (form) => {
      const cents = parseCents(form.elements.balance.value);
      if (cents === null || cents < 0) throw new Error('Balance must be zero or more.');
      try { await api.put(`/api/cards/${card.id}`, { balance_cents: cents }); }
      catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) done(); });
}

/* ---------- Bank sync panel ---------- */

/** Compact bank sync bar. Renders nothing when Plaid is not configured or unavailable. */
async function plaidPanel(main, cards, cashAccounts) {
  let status = null;
  let accounts = [];
  try {
    status = await api.get('/api/plaid/status');
    if (!status || !status.configured) return;
    accounts = await api.get('/api/plaid/accounts');
  } catch { return; } // no Plaid routes yet — the app must keep working without it

  const items = (status && status.items) || [];
  const unlinked = (accounts || []).filter((a) => !a.card_id && !a.cash_account_id && !a.ignored);
  const ignored = (accounts || []).filter((a) => a.ignored);
  const needsAttention = items.some((i) => i.status !== 'ok') || unlinked.length > 0;
  const latestSync = items.reduce((latest, i) => {
    if (!i.last_synced_at) return latest;
    return !latest || i.last_synced_at > latest ? i.last_synced_at : latest;
  }, null);

  const panel = el(`<section class="panel sync-panel">
    <div class="section-head">
      <h2>Bank sync</h2>
      ${items.length ? `<span class="pill good">${items.length} bank${items.length === 1 ? '' : 's'} connected</span>` : ''}
      ${latestSync ? `<span class="small muted">last synced ${esc(relTime(latestSync))}</span>` : ''}
      <span class="btn-row">
        <button class="btn small" data-sync>Sync now</button>
        <button class="btn small primary" data-connect>Connect a bank</button>
      </span>
    </div>
    <details class="sub-disclosure"${needsAttention ? ' open' : ''}>
      <summary>Connected banks &amp; ignored accounts (${items.length} bank${items.length === 1 ? '' : 's'}${ignored.length ? `, ${ignored.length} ignored` : ''})</summary>
      <div class="plaid-items"></div>
      <p class="form-err" hidden role="alert" data-err></p>
      <div class="plaid-results small" hidden></div>
      <div class="plaid-accounts"></div>
    </details>
  </section>`);
  main.appendChild(panel);

  const itemsBox = panel.querySelector('.plaid-items');
  const acctsBox = panel.querySelector('.plaid-accounts');
  const errEl = panel.querySelector('[data-err]');
  const resultsEl = panel.querySelector('.plaid-results');
  const connectBtn = panel.querySelector('[data-connect]');
  const syncBtn = panel.querySelector('[data-sync]');

  function showErr(msg) { errEl.textContent = msg; errEl.hidden = false; }
  function setBusy(b) { connectBtn.disabled = b; syncBtn.disabled = b; }

  function renderItems() {
    itemsBox.innerHTML = '';
    const items = (status && status.items) || [];
    if (!items.length) {
      itemsBox.appendChild(el('<p class="small muted">No banks connected yet.</p>'));
      return;
    }
    for (const item of items) {
      const row = el(`<div class="plaid-item">
        <span class="iname">${esc(item.institution_name || 'Bank')}</span>
        ${itemStatusPill(item)}
        <span class="when small muted">synced ${esc(relTime(item.last_synced_at))}</span>
        ${item.status === 'login_required' ? '<button class="btn small warn" data-reconnect>Reconnect</button>' : ''}
        <button class="linklike" data-remove title="Revoke this bank connection">Remove</button>
      </div>`);
      const rbtn = row.querySelector('[data-reconnect]');
      if (rbtn) rbtn.addEventListener('click', () => openLinkFlow(`update:${item.id}`));
      row.querySelector('[data-remove]').addEventListener('click', async () => {
        const name = item.institution_name || 'this bank';
        if (!(await confirmDialog(
          `Remove ${name} from Paydown? This revokes Paydown's access at the bank and deletes its ` +
          'synced accounts and transactions here. Cards you added from it stay, as manual entries.',
        ))) return;
        try { await api.del(`/api/plaid/items/${item.id}`); }
        catch (e) { showErr(errText(e)); return; }
        refresh();
      });
      itemsBox.appendChild(row);
    }
  }

  function acctLine(a) {
    return `${esc(a.name)}${a.mask ? ` ••${esc(a.mask)}` : ''} · <span class="num">${usd(a.last_balance_cents)}</span>`;
  }

  function linkRow(a, kind, optsHtml, linkBody) {
    const row = el(`<div class="plaid-acct">
      <span class="acct-line">${acctLine(a)}</span>
      <label class="field acct-pick"><span class="lbl">${esc(kind === 'credit' ? 'Card' : 'Cash account')}</span>
        <select data-pick>
          <option value="create">${kind === 'credit' ? 'Create new card' : 'Create new cash account'}</option>
          ${optsHtml}
        </select>
      </label>
      <button class="btn small primary" data-link>Link</button>
      <button class="btn small" data-ignore>Ignore</button>
    </div>`);
    const sel = row.querySelector('[data-pick]');
    const hasOpts = !!optsHtml;
    if (!hasOpts) sel.hidden = true;
    row.querySelector('[data-link]').addEventListener('click', async () => {
      setBusy(true);
      errEl.hidden = true;
      try {
        const body = sel.value === 'create' ? { create: true } : linkBody(Number(sel.value));
        await api.post(`/api/plaid/accounts/${a.id}/link`, body);
        refresh();
      } catch (e) { showErr(errText(e)); setBusy(false); }
    });
    row.querySelector('[data-ignore]').addEventListener('click', async () => {
      setBusy(true);
      errEl.hidden = true;
      try {
        await api.post(`/api/plaid/accounts/${a.id}/ignore`, { ignored: true });
        await reloadPanel();
      } catch (e) { showErr(errText(e)); } finally { setBusy(false); }
    });
    return row;
  }

  function renderAccounts() {
    acctsBox.innerHTML = '';
    const list = accounts || [];
    const unlinked = list.filter((a) => !a.card_id && !a.cash_account_id && !a.ignored);
    const ignored = list.filter((a) => a.ignored);
    const manualCards = cards.filter((c) => c.sync !== 'plaid');
    const manualCash = (cashAccounts || []).filter((c) => c.sync !== 'plaid');
    if (!unlinked.length && !ignored.length) return;

    // Grouped by type: credit accounts link to cards, depository to cash accounts.
    const credit = unlinked.filter((a) => a.type !== 'depository');
    const depository = unlinked.filter((a) => a.type === 'depository');
    if (credit.length) {
      acctsBox.appendChild(el('<h3>Cards</h3>'));
      const opts = manualCards.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
      for (const a of credit) acctsBox.appendChild(linkRow(a, 'credit', opts, (id) => ({ card_id: id })));
    }
    if (depository.length) {
      acctsBox.appendChild(el('<h3>Cash accounts</h3>'));
      const opts = manualCash.map((c) => `<option value="${c.id}">${esc(c.name)}</option>`).join('');
      for (const a of depository) acctsBox.appendChild(linkRow(a, 'depository', opts, (id) => ({ cash_account_id: id })));
    }
    if (ignored.length) {
      const det = el(`<details class="ignored-box"><summary>Ignored (${ignored.length})</summary><div class="ignored-list"></div></details>`);
      const listBox = det.querySelector('.ignored-list');
      for (const a of ignored) {
        const row = el(`<div class="plaid-acct">
          <span class="acct-line">${esc(a.name)}${a.mask ? ` ••${esc(a.mask)}` : ''}</span>
          <button class="btn small" data-unignore>Unignore</button>
        </div>`);
        row.querySelector('[data-unignore]').addEventListener('click', async () => {
          setBusy(true);
          errEl.hidden = true;
          try {
            await api.post(`/api/plaid/accounts/${a.id}/ignore`, { ignored: false });
            await reloadPanel();
          } catch (e) { showErr(errText(e)); } finally { setBusy(false); }
        });
        listBox.appendChild(row);
      }
      acctsBox.appendChild(det);
    }
  }

  // Refetch status + accounts and re-render in place (keeps panel state/results).
  async function reloadPanel() {
    try {
      status = await api.get('/api/plaid/status');
      accounts = await api.get('/api/plaid/accounts');
    } catch (e) {
      showErr(errText(e));
      return;
    }
    renderItems();
    renderAccounts();
  }

  async function openLinkFlow(mode) {
    errEl.hidden = true;
    setBusy(true);
    try {
      const body = mode === 'new' ? {} : { item_id: Number(mode.slice('update:'.length)) };
      const lt = await api.post('/api/plaid/link-token', body);
      storeLinkContext(lt.link_token, mode);
      const res = await openPlaidLink(mode);
      await finishLink(mode, res);
      clearLinkContext();
      refresh();
    } catch (e) {
      showErr(errText(e));
      setBusy(false);
    }
  }

  connectBtn.addEventListener('click', () => openLinkFlow('new'));

  syncBtn.addEventListener('click', async () => {
    errEl.hidden = true;
    resultsEl.hidden = true;
    setBusy(true);
    try {
      const res = await api.post('/api/plaid/sync');
      const lines = (res.items || []).map((i) =>
        `${i.institution_name || 'Bank'}: ${i.status === 'ok' ? 'ok' : i.status}` +
        `${i.error_code ? ` (${i.error_code})` : ''} · ` +
        `${i.accounts_updated} account${i.accounts_updated === 1 ? '' : 's'} updated`);
      resultsEl.textContent = `Synced — ${lines.join(' · ')}`; // textContent: Plaid strings need no further escaping
      resultsEl.hidden = false;
      await reloadPanel();
    } catch (e) {
      showErr(errText(e));
    } finally {
      setBusy(false);
    }
  });

  renderItems();
  renderAccounts();

  function refresh() { window.dispatchEvent(new HashChangeEvent('hashchange')); }
}
