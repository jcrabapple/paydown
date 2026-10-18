// Minimal same-origin fetch wrapper. The browser supplies HTTP Basic auth
// credentials natively — no auth headers here.

export class ApiError extends Error {
  constructor(status, detail) {
    super(typeof detail === 'string' ? detail : `Request failed (${status})`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

function detailText(detail) {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    // FastAPI 422 validation errors
    return detail.map((d) => {
      const loc = (d.loc || []).filter((p) => p !== 'body').join('.');
      return `${loc}: ${d.msg}`;
    }).join('; ');
  }
  if (detail && typeof detail === 'object') {
    return Object.entries(detail).map(([k, v]) => `${k}: ${v}`).join('; ') || 'Request failed';
  }
  return 'Request failed';
}

async function request(method, path, body) {
  const opts = { method };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, opts);
  } catch (e) {
    throw new ApiError(0, 'Network error — is the server running?');
  }
  if (res.status === 204) return null;
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON error body */ }
  if (!res.ok) throw new ApiError(res.status, data && data.detail !== undefined ? data.detail : res.statusText);
  return data;
}

export const api = {
  get: (path) => request('GET', path),
  post: (path, body) => request('POST', path, body ?? {}),
  put: (path, body) => request('PUT', path, body ?? {}),
  del: (path) => request('DELETE', path),
};

/** Human-readable message from any thrown error (422 detail included). */
export function errText(err) {
  if (err instanceof ApiError) return detailText(err.detail);
  return err && err.message ? err.message : String(err);
}

/** Extra cents: override wins, else the budget suggestion. */
export function currentExtra(settings, budget) {
  if (settings && settings.extra_override_cents !== null && settings.extra_override_cents !== undefined) {
    return settings.extra_override_cents;
  }
  // Recurring extra only: this month's one-offs (bonus, cash sweeps) must not be
  // projected as if they repeat every month.
  if (budget && budget.recurring_debt_budget_cents != null) {
    return Math.max(0, budget.recurring_debt_budget_cents - (budget.minimums_total_cents || 0));
  }
  return (budget && budget.extra_cents) || 0;
}
