// Plan view: unified simulator panel (strategy + extra slider + lump sum),
// hero projection KPI strip, comparison table, payoff timeline, balance chart.

import { api, errText, currentExtra } from '../api.js';
import {
  usd, esc, monthName, el, field, moneyInput, parseCents, debounce,
  errorBox, emptyState, errorBanner,
} from '../util.js';
import { balanceChart, payoffTimeline } from '../charts.js';

export async function planView(main) {
  let settings, cards, bundle;
  try {
    [settings, cards, bundle] = await Promise.all([
      api.get('/api/settings'),
      api.get('/api/cards'),
      api.get(`/api/month/${curMonth()}`),
    ]);
  } catch (err) {
    main.innerHTML = '';
    errorBox(main, err);
    return;
  }
  main.innerHTML = '';
  main.appendChild(el('<div class="page-head"><h1>Plan</h1></div>'));

  const open = cards.filter((c) => !c.closed && c.balance_cents > 0);
  if (!open.length) {
    emptyState(main);
    return;
  }

  // Recurring suggestion (excludes this month's one-offs) so the slider and its label describe a normal month.
  const suggested = currentExtra(null, bundle.budget || {});
  const current = currentExtra(settings, bundle.budget || {});
  // Slider range: 0..2x suggested (fallback when suggested is 0 so the slider is still usable).
  const maxExtra = suggested > 0 ? suggested * 2 : Math.max(current, 20000);

  const state = { strategy: settings.strategy, extra: current, lump: 0, proj: null };

  // --- APR source banner (unknown cards treated as 0% by the projection)
  const unknownApr = open.filter((c) => c.apr_effective_source === 'unknown');
  const inferredApr = open.filter((c) => c.apr_effective_source === 'inferred');
  if (unknownApr.length) {
    main.appendChild(el(`<div class="banner warn-banner" role="alert">APR unknown for ${unknownApr.map((c) => esc(c.name)).join(', ')}; the projection treats it as 0%. Set it on <a href="#/accounts">Accounts</a>.</div>`));
  }
  if (inferredApr.length) {
    main.appendChild(el(`<div class="banner note" role="status">APR estimated from interest charges for ${inferredApr.map((c) => esc(c.name)).join(', ')}.</div>`));
  }

  // --- Unified simulator panel: strategy picker + extra slider + lump sum in one deck.
  const NOTES = {
    avalanche: 'Highest APR first — minimizes total interest.',
    snowball: 'Smallest balance first — quickest wins.',
    custom: 'Pay cards in your own order.',
  };
  const sim = el(`<section class="panel simulator-panel">
    <div class="section-head"><h2>Simulator</h2></div>
    <div class="seg" role="group" aria-label="Payoff strategy">
      <button type="button" data-strategy="avalanche">Avalanche</button>
      <button type="button" data-strategy="snowball">Snowball</button>
      <button type="button" data-strategy="custom">Custom</button>
    </div>
    <p class="small muted" id="strat-note"></p>
    <div class="order" hidden></div>
    <hr class="rule">
    <label class="lbl" for="extra-slider">Extra payment per month</label>
    <div class="slider-row">
      <input type="range" min="0" max="${maxExtra}" step="100" value="${Math.min(current, maxExtra)}" id="extra-slider" aria-label="Extra payment per month">
      <span class="slider-val num" id="extra-val"></span>
    </div>
    <p class="small muted">
      ${suggested > 0 ? `Suggested from budget: ${usd(suggested)}/mo. Slider goes up to ${usd(maxExtra)}.` : 'No surplus suggested — drag to experiment anyway.'}
      ${state.extra !== suggested ? '<button type="button" class="linklike" data-reset-extra>Reset to suggested</button>' : ''}
    </p>
    <form class="inline-form">
      <label class="field"><span class="lbl">One-time lump sum ($)</span><input class="num-input" name="lump" inputmode="decimal" value="0" autocomplete="off"></label>
      <button class="btn small" type="submit">Apply</button>
    </form>
  </section>`);
  main.appendChild(sim);

  const segBtns = [...sim.querySelectorAll('[data-strategy]')];
  const orderBox = sim.querySelector('.order');
  const note = sim.querySelector('#strat-note');
  const slider = sim.querySelector('#extra-slider');
  const sliderVal = sim.querySelector('#extra-val');

  function renderOrder() {
    if (state.strategy !== 'custom') { orderBox.hidden = true; return; }
    orderBox.hidden = false;
    orderBox.innerHTML = '';
    const sorted = cards.filter((c) => !c.closed)
      .slice()
      .sort((a, b) => (a.priority - b.priority) || (a.id - b.id));
    sorted.forEach((c, i) => {
      const row = document.createElement('div');
      row.className = 'tl-row';
      row.innerHTML = `
        <span class="tl-name">${esc(c.name)}</span>
        <span class="muted small">#${i + 1}</span>
        <span>
          <button class="btn small ghost" data-up aria-label="Move ${esc(c.name)} up" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button class="btn small ghost" data-down aria-label="Move ${esc(c.name)} down" ${i === sorted.length - 1 ? 'disabled' : ''}>↓</button>
        </span>`;
      const swap = async (dir) => {
        const other = sorted[i + dir];
        try {
          await Promise.all([
            api.put(`/api/cards/${c.id}`, { priority: other.priority }),
            api.put(`/api/cards/${other.id}`, { priority: c.priority }),
          ]);
        } catch (e) { showError(main, errText(e)); return; }
        // Re-fetch authoritative order, then re-query projection
        try { cards = await api.get('/api/cards'); } catch { /* keep local */ }
        renderOrder();
        queryProjection();
      };
      row.querySelector('[data-up]').addEventListener('click', () => swap(-1));
      row.querySelector('[data-down]').addEventListener('click', () => swap(1));
      orderBox.appendChild(row);
    });
  }

  for (const btn of segBtns) {
    btn.addEventListener('click', async () => {
      state.strategy = btn.dataset.strategy;
      try { await api.put('/api/settings', { strategy: state.strategy }); }
      catch (e) { showError(main, errText(e)); return; }
      syncSeg();
      renderOrder();
      queryProjection();
    });
  }
  function syncSeg() {
    for (const b of segBtns) b.setAttribute('aria-pressed', String(b.dataset.strategy === state.strategy));
    note.textContent = NOTES[state.strategy] || '';
  }
  syncSeg();
  renderOrder();

  const showVal = () => { sliderVal.textContent = usd(state.extra); };
  showVal();
  slider.addEventListener('input', debounce(() => {
    state.extra = Number(slider.value);
    showVal();
    queryProjection();
  }, 250));
  const resetBtn = sim.querySelector('[data-reset-extra]');
  if (resetBtn) resetBtn.addEventListener('click', () => {
    state.extra = suggested;
    slider.value = Math.min(suggested, maxExtra);
    showVal();
    queryProjection();
  });
  sim.querySelector('form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    let lump;
    try { lump = parseCents(ev.currentTarget.elements.lump.value); }
    catch (e) { showError(main, e.message); return; }
    state.lump = Math.max(0, lump || 0);
    queryProjection();
  });

  // --- Results
  const results = el('<div></div>');
  main.appendChild(results);

  async function queryProjection() {
    const url = `/api/projection?extra=${state.extra}&strategy=${encodeURIComponent(state.strategy)}&lump=${state.lump}`;
    let proj;
    try {
      proj = await api.get(url);
    } catch (e) {
      showError(main, errText(e));
      return;
    }
    state.proj = proj;
    renderResults(proj);
  }

  function renderResults(proj) {
    results.innerHTML = '';
    const chosen = proj[state.strategy] || proj.avalanche;
    if (!chosen) return;

    // Hero KPI strip for the chosen strategy.
    const tiles = el(`<section class="tiles">
      <div class="tile"><span class="t-label">Debt-free date</span><span class="t-value num">${chosen.debt_free_month ? esc(monthName(chosen.debt_free_month)) : 'Not in sight'}</span><span class="t-note">${chosen.months ? `${chosen.months} months` : '—'}</span></div>
      <div class="tile"><span class="t-label">Total interest</span><span class="t-value num">${usd(chosen.total_interest_cents)}</span><span class="t-note">under ${STRAT_LABEL[state.strategy]}</span></div>
      <div class="tile"><span class="t-label">Interest saved</span><span class="t-value num good">${proj.interest_saved_cents == null ? '—' : usd(proj.interest_saved_cents)}</span><span class="t-note">${proj.months_saved ? `${proj.months_saved} mo faster vs minimums` : 'vs minimums only'}</span></div>
    </section>`);
    results.appendChild(tiles);

    // Charts side-by-side on desktop.
    const names = new Map(open.map((c) => [c.id, c.name]));
    const grid = el('<div class="grid-2"></div>');
    const chartPanel = el('<section class="panel"><h2>Balance over time</h2><div class="chart-wrap"></div></section>');
    chartPanel.querySelector('.chart-wrap').innerHTML = balanceChart(chosen.schedule, open);
    grid.appendChild(chartPanel);

    const tlPanel = el('<section class="panel"><h2>Per-card payoff timeline</h2></section>');
    tlPanel.insertAdjacentHTML('beforeend', payoffTimeline(chosen, names));
    // CSP-safe bar widths via CSSOM (no inline style attributes)
    for (const fill of tlPanel.querySelectorAll('.tl-fill')) {
      fill.style.setProperty('width', `${fill.dataset.w}%`);
    }
    grid.appendChild(tlPanel);
    results.appendChild(grid);

    // Comparison table
    const rows = [
      ['Avalanche', proj.avalanche],
      ['Snowball', proj.snowball],
      ['Custom', proj.custom],
      ['Minimums only', proj.minimums_only],
    ];
    const table = el(`<section class="panel">
      <h2>Compare strategies</h2>
      <div class="table-scroll"><table class="table">
        <thead><tr><th>Strategy</th><th class="r">Debt-free</th><th class="r">Months</th><th class="r">Total interest</th></tr></thead>
        <tbody></tbody>
      </table></div>
      <p class="small muted">${proj.interest_saved_cents == null
        ? 'Minimums alone never pay these cards off.'
        : `Your strategy saves <strong class="good">${usd(proj.interest_saved_cents || 0)}</strong> in interest and
      ${proj.months_saved ? `<strong>${proj.months_saved}</strong> months` : 'time'} vs minimums only.`}</p>
    </section>`);
    const tbody = table.querySelector('tbody');
    for (const [label, simRow] of rows) {
      if (!simRow) continue;
      const never = label === 'Minimums only' &&
        (proj.minimums_only_never === true || simRow.debt_free_month == null);
      const tr = el(`<tr class="${label === STRAT_LABEL[state.strategy] ? 'chosen' : ''}">
        <td>${label}</td>
        <td class="r">${never ? 'Never' : (simRow.debt_free_month ? esc(monthName(simRow.debt_free_month)) : (simRow.underfunded ? '—' : '600+ mo'))}</td>
        <td class="r">${never ? '—' : (simRow.months ?? '—')}</td>
        <td class="r">${never ? '—' : usd(simRow.total_interest_cents)}</td>
      </tr>`);
      tbody.appendChild(tr);
    }
    results.appendChild(table);

    if (chosen.underfunded) {
      results.appendChild(el(`<div class="banner error" role="alert">Budget is underfunded — it doesn’t even cover the minimums. Adjust <a href="#/budget">Budget</a>.</div>`));
    }
  }

  await queryProjection();
}

const STRAT_LABEL = { avalanche: 'Avalanche', snowball: 'Snowball', custom: 'Custom' };


function showError(main, msg) {
  errorBanner(main, msg); // errors stay until dismissed
}

function curMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}`;
}
