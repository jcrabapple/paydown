# Security Policy

Paydown stores financial data and, optionally, Plaid bank-connection tokens.
Please report vulnerabilities privately rather than in a public issue.

**Report via** [GitHub private vulnerability reporting](https://github.com/jcrabapple/paydown/security/advisories/new).
Include the affected version/commit, steps to reproduce, and the impact you
observed. You should get an acknowledgement within a week.

Scope notes:
- Paydown is single-user and self-hosted; the deployment model (HTTP Basic
  behind your own TLS/tunnel) is documented in the README's Security Notes.
- Never include real bank data, Plaid tokens, or `.env` contents in a report.
  If you believe a token leaked, revoke it at Plaid first.
