// Hand-built SVG charts. All sizing via SVG attributes (CSP-safe; no inline styles).
// User text only appears inside <title> tooltips and is escaped by callers via esc().

import { esc, monthAdd, monthShort, usd0 } from './util.js';

function pathFrom(points) {
  return points.map((p, i) => `${i === 0 ? 'M' : 'L'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
}

/** Balance-history sparkline. history: [{logged_on, balance_cents}] (any order). */
export function sparkline(history, { w = 150, h = 40 } = {}) {
  const pts = (history || [])
    .slice()
    .sort((a, b) => (a.logged_on < b.logged_on ? -1 : 1))
    .map((p) => p.balance_cents);
  if (pts.length < 2) {
    return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="Not enough history yet"></svg>`;
  }
  const min = Math.min(...pts);
  const max = Math.max(...pts);
  const span = max - min || 1;
  const coords = pts.map((v, i) => [
    2 + (i / (pts.length - 1)) * (w - 4),
    h - 3 - ((v - min) / span) * (h - 6),
  ]);
  const line = pathFrom(coords);
  const area = `${line} L${coords[coords.length - 1][0].toFixed(1)},${h} L${coords[0][0].toFixed(1)},${h} Z`;
  const label = `Balance from ${usd0(max)} to ${usd0(pts[pts.length - 1])}`;
  return `<svg class="spark" viewBox="0 0 ${w} ${h}" width="${w}" height="${h}" role="img" aria-label="${label}">
    <path class="spark-area" d="${area}"></path>
    <path class="spark-line" d="${line}"></path>
  </svg>`;
}

/**
 * Total balance-over-time chart with stacked per-card areas.
 * schedule: simulate() schedule rows (balances keyed by card id).
 * cards: [{id, name}] participating cards.
 */
export function balanceChart(schedule, cards) {
  if (!schedule || schedule.length < 1) return '';
  const W = 640, H = 240, PL = 54, PB = 22, PT = 8, PR = 10;
  const n = schedule.length;
  const maxTotal = Math.max(...schedule.map((s) => s.total_balance_cents), 1);
  const x = (i) => PL + (i / Math.max(n - 1, 1)) * (W - PL - PR);
  const y = (v) => PT + (1 - v / maxTotal) * (H - PT - PB);
  const ids = cards.map((c) => c.id);

  // Stacked cumulative sums per month.
  const stack = schedule.map(() => 0);
  const series = ids.map((id) => schedule.map((s, i) => {
    stack[i] += (s.balances && s.balances[id]) || 0;
    return stack[i];
  }));
  const palette = ['c1', 'c2', 'c3', 'c4', 'c5', 'c6'];
  let out = '';
  series.forEach((vals, k) => {
    const base = k === 0 ? schedule.map(() => 0) : series[k - 1];
    const top = [...vals].reverse();
    const bot = [...base].reverse();
    const d = vals.map((v, i) => `${i === 0 ? 'M' : 'L'}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(' ') +
      ' ' + top.map((v, j) => {
        const i = n - 1 - j;
        return `L${x(i).toFixed(1)},${y(base[i]).toFixed(1)}`;
      }).join(' ') + ' Z';
    if (vals.some((v) => v > 0)) {
      out += `<path class="area ${palette[k % palette.length]}" d="${d}"><title>${esc(cards[k].name)}</title></path>`;
    }
  });
  const total = pathFrom(schedule.map((s, i) => [x(i), y(s.total_balance_cents)]));
  // Y gridlines: 3 ticks.
  let grid = '';
  for (let t = 0; t <= 2; t++) {
    const v = (maxTotal * t) / 2;
    const yy = y(v).toFixed(1);
    grid += `<line class="grid" x1="${PL}" y1="${yy}" x2="${W - PR}" y2="${yy}"></line>` +
      `<text class="tick" x="${PL - 4}" y="${Number(yy) + 3}" text-anchor="end">${usd0(v)}</text>`;
  }
  const xTicks = [];
  const step = Math.max(1, Math.ceil(n / 6));
  for (let i = 0; i < n; i += step) {
    xTicks.push(`<text class="tick" x="${x(i).toFixed(1)}" y="${H - 6}" text-anchor="middle">${monthShort(monthAdd(schedule[0].month, i))}</text>`);
  }
  const legend = cards.map((c, k) =>
    `<span class="legend-item"><span class="swatch ${palette[k % palette.length]}"></span>${esc(c.name)}</span>`).join('');
  return `<figure class="chart">
    <svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Total balance over time">
      ${grid}${out}
      <path class="total-line" d="${total}"></path>
      ${xTicks.join('')}
    </svg>
    <figcaption class="legend">${legend}<span class="legend-item"><span class="swatch total"></span>Total</span></figcaption>
  </figure>`;
}

/**
 * Per-card payoff timeline: one horizontal bar per card, from month 1 to its payoff month.
 * sim: simulate() result (uses cards[].months_to_payoff and schedule length).
 */
export function payoffTimeline(sim, cardNames) {
  const cards = (sim.cards || []).filter((c) => cardNames.has(c.id) && (c.months_to_payoff > 0 || c.never));
  if (!cards.length) return '';
  const total = sim.schedule ? sim.schedule.length : 0;
  const maxM = Math.max(...cards.map((c) => c.months_to_payoff || 0), 1);
  const rows = cards.map((c) => {
    const m = c.never ? null : c.months_to_payoff;
    const w = m === null ? 100 : Math.max(2, (m / maxM) * 100);
    const label = m === null ? 'never (minimums don’t cover interest)' : `${m} mo`;
    return `<div class="tl-row">
      <span class="tl-name">${esc(cardNames.get(c.id))}</span>
      <span class="tl-bar"><span class="tl-fill ${m === null ? 'never' : ''}" data-w="${w.toFixed(1)}"></span></span>
      <span class="tl-label${m === null ? ' bad' : ''}">${label}</span>
    </div>`;
  }).join('');
  return `<div class="timeline">${rows}</div>`;
}
