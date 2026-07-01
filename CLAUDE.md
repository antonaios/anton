# CLAUDE.md — ANTON workspace guide (for contributors)

> Orientation for anyone working in this repository with Claude Code (or any
> agentic coding tool). The operating *rules* the assistant follows when it runs
> ANTON live in the vault constitution (`vault/CLAUDE.md`); **this** file is
> about the code.

## Repo map

| Path | What it is |
|---|---|
| `routines/` | The bridge: FastAPI app, skills, scheduler, and the central sensitivity guard. Python package at `routines/routines/`. Has its own `CLAUDE.md` — the SKILL.md authoring contract. |
| `dashboard/` | React/TS operator dashboard; the bridge serves the built assets in production. |
| `engine/` | Python valuation engine — the only place numerical work happens. LLMs never do the maths. |
| `templates/` | Bundled corporate-finance deal-folder structure (and model templates). New workspaces are scaffolded from here. |
| `vault/` | A starter vault skeleton plus the operating constitution (`vault/CLAUDE.md`). |

## Core principles

- **No LLM does the maths.** Numerical work goes through the engine; the model picks inputs and narrates outputs. See `vault/CLAUDE.md` §5.
- **Sensitivity-aware routing.** Every task carries a sensitivity tier (public / internal / confidential / MNPI). Confidential and MNPI stay on local models by default. The central `before_llm_call` guard enforces this — no skill, composite, or crew can bypass it.
- **Configurable, not hardcoded.** Filesystem roots, sectors, and operator identity come from `vault/_claude/profile.md` and environment variables (`.env`), never hardcoded. If you find an operator- or firm-specific value baked into code, treat it as a deployment-readiness bug and move it to the config layer.
- **Auditable over clever.** Prefer deterministic, logged workflows; the audit trail wins over elegance.

## Getting started

`README.md` covers the install flow; `CONTRIBUTING.md` covers dev setup, conventions, and how to propose changes. The SKILL.md authoring contract is in `routines/CLAUDE.md`.
