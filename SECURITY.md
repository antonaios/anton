# Security Policy

## Reporting a vulnerability

**Please do not report security vulnerabilities through public GitHub issues, discussions, or
pull requests.**

Instead, use **GitHub's private vulnerability reporting** for this repository:
**Security → Report a vulnerability** (the "Report a vulnerability" button on the repo's
*Security* tab). It opens a private advisory visible only to the maintainer and you.

If private reporting is not enabled for your fork, contact the maintainer through the address on
their GitHub profile rather than filing a public issue.

Please include, where possible:

- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- the affected component (**bridge** / **engine** / **dashboard**) and the commit you tested.

You will get a best‑effort acknowledgement. This is a mirror of a private, single‑operator system
shared for feedback — there is no formal SLA — but security reports are taken seriously and
triaged ahead of other work.

## What's in scope

This project's whole point is a **sensitivity guard** that keeps `confidential` and `MNPI`
material on a local model lane and never lets it reach a cloud provider by default. The most
valuable reports are ones that undermine that guarantee:

- **Any way to make a `confidential` or `MNPI` task reach a cloud lane** — a routing path that
  skips the central `before_llm_call` hook, a lane (skill / composite / crew) that dispatches
  without passing the guard, a sensitivity‑downgrade, or a fail‑*open* default.
- **Secret exposure** — a credential, token, or path that escapes the encrypted credentials store,
  the redaction pipeline, or the audit sanitiser.
- **Loopback escape** — anything that lets a non‑loopback caller reach a bridge route that asserts
  loopback‑only.
- **Path / write‑policy escape** — a write that lands outside the permitted vault/workspace
  locations, or overwrites an operating‑rule file.
- Standard web classes in the bridge or dashboard (injection, SSRF, CSRF, auth bypass).

## What's out of scope

- The absence of a feature that is documented as **not yet built** (see `docs/OVERVIEW.md`).
- Findings that require a non‑default, explicitly operator‑enabled escalation that is itself
  documented, audited, and fail‑closed (e.g. the default‑off enterprise‑MNPI attestation gate).
- Secrets you introduce into your own fork.

## A note on secrets

This mirror ships **no secrets**. `.env.example` contains variable *names* with placeholder
values only, and git history is not published. If you believe a real secret has nonetheless
leaked into the repository, report it privately as above — don't open a public issue.
