# Contributing to ANTON

Thanks for looking at ANTON. This repository is **shared for review, testing, and suggestions** —
it is a one‑way public mirror of a working, single‑operator private system. That shapes how
contributions work, so please read this first.

## What to expect

- **Issues are open.** Bug reports, design critique, architecture questions, and "why on earth did
  you do it this way" are all welcome. Well‑reasoned disagreement is a gift.
- **Pull requests are open** — but please **open an issue first** for anything non‑trivial. Because
  this mirror is regenerated from a private tree, not every PR can be merged as‑is; accepted
  changes are typically **re‑applied upstream by hand and credited** (`Suggested-by:`), then flow
  back out in the next snapshot.
- **No support SLA.** This is shared in the spirit of feedback, not as a supported product.
  Responses are best‑effort.
- **Security issues do _not_ go in public issues.** See [`SECURITY.md`](SECURITY.md).

## Ground rules

- **Keep PRs focused and small.** One idea per PR; a clear before/after in the description.
- **Match the existing style.** Python is linted with `ruff`; the dashboard follows its configured
  ESLint/Prettier conventions. Read the surrounding code and mirror it.
- **Add or update tests for behavioural changes** (`routines/tests`, `engine/tests`). The bridge
  ships thousands of tests for a reason — keep them green.
- **Respect the architecture's load‑bearing rules.** In particular:
  - No LLM computes a number — numerical work goes through the `engine/`.
  - No code path may reach a cloud model for `confidential` / `MNPI` work; everything routes
    through the central sensitivity guard.
  - The engines (`synapse` / crews) are reached over HTTP/subprocess only — **never** `import` them
    into `routines/`.
- **Never add real client, personal, or confidential data anywhere** — fixtures included. This is a
  public mirror; treat every file as world‑readable forever.

## Good first contributions

- Cross‑platform fixes (the project is Windows‑first; macOS/Linux gaps are real and welcome).
- Tightening or adding tests around the sensitivity guard and routing.
- Documentation clarity — if something in `docs/` didn't match the code, that's a bug worth filing.

By contributing, you agree your contributions are licensed under the repository's
[MIT License](LICENSE).
