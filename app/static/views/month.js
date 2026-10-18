// Month view: summary strip, shortfall banner, this-month action card,
// adjustments, debt-free summary. Route: #/month/YYYY-MM

import { api, errText } from '../api.js';
import {
  usd, usd0, esc, monthAdd, monthName, todayISO, el, field, moneyInput,
  openFormDialog, confirmDialog, parseCents, errorBox, emptyState,
} from '../util.js';

/** 'YYYY-MM-DD' -> 'Oct 15' (month abbreviation + day, no timezone surprises). */
const MONTH_ABBR = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function dueDateLabel(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(iso || ''));
  if (!m) return null;
  return `${MONTH_ABBR[Number(m[2]) - 1]} ${Number(m[3])}`;
}

export async function monthView(main, month) {
  let bundle, payments = [], adjustments = [], trend = null;
  try {
    [bundle, payments, adjustments, trend] = await Promise.all([
      api.get(`/api/month/${month}`),
      api.get(`/api/payments?month=${month}`),
      api.get(`/api/adjustments?month=${month}`),
      api.get('/api/insights/debt-trend?months=6').catch(() => null),
    ]);
  } catch (err) {
    main.innerHTML = '';
    if (err && err.status === 401) {
      main.appendChild(el('<div class="banner error" role="alert">Not signed in — reload and enter your credentials.</div>'));
      return;
    }
    errorBox(main, err);
    return;
  }
  main.innerHTML = '';

  const b = bundle.budget || {};
  const v = (k) => (b[k] === null || b[k] === undefined ? 0 : b[k]);
  const settings = bundle.settings || {};

  // Empty state on first load
  const cards = bundle.cards || [];
  if (!cards.length && v('income_total_cents') === 0 && (v('bills_total_cents') + v('spending_total_cents') + v('subscriptions_total_cents')) === 0) {
    const isCur0 = month !== todayISO().slice(0, 7);
    const head = el(`<div class="month-head">
      <a class="month-arrow" href="#/month/${monthAdd(month, -1)}" aria-label="Previous month">‹</a>
      <h1>${esc(monthName(month))}</h1>
      <a class="month-arrow" href="#/month/${monthAdd(month, 1)}" aria-label="Next month">›</a>
      ${isCur0 ? `<a class="btn small ghost" href="#/month/${todayISO().slice(0, 7)}">Current month</a>` : ''}
    </div>`);
    main.appendChild(head);
    emptyState(main);
    return;
  }

  // Summary strip
  const isCurrent = month === todayISO().slice(0, 7);
  const tiles = el(`<div>
    <div class="month-head">
      <a class="month-arrow" href="#/month/${monthAdd(month, -1)}" aria-label="Previous month">‹</a>
      <h1>${esc(monthName(month))}</h1>
      <a class="month-arrow" href="#/month/${monthAdd(month, 1)}" aria-label="Next month">›</a>
      ${!isCurrent ? `<a class="btn small ghost" href="#/month/${todayISO().slice(0, 7)}">Current month</a>` : ''}
    </div>
    <div class="tiles">
      <div class="tile"><span class="t-label">Income</span><span class="t-value">${usd(v('income_total_cents'))}</span></div>
      <div class="tile"><span class="t-label">Committed</span><span class="t-value">${usd(v('bills_total_cents') + v('spending_total_cents') + v('subscriptions_total_cents') + v('minimums_total_cents'))}</span><span class="t-note">bills ${usd0(v('bills_total_cents'))} · spend ${usd0(v('spending_total_cents'))} · subs ${usd0(v('subscriptions_total_cents'))} · min ${usd0(v('minimums_total_cents'))}</span></div>
      <div class="tile"><span class="t-label">Extra to cards</span><span class="t-value">${usd(v('extra_cents'))}</span></div>
      ${bundle.cash ? `<div class="tile"><span class="t-label">Cash</span><span class="t-value">${usd(bundle.cash.cash_total_cents)}</span></div>` : ''}
    </div>
  </div>`);
  main.appendChild(tiles);

  if (v('shortfall_cents') > 0) {
    main.appendChild(el(
      `<div class="banner error" role="alert">⚠ Minimums exceed what’s left by ${usd(v('shortfall_cents'))}. Free up money in <a href="#/budget">Budget</a> or reduce card minimums.</div>`));
  }

  // Cash panel (spare cash in checking & savings), under the summary tiles.
  const cash = bundle.cash || null;
  if (cash) main.appendChild(cashPanel(cash, month, refresh));

  // This month action card
  const plan = bundle.plan || [];
  const paidTotal = bundle.paid_total_cents || 0;
  const recTotal = bundle.recommended_total_cents || 0;
  const totalPct = recTotal > 0 ? Math.min(100, Math.round((paidTotal / recTotal) * 100)) : (paidTotal > 0 ? 100 : 0);
  const planDone = paidTotal >= recTotal && recTotal > 0;
  const planPanel = el(`<section class="panel">
    <div class="section-head">
      <h2>This month’s plan</h2>
      <span class="pill ${planDone ? 'good' : 'bill'} num">Paid ${usd(paidTotal)} of ${usd(recTotal)}</span>
    </div>
    <progress max="100" value="${totalPct}" aria-label="Total monthly plan progress"></progress>
  </section>`);
  main.appendChild(planPanel);
  if (!plan.length) {
    planPanel.appendChild(el('<p class="muted">No open card balances to pay down. 🎉</p>'));
  } else {
    const paysByCard = new Map();
    for (const p of payments) {
      if (!paysByCard.has(p.card_id)) paysByCard.set(p.card_id, []);
      paysByCard.get(p.card_id).push(p);
    }
    const nothingRows = [];
    for (const row of plan) {
      if (!row.recommended_cents && !row.paid_cents) { nothingRows.push(row); continue; }
      const paid = row.paid_cents || 0;
      const remaining = row.remaining_cents !== undefined ? row.remaining_cents : Math.max(0, row.recommended_cents - paid);
      const pct = row.recommended_cents > 0 ? Math.min(100, Math.round((paid / row.recommended_cents) * 100)) : 100;
      const pays = paysByCard.get(row.card_id) || [];
      const card = cards.find((c) => c.id === row.card_id) || null;
      const synced = !!card && card.sync === 'plaid';
      const dueHtml = row.next_due_date
        ? `<span class="pill off">due ${esc(dueDateLabel(row.next_due_date))}</span>`
        : (row.due_day ? `<span class="pill off">due day ${row.due_day}</span>` : '');
      const done = remaining === 0 && paid > 0;
      const li = el(`<div class="plan-row">
        <div class="plan-top">
          <span class="cname">${esc(row.name)}</span>
          ${done ? '<span class="pill good">Done</span>' : ''}
          ${synced ? '<span class="pill off">synced</span>' : ''}
          ${dueHtml}
          <button class="btn ${done ? '' : 'primary'} small" data-log>Log payment</button>
        </div>
        <div class="plan-nums">
          <span><span class="k">Recommended</span> <strong class="num">${usd(row.recommended_cents)}</strong></span>
          ${row.extra_cents > 0 ? `<span class="pill extra">extra ${usd(row.extra_cents)}</span>` : ''}
          <span><span class="k">Paid</span> <span class="num">${usd(paid)}</span></span>
          <span><span class="k">Remaining</span> <span class="num">${usd(remaining)}</span></span>
        </div>
        <progress max="100" value="${pct}" aria-label="Paid progress for ${esc(row.name)}"></progress>
        <ul class="pay-list"></ul>
      </div>`);
      let payList = li.querySelector('.pay-list');
      if (pays.length > 2) {
        // Long histories (auto-logged bank payments) collapse behind a summary.
        const det = el(`<details class="pay-more"><summary class="small muted">${pays.length} payments</summary><ul class="pay-list"></ul></details>`);
        payList.replaceWith(det);
        payList = det.querySelector('.pay-list');
      }
      for (const p of pays) {
        const bankNote = p.transaction_id && p.note === 'From your bank';
        const item = el(`<li><span class="amt num">${usd(p.amount_cents)}</span>
          <span>paid ${esc(p.paid_on)}${p.note && !bankNote ? ` — ${esc(p.note)}` : ''}${p.transaction_id ? ' <span class="from-bank">from bank</span>' : ''}</span>
          <span class="row-actions"><button class="linklike" data-del>delete</button></span></li>`);
        item.querySelector('[data-del]').addEventListener('click', async () => {
          if (!(await confirmDialog(
            p.transaction_id
              ? `Delete this ${usd(p.amount_cents)} bank payment? It won’t be logged again automatically.`
              : synced
                ? `Delete this ${usd(p.amount_cents)} payment? The card’s balance won’t change — it updates when the payment posts to your bank.`
                : `Delete this ${usd(p.amount_cents)} payment? The card balance will be adjusted back up.`,
          ))) return;
          try { await api.del(`/api/payments/${p.id}`); } catch (e) { alert_(planPanel, errText(e)); return; }
          rerun();
        });
        payList.appendChild(item);
      }
      li.querySelector('[data-log]').addEventListener('click', () => logPayment(row, remaining, month, refresh, synced));
      planPanel.appendChild(li);
    }
    if (nothingRows.length) {
      const line = el(`<p class="small muted plan-nothing">Nothing recommended this month: ${nothingRows.map((r) => esc(r.name)).join(', ')}.</p>`);
      const firstRow = planPanel.querySelector('.plan-row');
      if (firstRow) planPanel.insertBefore(line, firstRow);
      else planPanel.appendChild(line);
    }
  }

  // Reality check (Round 3 R1): month-by-month card debt trend, after "This month".
  const rc = realityCheckPanel(trend);
  if (rc) planPanel.after(rc);

  // One-off adjustments: collapsed when empty so an unused form doesn't clutter the page.
  const adjSummary = adjustments.length
    ? `<summary><strong>One-off adjustments (${adjustments.length})</strong></summary>`
    : `<summary><strong>One-off adjustments</strong> <span class="small muted">(none this month — click to add bonus or expense)</span></summary>`;
  const adjPanel = el(`<details class="panel"${adjustments.length ? ' open' : ''}>
    ${adjSummary}
    <p class="small muted">Extra income (positive) or one-off expenses (negative) that only apply to ${esc(monthName(month))}.</p>
    <div class="adj-list"></div>
    <form class="inline-form">
      <label class="field"><span class="lbl">Name</span><input name="name" maxlength="80" required autocomplete="off"></label>
      <label class="field"><span class="lbl">Amount ($, − for expense)</span><input class="num-input" name="amount" inputmode="decimal" placeholder="-120.00" autocomplete="off"></label>
      <button class="btn small" type="submit">Add</button>
    </form>
  </details>`);
  main.appendChild(adjPanel);
  const adjList = adjPanel.querySelector('.adj-list');
  if (adjustments.length) {
    const ul = el('<ul class="list"></ul>');
    for (const a of adjustments) {
      const isCash = a.kind === 'cash';
      const label = isCash ? 'Cash from checking' : a.name;
      const li = el(`<li><span class="grow name">${esc(label)}</span>
        ${isCash ? '<span class="pill extra">cash sweep</span>' : ''}
        <span class="num ${a.amount_cents >= 0 ? 'good' : 'bad'}">${usd(a.amount_cents)}</span>
        <button class="linklike" data-del>remove</button></li>`);
      li.querySelector('[data-del]').addEventListener('click', async () => {
        try { await api.del(`/api/adjustments/${a.id}`); } catch (e) { alert_(adjPanel, errText(e)); return; }
        rerun();
      });
      ul.appendChild(li);
    }
    adjList.appendChild(ul);
  }
  adjPanel.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const form = ev.currentTarget;
    let cents;
    try { cents = parseCents(form.elements.amount.value); } catch (e) { alert_(adjPanel, e.message); return; }
    if (cents === null || cents === 0) { alert_(adjPanel, 'Enter a non-zero amount (negative for an expense).'); return; }
    try {
      await api.post('/api/adjustments', { month, name: form.elements.name.value.trim(), amount_cents: cents });
    } catch (e) { alert_(adjPanel, errText(e)); return; }
    rerun();
  });

  // Summary from projection
  const proj = bundle.projection;
  if (proj) {
    const chosen = proj[settings.strategy] || proj.avalanche;
    if (chosen) {
      const mo = chosen.debt_free_month;
      const panel = el(`<section class="panel">
        <h2>Where this is headed</h2>
        <div class="tiles">
          <div class="tile"><span class="t-label">Debt-free</span><span class="t-value">${mo ? esc(monthName(mo)) : 'Not in sight'}</span></div>
          <div class="tile"><span class="t-label">Total interest</span><span class="t-value">${usd(chosen.total_interest_cents)}</span></div>
          <div class="tile"><span class="t-label">Interest saved</span><span class="t-value good">${proj.interest_saved_cents == null ? '—' : usd(proj.interest_saved_cents)}</span></div>
        </div>
        <p class="small muted">vs paying minimums only. See <a href="#/plan">Plan</a> for the full projection.</p>
      </section>`);
      main.appendChild(panel);
    }
  }

  // Re-render this view in place (the router listens for hashchange).
  function refresh() { window.dispatchEvent(new HashChangeEvent('hashchange')); }
  const rerun = refresh;

  function alert_(panel, msg) {
    let box = panel.querySelector('.inline-err');
    if (!box) {
      box = el('<p class="form-err inline-err" role="alert"></p>');
      panel.prepend(box);
    }
    box.textContent = msg;
  }
}

async function logPayment(row, remaining, month, refresh, synced = false) {
  const ok = await openFormDialog({
    title: `Log payment — ${row.name}`,
    submitLabel: 'Log payment',
    body: `
      ${synced ? '<p class="sync-note">Your bank balance updates when the payment posts.</p>' : ''}
      ${field('Amount ($)', moneyInput('amount', `value="${(remaining / 100).toFixed(2)}"`), 'Defaults to what’s remaining.')}
      ${field('Date', `<input type="date" name="paid_on" value="${todayISO()}">`)}
      ${field('Note (optional)', '<input name="note" maxlength="200" autocomplete="off">')}`,
    onSubmit: async (form) => {
      const cents = parseCents(form.elements.amount.value);
      if (cents === null || cents <= 0) throw new Error('Enter an amount greater than zero.');
      await api.post('/api/payments', {
        card_id: row.card_id,
        amount_cents: cents,
        paid_on: form.elements.paid_on.value || todayISO(),
        month,
        note: form.elements.note.value.trim() || null,
      });
    },
  });
  if (ok) refresh();
}

/**
 * Cash panel: totals, what next month keeps, spare with a Use-it dialog.
 * `cash` is the month bundle's `cash` object.
 */
function cashPanel(cash, month, refresh) {
  const keeps = [];
  keeps.push(`Keeps ${usd(cash.essentials_cents)} for the next month of bills, spending and minimums`);
  if (cash.cash_floor_cents > 0) keeps.push(`a ${usd(cash.cash_floor_cents)} floor`);
  if (cash.goals_set_aside_cents > 0) keeps.push(`+ ${usd(cash.goals_set_aside_cents)} set aside for goals`);
  if (cash.extra_remaining_cents > 0) keeps.push(`${usd(cash.extra_remaining_cents)} of unpaid extra`);
  const keepsHtml = keeps.length > 1
    ? `${esc(keeps[0])}, ${esc(keeps.slice(1).join(' and '))}.`
    : `${esc(keeps[0])}.`;
  const isCurrent = cash.is_current_month !== false;
  const covers = cash.months_covered == null
    ? ''
    : (Number(cash.months_covered) <= 0
      ? '<p class="small muted">Cash doesn’t cover a month of essentials yet.</p>'
      : `<p class="small muted">Covers ${esc(Number(cash.months_covered).toFixed(1))} month${Number(cash.months_covered) === 1 ? '' : 's'} of essentials.</p>`);
  const appliedHtml = cash.applied_cents > 0
    ? `<p class="small muted">${usd(cash.applied_cents)} of spare cash applied to cards this month.</p>`
    : '';
  const panel = isCurrent && cash.spare_cents > 0
    ? el(`<section class="panel cash-panel">
        <div class="cash-spare">
          <strong class="num">${usd(cash.spare_cents)} spare cash</strong> ready to put toward your cards
          <button class="btn primary small" data-use>Use spare cash</button>
        </div>
        <details class="sub-disclosure">
          <summary>Cash reserve breakdown (${usd(cash.cash_total_cents)} total)</summary>
          <p class="small muted">${keepsHtml}</p>
          ${covers}
          ${appliedHtml}
        </details>
      </section>`)
    : el(`<section class="panel cash-panel">
        <div class="section-head"><h2>Cash reserve</h2><span class="pill off">${cash.months_covered != null && Number(cash.months_covered) > 0 ? `${Number(cash.months_covered).toFixed(1)} mo runway` : 'Building reserve'}</span></div>
        <p class="small muted">${keepsHtml} ${appliedHtml}</p>
      </section>`);
  const useBtn = panel.querySelector('[data-use]');
  if (useBtn) useBtn.addEventListener('click', () => useCashDialog(cash, month, refresh));
  return panel;
}

/** Dialog to sweep spare cash into this month's card payments. */
function useCashDialog(cash, month, refresh) {
  openFormDialog({
    title: 'Use spare cash',
    submitLabel: 'Apply to cards',
    body: field('Amount ($)', moneyInput('amount', `value="${(cash.spare_cents / 100).toFixed(2)}"`),
      `Moved into this month’s card payments as extra. Up to ${usd(cash.spare_cents)}.`),
    onSubmit: async (form) => {
      const cents = parseCents(form.elements.amount.value);
      if (cents === null || cents <= 0) throw new Error('Enter an amount greater than zero.');
      if (cents > cash.spare_cents) throw new Error(`That’s more than the ${usd(cash.spare_cents)} spare.`);
      try {
        await api.post(`/api/month/${month}/use-cash`, { amount_cents: cents });
      } catch (e) {
        throw new Error(errText(e));
      }
    },
  }).then((ok) => { if (ok) refresh(); });
}

/**
 * Reality check panel (Round 3 R1): one row per month of card-debt trend from
 * GET /api/insights/debt-trend. Headline uses the last complete month.
 * Returns a detached <section> or null when there is no trend data.
 */
function realityCheckPanel(trend) {
  if (!trend || !Array.isArray(trend.months) || !trend.months.length) return null;
  // Months before the bank history starts are empty, not "$0 change": drop them.
  const covStart = (trend.coverage_start || '').slice(0, 7);
  const months = covStart ? trend.months.filter((m) => m.month >= covStart) : trend.months;
  if (!months.length) return null;
  const last = [...months].reverse().find((m) => !m.partial) || null;

  let headline = '';
  if (last) {
    const net = last.net_change_cents || 0;
    if (net > 0) {
      headline = `<p class="rc-headline bad">Card debt went up ${usd(net)} last month.</p>`;
    } else if (net < 0) {
      headline = `<p class="rc-headline good">Card debt went down ${usd(-net)} last month.</p>`;
    } else {
      headline = '<p class="rc-headline">Card debt didn’t change last month.</p>';
    }
  }
  const interest = trend.interest_last_30_cents > 0
    ? `<p class="small muted rc-interest">Interest in the last 30 days: ${usd(trend.interest_last_30_cents)}.</p>`
    : '';

  const rows = months.slice().reverse().map((m) => {
    const net = m.net_change_cents || 0;
    const cls = net > 0 ? 'bad' : net < 0 ? 'good' : '';
    const signed = net > 0 ? `+${usd(net)}` : net < 0 ? `−${usd(-net)}` : usd(0);
    const bits = [
      `charged ${usd0(m.charges_cents)}`,
      `paid ${usd0(m.payments_cents)}`,
      `interest ${usd0(m.interest_cents)}`,
      `fees ${usd0(m.fees_cents)}`,
    ];
    if (m.refunds_cents > 0) bits.push(`refunds ${usd0(m.refunds_cents)}`);
    return `<li class="rc-row">
      <span class="rc-month">${esc(monthName(m.month))}${m.partial ? ' <span class="rc-partial">(partial)</span>' : ''}</span>
      <span class="rc-net num ${cls}">${signed}</span>
      <span class="rc-break">${esc(bits.join(' · '))}</span>
    </li>`;
  }).join('');

  return el(`<section class="panel reality-check">
    <h2>Reality check</h2>
    ${headline}
    ${interest}
    <ul class="rc-rows">${rows}</ul>
  </section>`);
}
