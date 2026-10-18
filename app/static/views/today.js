// Today view: Safe-to-Spend hero, To do strip, breakdown, before-payday list,
// bank payments to log, deposits to review (groups + labels + rules), upcoming 30 days.

import { api, errText } from '../api.js';
import {
  usd, esc, el, field, openFormDialog, errorBox, errorBanner, toast, debounce,
} from '../util.js';
import { detectSchedule } from './budget.js';

const WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

function fmtDay(iso) {
  const d = new Date(`${iso}T12:00:00`);
  return `${WEEKDAYS[d.getDay()].slice(0, 3)} ${MONTHS[d.getMonth()]} ${d.getDate()}`;
}

export async function todayView(main) {
  let sts, deposits, unlogged, upcoming, schedules, goals, cashAccounts, trend = null;
  const fallback = (p, fb) => p.catch((e) => (e && e.status === 404 ? fb : Promise.reject(e)));
  try {
    [sts, deposits, unlogged, upcoming, schedules, goals] = await Promise.all([
      api.get('/api/safe-to-spend'),
      fallback(api.get('/api/deposits?days=45'), []),
      fallback(api.get('/api/card-payments/unlogged'), []),
      fallback(api.get('/api/upcoming?days=30'), []),
      fallback(api.get('/api/pay-schedules'), []),
      fallback(api.get('/api/goals'), []),
    ]);
    trend = await api.get('/api/insights/debt-trend?months=6').catch(() => null);
  } catch (err) {
    main.innerHTML = '';
    errorBox(main, err);
    return;
  }
  try { cashAccounts = (await api.get('/api/accounts')).filter((a) => a.kind === 'cash'); } catch { cashAccounts = []; }

  main.innerHTML = '';
  main.appendChild(hero(sts, trend));
  main.appendChild(todoStrip(sts, unlogged, deposits, schedules, cashAccounts, refresh, logAll));
  const twoCol = el('<div class="grid-2"></div>');
  twoCol.appendChild(beforePaydayPanel(sts, refresh));
  twoCol.appendChild(upcomingPanel(upcoming));
  main.appendChild(twoCol);
  main.appendChild(unloggedPanel(unlogged, refresh));
  main.appendChild(depositsPanel(deposits, schedules, cashAccounts, refresh));

  async function logAll(btn) {
    btn.disabled = true;
    try {
      const res = await api.post('/api/card-payments/log-all');
      toast(`Logged ${res.logged ?? 0} payment${res.logged === 1 ? '' : 's'}`);
      refresh();
    } catch (e) {
      errorBanner(main, errText(e));
    } finally {
      btn.disabled = false;
    }
  }

  function refresh() { window.dispatchEvent(new HashChangeEvent('hashchange')); }
}

/* ---------- Hero ---------- */

function hero(sts, trend) {
  const amt = sts.safe_to_spend_cents;
  const neg = amt < 0;
  let sub;
  const pd = sts.payday;
  if (pd && pd.status === 'today') {
    sub = 'Payday is today';
  } else if (pd && pd.status === 'late') {
    sub = `Paycheck expected ${esc(fmtDay(pd.date))}, not seen yet`;
  } else if (pd) {
    sub = `about <strong class="num">${usd(sts.per_day_cents)}</strong> a day until ${esc(fmtDay(pd.date))} (${sts.days_until} day${sts.days_until === 1 ? '' : 's'})`;
  } else {
    const d = new Date(`${sts.horizon}T12:00:00`);
    sub = `Until ${esc(MONTHS[d.getMonth()])} ${d.getDate()} · <a href="#/budget">add your pay schedule</a> for a more accurate number`;
  }
  const allowance = pd && pd.status !== 'today' && pd.status !== 'late'
    ? `<span class="pill good num">${usd(sts.per_day_cents)}/day</span>`
    : '';
  const growth = growthNote(trend);
  const todaySub = sts.today ? ` · ${esc(fmtDay(sts.today))}` : '';
  const head = el(`<div class="page-head">
    <h1>Today</h1>
    ${todaySub ? `<span class="page-sub muted small">${todaySub.slice(3)}</span>` : ''}
  </div>`);
  const section = el(`<section class="sts-hero panel ${neg ? 'short' : ''}">
    <span class="sts-label">Safe to spend</span>
    ${neg
      ? `<span class="sts-amount">Short by ${usd(-amt)}</span>`
      : `<span class="sts-amount num">${usd(amt)}</span>`}
    <span class="sts-sub">${sub}</span>
    ${allowance}
  </section>`);
  if (growth) section.appendChild(growth);
  section.appendChild(breakdown(sts));
  const frag = document.createDocumentFragment();
  frag.appendChild(head);
  frag.appendChild(section);
  return frag;
}

/* ---------- Growth note (Round 3 R2) ---------- */

/** One-line debt-growth note under the hero, only when debt grew last complete month. */
function growthNote(trend) {
  if (!trend || !Array.isArray(trend.months) || !trend.months.length) return null;
  const last = [...trend.months].reverse().find((m) => !m.partial);
  if (!last || !(last.net_change_cents > 0)) return null;
  return el(`<a class="growth-note" href="#/month">Card balances grew ${usd(last.net_change_cents)} last month. Interest: ${usd(trend.interest_last_30_cents || 0)} in the last 30 days.</a>`);
}

/* ---------- To do strip ---------- */

function todoStrip(sts, unlogged, deposits, schedules, cashAccounts, refresh, logAll) {
  const items = [];
  const warnings = sts.warnings || [];
  const paycheckCount = (deposits || []).filter((d) => d.label === 'paycheck').length;

  for (const w of warnings) {
    if (w.code === 'unlogged_card_payments' && (unlogged || []).length) {
      items.push({
        text: `Log ${unlogged.length} bank payment${unlogged.length === 1 ? '' : 's'}`,
        render(row) {
          const b = el('<button class="btn small primary" data-log-all>Log all</button>');
          b.addEventListener('click', () => logAll(b));
          row.appendChild(b);
        },
      });
    } else if (w.code === 'unlabelled_income') {
      const m = /(\d+)/.exec(w.message || '');
      const n = m ? Number(m[1]) : (deposits || []).filter((d) => !d.label).length;
      items.push({
        text: `Review ${n} deposits`,
        render(row) {
          const b = el('<button class="btn small">Review</button>');
          b.addEventListener('click', () =>
            document.querySelector('[data-panel="deposits"]')?.scrollIntoView({ behavior: 'smooth' }));
          row.appendChild(b);
        },
      });
    } else if (w.code === 'no_pay_schedule') {
      if (paycheckCount >= 2) {
        items.push({
          text: 'Set up pay schedule',
          render(row) {
            const b = el('<button class="btn small">Set up</button>');
            b.addEventListener('click', () => detectSchedule(document.getElementById('app'), schedules, refresh, null, cashAccounts));
            row.appendChild(b);
          },
        });
      } else {
        items.push({
          text: 'Set up pay schedule',
          render(row) { row.appendChild(el('<a class="btn small" href="#/budget">Go to Budget</a>')); },
        });
      }
    } else if (w.code === 'undated_bills') {
      items.push({
        text: 'Add dates to undated bills',
        render(row) { row.appendChild(el('<a class="btn small" href="#/budget">Go to Budget</a>')); },
      });
    } else if (w.code !== 'short') {
      // The "short" warning is already the hero's headline — never repeat it.
      items.push({ text: w.message, render() {} });
    }
  }

  if (!items.length) return el('<div data-todo hidden></div>');
  const strip = el('<section class="panel todo-strip" data-todo aria-label="To do"><h2>To do</h2><ul class="list"></ul></section>');
  const ul = strip.querySelector('ul');
  for (const item of items) {
    const li = el('<li><span class="grow name"></span><span data-act></span></li>');
    li.querySelector('.name').textContent = item.text;
    item.render(li.querySelector('[data-act]'));
    ul.appendChild(li);
  }
  return strip;
}

/* ---------- Breakdown ---------- */

function breakdown(sts) {
  const billRes = (sts.obligations || []).filter((o) => o.kind === 'bill' && o.reserved).reduce((s, o) => s + o.amount_cents, 0);
  const cardRes = (sts.obligations || []).filter((o) => o.kind === 'card' && o.reserved).reduce((s, o) => s + o.amount_cents, 0);
  const logged = Math.max(0, sts.cash_total_cents + (sts.pending_out_cents || 0) + billRes + cardRes
    + (sts.goals_set_aside_cents || 0) + (sts.cash_floor_cents || 0) - sts.safe_to_spend_cents);
  // "card payments logged, not yet at the bank" = pending_out
  const pending = sts.pending_out_cents || 0;
  const rows = [];

  rows.push(`<div class="w-row"><span>Cash in checking &amp; savings</span><span class="val">${usd(sts.cash_total_cents)}</span></div>`);
  if (pending > 0) rows.push(`<div class="w-row sub minus"><span>Card payments logged, not yet at the bank</span><span class="val">${usd(pending)}</span></div>`);
  rows.push(`<div class="w-row sub minus"><span>Bills before payday</span><span class="val">${usd(billRes)}</span></div>`);
  rows.push(`<div class="w-row sub minus"><span>Card minimums before payday</span><span class="val">${usd(cardRes)}</span></div>`);
  if ((sts.goals_set_aside_cents || 0) > 0) rows.push(`<div class="w-row sub minus"><span>Set aside for goals</span><span class="val">${usd(sts.goals_set_aside_cents)}</span></div>`);
  if ((sts.cash_floor_cents || 0) > 0) rows.push(`<div class="w-row sub minus"><span>Cash floor</span><span class="val">${usd(sts.cash_floor_cents)}</span></div>`);
  rows.push(`<div class="w-row total"><span>= Safe to spend</span><span class="val">${usd(sts.safe_to_spend_cents)}</span></div>`);
  // Planned extra is informational — planned, not owed — never subtracted.
  if ((sts.planned_extra_cents || 0) > 0) {
    rows.push(`<div class="w-row extra-line"><span>Planned extra card payments (optional)</span><span class="val">${usd(sts.planned_extra_cents)}</span></div>`);
  }

  const planned = sts.planned_spending_cents || 0;
  const stss = sts.safe_to_spend_cents;
  let plannedLine;
  if (planned === 0) {
    plannedLine = `No everyday spending in your budget yet. <a href="#/budget">Add groceries, gas, etc. on Budget</a>.`;
  } else if (stss >= planned) {
    plannedLine = `Planned spending until payday: ${usd(planned)} — covered.`;
  } else {
    plannedLine = `Planned spending until payday: ${usd(planned)} — <strong class="bad">${usd(planned - stss)} short</strong>.`;
  }

  const resTotal = sts.obligations_total_cents || (billRes + cardRes);
  const det = el(`<details class="sub-disclosure sts-breakdown">
    <summary><span>How this is calculated</span><span class="muted small num">${usd(sts.cash_total_cents)} cash − ${usd(resTotal)} reserved</span></summary>
    <div class="waterfall">${rows.join('')}</div>
    <p class="small muted num">${plannedLine}</p>
    ${(sts.accounts || []).length ? `<p class="small muted">${sts.accounts.map((a) => esc(a.name)).join(', ')}</p>` : ''}
  </details>`);
  // Start closed everywhere: the hero stays ultra-clean and scannable.
  det.open = false;
  return det;
}

/* ---------- Before payday ---------- */

const OB_PILL = {
  due: ['warn', 'due'],
  overdue: ['bad', 'overdue'],
  covered: ['good', 'min paid'],
  paid: ['good', 'paid'],
  on_card: ['off', 'on card'],
  spread: ['off', 'spread'],
};

function beforePaydayPanel(sts, refresh) {
  const panel = el(`<section class="panel">
    <h2>Before payday</h2>
    <div data-ob-list></div>
  </section>`);
  const box = panel.querySelector('[data-ob-list]');
  const obs = sts.obligations || [];
  if (!obs.length) {
    box.appendChild(el('<p class="small muted">Nothing is due before your next payday.</p>'));
    return panel;
  }
  const ul = el('<ul class="list"></ul>');
  for (const o of obs) {
    const [pillClass, pillText] = OB_PILL[o.status] || ['off', o.status];
    const extra = o.kind === 'card' && o.extra_remaining_cents > 0
      ? `<span class="muted small num">+$${(o.extra_remaining_cents / 100).toFixed(2)} extra planned</span>` : '';
    const li = el(`<li>
      <span class="grow"><span class="name">${esc(o.name)}</span>
        <span class="muted small">${esc(fmtDay(o.date))}${o.undated ? ' · undated' : ''}</span>
        ${extra}</span>
      <span class="num">${usd(o.amount_cents)}</span>
      <span class="pill ${pillClass}">${esc(pillText)}</span>
      <span data-act></span>
    </li>`);
    const act = li.querySelector('[data-act]');
    if ((o.kind === 'bill') && (o.status === 'due' || o.status === 'overdue')) {
      const b = el('<button class="btn small" data-mark>Mark paid</button>');
      b.addEventListener('click', async () => {
        try { await api.post(`/api/bills/${o.id}/paid`, { due_date: o.date }); }
        catch (e) { errorBanner(panel, errText(e)); return; }
        refresh();
      });
      act.appendChild(b);
    } else if (o.kind === 'bill' && o.status === 'paid') {
      const b = el('<button class="linklike" data-undo>Undo</button>');
      b.addEventListener('click', async () => {
        try { await api.del(`/api/bills/${o.id}/paid/${o.date}`); }
        catch (e) { errorBanner(panel, errText(e)); return; }
        refresh();
      });
      act.appendChild(b);
    } else if (o.kind === 'card' && (o.status === 'due' || o.status === 'overdue')) {
      const a = el(`<a class="linklike" href="#/month/${esc(o.date.slice(0, 7))}">Log payment</a>`);
      act.appendChild(a);
    }
    ul.appendChild(li);
  }
  box.appendChild(ul);
  return panel;
}

/* ---------- Bank payments to log ---------- */

function unloggedPanel(unlogged, refresh) {
  const rows = unlogged || [];
  if (!rows.length) return el('<div data-panel="unlogged" hidden></div>');
  const panel = el(`<section class="panel" data-panel="unlogged">
    <div class="section-head">
      <h2>Card payments from your bank not logged yet</h2>
      <button class="btn small" data-log-all>Log all</button>
    </div>
    <p class="small muted">These payments show at the bank but aren’t in Paydown — logging them avoids double-counting.</p>
    <div data-ul-list></div>
  </section>`);
  panel.querySelector('[data-log-all]').addEventListener('click', async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await api.post('/api/card-payments/log-all');
      toast(`Logged ${res.logged ?? 0} payment${res.logged === 1 ? '' : 's'}`);
      refresh();
    } catch (err) {
      errorBanner(panel, errText(err));
      btn.disabled = false;
    }
  });
  const box = panel.querySelector('[data-ul-list]');
  const ul = el('<ul class="list"></ul>');
  for (const u of rows) {
    const li = el(`<li>
      <span class="grow"><span class="name">${esc(u.card_name)}</span>
        <span class="muted small">${esc(fmtDay(u.date))} · ${esc(u.name)}</span></span>
      <span class="num">${usd(u.amount_cents)}</span>
      <button class="btn small primary" data-log>Log it</button>
      <button class="linklike" data-dismiss>Dismiss</button>
    </li>`);
    li.querySelector('[data-log]').addEventListener('click', async () => {
      try { await api.post(`/api/card-payments/${u.transaction_id}/log`); }
      catch (e) { errorBanner(panel, errText(e)); return; }
      refresh();
    });
    li.querySelector('[data-dismiss]').addEventListener('click', async () => {
      try { await api.post(`/api/card-payments/${u.transaction_id}/dismiss`); }
      catch (e) { errorBanner(panel, errText(e)); return; }
      refresh();
    });
    ul.appendChild(li);
  }
  box.appendChild(ul);
  return panel;
}

/* ---------- Deposits to review ---------- */

/** Normalize a deposit name for grouping: uppercase, digits & punctuation stripped, spaces collapsed. */
function normalizeName(name) {
  return String(name || '').toUpperCase().replace(/[^A-Z ]/g, ' ').replace(/\s+/g, ' ').trim();
}

function depositsPanel(deposits, schedules, cashAccounts, refresh) {
  const unlabelled0 = (deposits || []).filter((d) => !d.label);
  const headHtml = unlabelled0.length > 0
    ? `<h2>Deposits to review <span class="pill warn">${unlabelled0.length}</span></h2>`
    : '<h2>Deposits to review</h2>';
  const panel = el(`<section class="panel" data-panel="deposits">
    <div class="section-head">
      ${headHtml}
      <label class="toggle"><input type="checkbox" data-transfers> Show transfers</label>
    </div>
    <div data-dep-list></div>
  </section>`);
  const box = panel.querySelector('[data-dep-list]');
  let current = deposits;

  const render = (rows) => {
    box.innerHTML = '';
    if (!rows.length) {
      box.appendChild(el('<p class="small muted">No deposits in the last 45 days.</p>'));
      return;
    }
    const activeSchedules = schedules.filter((s) => s.active);

    // Unlabelled first, then by date descending.
    rows.sort((a, b) => (!!a.label) - (!!b.label) || b.date.localeCompare(a.date));

    const unlabelled = rows.filter((d) => !d.label);
    const labelled = rows.filter((d) => d.label);

    // Everything reviewed: a compact summary state instead of the full checklist.
    if (!unlabelled.length) {
      box.appendChild(el('<p class="small muted">All recent deposits reviewed.</p>'));
      if (labelled.length) {
        const det = el(`<details class="dep-labelled"><summary>Labelled (${labelled.length})</summary><ul class="list dep-list"></ul></details>`);
        const lul = det.querySelector('ul');
        for (const d of labelled) lul.appendChild(depositRow(d, schedules.filter((s) => s.active), refresh, panel));
        box.appendChild(det);
      }
      return;
    }

    // Group unlabelled deposits by normalized name when a group has >= 2 rows.
    const groups = new Map();
    const singles = [];
    for (const d of unlabelled) {
      const key = normalizeName(d.merchant_name || d.name);
      if (!key) { singles.push(d); continue; }
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(d);
    }
    const ul = el('<ul class="list dep-list"></ul>');
    for (const [, rowsInGroup] of groups) {
      if (rowsInGroup.length < 2) { singles.push(...rowsInGroup); continue; }
      // rowsInGroup is date-descending; the first is the newest.
      rowsInGroup.sort((a, b) => b.date.localeCompare(a.date));
      const display = rowsInGroup[0].merchant_name || rowsInGroup[0].name;
      const total = rowsInGroup.reduce((s, d) => s + d.amount_cents, 0);
      const li = el(`<li class="dep-group">
        <details>
          <summary>
            <span class="grow"><span class="name">${esc(display)}</span>
              <span class="muted small">· ${rowsInGroup.length} deposits ·</span></span>
            <span class="num">+${usd(total)}</span>
          </summary>
          <ul class="list small dep-group-rows"></ul>
        </details>
        <span class="dep-group-actions" data-act></span>
      </li>`);
      const inner = li.querySelector('.dep-group-rows');
      for (const d of rowsInGroup) {
        inner.appendChild(el(`<li>
          <span class="muted small">${esc(fmtDay(d.date))}</span>
          <span class="name small">${esc(d.name)}</span>
          <span class="num small">${usd(d.amount_cents)}</span>
        </li>`));
      }
      const act = li.querySelector('[data-act]');
      const ids = rowsInGroup.map((d) => d.transaction_id);
      const np = el('<button class="btn small" data-np>Not paychecks</button>');
      np.addEventListener('click', async () => {
        try { await api.post('/api/transactions/labels', { transaction_ids: ids, label: 'not_paycheck', pay_schedule_id: null }); }
        catch (e) { errorBanner(panel, errText(e)); return; }
        toast(`Labelled ${ids.length} deposits`);
        refresh();
      });
      const ig = el('<button class="linklike" data-ig>Always ignore</button>');
      ig.addEventListener('click', async () => {
        try {
          await api.put(`/api/transactions/${rowsInGroup[0].transaction_id}/label`, {
            label: 'not_paycheck', pay_schedule_id: null, create_rule: true, rule_text: display,
          });
        } catch (e) { errorBanner(panel, errText(e)); return; }
        toast('Rule saved — future deposits like this are ignored');
        refresh();
      });
      act.appendChild(np);
      act.appendChild(ig);
      ul.appendChild(li);
    }
    for (const d of singles) ul.appendChild(depositRow(d, activeSchedules, refresh, panel));
    box.appendChild(ul);

    if (labelled.length) {
      const det = el(`<details class="dep-labelled"><summary>Labelled (${labelled.length})</summary><ul class="list dep-list"></ul></details>`);
      const lul = det.querySelector('ul');
      for (const d of labelled) lul.appendChild(depositRow(d, activeSchedules, refresh, panel));
      box.appendChild(det);
    }
  };

  panel.querySelector('[data-transfers]').addEventListener('change', async (e) => {
    const includeTransfers = e.target.checked;
    try {
      current = await api.get(`/api/deposits?days=45&include_transfers=${includeTransfers}`);
      render(current);
    } catch (err) {
      box.innerHTML = '';
      errorBanner(box, errText(err));
    }
  });

  render(current);
  return panel;
}

function depositRow(d, activeSchedules, refresh, panel) {
  const labelled = !!d.label;
  const ruleSuffix = d.label_source === 'rule'
    ? (d.rule_text ? ` · via rule “${esc(d.rule_text)}”` : ' · rule')
    : '';
  const autoPill = d.label_source === 'auto' ? '<span class="pill extra">auto</span>' : '';
  const pill = d.label === 'paycheck'
    ? `<span class="pill good">Paycheck${d.pay_schedule_name ? ` · ${esc(d.pay_schedule_name)}` : ''}${ruleSuffix}</span>`
    : d.label === 'not_paycheck'
      ? `<span class="pill off">Not a paycheck${ruleSuffix}</span>`
      : '<span class="pill warn">needs review</span>';
  const li = el(`<li>
    <span class="grow">
      <span class="name">${esc(d.name)}</span>
      <span class="muted small">${esc(fmtDay(d.date))} · ${esc(d.account_name || '')}${d.mask ? ` ••${esc(d.mask)}` : ''}</span>
    </span>
    <span class="num good">+${usd(d.amount_cents)}</span>
    ${pill}
    ${autoPill}
    <span data-act></span>
  </li>`);
  const act = li.querySelector('[data-act]');
  if (labelled) {
    const b = el('<button class="linklike" data-change>Change</button>');
    b.addEventListener('click', () => labelDialog(d, activeSchedules, refresh));
    act.appendChild(b);
  } else {
    const pk = el('<button class="btn small primary" data-pk>Paycheck</button>');
    const np = el('<button class="btn small" data-np>Not a paycheck</button>');
    pk.addEventListener('click', () => labelDialog(d, activeSchedules, refresh));
    np.addEventListener('click', async () => {
      try {
        await api.put(`/api/transactions/${d.transaction_id}/label`, { label: 'not_paycheck', pay_schedule_id: null, create_rule: false, rule_text: null });
      } catch (e) { errorBanner(panel, errText(e)); return; }
      refresh();
    });
    act.appendChild(pk);
    act.appendChild(np);
  }
  return li;
}

function labelDialog(d, activeSchedules, refresh) {
  const opts = activeSchedules.map((s) =>
    `<option value="${s.id}"${d.pay_schedule_id === s.id ? ' selected' : ''}>${esc(s.name)}</option>`).join('');
  const prefill = d.merchant_name || d.name;
  const MIN_RULE_LEN = 4;
  openFormDialog({
    title: 'Label as paycheck',
    submitLabel: 'Save',
    body: `
      <p class="small muted">${esc(d.name)} · ${esc(fmtDay(d.date))} · <span class="num">+${usd(d.amount_cents)}</span></p>
      ${field('Which pay schedule', `<select name="pay_schedule_id">
        <option value="">No schedule yet</option>${opts}</select>`)}
      <label class="field"><span class="lbl">Always treat deposits matching</span>
        <input type="checkbox" name="create_rule"></label>
      <label class="field" data-rule-field hidden><span class="lbl">Matching text</span>
        <input name="rule_text" maxlength="80" value="${esc(prefill)}" autocomplete="off">
        <span class="hint">Saves a rule so future deposits like this are labelled automatically.</span></label>
      <p class="form-err rule-text-err" hidden></p>
      <div class="rule-preview" data-rule-preview hidden></div>
    `,
    onMount(form) {
      const cb = form.elements.create_rule;
      const ruleField = form.querySelector('[data-rule-field]');
      const preview = form.querySelector('[data-rule-preview]');
      const err = form.querySelector('.rule-text-err');
      let seq = 0;
      const runPreview = async (text) => {
        const my = ++seq;
        let res;
        try {
          res = await api.get(`/api/deposit-rules/preview?text=${encodeURIComponent(text)}&exclude=${encodeURIComponent(d.transaction_id)}`);
        } catch (e) {
          if (my !== seq) return;
          preview.hidden = true;
          err.textContent = errText(e);
          err.hidden = false;
          return;
        }
        if (my !== seq) return;
        const n = res.count || 0;
        const lot = n >= 10;
        const samples = (res.samples || []).map((s) =>
          `<li><span class="muted small">${esc(s.date)}</span> <span class="name">${esc(s.name)}</span> <span class="num">${usd(s.amount_cents)}</span></li>`).join('');
        preview.innerHTML = `
          <p class="small ${lot ? 'bad' : 'muted'}">Also labels ${n} other deposit${n === 1 ? '' : 's'}${lot ? '' : ' — these will be labelled too'}.</p>
          ${lot ? '<p class="form-err" role="alert">That’s a lot. Make the text more specific if these aren’t all paychecks.</p>' : ''}
          ${samples ? `<ul class="list small">${samples}</ul>` : ''}`;
        preview.hidden = false;
      };
      const sync = () => {
        ruleField.hidden = !cb.checked;
        if (!cb.checked) {
          preview.hidden = true;
          err.hidden = true;
          return;
        }
        const text = form.elements.rule_text.value.trim();
        if (text.length < MIN_RULE_LEN) {
          preview.hidden = true;
          err.textContent = `Matching text must be at least ${MIN_RULE_LEN} characters.`;
          err.hidden = false;
          return;
        }
        err.hidden = true;
        debouncedPreview(text);
      };
      const debouncedPreview = debounce(runPreview, 300);
      cb.addEventListener('change', sync);
      form.elements.rule_text.addEventListener('input', sync);
      sync();
    },
    onSubmit: async (form) => {
      const createRule = form.elements.create_rule.checked;
      let ruleText = null;
      if (createRule) {
        ruleText = form.elements.rule_text.value.trim();
        if (ruleText.length < MIN_RULE_LEN) throw new Error(`Matching text must be at least ${MIN_RULE_LEN} characters.`);
      }
      const sched = form.elements.pay_schedule_id.value;
      const body = {
        label: 'paycheck',
        pay_schedule_id: sched === '' ? null : Number(sched),
        create_rule: createRule,
        rule_text: ruleText,
      };
      try {
        await api.put(`/api/transactions/${d.transaction_id}/label`, body);
      } catch (e) { throw new Error(errText(e)); }
    },
  }).then((ok) => { if (ok) refresh(); });
}

/* ---------- Coming up ---------- */

const KIND_ORDER = { payday: 0, bill: 1, card: 2, goal: 3 };

function upcomingPanel(upcoming) {
  const panel = el(`<section class="panel">
    <h2>Coming up (30 days)</h2>
    <div data-up-list></div>
  </section>`);
  const box = panel.querySelector('[data-up-list]');
  const rows = (upcoming || []).slice().sort((a, b) =>
    a.date.localeCompare(b.date) || (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9));
  if (!rows.length) {
    box.appendChild(el('<p class="small muted">Nothing scheduled in the next 30 days.</p>'));
    return panel;
  }
  let lastDate = null;
  let ul = null;
  let restBox = null;
  let shown = 0;
  const LIMIT = 6;
  let det = null;
  for (const r of rows) {
    const overflow = shown >= LIMIT;
    if (overflow && !restBox) {
      det = el(`<details class="sub-disclosure up-more"><summary>Show all ${rows.length} upcoming items</summary></details>`);
      restBox = el('<div></div>');
      det.appendChild(restBox);
      box.appendChild(det);
      lastDate = null; // start a fresh date group inside the disclosure
    }
    if (r.date !== lastDate) {
      ul = el(`<div class="up-date"><h3 class="small muted">${esc(fmtDay(r.date))}</h3><ul class="list"></ul></div>`);
      lastDate = r.date;
      (overflow ? restBox : box).appendChild(ul);
    }
    shown += 1;
    const isPayday = r.kind === 'payday';
    const li = el(`<li class="${isPayday ? 'up-payday' : ''}">
      <span class="grow"><span class="name">${esc(r.name)}</span>
        <span class="pill ${isPayday ? 'extra' : 'off'}">${esc(r.kind)}</span></span>
      ${r.kind === 'card' && r.min_remaining_cents != null
        ? `<span class="num">${usd(r.min_remaining_cents)}${r.extra_remaining_cents > 0 ? `<br><span class="small muted">+${usd(r.extra_remaining_cents)} extra planned</span>` : ''}</span>`
        : `<span class="num">${usd(r.amount_cents)}</span>`}
    </li>`);
    ul.querySelector('ul').appendChild(li);
  }
  return panel;
}
