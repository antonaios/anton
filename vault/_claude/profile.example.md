---
type: operator-profile
memory_kind: procedural
operator: <operator-name>             # full name of the person operating this Agentic OS
operator_slug: <operator-slug>        # short handle — the DEFAULT owner for action items
                                      # (`- [ ] task [owner:<slug>]`), and the actor id in audit
                                      # logs + tag values. Routines (sectors, BD, actions) read it.
qualifications: [<qualification>]     # e.g. [ACA] / [CFA] / [MBA] — informational only
years-experience: <years>
current_role:
  title: <role-title>                 # e.g. "Associate Director", "VP", "Principal"
  firm: "<firm-name>"                 # plain string, or "[[Companies/<Firm>]]" once a Company page exists
  description: <one line on what you do — sectors, client types, mandate mix>
career_arc:
  # Free-text history. Informational context for cross-sector reasoning — replace with
  # your own. Each line: "<prior firm> — <what you did there>".
  - "<prior-firm> — <role / focus>"
  - "<prior-firm> — <role / focus>"
  - "<firm-name> — <current role>"
active_sectors:
  # CURRENT focus. New `Sectors/<X>.md` enrichment, sector routines, and default framing
  # derive from this list. Replace with your own — the system is sector-agnostic.
  - <Sector A>
  - <Sector B>
  - <Sector C>
sector_sub_lens:
  # Optional granularity within active sectors. Keyed form `- <Sector>: <descriptor> (sub / sub)`
  # so the comps Stage-0 parser produces granular subsector slugs. Multiple lines per sector
  # compose. Slugs land in comps subsector proposals and `Sectors/<sector>/Comps.md`.
  - "<Sector A>: <segment> (<sub> / <sub> / <sub>)"
  - "<Sector B>: <segment> (<sub> / <sub>)"
sector_lens_directives:
  # Framing rules applied across sector_sub_lens — read by sessions for voice/framing.
  - "<e.g. 'UK-first; EU/US comparables when helpful, label geography clearly'>"
  - "<e.g. 'use post-event-normalised data; avoid trough-year comps inflating growth'>"
career_sectors:
  # Cross-sector breadth — sectors you've worked but aren't actively covering now.
  - <Sector D>
  - <Sector E>
working_language: en-GB               # en-GB | en-US | ...
voice_preferences:
  register: precise, hedge-light, domain-literate, investor-grade
  avoid:
    - corporate filler
    - '"as we previously discussed" preambles'
    - sycophancy
    - false precision (e.g. 4 dp where 1 dp is supported)
  date_format: "YYYY-MM-DD"
  decimal: "."
  currency_format: "{symbol}{number} (no space). £12bn, €37.4bn, $2.1bn"
  output_preferences:
    - "Structured outputs with clear tables"
    - "Concise but complete analysis"
    - "Sources in the body of the answer when research is involved"
    - "Explicit definitions of metrics"
    - "No unsupported assumptions"
    - "Clear differentiation between facts, assumptions, recommendations"
  for_finance_outputs:
    # Mandatory whenever producing financial analysis.
    - "Show currency"
    - "Show period (FY / LTM / NTM / budget / forecast / estimated)"
    - "Show source"
    - "Show calculation basis"
    - "Distinguish enterprise value vs equity value (and market cap, transaction value)"
    - "Explain bridges from EV to equity value when material"
    - "Don't mix calendar years / financial years / LTM without labelling"
plan_tier: bridge                     # bridge | enterprise — read by routing.py
sensitivity_defaults:
  default_lane: cloud-claude          # default lane for NON-sensitive work
  confidential_during_bridge: ollama-local
  mnpi_always: ollama-local           # MNPI never leaves the machine at consumer/bridge tier
heartbeat_preferences:
  # Prefer local / cheap models for background, health-check, and triage work —
  # don't burn premium cloud tokens on poll cycles.
  preferred_models:
    - "ollama/qwen3:8b"
    - "ollama/qwen3:14b"
  avoid_models:
    - "<premium-cloud-model>"
engine:
  repo: "<absolute-path-to>/engine"
  templates_dir: "<absolute-path-to>/engine/templates/models"
# Where real client work lands on disk (governed by the workspace-write-policy).
# Vault Projects/ is for TEST / public-data projects; real mandates live under these paths.
external_project_paths:
  - "<absolute-path-to-your-projects-root>"
external_bd_path: "<absolute-path-to-your-business-development-root>"
external_general_path: "<absolute-path-to-your-general-workspaces-root>"
sessions_path: "<absolute-path-to-your-sessions-dir>"
tldr: Configurable operator profile. Edit this file to switch sectors, change role/firm, or deploy this Agentic OS to a different operator/team.
tags: [profile, config, claude-config]
---

# Operator profile (example)

> **This file is the single source of truth for "who is operating this Agentic OS".**
> `CLAUDE.md` references this file rather than hardcoding values, so switching sectors,
> changing roles, or deploying to a different operator is a one-file edit.
>
> This is the **example** shipped with the public mirror. Copy it to `profile.md` in your
> own vault's `_claude/` directory and replace every `<placeholder>` with your details.

## How to read this

The frontmatter above is the **structured config** — read at the start of any vault session.
The body below is human-facing context.

## How to switch sectors

Edit the `active_sectors:` list in the frontmatter. Stub `Sectors/<X>.md` if it doesn't
exist. Sector-specific knowledge accumulates in `Sectors/<X>.md` and persists across role
transitions. `career_sectors:` is informational only — it tells the system what cross-sector
breadth to assume when reasoning.

## How to deploy this Agentic OS to another operator / team

The vault structure is operator-agnostic. To deploy to a new operator:

1. **Clone the vault skeleton** (everything *except* personal artefacts):
   ```
   _claude/                  ← keep CLAUDE.md, replace profile.md
   Templates/                ← keep
   Sectors/                  ← keep stubs; new operator enriches
   Topics/                   ← keep
   Inbox/                    ← keep folder structure
   Registers/                ← keep schema
   Routines/                 ← keep
   .gitignore                ← keep
   ```
2. **Drop these** (operator-specific personal data):
   ```
   People/                   ← drop except _template.md
   Companies/                ← drop except _template.md
   Projects/                 ← drop except _template/
   Daily/                    ← drop
   Archive/                  ← drop
   ```
3. **Write a new `_claude/profile.md`** with the new operator's identity, sectors, role.
4. **Re-init git** at the new vault location (`git init -b main`, set git identity).
5. **Customise `_claude/CLAUDE.md` only if needed** — most things should already be
   parameterised through `profile.md`. If you find something hardcoded that should be
   configurable, move it to `profile.md` and reference it from `CLAUDE.md`. Treat each
   instance as a deployment-readiness bug.
6. **Engine** is sector-agnostic. The new operator drops in their own Excel templates and
   updates `templates/templates.yaml` with the cell maps.

## How to deploy to another *firm* (more than just operator)

Add firm-level config too — see `firm.example.md`. That layer captures branded templates,
governance/approver lists, firm sensitivity policy, approved data vendors, and the firm git
remote: things tied to the firm rather than the individual operator.

## What's intentionally NOT in this profile

- Active deal codenames (transient — live in `Projects/`)
- Live people network (lives in `People/`)
- API keys / OAuth tokens (live in the credentials manager / shell env, **never** in the vault)
- Specific Excel templates (live in `engine/templates/templates.yaml`, registry pattern)

The principle: **profile.md is durable identity + active focus. Everything else has its own home.**
