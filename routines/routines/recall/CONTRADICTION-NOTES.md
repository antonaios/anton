# #54-contradiction — design note + chosen-path record

> Status: **NARROW version SHIPPED** (conservative, no-op on today's vault),
> general version **DEFERRED to #41**. Lives in
> `routines/recall/retrieve.py::apply_contradiction_penalty`.

## The brief

> agentmemory evicts on "TTL expiry, contradiction detection, importance
> eviction". Anton has TTL (`expires`) + importance but no contradiction
> step: when a newer dated fact contradicts an older one on the same
> subject (e.g. a revised valuation), the old should decay faster / be
> flagged, not coexist eternally.

Blocked-by: **#41** (`memory_kind` tri-store labelling, ~50% done).

## Why a *general* contradiction detector is blocked by #41

Recall's index is **note-level**, not **fact-level**. A `notes` row is a
whole markdown file (frontmatter + tldr + body_excerpt + a whole-file
embedding); `chunks` are word-windows of the body; `recall_fts` mirrors
title/body + the `importance`/`expires`/`provenance` triad. **Nowhere is
a claim decomposed into a structured `(subject, field, value, date)`
tuple.** So "two facts on the same subject+field carry different values"
cannot be evaluated without first *extracting* those facts — which is
exactly the structured-labelling work #41 owns (and is only ~50% done).

Building a general detector now would mean **speculatively** inferring
subject/field/value from free-text bodies (NER + relation extraction +
value normalisation) — precisely the "do NOT build anything that depends
on full labelling … do NOT force it … do NOT speculate" the brief
forbids. A wrong inference would *wrongly demote a correct, current note*
— a silent recall-quality regression with no operator footprint. Not safe
to ship unattended.

## The narrow version that WAS cleanly doable (and shipped)

There is one clean, non-speculative signal available **today**, against
the existing triad + frontmatter, with **zero risk to current behaviour**:
notes the operator has *deliberately* marked as discrete dated claims via
three opt-in frontmatter fields.

```yaml
subject: Companies/Acme        # the entity the claim is about
field:   ev_ebitda_multiple    # the attribute being asserted
value:   "11.5x"               # the asserted value
asof:    2026-05-20            # (or as_of / date / expires) — the claim date
```

`apply_contradiction_penalty` runs as a post-fusion pass over the
candidate set:

1. Bucket candidates that carry **all of** `subject` + `field` + `value`
   + a usable claim date, grouped by `(subject, field)`.
2. A group with ≥2 members whose **values differ** AND whose **dates
   differ** is a genuine contradiction.
3. Every member **older than** the newest-dated one **whose value
   differs** from the newest value takes a modest multiplier
   (`_CONTRADICTION_PENALTY`, default **0.85**) on its final RRF score.
   The newest value wins; stale contradicted values **decay, not evict**
   (recall is read-only — CLAUDE.md §5.7; eviction stays operator-gated).

### Why this is safe to ship unattended

- **No-op on every note in the vault today** — none carry the
  `subject`/`field`/`value` triple, so the detector buckets nothing and
  mutates nothing. The 1870-test baseline + live `/recall` are unchanged.
- **No dependency on #41** — it reads only fields the operator sets
  deliberately, never `memory_kind` or any inferred label.
- **Conservative magnitude** — a 0.85 nudge re-orders ties / near-ties,
  it doesn't evict or hide. Configurable via the `penalty` kwarg.
- **Forward-compatible** — the day #41's labelling pass (or the operator)
  starts emitting `subject`/`field`/`value`, the detector activates with
  **no code change**. It's the structured *consumer* waiting for #41 to
  become the structured *producer*.

## When #41 lands — the general version

Once #41 emits structured claims (a `(subject, field, value, valid_from,
valid_to, source)` shape — cf. the `VaultEdge` / Mnemosyne triple in
OUTSTANDING #54c / #45b), the general detector is a drop-in extension of
the same pass:

- Source claims from the #41 fact store (or `vault_edges`) instead of
  requiring hand-authored frontmatter.
- Use `valid_from` / `valid_to` for proper bitemporal ordering rather
  than a single claim date.
- Optionally graduate the flat 0.85 multiplier to a recency-scaled decay
  (older ⇒ larger penalty) and surface a `superseded_by` back-pointer on
  the demoted hit for the operator-gated promotion loop.

Until then, the narrow detector above is the safe, complete, non-
speculative slice — exactly what the brief asked for when a clean narrow
version exists.
