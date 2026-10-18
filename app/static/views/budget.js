// Budget view: overview + allocation bar, income & pay schedules, bills & spending
// with bank-suggested spending, subscriptions (embedded), goals, and waterfall.

import { api, errText } from '../api.js';
import {
  usd, esc, el, field, moneyInput, openFormDialog, confirmDialog,
  parseCents, errorBox, todayISO, toast, errorBanner, monthShort,
} from '../util.js';
import { buildSubscriptionsPanel } from './subscriptions.js';

export async function budgetView(main) {
  let income, expenses, settings, bundle, schedules = [], goals = [], cashAccounts = [], spending = null;
  let subsData = null, subsSuggestions = [];
  const fetches = [
    api.get('/api/income'),
    api.get('/api/expenses'),
    api.get('/api/settings'),
    api.get(`/api/month/${curMonth()}`),
  ];
  // Phase-4 endpoints may not exist yet on a mid-upgrade server; degrade gracefully.
  const tryGet = (path, fallback) => api.get(path).catch((e) => (e && e.status === 404 ? fallback : Promise.reject(e)));
  fetches.push(tryGet('/api/pay-schedules', []));
  fetches.push(tryGet('/api/goals', []));
  fetches.push(tryGet('/api/insights/spending?months=3', null));
  fetches.push(tryGet('/api/subscriptions', null));
  fetches.push(tryGet('/api/subscriptions/suggestions', []));
  try {
    cashAccounts = (await api.get('/api/accounts')).filter((a) => a.kind === 'cash');
  } catch { cashAccounts = []; }
  try {
    [income, expenses, settings, bundle, schedules, goals, spending, subsData, subsSuggestions] = await Promise.all(fetches);
  } catch (err) {
    main.innerHTML = '';
    errorBox(main, err);
    return;
  }
  main.innerHTML = '';

  // ---- Page head ----
  main.appendChild(el(`<div class="page-head">
    <h1>Budget</h1>
    <a class="btn small ghost" href="#/settings">Preferences &amp; buffer →</a>
  </div>`));

  const b = bundle.budget || {};
  const v = (k) => (b[k] === null || b[k] === undefined ? 0 : b[k]);

  // ---- Top overview: summary metrics + stacked allocation bar ----
  main.appendChild(overviewPanel(b, v));

  if (!income.length && !expenses.length) {
    main.appendChild(el(`<p class="muted">Start by adding your monthly income and your regular bills and spending. Card minimums are counted separately from your cards.</p>`));
  }

  // ---- Income & Pay Schedule (unified panel) ----
  const incPanel = el(`<section class="panel">
    <div class="section-head"><h2>Income</h2>
      <span class="small num muted total-inc"></span>
      <button class="btn primary small" data-add-income>Add income</button>
    </div>
    <p class="small muted">Monthly money coming in.</p>
    <ul class="list inc-list"></ul>
    <div class="panel-divider" role="presentation"></div>
    <div class="section-head sub-head">
      <h3>Pay schedule</h3>
      ${schedules !== null ? `
        <button class="btn small" data-detect>Detect from paychecks</button>
        <button class="btn small" data-add-sched>Add schedule</button>` : ''}
    </div>
    ${schedules !== null ? '<div class="sched-list"></div>' : ''}
  </section>`);
  main.appendChild(incPanel);

  if (schedules !== null) {
    renderSchedules(incPanel.querySelector('.sched-list'), schedules, main);
    incPanel.querySelector('[data-detect]').addEventListener('click', () => detectSchedule(main, schedules, refresh, null));
    incPanel.querySelector('[data-add-sched]').addEventListener('click', () => scheduleDialog(null, schedules, refresh));
  }

  // ---- Bills & Spending (filterable panel) ----
  const expPanel = el(`<section class="panel">
    <div class="section-head">
      <h2>Bills &amp; spending</h2>
      <div class="filter-pills" role="group" aria-label="Filter expenses">
        <button type="button" data-filter="all" aria-pressed="true">All</button>
        <button type="button" data-filter="bill" aria-pressed="false">Bills</button>
        <button type="button" data-filter="spending" aria-pressed="false">Spending</button>
      </div>
      <button class="btn primary small" data-add-expense>Add expense</button>
    </div>
    <p class="small muted">Recurring monthly outflows — <strong>not</strong> card minimums (those are counted separately).</p>
    <ul class="list exp-list"></ul>
    <p class="num small total-exp"></p>
  </section>`);
  main.appendChild(expPanel);

  const expList = expPanel.querySelector('.exp-list');
  const totalExp = expPanel.querySelector('.total-exp');
  const filterFor = { all: () => true, bill: (r) => r.kind === 'bill', spending: (r) => r.kind === 'spending' };
  let activeFilter = 'all';
  const renderExpenses = () => {
    renderList(expList, expenses.filter(filterFor[activeFilter]), 'expense');
    const shown = expenses.filter(filterFor[activeFilter]).filter((r) => r.active).reduce((s, r) => s + r.amount_cents, 0);
    totalExp.textContent = activeFilter === 'all'
      ? `Active total: ${usd(shown)}`
      : `${activeFilter === 'bill' ? 'Bills' : 'Spending'} total: ${usd(shown)}`;
  };
  for (const btn of expPanel.querySelectorAll('[data-filter]')) {
    btn.addEventListener('click', () => {
      activeFilter = btn.dataset.filter;
      for (const b2 of expPanel.querySelectorAll('[data-filter]')) b2.setAttribute('aria-pressed', String(b2 === btn));
      renderExpenses();
    });
  }
  renderExpenses();
  expPanel.querySelector('[data-add-expense]').addEventListener('click', () => expenseForm(null, refresh));

  function renderList(ul, rows, kind) {
    ul.innerHTML = '';
    if (!rows.length) ul.appendChild(el(`<li><span class="muted small">Nothing here.</span></li>`));
    for (const r of rows) {
      const schedName = kind === 'income' && r.pay_schedule_id
        ? (schedules.find((s) => s.id === r.pay_schedule_id) || {}).name : null;
      const li = el(`<li>
        <span class="grow"><span class="name">${esc(r.name)}</span>
          ${kind === 'expense' ? `<span class="pill ${r.kind === 'bill' ? 'bill' : 'spending'}">${r.kind === 'bill' ? 'bill' : 'spending'}</span>` : ''}
          ${schedName ? `<span class="pill good">from pay schedule</span>` : ''}
          ${!r.active ? '<span class="pill off">inactive</span>' : ''}
          ${r.due_day ? `<span class="muted small">due day ${r.due_day}</span>` : ''}
          ${kind === 'expense' && r.paid_from === 'card' ? '<span class="pill off">on card</span>' : ''}
        </span>
        <span class="num">${usd(r.amount_cents)}</span>
        <span class="row-actions">
          ${kind === 'expense' && r.kind === 'bill' && r.active ? '<button class="linklike" data-sub title="Track as a subscription instead">make subscription</button>' : ''}
          <button class="linklike" data-edit>edit</button>
          <button class="linklike" data-del>delete</button>
        </span>
      </li>`);
      const subBtn = li.querySelector('[data-sub]');
      if (subBtn) {
        subBtn.addEventListener('click', async () => {
          const msg = `Move "${r.name}" to Subscriptions? The bill is switched off and the subscription takes over its cost in your plan. Its due date will no longer count in Safe-to-Spend.`;
          if (!(await confirmDialog(msg, false))) return;
          try { await api.post(`/api/subscriptions/from-expense/${r.id}`); }
          catch (e) { showError(main, errText(e)); return; }
          refresh();
        });
      }
      li.querySelector('[data-edit]').addEventListener('click', () =>
        kind === 'income' ? incomeForm(r, refresh, schedules) : expenseForm(r, refresh));
      li.querySelector('[data-del]').addEventListener('click', async () => {
        if (!(await confirmDialog(`Delete "${r.name}"?`))) return;
        try { await api.del(`/api/${kind === 'income' ? 'income' : 'expenses'}/${r.id}`); }
        catch (e) { showError(main, errText(e)); return; }
        refresh();
      });
      ul.appendChild(li);
    }
  };
  renderList(incPanel.querySelector('.inc-list'), income, 'income');
  const incSum = income.filter((r) => r.active).reduce((s, r) => s + r.amount_cents, 0);
  incPanel.querySelector('.total-inc').textContent = `Active total: ${usd(incSum)}`;
  incPanel.querySelector('[data-add-income]').addEventListener('click', () => incomeForm(null, refresh, schedules));

  // ---- Suggested spending from bank data (collapsed disclosure) ----
  if (spending && Array.isArray(spending.categories) && spending.categories.length) {
    main.appendChild(spendingPanel(spending, expenses, refresh));
  }

  // ---- Subscriptions (embedded first-class section) ----
  if (subsData) {
    main.appendChild(buildSubscriptionsPanel(subsData, subsSuggestions, refresh, (e) => showError(main, errText(e))));
  }

  // ---- Goals ----
  if (goals !== null) {
    main.appendChild(goalsPanel(main, goals, refresh));
  }

  // ---- Waterfall (collapsible) ----
  main.appendChild(waterfallPanel(b, v, settings));

  function refresh() { window.dispatchEvent(new HashChangeEvent('hashchange')); }
}

/** Top overview: 4 summary metrics + stacked allocation bar with legend. */
function overviewPanel(b, v) {
  const panel = el(`<section class="panel budget-overview">
    <div class="overview-metrics">
      <div class="metric"><span class="m-label">Monthly income</span><span class="m-value num">${usd(v('income_total_cents'))}</span></div>
      <div class="metric"><span class="m-label">Bills &amp; subs</span><span class="m-value num">${usd(v('bills_total_cents') + v('subscriptions_total_cents'))}</span></div>
      <div class="metric"><span class="m-label">Spending &amp; mins</span><span class="m-value num">${usd(v('spending_total_cents') + v('minimums_total_cents'))}</span></div>
      <div class="metric"><span class="m-label">Extra to debt</span><span class="m-value num">${usd(v('extra_cents'))}</span></div>
    </div>
    <div class="alloc-bar" role="img" aria-label="How your monthly income is allocated">
      <div class="alloc-seg bills"></div>
      <div class="alloc-seg spending"></div>
      <div class="alloc-seg subs"></div>
      <div class="alloc-seg mins"></div>
      <div class="alloc-seg extra"></div>
    </div>
    <div class="alloc-legend">
      <span><span class="swatch bills"></span> Bills</span>
      <span><span class="swatch spending"></span> Spending</span>
      <span><span class="swatch subs"></span> Subscriptions</span>
      <span><span class="swatch mins"></span> Card minimums</span>
      <span><span class="swatch extra"></span> Extra to debt</span>
    </div>
  </section>`);
  // Set segment widths via CSSOM (CSP forbids inline style attributes in HTML).
  const income = Math.max(1, v('income_total_cents'));
  const segs = panel.querySelectorAll('.alloc-seg');
  const shares = [
    v('bills_total_cents'), v('spending_total_cents'), v('subscriptions_total_cents'),
    v('minimums_total_cents'), v('extra_cents'),
  ];
  shares.forEach((cents, i) => segs[i].style.setProperty('width', `${Math.min(100, (cents / income) * 100)}%`));
  return panel;
}

/** "Your actual spending" suggestions, collapsed by default. */
function spendingPanel(spending, expenses, refresh) {
  const monthsLabel = (spending.months_used || []).map(monthShort).join('–');
  const existing = new Set(expenses
    .filter((e) => e.kind === 'spending' && e.active)
    .map((e) => String(e.name || '').trim().toLowerCase()));
  const cats = spending.categories;
  const rows = cats.map((c, i) => {
    const already = existing.has(String(c.label || '').trim().toLowerCase());
    const checked = !!c.suggest && !already;
    const note = c.note ? `<span class="spend-note">${esc(c.note)}</span>` : '';
    const alreadyNote = already ? '<span class="spend-note">already in your budget</span>' : '';
    return `<li><label><input type="checkbox" data-cat data-i="${i}"${checked ? ' checked' : ''}>
      <span class="name">${esc(c.label)}</span>${note}${alreadyNote}
      <span class="spend-amt num">${usd(c.monthly_avg_cents)}/mo</span></label></li>`;
  }).join('');
  const budget = spending.budget_spending_cents || 0;
  const panel = el(`<details class="sub-disclosure spend-panel">
    <summary>
      <span>Suggested from bank spending
        <span class="pill warn num">≈ ${usd(spending.total_monthly_avg_cents)}/mo actual vs ${usd(budget)} budgeted</span>
      </span>
      <span class="small muted">Check what's real</span>
    </summary>
    <p class="small muted">What your linked accounts averaged ${esc(monthsLabel)}${monthsLabel ? '' : ' recently'}.
      Check the categories that are real everyday spending and add them as budget lines — the extra going to
      your cards will drop to what you can actually afford once that spending is counted.</p>
    <ul class="spend-list">${rows}</ul>
    <button class="btn primary small" data-add-spend>Add selected as Spending</button>
  </details>`);
  panel.querySelector('[data-add-spend]').addEventListener('click', async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    const picked = [...panel.querySelectorAll('input[data-cat]:checked')]
      .map((box) => cats[Number(box.dataset.i)])
      .filter((c) => c && !existing.has(String(c.label || '').trim().toLowerCase()));
    let added = 0;
    try {
      for (const c of picked) {
        await api.post('/api/expenses', {
          name: c.label,
          amount_cents: c.monthly_avg_cents,
          kind: 'spending',
          due_day: null,
          active: true,
          paid_from: 'cash',
          match_text: null,
        });
        existing.add(String(c.label || '').trim().toLowerCase());
        added += 1;
      }
    } catch (e) {
      errorBanner(panel, errText(e));
      btn.disabled = false;
      return;
    }
    if (added > 0) {
      toast(`Added ${added} spending line${added === 1 ? '' : 's'}`);
      refresh();
    } else {
      toast('Nothing new to add — those are already in your budget.', 'info');
      btn.disabled = false;
    }
  });
  return panel;
}

function curMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}

/* ---------------- Pay schedules ---------------- */

const FREQ_WORDS = {
  weekly: 'Every week', biweekly: 'Every other week',
  semimonthly: 'Twice a month', monthly: 'Monthly',
};

export function scheduleFrequencyWords(s) {
  if (s.frequency === 'semimonthly') {
    const day = (n) => (n === 31 ? 'last day' : `${ordinal(n)}`);
    return `${day(s.semimonthly_day1)} and ${day(s.semimonthly_day2)}`;
  }
  if (s.frequency === 'monthly') return `Monthly on the ${ordinal(s.anchor_date ? Number(s.anchor_date.slice(8, 10)) : 1)}`;
  if (s.frequency === 'weekly' || s.frequency === 'biweekly') {
    const d = s.anchor_date ? new Date(`${s.anchor_date}T12:00:00`) : null;
    const wd = d ? ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'][d.getDay()] : '';
    return s.frequency === 'weekly' ? `Every ${wd}` : `Every other ${wd}`;
  }
  return FREQ_WORDS[s.frequency] || s.frequency;
}

function ordinal(n) {
  const s = ['th', 'st', 'nd', 'rd'];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(`${iso}T12:00:00`);
  return d.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric' });
}

function renderSchedules(list, schedules, main) {
  list.innerHTML = '';
  if (!schedules.length) {
    list.appendChild(el('<p class="small muted">No pay schedule yet — add one so Paydown knows when money arrives.</p>'));
  }
  for (const s of schedules) {
    const item = el(`<div class="sched-item">
      <div class="sched-top">
        <span class="name">${esc(s.name)}</span>
        ${!s.active ? '<span class="pill off">inactive</span>' : ''}
        ${s.deposit_confirmed ? '<span class="pill good">Confirmed by deposits</span>' : ''}
      </div>
      <div class="sched-nums small muted">
        <span>${esc(scheduleFrequencyWords(s))}</span>
        <span>Paycheck ${usd(s.amount_cents)} · ≈ ${usd(s.monthly_equivalent_cents)}/month</span>
        ${s.last_paycheck ? `<span>Last: ${esc(fmtDate(s.last_paycheck.date))} · ${usd(s.last_paycheck.amount_cents)}</span>` : ''}
      </div>
      <div class="sched-dates small num">${(s.next_dates || []).map((d) => esc(fmtDate(d))).join(' · ')}</div>
      <div class="row-actions">
        <button class="btn small" data-edit>Edit</button>
        <button class="btn small" data-income>Use as income</button>
        <button class="linklike" data-del>delete</button>
      </div>
    </div>`);
    item.querySelector('[data-edit]').addEventListener('click', () => scheduleDialog(s, schedules, refresh));
    item.querySelector('[data-del]').addEventListener('click', async () => {
      if (!(await confirmDialog(`Delete pay schedule "${s.name}"?`))) return;
      try { await api.del(`/api/pay-schedules/${s.id}`); } catch (e) { showError(main, errText(e)); return; }
      refresh();
    });
    item.querySelector('[data-income]').addEventListener('click', async () => {
      try {
        const row = await api.post(`/api/pay-schedules/${s.id}/use-as-income`);
        toast(`Income row "${row.name}" ready (${usd(row.amount_cents)}/month).`);
        refresh();
      } catch (e) { showError(main, errText(e)); }
    });
    list.appendChild(item);
  }
  function refresh() { window.dispatchEvent(new HashChangeEvent('hashchange')); }
}

/** Add/Edit pay schedule dialog. `prefill` (from detect) pre-fills a NEW schedule. */
export function scheduleDialog(row, schedules, done, prefill = null, cashAccounts = []) {
  const r = prefill || row || {};
  const freq = r.frequency || 'biweekly';
  const isSM = freq === 'semimonthly';
  const smField = (name, label, val) =>
    field(label, `<select name="${name}">` +
      Array.from({ length: 31 }, (_, i) => i + 1)
        .map((d) => `<option value="${d}"${Number(val) === d ? ' selected' : ''}>${d === 31 ? 'Last day (31)' : ordinal(d)}</option>`)
        .join('') + `</select>`);
  const body = `
    ${field('Name', `<input name="name" maxlength="80" required value="${esc(r.name || '')}" autocomplete="off">`)}
    ${field('How often', `<select name="frequency">
      <option value="weekly"${freq === 'weekly' ? ' selected' : ''}>Weekly</option>
      <option value="biweekly"${freq === 'biweekly' ? ' selected' : ''}>Every other week</option>
      <option value="semimonthly"${isSM ? ' selected' : ''}>Twice a month</option>
      <option value="monthly"${freq === 'monthly' ? ' selected' : ''}>Monthly</option>
    </select>`)}
    ${field('A payday you know (date)', `<input type="date" name="anchor_date" value="${esc(r.anchor_date || todayISO())}">`,
      freq === 'monthly' ? 'The day of the month you get paid.' : 'Any recent payday.')}
    <div data-sm-fields${isSM ? '' : ' hidden'}>
      ${smField('semimonthly_day1', 'First payday of the month', r.semimonthly_day1 || 1)}
      ${smField('semimonthly_day2', 'Second payday of the month', r.semimonthly_day2 || 15)}
    </div>
    ${field('If payday lands on a weekend', `<select name="weekend_rule">
      <option value="previous"${(r.weekend_rule || 'previous') === 'previous' ? ' selected' : ''}>Counted the Friday before</option>
      <option value="next"${r.weekend_rule === 'next' ? ' selected' : ''}>Counted the Monday after</option>
      <option value="none"${r.weekend_rule === 'none' ? ' selected' : ''}>No change</option>
    </select>`)}
    ${field('Expected paycheck ($)', moneyInput('amount', `value="${((r.amount_cents || 0) / 100).toFixed(2)}"`), 'Net take-home.')}
    ${field('Deposit account (optional)', `<select name="cash_account_id"><option value="">—</option>` +
          cashAccounts.map((a) => `<option value="${a.id}"${r.cash_account_id === a.id ? ' selected' : ''}>${esc(a.name)}</option>`).join('') + `</select>`)}
    <label class="field"><span class="lbl">Active</span><input type="checkbox" name="active"${r.active === undefined || r.active ? ' checked' : ''}></label>`;
  openFormDialog({
    title: row ? `Edit pay schedule` : 'Add pay schedule',
    submitLabel: 'Save schedule',
    body,
    onMount: (form) => {
      const freqSel = form.elements.frequency;
      const smBox = form.querySelector('[data-sm-fields]');
      const sync = () => { smBox.hidden = freqSel.value !== 'semimonthly'; };
      freqSel.addEventListener('change', sync);
      sync();
    },
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      const frequency = form.elements.frequency.value;
      const anchor = form.elements.anchor_date.value;
      if (!anchor) throw new Error('Pick a payday date.');
      const body2 = {
        name, frequency, anchor_date: anchor,
        weekend_rule: form.elements.weekend_rule.value,
        active: form.elements.active.checked,
      };
      const cents = parseCents(form.elements.amount.value);
      if (cents === null || cents < 0) throw new Error('Amount must be zero or more.');
      body2.amount_cents = cents;
      const acct = form.elements.cash_account_id.value;
      body2.cash_account_id = acct === '' ? null : Number(acct);
      if (frequency === 'semimonthly') {
        const d1 = Number(form.elements.semimonthly_day1.value);
        const d2 = Number(form.elements.semimonthly_day2.value);
        if (!(d1 >= 1 && d1 <= 31 && d2 >= 1 && d2 <= 31 && d1 < d2)) throw new Error('Semimonthly days must be 1–31 with the first before the second.');
        body2.semimonthly_day1 = d1;
        body2.semimonthly_day2 = d2;
      }
      try {
        if (row) await api.put(`/api/pay-schedules/${row.id}`, body2);
        else await api.post('/api/pay-schedules', body2);
      } catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) done(); });
}

/** Ask the backend to infer a schedule from labelled paychecks; opens a prefilled dialog. */
export async function detectSchedule(main, schedules, refresh, transactionIds, cashAccounts = []) {
  let res;
  try {
    res = await api.post('/api/pay-schedules/detect', { transaction_ids: transactionIds });
  } catch (e) { showError(main, errText(e)); return; }
  if (!res.suggestion) {
    // Show the API reason verbatim (it now explains the no-pattern case), with the count.
    const reason = res.reason || 'Not enough paychecks to detect a schedule.';
    const msg = res.count != null ? `Checked ${res.count} labelled paycheck${res.count === 1 ? '' : 's'}. ${reason}` : reason;
    toast(msg, 'info');
    return;
  }
  const s = res.suggestion;
  toast(`Detected a ${FREQ_WORDS[s.frequency] || s.frequency} paycheck of ${usd(s.amount_cents)} (${s.confidence} confidence) from ${(s.dates_used || []).length} paychecks.`, 'info');
  scheduleDialog(null, schedules, () => { refresh(); }, s, cashAccounts);
}

/* ---------------- Waterfall ---------------- */

function waterfallPanel(b, v, settings) {
  const det = el(`<details class="panel">
    <summary><strong>Full monthly math waterfall</strong>
      <span class="small muted num">Debt budget: ${usd(v('debt_budget_cents'))}/mo</span></summary>
    <div class="waterfall">
      <div class="w-row"><span>Income</span><span class="val">${usd(v('income_total_cents'))}</span></div>
      <div class="w-row sub minus"><span>Bills</span><span class="val">${usd(v('bills_total_cents'))}</span></div>
      <div class="w-row sub minus"><span>Spending</span><span class="val">${usd(v('spending_total_cents'))}</span></div>
      ${v('subscriptions_total_cents') > 0 ? `<div class="w-row sub minus"><span><a href="#/subscriptions">Subscriptions</a></span><span class="val">${usd(v('subscriptions_total_cents'))}</span></div>` : ''}
      <div class="w-row sub minus"><span>One-off expenses</span><span class="val">${usd(v('adjust_out_cents'))}</span></div>
      <div class="w-row sub minus"><span>Card minimums</span><span class="val">${usd(v('minimums_total_cents'))}</span></div>
      ${v('goals_cents') > 0 ? `<div class="w-row sub minus"><span>Set aside for goals</span><span class="val">${usd(v('goals_cents'))}</span></div>` : ''}
      <div class="w-row"><span>= Available after minimums</span><span class="val">${usd(v('available_after_min_cents'))}</span></div>
      <div class="w-row sub minus"><span>Buffer kept aside</span><span class="val">${usd(v('buffer_cents'))}</span></div>
      <div class="w-row"><span>= Surplus</span><span class="val">${usd(v('surplus_cents'))}</span></div>
      <div class="w-row sub minus"><span>Extra to debt (${settings.debt_share_pct ?? 100}%${settings.extra_override_cents != null ? ', overridden' : ''})</span><span class="val">${usd(v('extra_cents'))}</span></div>
      ${b.cash_applied_cents > 0 ? `<div class="w-row sub extra-line"><span>Cash from checking</span><span class="val">${usd(b.cash_applied_cents)}</span></div>` : ''}
      <div class="w-row total"><span>Debt budget (minimums + extra)</span><span class="val">${usd(v('debt_budget_cents'))}</span></div>
      <div class="w-row"><span>Leftover (unassigned)</span><span class="val">${usd(v('leftover_cents'))}</span></div>
    </div>
    ${v('shortfall_cents') > 0 ? `<p class="form-err" role="alert">Minimums exceed what’s left by ${usd(v('shortfall_cents'))}.</p>` : ''}
  </details>`);
  return det;
}

/* ---------------- Goals ---------------- */

function goalsPanel(main, goals, refresh) {
  const panel = el(`<section class="panel">
    <div class="section-head"><h2>Goals</h2><button class="btn primary small" data-add-goal>Add</button></div>
    <p class="small muted">Money set aside inside checking for irregular bills — kept out of Safe-to-Spend.</p>
    <div class="goal-list"></div>
  </section>`);
  const list = panel.querySelector('.goal-list');
  if (!goals.length) {
    list.appendChild(el('<p class="small muted">No goals yet — set money aside for car repairs, insurance, holidays.</p>'));
  }
  for (const g of goals) {
    const pct = Math.min(100, Math.max(0, g.progress_pct ?? 0));
    const item = el(`<div class="goal-item">
      <div class="sched-top">
        <span class="name">${esc(g.name)}</span>
        ${!g.active ? '<span class="pill off">inactive</span>' : ''}
      </div>
      <div class="prog-row">
        <progress max="100" value="${pct}" aria-label="Progress for ${esc(g.name)}"></progress>
        <span class="small num">${usd(g.set_aside_cents)} of ${usd(g.target_cents)}</span>
      </div>
      <div class="sched-nums small muted">
        ${g.target_date ? `<span>By ${esc(fmtDate(g.target_date))}</span>` : ''}
        ${g.per_paycheck_cents ? `<span>Set aside ${usd(g.per_paycheck_cents)} each paycheck</span>` : ''}
        ${g.monthly_need_cents > 0 ? `<span>${usd(g.monthly_need_cents)}/month in your budget</span>` : ''}
      </div>
      <div class="row-actions">
        <button class="btn small" data-set>Set aside</button>
        <button class="btn small" data-use>Use</button>
        <button class="btn small ghost" data-edit>Edit</button>
        <button class="linklike" data-del>delete</button>
      </div>
    </div>`);
    item.querySelector('[data-set]').addEventListener('click', () => goalMoveDialog(g, 1, refresh));
    item.querySelector('[data-use]').addEventListener('click', () => goalMoveDialog(g, -1, refresh));
    item.querySelector('[data-edit]').addEventListener('click', () => goalDialog(g, refresh));
    item.querySelector('[data-del]').addEventListener('click', async () => {
      if (!(await confirmDialog(`Delete goal "${g.name}"? Its moves are removed too.`))) return;
      try { await api.del(`/api/goals/${g.id}`); } catch (e) { showError(main, errText(e)); return; }
      refresh();
    });
    list.appendChild(item);
  }
  panel.querySelector('[data-add-goal]').addEventListener('click', () => goalDialog(null, refresh));
  return panel;
}

function goalDialog(row, done) {
  const r = row || {};
  openFormDialog({
    title: row ? `Edit goal` : 'Add goal',
    submitLabel: 'Save goal',
    body: `
      ${field('Name', `<input name="name" maxlength="80" required value="${esc(r.name || '')}" autocomplete="off">`)}
      ${field('Target ($)', moneyInput('target', `value="${((r.target_cents || 0) / 100).toFixed(2)}"`))}
      ${field('Target date (optional)', `<input type="date" name="target_date" value="${esc(r.target_date || '')}">`,
        'When you need the money — Paydown suggests how much to set aside.')}`,
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      const cents = parseCents(form.elements.target.value);
      if (cents === null || cents <= 0) throw new Error('Target must be more than zero.');
      const body = { name, target_cents: cents, target_date: form.elements.target_date.value || null };
      try {
        if (row) await api.put(`/api/goals/${row.id}`, body);
        else await api.post('/api/goals', body);
      } catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) done(); });
}

/** dir: 1 = set aside, -1 = use/release. */
export function goalMoveDialog(goal, dir, done) {
  const setAside = dir === 1;
  openFormDialog({
    title: setAside ? `Set aside for ${goal.name}` : `Use money from ${goal.name}`,
    submitLabel: setAside ? 'Set aside' : 'Use',
    body: `
      ${field('Amount ($)', moneyInput('amount'), setAside
        ? `Currently set aside: ${usd(goal.set_aside_cents)} of ${usd(goal.target_cents)}.`
        : `Currently set aside: ${usd(goal.set_aside_cents)}.`)}
      ${field('Note (optional)', '<input name="note" maxlength="200" autocomplete="off">')}`,
    onSubmit: async (form) => {
      const cents = parseCents(form.elements.amount.value);
      if (cents === null || cents <= 0) throw new Error('Enter an amount greater than zero.');
      try {
        await api.post(`/api/goals/${goal.id}/move`, {
          amount_cents: setAside ? cents : -cents,
          note: form.elements.note.value.trim() || null,
        });
      } catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) done(); });
}

/* ---------------- Income & expense forms ---------------- */

function incomeForm(row, done, schedules) {
  const r = row || {};
  const linked = !!(row && row.pay_schedule_id && schedules.some((s) => s.id === row.pay_schedule_id));
  openFormDialog({
    title: row ? `Edit ${r.name}` : 'Add income',
    body: `
      ${field('Name', `<input name="name" maxlength="80" required value="${esc(r.name || '')}" autocomplete="off">`)}
      ${field('Monthly amount ($)', moneyInput('amount', `value="${((r.amount_cents || 0) / 100).toFixed(2)}"${linked ? ' disabled' : ''}`),
        linked ? 'Comes from the pay schedule — edit the schedule to change it.' : null)}
      <label class="field"><span class="lbl">Active</span><input type="checkbox" name="active"${r.active === undefined || r.active ? ' checked' : ''}></label>`,
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      let cents = parseCents(form.elements.amount.value);
      if (linked) cents = row.amount_cents; // read-only when driven by the schedule
      if (cents === null || cents < 0) throw new Error('Amount must be zero or more.');
      try {
        const body = { name, amount_cents: cents, active: form.elements.active.checked };
        if (row) await api.put(`/api/income/${row.id}`, body);
        else await api.post('/api/income', body);
      } catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) done(); });
}

function expenseForm(row, done) {
  const r = row || {};
  openFormDialog({
    title: row ? `Edit ${r.name}` : 'Add expense',
    body: `
      ${field('Name', `<input name="name" maxlength="80" required value="${esc(r.name || '')}" autocomplete="off">`)}
      ${field('Monthly amount ($)', moneyInput('amount', `value="${((r.amount_cents || 0) / 100).toFixed(2)}"`))}
      ${field('Kind', `<select name="kind">
        <option value="bill"${r.kind !== 'spending' ? ' selected' : ''}>Bill (fixed: rent, utilities)</option>
        <option value="spending"${r.kind === 'spending' ? ' selected' : ''}>Spending (variable: groceries, gas)</option>
      </select>`)}
      ${field('Due day (optional)', `<input class="num-input" name="due_day" inputmode="numeric" value="${r.due_day ?? ''}" autocomplete="off">`, '1–31')}
      <div data-bill-only>
      ${field('Paid from', `<select name="paid_from">
        <option value="cash"${(r.paid_from || 'cash') === 'cash' ? ' selected' : ''}>Checking</option>
        <option value="card"${r.paid_from === 'card' ? ' selected' : ''}>Credit card</option>
      </select>`)}
      ${field('Bank description contains', `<input name="match_text" maxlength="80" value="${esc(r.match_text || '')}" autocomplete="off">`,
        'Optional — lets Paydown mark this bill paid automatically when it sees the payment.')}
      </div>
      <label class="field"><span class="lbl">Active</span><input type="checkbox" name="active"${r.active === undefined || r.active ? ' checked' : ''}></label>`,
    onMount: (form) => {
      const kindSel = form.elements.kind;
      const billBox = form.querySelector('[data-bill-only]');
      const sync = () => { billBox.hidden = kindSel.value !== 'bill'; };
      kindSel.addEventListener('change', sync);
      sync();
    },
    onSubmit: async (form) => {
      const name = form.elements.name.value.trim();
      if (!name) throw new Error('Name is required.');
      const cents = parseCents(form.elements.amount.value);
      if (cents === null || cents < 0) throw new Error('Amount must be zero or more.');
      const due = form.elements.due_day.value.trim();
      if (due !== '' && !(Number(due) >= 1 && Number(due) <= 31 && Number.isInteger(Number(due)))) {
        throw new Error('Due day must be between 1 and 31.');
      }
      const kind = form.elements.kind.value;
      const body = {
        name, amount_cents: cents,
        kind,
        due_day: due === '' ? null : Number(due),
        active: form.elements.active.checked,
        paid_from: kind === 'bill' ? form.elements.paid_from.value : 'cash',
        match_text: kind === 'bill' ? (form.elements.match_text.value.trim() || null) : null,
      };
      try {
        if (row) await api.put(`/api/expenses/${row.id}`, body);
        else await api.post('/api/expenses', body);
      } catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) done(); });
}
