// Settings view: buffer, debt share %, extra override, export/import.

import { api, errText } from '../api.js';
import {
  usd, esc, el, field, moneyInput, parseCents, openFormDialog,
  errorBox, confirmDialog, toast, errorBanner,
} from '../util.js';

export async function settingsView(main) {
  let settings;
  try {
    settings = await api.get('/api/settings');
  } catch (err) {
    main.innerHTML = '';
    errorBox(main, err);
    return;
  }
  main.innerHTML = '';
  main.appendChild(el(`<div class="page-head"><h1>Settings</h1><a class="btn small ghost" href="#/budget">← Back to Budget</a></div>`));

  // Real <form> element (P4) — no innerHTML-copy hack.
  const formEl = el(`<form class="panel" id="settings-form">
    <h2>Budget preferences</h2>
    <fieldset>
      <legend>Monthly payoff math</legend>
      ${field('Buffer ($ kept out of the debt budget each month)', moneyInput('buffer', `value="${((settings.buffer_cents || 0) / 100).toFixed(2)}"`), 'A cushion for surprises; not assigned to debt.')}
      ${field('Debt share (%)', `<input class="num-input" name="debt_share" inputmode="numeric" value="${settings.debt_share_pct ?? 100}">`, 'Percent of monthly surplus suggested as extra debt payment (0–100).')}
      ${field('Extra payment override ($, blank = use suggestion)', moneyInput('extra_override', `value="${settings.extra_override_cents == null ? '' : (settings.extra_override_cents / 100).toFixed(2)}"`), 'Leave blank to use the suggested extra each month.')}
    </fieldset>
    <fieldset>
      <legend>Cash reserve &amp; automation</legend>
      ${field('Cash floor ($)', moneyInput('cash_floor', `value="${((settings.cash_floor_cents || 0) / 100).toFixed(2)}"`), 'Kept in checking before any cash counts as spare.')}
      ${field('Reserve (months of essentials)', `<input class="num-input" name="cash_reserve_months" inputmode="decimal" value="${settings.cash_reserve_months ?? 1.0}">`, 'Months of bills, spending and minimums kept in cash (0–6).')}
      <label class="field"><span class="lbl">Log card payments from my bank automatically</span>
        <input type="checkbox" name="auto_log_card_payments"${settings.auto_log_card_payments === undefined || settings.auto_log_card_payments ? ' checked' : ''}>
        <span class="hint">When a card payment shows up in your bank feed, record it on the card for you.</span>
      </label>
    </fieldset>
    <button class="btn primary" type="submit">Save settings</button>
  </form>`);
  main.appendChild(formEl);

  formEl.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      const f = ev.currentTarget;
      const buffer = parseCents(f.elements.buffer.value);
      if (buffer === null || buffer < 0) throw new Error('Buffer must be zero or more.');
      const share = f.elements.debt_share.value.trim() === '' ? 100 : Number(f.elements.debt_share.value);
      if (!(Number.isInteger(share) && share >= 0 && share <= 100)) throw new Error('Debt share must be a whole number 0–100.');
      const overrideRaw = f.elements.extra_override.value.trim();
      const override = overrideRaw === '' ? null : parseCents(overrideRaw);
      if (override !== null && override < 0) throw new Error('Extra override must be zero or more.');
      const cashFloor = parseCents(f.elements.cash_floor.value);
      if (cashFloor === null || cashFloor < 0) throw new Error('Cash floor must be zero or more.');
      const reserveRaw = f.elements.cash_reserve_months.value.trim();
      const reserve = reserveRaw === '' ? 1.0 : Number(reserveRaw);
      if (!(reserve >= 0 && reserve <= 6)) throw new Error('Reserve must be between 0 and 6 months.');
      await api.put('/api/settings', {
        buffer_cents: buffer,
        debt_share_pct: share,
        extra_override_cents: override,
        cash_floor_cents: cashFloor,
        cash_reserve_months: reserve,
        auto_log_card_payments: f.elements.auto_log_card_payments.checked,
      });
      toast('Saved.');
    } catch (e) {
      errorBanner(main, e.message);
    }
  });

  // Deposit rules + Your data share a two-column row so Settings fits one screen.
  const bottomGrid = el('<div class="grid-2"></div>');
  // Deposit rules (phase 4)
  try {
    const [rules, schedules] = await Promise.all([
      api.get('/api/deposit-rules'),
      api.get('/api/pay-schedules').catch(() => []),
    ]);
    const panel = el(`<section class="panel">
      <h2>Deposit rules</h2>
      <p class="small muted">Deposits whose bank description matches are labelled automatically. <a href="#/today">Review deposits</a> to add rules.</p>
      <div data-rules-list></div>
    </section>`);
    const box = panel.querySelector('[data-rules-list]');
    if (!rules.length) {
      box.appendChild(el('<p class="small muted">No rules yet.</p>'));
    } else {
      const ul = el('<ul class="list"></ul>');
      for (const r of rules) {
        const schedName = r.pay_schedule_id
          ? ((schedules.find((s) => s.id === r.pay_schedule_id) || {}).name || '') : null;
        const li = el(`<li>
          <span class="grow"><span class="name">“${esc(r.match_text)}”</span>
            ${r.label === 'paycheck' ? '<span class="pill good">paycheck</span>' : '<span class="pill off">not a paycheck</span>'}
            <span class="muted small">${r.labelled_count ? `labelled ${r.labelled_count} deposit${r.labelled_count === 1 ? '' : 's'}` : 'no deposits labelled'}</span>
            ${schedName ? `<span class="muted small">${esc(schedName)}</span>` : ''}</span>
          <button class="linklike" data-del>Delete rule</button>
          <button class="linklike" data-unlabel ${r.labelled_count ? '' : 'hidden'}>Delete and unlabel ${r.labelled_count || 0}</button>
        </li>`);
        li.querySelector('[data-del]').addEventListener('click', async () => {
          try { await api.del(`/api/deposit-rules/${r.id}`); }
          catch (e) { errorBanner(panel, errText(e)); return; }
          window.dispatchEvent(new HashChangeEvent('hashchange'));
        });
        li.querySelector('[data-unlabel]').addEventListener('click', async () => {
          const n = r.labelled_count || 0;
          const ok = await confirmDialog(
            `Delete this rule and remove the paycheck label from the ${n} deposit${n === 1 ? '' : 's'} it labelled? They'll need to be reviewed again. Continue?`);
          if (!ok) return;
          try { await api.del(`/api/deposit-rules/${r.id}?clear_labels=true`); }
          catch (e) { errorBanner(panel, errText(e)); return; }
          window.dispatchEvent(new HashChangeEvent('hashchange'));
        });
        ul.appendChild(li);
      }
      box.appendChild(ul);
    }
    bottomGrid.appendChild(panel);
  } catch { /* deposit rules not available */ }

  // Export
  const dataPanel = el(`<section class="panel">
    <h2>Your data</h2>
    <p class="small muted">Download everything as JSON, or import a previous export (this replaces all current data).</p>
    <div class="card-actions">
      <button class="btn" type="button" data-export>Export JSON</button>
      <button class="btn" type="button" data-import>Import JSON</button>
    </div>
  </section>`);
  bottomGrid.appendChild(dataPanel);
  main.appendChild(bottomGrid);

  dataPanel.querySelector('[data-export]').addEventListener('click', async () => {
    try {
      const dump = await api.get('/api/export');
      const blob = new Blob([JSON.stringify(dump, null, 2)], { type: 'application/json' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `paydown-export-${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (e) {
      errorBanner(dataPanel, errText(e));
    }
  });

  dataPanel.querySelector('[data-import]').addEventListener('click', () => {
    const input = document.createElement('input');
    input.type = 'file';
    input.accept = 'application/json,.json';
    input.addEventListener('change', async () => {
      const file = input.files && input.files[0];
      if (!file) return;
      let text;
      try { text = await file.text(); } catch (e) { errorBanner(dataPanel, 'Could not read the file.'); return; }
      let dump;
      try { dump = JSON.parse(text); } catch { errorBanner(dataPanel, 'That file is not valid JSON.'); return; }
      const ok = await confirmDialog(
        'Importing replaces ALL current data (cards, payments, income, expenses, settings) with the file’s contents. Continue?',
        true);
      if (!ok) return;
      try {
        await api.post('/api/import', dump);
        window.dispatchEvent(new HashChangeEvent('hashchange'));
      } catch (e) {
        errorBanner(dataPanel, errText(e));
      }
    });
    input.click();
  });
}
