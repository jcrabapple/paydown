# API1: Mobile-Readiness API (Paydown backend)

Spec for the Paydown Android app (Flutter, full parity). The existing ~80
endpoints under `/api/*` are the wire contract and do NOT change paths or
shapes; this round adds the three things a mobile client needs and pins the
behavior that already works by accident.

## Decisions

- D1: Auth stays HTTP Basic (single configured user) on every request. No token system,
  no sessions. Mobile stores credentials in platform secure storage and sends
  `Authorization: Basic ...` on every `/api/*` call.
- D2: No re-versioning (`/api/v1`). Existing paths are the contract; the
  OpenAPI schema at `/openapi.json` documents request models. Response models
  stay implicit (documented here for the endpoints the app consumes).
- D3: Error envelope stays FastAPI's `{"detail": ...}` (string, or the
  standard 422 array). 401 + `WWW-Authenticate` on bad credentials, 403
  cross-site, 429 lockout with `Retry-After`, 5xx `detail` strings.
- D4: The CSRF guard treats "no `Origin` and no `Sec-Fetch-Site: cross-site`"
  as a non-browser client and passes it. Flutter's `http` package sends
  neither by default; the app MUST NOT set an `Origin` header. Pinned by test.
- D5: Plaid Link from Android uses the native SDK via `plaid_flutter`. The
  link token must carry `android_package_name`; the app sends it when
  requesting a token. The package name must ALSO be registered in the Plaid
  developer dashboard (app ID + signing-cert SHA-256) or Plaid rejects
  token creation with `INVALID_FIELD Android package name must be configured
  in the developer dashboard` (verified 2026-10-27). Register the
  applicationId of the build you install: the reference Android client
  ships as `us.snakepit.paydown`; a self-built fork with its own id must
  register that id instead, against the SHA-256 of the key that signs it.
  OAuth-redirect banks additionally need an https app-link redirect
  registered (operator task).
- D6: One aggregate endpoint serves the mobile home screen so the app makes
  one request per cold open instead of five.

## Changes

### C1: `POST /api/plaid/link-token` accepts `android_package_name`

`PlaidLinkTokenIn` gains `android_package_name: str | None = None`,
validated against `^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$` (1-5 segments,
segment <= 63 chars). When set, `link_token_create` includes
`android_package_name` in the Plaid request. Response unchanged:
`{"link_token": str, "expiration": str | null}`.

### C2: `GET /api/mobile/home`

Single round-trip payload for the app's home screen. Composes existing
functions only; introduces NO new math.

```jsonc
{
  "month": "YYYY-MM",                    // current month
  "today": "YYYY-MM-DD",
  "budget": { ... },                     // same shape as /api/month/{m}.budget
  "settings": { ... },                   // same shape as /api/month/{m}.settings
  "plan": [ ... ],                       // same rows as /api/month/{m}.plan
  "paid_total_cents": 0,
  "recommended_total_cents": 0,
  "projection": { ... },                 // same shape as /api/month/{m}.projection
  "cash": { ... },                       // same shape as /api/month/{m}.cash
  "cards": [ ... ],                      // _card_view rows, like /api/month
  "safe_to_spend": { ... },              // exact /api/safe-to-spend payload
  "upcoming": [ ... ],                   // exact /api/upcoming?days=30 rows
  "reminders": [ ... ]                   // exact /api/reminders?days=14 rows
}
```

Semantics identical to calling the five endpoints with the current month;
payload is large (projection included) so the app fetches it on cold open
and after mutations, not on tab switches. No caching directives change
(`no-store` on `/api/*` already).

### C3: CSRF-guard regression pin

Test: POST `/api/expenses` (or any mutation) with valid Basic auth and NO
`Origin`/`Sec-Fetch-Site` headers returns 2xx; same request with
`Origin: https://evil.example` returns 403. Documents D4 so a future
hardening pass can't silently break mobile clients.

## Out of scope

- Response `response_model` coverage for all endpoints (YAGNI; app consumes
  the subset documented here).
- API tokens / sessions (D1).
- Plaid dashboard registration of the Android redirect (D5, operator task).
