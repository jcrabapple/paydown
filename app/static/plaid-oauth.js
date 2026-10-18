// OAuth redirect page for Plaid Link (no inline scripts — CSP: script-src 'self').
// Reads the link token + mode stashed by the Cards view, re-initializes Link with
// receivedRedirectUri, then exchanges (new item) or re-syncs (update mode) and
// redirects back to the app.

import { api, errText } from './api.js';
import { openPlaidLink, finishLink, clearLinkContext } from './plaid.js';

const statusEl = document.querySelector('[data-status]');
const errEl = document.querySelector('[data-err]');

function fail(msg) {
  if (errEl) { errEl.textContent = msg; errEl.hidden = false; }
  if (statusEl) statusEl.textContent = 'The connection didn’t finish. You can try again from the Cards page.';
}

const mode = sessionStorage.getItem('paydown.linkMode') || 'new';

try {
  const res = await openPlaidLink(mode, window.location.href);
  await finishLink(mode, res);
  clearLinkContext();
  if (statusEl) statusEl.textContent = 'Connected. Redirecting…';
  location.href = '/#/cards';
} catch (e) {
  fail(errText(e));
}
