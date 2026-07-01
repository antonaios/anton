# Templates registry

`templates.yaml` is the cell-map registry — the durable contract that says
"the engine knows about these Excel templates and how to drive them".

See `../README.md` §5 ("The cell-map pattern") for the YAML schema and §11
("Adding a new template") for the per-template wire-up checklist.

## Currently registered

| skill | template file | version | notes |
|---|---|---|---|
| `lbo` | `os-templates/Project_x_LBO_v2.xlsx` | 2.0 | DemoDeal-shape; 20 required + 10 optional inputs; circular-break post-recalc hardcode for M103 → S118/AC186/X191 |

## Reference inputs

`inputs/` holds example input JSONs the engine can run against:

- `lbo-DemoDeal-example.json` — reproduces the v1 template's hardcoded
  current state. Use as a regression test fixture or as a starting point
  for a new deal.
- `lbo-client-fs-example.json` — example `--client-fs` operating-model
  block (DemoCo downside, March FYE). Values are VERBATIM full currency
  units (no ×1e6 applied by the engine); see `templates.yaml` → lbo →
  "client_fs pre-write" for the schema + safety rules.

## Adding a template

1. `python ../scripts/survey_lbo.py` (copy and adapt) — understand the file:
   sheet inventory, defined names, orange-fill input convention, formula
   errors, cross-sheet refs.
2. Programmatically clean (strip junk named ranges, remove vestigial hidden
   sheets) and add named ranges for stable cell-map references, using the
   `valuation.template_ops` helpers.
3. Append a new entry to `templates.yaml`. The LBO entry is the canonical
   example — copy the shape, replace inputs/outputs/post_recalc_hardcode/
   validation.
4. `engine validate <skill>` — offline ref resolution must pass before
   first run.
5. Add an integration test under `../tests/` (gate behind a skip marker
   until the Windows venv + xlwings are confirmed working).
6. First end-to-end run with a known historical deal; compare engine
   outputs to the operator's manual baseline. Lock the cell map.
7. Commit.

## File-hash + template versioning

The registry sha256-hashes each template at load time. Run-time check
compares live file hash vs registered; mismatch raises `TemplateHashMismatch`
and refuses the run. If you intentionally edited the template, bump
`version` in `templates.yaml` and re-run `engine validate <skill>` to
re-derive the hash before the next run.

## Named ranges — strong preference

Prefer named ranges in `inputs:` / `outputs:` over raw A1 refs. Named ranges
follow the cell when rows are inserted above; A1 refs don't. The v2 LBO
template carries 28 workbook-scope named ranges added specifically for the
cell map. If a future template doesn't have named ranges for the cells you
need, add them via `template_ops.add_named_ranges_bulk` rather than
hand-editing in Excel — that way the addition is reproducible and tracked
in the engine repo.
