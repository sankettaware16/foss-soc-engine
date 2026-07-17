# Security Policy

TLSOC is security software — vulnerability reports are taken seriously and
handled with priority. This policy applies to every repository in the TLSOC
ecosystem.

## Supported versions

Security fixes are applied to the `main` branch of each repository and included
in the next tagged release. Older tags do not receive backported fixes.

## Reporting a vulnerability

**Do not open a public issue for security problems.**

Report privately using either channel:

1. **GitHub private vulnerability reporting** (preferred): on the affected
   repository, go to **Security → Report a vulnerability**.
2. **Email**: sankettaware1612@gmail.com with the subject line
   `[TLSOC SECURITY] <short summary>`.

Please include:

- The affected repository and, if known, file/component and commit or version.
- A description of the vulnerability and its impact.
- Reproduction steps or a proof of concept.
- Any suggested remediation.

## What to expect

- **Acknowledgement** of your report within 7 days.
- An assessment and, for accepted reports, a fix targeted to `main` with a
  changelog entry that credits you (unless you prefer anonymity).
- Coordinated disclosure: please give us reasonable time to release a fix before
  publishing details.

## Scope notes

- The TLSOC stack is designed for deployment on trusted networks. Reports about
  intentionally documented behavior (for example, the engine Web UI defaulting to
  plain HTTP on `127.0.0.1`) are welcome as hardening suggestions via normal
  issues rather than vulnerability reports.
- Real deployment data (IPs, hostnames, credentials, log excerpts) must not be
  included in reports — reproduce with placeholder data.
