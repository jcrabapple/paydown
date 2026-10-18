// Plaid Link helpers shared by the Cards view and the OAuth redirect page.
// No inline scripts/styles (CSP): the Link SDK is loaded once via a created
// <script src> element. Every Plaid-provided string must be esc()ed by callers.

import { api, errText } from './api.js';
import { esc } from './util.js';

/** "2026-10-02T14:05:11Z" -> "2 hours ago" (relative words, best-effort). */
export function relTime(iso) {
  if (!iso) return 'never';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return 'never';
  const s = Math.max(0, Math.floor((Date.now() - t) / 1000));
  if (s < 45) return 'just now';
  const m = Math.floor(s / 60);
  if (m < 60) return m === 1 ? '1 minute ago' : `${m} minutes ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return h === 1 ? '1 hour ago' : `${h} hours ago`;
  const d = Math.floor(h / 24);
  if (d < 30) return d === 1 ? '1 day ago' : `${d} days ago`;
  return new Date(t).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/** Remember the link token + mode ('new' | 'update:<item_id>') for the OAuth page. */
export function storeLinkContext(linkToken, mode) {
  sessionStorage.setItem('paydown.linkToken', linkToken);
  sessionStorage.setItem('paydown.linkMode', mode);
}

export function clearLinkContext() {
  sessionStorage.removeItem('paydown.linkToken');
  sessionStorage.removeItem('paydown.linkMode');
}

let linkScriptPromise = null;

/** Load https://cdn.plaid.com/link/v2/stable/link-initialize.js once. */
export function loadPlaidLink() {
  if (window.Plaid) return Promise.resolve();
  if (linkScriptPromise) return linkScriptPromise;
  linkScriptPromise = new Promise((resolve, reject) => {
    const s = document.createElement('script');
    s.src = 'https://cdn.plaid.com/link/v2/stable/link-initialize.js';
    s.addEventListener('load', () => resolve());
    s.addEventListener('error', () => {
      linkScriptPromise = null;
      reject(new Error('Could not load the Plaid Link script. Check your connection and try again.'));
    });
    document.head.appendChild(s);
  });
  return linkScriptPromise;
}

/**
 * Open Plaid Link. mode: 'new' or 'update:<item_id>'.
 * The link token must already be in sessionStorage (storeLinkContext).
 * receivedRedirectUri: pass only on the OAuth redirect page.
 * Resolves { public_token, institution_name } when the user succeeds;
 * rejects with an Error when Link exits with an error.
 */
export function openPlaidLink(mode, receivedRedirectUri = null) {
  return loadPlaidLink().then(() => new Promise((resolve, reject) => {
    const token = sessionStorage.getItem('paydown.linkToken');
    if (!token) {
      reject(new Error('Missing link token — close Link and try again.'));
      return;
    }
    let result = null;
    let exitErr = null;
    const opts = {
      token,
      onSuccess: (public_token, metadata) => {
        result = {
          public_token,
          institution_name: metadata && metadata.institution ? metadata.institution.name : null,
        };
      },
      onExit: (err) => { exitErr = err || null; },
    };
    if (receivedRedirectUri) opts.receivedRedirectUri = receivedRedirectUri;
    try {
      window.Plaid.create(opts).open();
    } catch (e) {
      reject(new Error(errText(e)));
      return;
    }
    // Link calls back from its popup/iframe; wait until onSuccess or onExit fires.
    const started = Date.now();
    const timer = setInterval(() => {
      if (result) {
        clearInterval(timer);
        resolve(result);
      } else if (exitErr) {
        clearInterval(timer);
        reject(new Error(exitErr.display_message || exitErr.error_message || 'Plaid Link was closed.'));
      } else if (Date.now() - started > 10 * 60 * 1000) {
        clearInterval(timer);
        reject(new Error('Plaid Link timed out.'));
      }
    }, 250);
  }));
}

/** After a successful Link: exchange (new item) or re-sync (update mode). */
export async function finishLink(mode, result) {
  if (mode === 'new') {
    return api.post('/api/plaid/exchange', {
      public_token: result.public_token,
      institution_name: result.institution_name,
    });
  }
  return api.post('/api/plaid/sync');
}

/** "ok" -> Connected, "login_required" -> Needs re-login, else Error <code>. */
export function itemStatusPill(item) {
  if (item.status === 'ok') return '<span class="pill good">Connected</span>';
  if (item.status === 'login_required') return '<span class="pill warn">Needs re-login</span>';
  return `<span class="pill bad">Error${item.error_code ? ` · ${esc(item.error_code)}` : ''}</span>`;
}