---
type: firm-profile
memory_kind: procedural
firm: "<firm-name>"
status: stub
sensitivity: internal
tags: [firm, config, claude-config, procedural-memory]
---

# Firm profile (example)

> Placeholder for **firm-level config** — the layer below `profile.md` (operator-level) that
> captures things tied to the firm rather than the individual operator.
>
> This is the **example** shipped with the public mirror. It is intentionally a stub: a solo
> or small-team setup may leave most of it empty. Populate it when deploying into a defined
> firm context.
>
> Why this exists: `profile.md` is "who is operating this Agentic OS". `firm.md` is "what firm
> are they operating it inside". Some downstream artefacts (governance forms, branded PPT
> templates, approver lists, sensitivity policies) live at the firm level, not the operator
> level — moving to a new role at the same firm shouldn't reset these; moving to a new firm
> should.

## What goes here

```yaml
# Frontmatter shape (when populated):

firm: "<firm-name>"
firm_type: advisory          # corporate | advisory | pe | family-office | other
size: <number-of-professionals>
governance_body: ""          # e.g. "Investment Committee", "Partners", "Board"
governance_approvers:
  - role: "<approver-role>"
    name: ""
  - role: "<approver-role>"
    name: ""
internal_teams:              # for sign-off forms (investment proposals etc.)
  - "Legal"
  - "Tax"
  - "Treasury"
  - "Compliance"
branded_assets:
  ppt_template: ""           # absolute path to a firm-branded PPT template
  word_template: ""
  email_signature: ""
data_vendors:
  approved: []               # e.g. ["LSEG / Refinitiv", "FactSet", "S&P Capital IQ"]
  blocked: []
sensitivity_policy:
  confidential_can_leave_firm: false
  zdr_in_place: false        # set true once an enterprise + ZDR agreement is active
  approved_cloud_lanes: []   # e.g. ["claude-enterprise"]
  cross_border_restrictions: []
git_remote:
  vault: ""                  # private git remote URL (or empty for local-only)
  engine: ""
tags: [firm, config]
```

## How this composes with profile.md and CLAUDE.md

```
CLAUDE.md     ← operating rules (operator-agnostic, firm-agnostic)
   ↓ references
profile.md    ← operator identity, sectors, voice, qualifications
   ↓ references
firm.md       ← this file: governance, branded assets, sensitivity policy
   ↓ governs
Vault content + Templates  ← actual work
```

Templates that have firm-specific elements (approver lists, branded PPT references) read from
`firm.md` rather than hardcoding. When `firm.md` is empty, those templates fall back to
generic placeholders.

## Deployment notes

- When deploying to a new firm: replace this entire file with the new firm's config.
- When the operator changes firms (career move): replace this file; `profile.md` gets
  `current_role:` updated.
- When `firm.md` is unpopulated (the default): templates use generic placeholders. The system
  works; outputs are just slightly more generic until firm context is added.
