// Entry point + hash router. Views render async into #app.
// Same-hash re-runs (view refresh) render into a detached container and swap
// it in, preserving scroll position — only real navigations show "Loading…".

import { currentMonth } from './util.js';
import { errText } from './api.js';
import { todayView } from './views/today.js';
import { monthView } from './views/month.js';
import { accountsView } from './views/accounts.js';
import { budgetView } from './views/budget.js';
import { subscriptionsView } from './views/subscriptions.js';
import { planView } from './views/plan.js';
import { settingsView } from './views/settings.js';

const routes = [
  { re: /^#\/today\/?$/, view: (main) => todayView(main) },
  { re: /^#\/month(?:\/(\d{4}-\d{2}))?$/, view: (main, m) => monthView(main, m[1] || currentMonth()) },
  { re: /^#\/accounts\/?$/, view: (main) => accountsView(main) },
  { re: /^#\/cards\/?$/, view: (main) => accountsView(main) }, // legacy alias
  { re: /^#\/budget\/?$/, view: (main) => budgetView(main) },
  { re: /^#\/subscriptions\/?$/, view: (main) => subscriptionsView(main) },
  { re: /^#\/plan\/?$/, view: (main) => planView(main) },
  { re: /^#\/settings\/?$/, view: (main) => settingsView(main) },
];

function markActive() {
  const h = location.hash || `#/today`;
  const rawPrefix = '/' + (h.match(/^#\/([a-z]+)/) || [])[1];
  // Subscriptions lives inside Budget; /cards aliases /accounts.
  const prefix = rawPrefix === '/subscriptions' ? '/budget' : (rawPrefix === '/cards' ? '/accounts' : rawPrefix);
  const match = (a) => {
    const href = a.getAttribute('href') || '';
    return href === `#${prefix}` || (prefix === '/month' && href === '#/month');
  };
  for (const a of document.querySelectorAll('#nav a, .settings-link')) {
    const on = match(a);
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
  for (const a of document.querySelectorAll('#tabbar a')) {
    const on = match(a);
    a.classList.toggle('active', on);
    if (on) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  }
}

let renderSeq = 0;
let lastRenderedHash = null;

async function route() {
  const seq = ++renderSeq;
  const hash = location.hash;
  if (!hash) {
    location.replace('#/today');
    return;
  }
  markActive();
  let matched = false;
  for (const r of routes) {
    const m = r.re.exec(hash);
    if (!m) continue;
    matched = true;
    const same = hash === lastRenderedHash;
    const main = document.getElementById('app');
    if (same) {
      // Refresh of the same view: render off-screen, swap in, keep scroll.
      const scratch = document.createElement('div');
      try {
        await r.view(scratch, m);
        if (seq !== renderSeq) return;
        const y = window.scrollY;
        main.replaceChildren(...scratch.childNodes);
        window.scrollTo(0, y);
      } catch (err) {
        if (seq !== renderSeq) return;
        main.innerHTML = '';
        const box = document.createElement('div');
        box.className = 'banner error';
        box.setAttribute('role', 'alert');
        box.textContent = `Something went wrong: ${errText(err)}`;
        main.appendChild(box);
      }
    } else {
      // Real navigation: show Loading… and start from the top.
      main.innerHTML = '<div class="loading">Loading…</div>';
      window.scrollTo(0, 0);
      try {
        await r.view(main, m);
      } catch (err) {
        if (seq !== renderSeq) return; // superseded by a newer navigation
        main.innerHTML = '';
        const box = document.createElement('div');
        box.className = 'banner error';
        box.setAttribute('role', 'alert');
        box.textContent = `Something went wrong: ${errText(err)}`;
        main.appendChild(box);
      }
    }
    lastRenderedHash = hash;
    break;
  }
  if (!matched) location.replace('#/today');
}

window.addEventListener('hashchange', route);
route();

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js', { scope: '/' }).catch(() => {});
  });
}
