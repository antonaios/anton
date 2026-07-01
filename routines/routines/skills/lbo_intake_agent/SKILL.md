---
name: lbo-intake-agent
description: |
  Use when the operator wants the agent to READ deal documents (CIM, IM,
  financial-model extracts, teasers) and prepare the LBO intake without an
  attended session: extract sourced deal assumptions, optionally draft the
  Client_FS operating-model block, ask targeted clarifications via the #63
  suspend loop, then hand back the standard deal-assumption boxes pre-filled
  with per-box citations for operator confirmation. Triggers: agent intake,
  read the CIM and set up the LBO, prefill the LBO from docs, lbo intake from
  documents. Inputs: deal name, document paths, deal context. Output: a
  suspended LBO intake (the standard boxes manifest) carrying prefill +
  per-box citations + optional client_fs; on operator confirmation the run
  assembles LBOInput and fires the engine exactly like /lbo intake mode.
version: 0.1.0
license: proprietary
allowed_tools:
  - llm_local
  - vault_read
  - engine_call
capabilities:                        # #61-capabilities — declared surface, validated at boot
  vault_read:  ["Projects/**"]                                   # deal brief / source register context
  vault_write: []                                                # nothing written; the lbo run owns capture
  fs_roots:    ["<workspace-root>/**", "<workspace-root>/**"]     # where deal docs live
  network:     []                                                # confidential ⇒ no external endpoints (§5.2)
metadata:
  sensitivity: confidential
  workspace_scope: project
  tile_label: "LBO Intake (Agent)"
  cost_ceiling_tokens: 60000
  cost_ceiling_seconds: 600
  guardrails:
    - every_prefilled_box_cited
    - transcribe_only_no_llm_maths
  guardrail_max_retries: 1
llm_system_prompt_file: system-prompt.md
---

# LBO Intake Agent

## Overview

The governed Phase-2 leg of the LBO agent arc (`#lbo-dashboard-wiring` →
`OVERNIGHT-2026-06-10-LBO-AGENT-LEG-DESIGN.md` Option C): a local-first
`@anton_skill` that does what the attended Orchestrate session does, in-bridge.
`POST /api/workflows/lbo-intake-agent {deal_name, workspace_*, doc_paths[],
deal_context}` reads the documents deterministically (pypdf / openpyxl —
bounded extraction), runs governed `llm()` judgment over per-doc digests,
suspends once for clarifications when the judgment raises open questions, and
finishes by suspending into the **standard LBO boxes manifest** — prefill
populated, every prefilled box carrying `{source, quote, provided_via:
"lbo-intake-agent"}` — so the operator confirms in the shipped modal and the
resume assembles `LBOInput` + fires the engine through the SAME
`_resume_intake` path as `/lbo` intake mode. The agent never runs the engine
behind the operator's back: its terminal act is always the boxes suspension.

## Sensitivity & the override window

Deal docs are confidential ⇒ `llm()` routes LOCAL (qwen3:14b) by default. For
frontier-model judgment on a real CIM, open a `#llm-routing-override` window
(skill=`lbo-intake-agent`, workspace=`project:<Deal>`, provider=`anthropic`,
ceiling=`confidential`, 5–30 min, justification required) BEFORE firing — the
gateway lifts the judgment calls to the claude lane (Opus) for that window,
audit-stamped per call. MNPI is never liftable; the route inherits the
wrapper's MNPI 403.

## Flow

```
fire {deal_name, doc_paths[], deal_context}
  ↓ governance jacket (workspace scope / MNPI → 403)
read docs (pypdf/openpyxl, bounded; unreadable paths → warning, not failure;
           ALL unreadable → 422)
  ↓
llm() digest per doc → llm() synthesis (strict JSON judgment)
  parse failure → one repair call → still failing → boxes suspension with
  empty prefill + note (degrade to the manual form, never burn the run)
  ↓
open questions?  → SUSPEND stage="clarify" (one round; answers merge into
                   prefill with operator-resume citations; non-box answers
                   append to the boxes note)
  ↓
SUSPEND stage="boxes": standard manifest + prefill + per-box source
annotations; state carries agent_citations + client_fs (if a valid
ClientFSBlock was transcribed — invalid blocks are DROPPED with a note)
  ↓ operator confirms boxes (modal / API)
resume → agent citations merged under the operator's → lbo._resume_intake →
LBOInput → engine (Iron Law, S&U tie, 4-phase verification all unchanged)
```

## Contracts

- **Transcribe-only (no-llm-maths):** a prefilled box must trace to a verbatim
  document location AND a non-empty verbatim quote; the parser demotes
  unsourced/un-quoted/un-coercible values to open questions. The agent NEVER
  computes, blends, or annualises. (codex slice-2 round)
- **Doc-path scoping:** `doc_paths` must resolve under the declared
  `capabilities.fs_roots`; UNC/network paths refused; 50 MB per-file cap
  before any parser opens the file. (codex slice-2 SEV-2)
- **Citations:** every prefilled box → `{box, source, quote, provided_via:
  "lbo-intake-agent"}` carried in the suspension state and merged UNDER the
  operator's citations on resume. An agent row is DROPPED when the operator
  overrides that box's value (the quote no longer supports it) — the standard
  citations gate then re-suspends if nothing is left. Clarification answers
  cite `operator-resume:<date>`. (codex slice-2 SEV-2)
- **client_fs is an operator-gated proposal:** transcribed only when a clean
  10-period model exists in the docs; validated against `ClientFSBlock`
  (full-currency units; invalid → dropped + noted); surfaced in the boxes
  manifest as the synthetic `client_fs_apply` field, **default `discard`** —
  it reaches the engine ONLY on an explicit `apply`. (codex slice-2 SEV-2)
- **Operator always wins:** prefill renders as editable defaults; the resume
  merge order is manifest defaults → prefill → operator boxes (unchanged from
  `/lbo` intake). Unknown suspension stages refuse with 409 (re-fire).

## Cost Envelope (D6 — first-pass numbers, operator to calibrate)

`cost_ceiling_tokens: 60000` — ≤8 doc digests (~4k tokens each) + 1 synthesis
+ 1 repair + headroom. `cost_ceiling_seconds: 600` — local-lane digests
dominate wall-clock (~30–60s/doc on qwen3:14b); an Opus window cuts this
sharply. Recalibrate to `1.25 × observed` after the first real fires (DemoCo
calibration run).

## Refusals

- Workspace ≠ project / MNPI tier → 403 (wrapper gate, same as /lbo).
- Zero readable documents → 422 listing each path's failure.
- `llm()` refused (budget gate / sensitivity guard / #67 cap) → 403 with the
  refusal reason; nothing dispatched.
