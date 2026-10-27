// Shared helpers: money, months, DOM, dialogs. No dependencies.

const usdFmt = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD' });
const usdFmt0 = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });

/** Format integer cents as USD. */
export function usd(cents) {
  if (cents === null || cents === undefined || Number.isNaN(cents)) return '—';
  return usdFmt.format(cents / 100);
}

/** Format cents as USD with no decimals (for chart axis labels). */
export function usd0(cents) {
  return usdFmt0.format((cents || 0) / 100);
}

/**
 * Parse a dollars input string ("1,234.56", "$12", "-40") into exact integer cents.
 * String arithmetic only — no float drift. Returns null for blank input.
 * Throws Error with a human message on malformed input.
 */
export function parseCents(input) {
  if (input === null || input === undefined) return null;
  let t = String(input).trim().replace(/^\$/, '').replace(/[,\s]/g, '');
  if (t === '') return null;
  const m = /^(-?)(\d+)(?:\.(\d{1,2}))?$/.exec(t);
  if (!m) throw new Error('Enter dollars like 1,234.56');
  const sign = m[1] === '-' ? -1 : 1;
  const cents = Number(m[2]) * 100 + Number((m[3] || '0').padEnd(2, '0'));
  return sign * cents;
}

/** Parse an APR percent input ("24.99") into a float, or null if blank. Throws on junk. */
export function parseApr(input) {
  const t = String(input ?? '').trim().replace(/%$/, '');
  if (t === '') return null;
  if (!/^\d{1,2}(\.\d{1,4})?$|^\d{1,2}\.$/.test(t)) throw new Error('Enter APR as a percent like 24.99');
  const v = Number(t);
  if (!(v >= 0 && v < 100)) throw new Error('APR must be between 0 and 99.99');
  return v;
}

export function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function pad(n) { return String(n).padStart(2, '0'); }

export function currentMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

export function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

export function monthAdd(month, n) {
  const [y, mo] = month.split('-').map(Number);
  const d = new Date(y, mo - 1 + n, 1);
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}`;
}

const MONTH_NAMES = ['January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December'];

export function monthName(month) {
  const [y, mo] = month.split('-').map(Number);
  return `${MONTH_NAMES[mo - 1]} ${y}`;
}

/** "2026-10" -> "Oct ’26" (short axis label). */
export function monthShort(month) {
  const [y, mo] = month.split('-').map(Number);
  return `${MONTH_NAMES[mo - 1].slice(0, 3)} ’${String(y).slice(2)}`;
}

export function debounce(fn, ms) {
  let t = null;
  return (...args) => {
    clearTimeout(t);
    t = setTimeout(() => fn(...args), ms);
  };
}

/** Build an element from an HTML string (trusted templates only; escape user data first). */
export function el(html) {
  const t = document.createElement('template');
  t.innerHTML = html.trim();
  return t.content.firstElementChild;
}

/** Read a money input's value into cents, or null if blank; surfaces parse errors via onErr. */
export function centsValue(input) {
  return parseCents(input.value);
}

/**
 * Open a modal <dialog> with a form.
 * body: HTML string of fields (labels wrap inputs — accessible without ids).
 * onSubmit(formEl): async; throw to keep the dialog open and show the error inline.
 * Resolves with true when submitted successfully, false on cancel/dismiss.
 */
export function openFormDialog({ title, body, submitLabel = 'Save', onSubmit, onMount }) {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.className = 'dlg';
    dlg.innerHTML = `
      <form novalidate>
        <h2>${esc(title)}</h2>
        <div class="dlg-body">${body}</div>
        <p class="form-err" hidden></p>
        <div class="dlg-actions">
          <button type="button" class="btn ghost" data-cancel>Cancel</button>
          <button type="submit" class="btn primary">${esc(submitLabel)}</button>
        </div>
      </form>`;
    document.body.appendChild(dlg);
    const form = dlg.querySelector('form');
    if (typeof onMount === 'function') onMount(form);
    const errEl = dlg.querySelector('.form-err');
    const close = (v) => { dlg.close(); resolve(v); };
    dlg.querySelector('[data-cancel]').addEventListener('click', () => close(false));
    dlg.addEventListener('close', () => { dlg.remove(); resolve(false); });
    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      errEl.hidden = true;
      const btn = form.querySelector('button[type="submit"]');
      btn.disabled = true;
      try {
        await onSubmit(form);
        dlg.close();
        resolve(true);
      } catch (e) {
        errEl.textContent = e && e.message ? e.message : String(e);
        errEl.hidden = false;
      } finally {
        btn.disabled = false;
      }
    });
    dlg.showModal();
    const first = form.querySelector('input, select, textarea');
    if (first) first.focus();
  });
}

export function confirmDialog(message, danger = true) {
  return new Promise((resolve) => {
    const dlg = document.createElement('dialog');
    dlg.className = 'dlg';
    dlg.innerHTML = `
      <div class="dlg-body"><p>${esc(message)}</p></div>
      <div class="dlg-actions">
        <button type="button" class="btn ghost" data-cancel>Cancel</button>
        <button type="button" class="btn ${danger ? 'danger' : 'primary'}" data-ok>Confirm</button>
      </div>`;
    document.body.appendChild(dlg);
    const close = (v) => { dlg.close(); resolve(v); };
    dlg.querySelector('[data-cancel]').addEventListener('click', () => close(false));
    dlg.querySelector('[data-ok]').addEventListener('click', () => close(true));
    dlg.addEventListener('close', () => { dlg.remove(); resolve(false); });
    dlg.showModal();
    dlg.querySelector('[data-ok]').focus();
  });
}

/** A labeled field wrapping its control (accessible without ids). */
export function field(label, controlHtml, hint) {
  return `<label class="field"><span class="lbl">${esc(label)}</span>${controlHtml}` +
    (hint ? `<span class="hint">${esc(hint)}</span>` : '') + `</label>`;
}

export const moneyInput = (name, valueAttrs = '') =>
  `<input class="num-input" name="${esc(name)}" inputmode="decimal" autocomplete="off" ${valueAttrs}>`;

/** Render an API error inside a container as a dismissible box. */
export function errorBox(container, err) {
  const msg = err && err.message ? err.message : String(err);
  container.appendChild(el(
    `<div class="banner error" role="alert">Something went wrong: ${esc(msg)}</div>`));
}

/** Empty-state walkthrough panel. */
export function emptyState(container) {
  container.appendChild(el(`
    <div class="empty panel">
      <h2>Welcome to Paydown</h2>
      <p>Set up your monthly plan in three steps:</p>
      <ol class="steps">
        <li><a href="#/budget">Add your income</a> — paycheck, side gigs, anything that arrives monthly.</li>
        <li><a href="#/budget">Add your bills &amp; spending</a> — rent, utilities, groceries. Don’t add card minimums here; Paydown counts those separately.</li>
        <li><a href="#/accounts">Add your credit cards</a> — balance, APR, and minimum payment for each.</li>
      </ol>
      <p class="muted">Checking and savings accounts are optional — add them on <a href="#/accounts">Accounts</a> if you want Paydown to find spare cash for your cards.</p>
      <p class="muted">Then come back to <a href="#/month">This month</a> to see exactly what to pay.</p>
    </div>`));
}

/**
 * Transient, non-blocking confirmation ("Saved.", "Logged 9 payments").
 * Success/info only: errors use errorBox/banners so they stay until read.
 * kind: 'good' | 'info'. Stacks bottom-center, auto-hides after `ms`.
 */
export function toast(message, kind = 'good', ms = 4000) {
  let host = document.getElementById('toasts');
  if (!host) {
    host = document.createElement('div');
    host.id = 'toasts';
    host.className = 'toasts';
    host.setAttribute('role', 'status');
    host.setAttribute('aria-live', 'polite');
    document.body.appendChild(host);
  }
  const t = document.createElement('div');
  t.className = `toast ${kind === 'info' ? 'info' : 'good'}`;
  t.textContent = message;
  host.appendChild(t);
  setTimeout(() => {
    t.classList.add('out');
    setTimeout(() => t.remove(), 300);
  }, ms);
}

/**
 * Error banner that stays until dismissed (replaces 6-second auto-vanishing
 * error banners). Prepended to `container`; returns the element.
 */
export function errorBanner(container, message) {
  const box = el(`<div class="banner error dismissible" role="alert">
    <span class="msg"></span>
    <button type="button" class="banner-x" aria-label="Dismiss">×</button></div>`);
  box.querySelector('.msg').textContent = message;
  box.querySelector('.banner-x').addEventListener('click', () => box.remove());
  container.prepend(box);
  return box;
}
