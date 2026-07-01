# CLAUDE.md — routines repo (the bridge)

> Orientation for working in `routines/`. Workspace-wide notes live in the
> top-level `CLAUDE.md`; the operating rules + universal safety anchors live in
> the vault constitution (`../vault/CLAUDE.md`). This file documents **how a
> skill is authored** — the SKILL.md contract.

## The SKILL.md contract

Every ANTON skill ships as a single `SKILL.md` at `routines/routines/skills/<skill>/SKILL.md`,
plus optional progressive-disclosure references at `.../<skill>/references/<topic>.md`.

### Mandatory sections (in order)

1. **YAML frontmatter** — `name`, `description` (use-when text), `version`, `license`, `allowed_tools`, `metadata` (sensitivity / workspace_scope / tile_label / cost ceilings / guardrails).
2. **Overview** — one paragraph: what the skill does, what the engine does, what the assistant does.
3. **When to Use** — three sub-lists: mandatory triggers (fire), optional triggers (propose), don't-use (refuse + explain).
4. **The Iron Law** — one skill-specific, non-negotiable rule, prefixed by the universals at [universal-iron-laws](../vault/CLAUDE.md#universal-iron-laws).
5. **Core Pattern** — alternating compute / verify phases. Each verification phase ends with a `STOP — do not proceed` marker; the bridge enforces phase order.
6. **Quick Reference** — an ASCII flow of the skill end-to-end, including hard gates and side effects.
7. **Common Rationalizations** — a 2-column table (Rationalization | Reality). Append-only after baseline runs.
8. **Red Flags** — quoted internal monologue; if the model catches itself thinking one, stop and re-read.
9. **Anti-Patterns** — approaches that look reasonable but are wrong.
10. **Example** — one worked end-to-end example.
11. **When Stuck** — a (symptom, diagnostic) table.
12. **Output Contract** — workspace target path, filename convention, sheet/range layout, JSON return shape.
13. **Citations Required** — a table mapping inputs to required source types.
14. **Cost Envelope** — frontmatter ceilings + per-phase budget + overrun behaviour.
15. **Verification Checklist** — pre-release checks before declaring the skill production-ready.

### Universal rules every skill inherits (don't duplicate)

Skills inherit these by cross-reference, never by copying. The anchors live in the constitution:

- [no-mnpi-to-cloud](../vault/CLAUDE.md#no-mnpi-to-cloud) — applies to every skill.
- [no-invented-sources](../vault/CLAUDE.md#no-invented-sources) — applies to every skill that cites.
- [no-llm-maths](../vault/CLAUDE.md#no-llm-maths) — applies to every skill that produces numbers.

### The `capabilities:` block

A skill may declare a top-level `capabilities:` block naming the surface it touches. The registry
parses it and **cross-checks it at bridge boot** — a violation hard-fails boot.

```yaml
capabilities:
  vault_read:  ["Projects/**", "Companies/**"]   # vault-relative globs it reads
  vault_write: ["Projects/<deal>/**"]            # vault-relative globs it writes
  fs_roots:    ["<workspace-root>/**"]           # external filesystem roots it touches
  network:     []                                # allowed hosts; [] = none
```

Validation enforced at startup:

1. **`network` ⇔ sensitivity.** A `confidential` or `MNPI` skill MUST declare `network: []` — the declarative form of [no-mnpi-to-cloud](../vault/CLAUDE.md#no-mnpi-to-cloud), checked once at boot. An `internal`/`public` skill may declare hosts.
2. **`vault_write` ⇔ `workspace_scope`.** A `project`-scoped skill may only declare `vault_write` globs under `Projects/**`.
3. **Shape.** Path globs are non-empty, forward-slash strings; network entries are non-empty host strings; unknown capability keys are rejected.

An absent `capabilities:` block parses as "declares nothing" and is not an error — but new skills should declare it.

### The `captures_to_vault:` block (optional)

A deliverable-producing skill may declare a `captures_to_vault:` block to record its conclusion back into the vault's semantic memory — operator-gated end to end (it writes a pending proposal; it never auto-writes the vault).

```yaml
captures_to_vault:
  target: "Companies/{deal_name}.md"
  fields: [irr_central_pct, moic_central_x, entry_multiple, exit_multiple]
  headline: "{deal_name}: {irr_central_pct}% IRR / {moic_central_x}x MOIC at {entry_multiple}x entry"
  section: "Valuation history"
```

### Voice convention

Within a single SKILL.md: **second-person imperative** ("Verify the S&U ties", "Stop — do not proceed") for the Iron Law, Red Flags, and phase instructions; **third-person procedural** ("the skill reads the inputs file") for Core Pattern narration, Quick Reference, and the Output Contract.
