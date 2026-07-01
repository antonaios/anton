# Agentic OS — Valuation Engine

> **Status (2026-06-08):** foundation v0.2.0 + **LBO** wired end-to-end
> against `Project_x_LBO_date.xlsx` (v4 canonical, hash
> `sha256:e9f1355422…`). xlwings smoke ✓ (2026-05-19, engine CLI).
> Bridge route + audit layer tested ✓ through
> `routines/runs/tool.lbo.jsonl` (1 passing smoke test exercising the
> live engine end-to-end on `routines/skills/lbo/test_input.json`
> (relative to the routines repo root), 2026-05-29 run_id `21fe955a`
> — output XLSX archived under
> `<workspace-root>/_LBO-Smoketest_archived_2026-06-08/`; plus passing
> iron-law-test fires asserting the validation gate maps to HTTP 502
> with `holding_period=9` — the iron-law row count grows by one on each
> pytest run of the LBO suite). **85 engine unit tests passing** (1
> xlwings integration test skip-marked).
>
> **No real production bridge runs yet** — every row in `tool.lbo.jsonl`
> is a test fixture (see `CALIBRATION-WATCHLIST.md` LBO §5 for the filter
> signatures). Structurally production-ready; production-trustworthy after
> the **first real operator-initiated run** on the canonical
> `<workspace-root>/1. Projects/` workspace tree (gated on
> `#lbo-dashboard-wiring` — see Open quality-gates below).
>
> **Open quality-gates** (none structural — all calibration):
> - `#21-lbo-calibration` (MEDIUM) — Rationalizations table needs a second
>   real-deal baseline; 8000-token cost ceiling is a first-pass guess;
>   Iron-Law wording is engine-reality version (not original S&U-tie spec).
>   Tracked in `CALIBRATION-WATCHLIST.md` → "LBO skill" §1-4.
> - `#24` guardrail retry loop — declared in SKILL.md frontmatter,
>   dispatcher not built; lands inside `#63` `@anton_skill` wrapper.
> - `#lbo-dashboard-wiring` (OUTSTANDING.md:200) — dashboard `/lbo` slash +
>   tile have ZERO client-side wiring; deferred for #63 suspend/resume
>   substrate (engine-side + bridge-side both ready and tested via CLI +
>   direct `POST /api/workflows/lbo`).
>
> **Adoption note (Phase 0).** Pattern locked 2026-05-24; LBO pilot
> shipped 2026-05-29 under `routines/skills/lbo/` with the full directory
> form (SKILL.md frontmatter w/ capabilities manifest + cost ceilings +
> guardrails + captures_to_vault, `scripts/lbo.py` Pydantic IO wrapper,
> `references/` × 3, `test_input.json` + `test_output.json`, bridge route
> at `routines/api/routes/lbo.py`). Original migration drafts preserved at
> `<repo>\1. OS Structure\drafts\` for historical reference; live
> source of truth is the skill directory. 10+ skills since LBO have
> followed the same template (comps, bd_decay, morning_brief, recall_query,
> equity_research, sector_news, ticker_multiples, lessons_suggest, …).
>
> **Next templates** (DCF, sensitivity, 3-statement, football field,
> audit-model) — each bootstraps against this foundation following §11
> "Adding a new template" → step 9 now lands the SKILL.md directly rather
> than the standalone-CLI path.

---

## 1. What this is

A thin Python orchestrator that drives the operator's **existing** Excel
templates (LBO, DCF, comps, sensitivity, etc.) as the deterministic numerical
engine. **No financial logic is rewritten in Python.** Excel does the maths;
Python feeds inputs in, triggers recalc, reads outputs back, runs any
post-recalc hardcoding steps that break circular refs, validates, and logs
the run with a hash trail.

The "no LLM does the maths" rule from `_claude/CLAUDE.md` §5.1 is satisfied
because every number in any IC memo / company profile / one-pager that
required computation traces to a populated `.xlsx` on disk, with a sha256
hash, timestamp, inputs, and outputs.

## 2. Why xlwings (not openpyxl, not from-scratch Python)

| Approach | Pro | Con | Verdict |
|---|---|---|---|
| **Rebuild logic in Python** | Cloud-portable, fast batch | Duplicates work; new code is unaudited; operator has to edit Python to refine assumptions | ✗ rejected — operator's templates have been used in real deals |
| **openpyxl alone** | Pure Python, no Excel install | Doesn't recalc formulas; drops INDIRECT, array formulas, dynamic arrays; ignores macros | ✗ rejected — LBO has iterative calcs (interest on revolver, circular refs) |
| **xlwings (Excel COM)** | Real Excel does the maths; templates evolve in Excel; populated `.xlsx` is the audit artefact | Windows + Excel runtime only; ~1–3 sec per run | ✓ chosen — IC-grade work, not batch screening |

If we ever need cloud / batch / Linux execution, the upgrade path is a
pure-Python reimplementation of specific templates. Future problem.

## 3. Repository layout

```
<repo>\engine\
├── README.md                              ← this file — architectural reference
├── pyproject.toml                         ← deps: pandas, numpy, openpyxl, xlwings (Windows),
│                                            pyyaml, click; dev: pytest, ruff, mypy
├── .python-version                        ← 3.13
├── valuation/                             ← the Python package
│   ├── __init__.py                        ← public surface (TemplateSpec, EngineRun, exceptions)
│   ├── models.py                          ← dataclasses (no other engine deps — break cycles)
│   ├── exceptions.py                      ← domain exceptions
│   ├── cell_refs.py                       ← cell-ref parser (A1 / range / named range)
│   ├── registry.py                        ← TemplateRegistry — loads templates.yaml, hashes files
│   ├── template_ops.py                    ← openpyxl utilities: strip names, add named ranges, remove sheets
│   ├── excel_engine.py                    ← xlwings driver: run(), validate()
│   ├── naming.py                          ← filename pattern + per-deal 00. OLD/ archive policy
│   ├── workspace.py                       ← three-tier workspace model + path resolution (project/bd/general)
│   ├── project.py                         ← deprecated shim over workspace.py (back-compat)
│   ├── audit.py                           ← JSONL audit log writer + tail reader
│   └── cli.py                             ← `engine` CLI (list / validate / run / new-workspace / list-workspaces / workspace-status / audit-tail)
├── templates/
│   ├── templates.yaml                     ← THE cell-map registry (LBO registered; DCF/SENS/3S/FF/AM append per #19)
│   ├── README.md                          ← how to register a new template
│   └── inputs/
│       └── lbo-DemoDeal-example.json     ← reference input set
├── runs/
│   ├── audit.jsonl                        ← append-only run audit log (gitignored)
│   └── outputs/                           ← legacy default (per-deal valuations now live in
│                                            <workspace-root>/1. Projects/<Deal>/3. Financials & analysis/2. Valuation/
│                                            per workspace-write-policy)
├── tests/
│   ├── conftest.py                        ← fixtures (tmp_xlsx, lbo_template)
│   ├── test_models.py
│   ├── test_cell_refs.py
│   ├── test_registry.py
│   ├── test_excel_engine.py               ← unit tests + 1 xlwings-integration test (skip-marked)
│   ├── test_template_ops.py
│   ├── test_naming.py
│   ├── test_project.py
│   └── test_audit.py
└── scripts/                               ← one-off operational scripts
    ├── survey_lbo.py                      ← broad survey of an .xlsx
    ├── survey_lbo_deep.py                 ← operator-flagged-cells deep dive
    └── survey_output_table.py             ← dump LBO!B101:Q120 head-to-head
```

## 4. How a single run works

```
engine run lbo --inputs lbo-DemoDeal-example.json --workspace project:DemoDeal-Test
                                    │
                                    ▼
  1.  cli.py loads templates.yaml      → TemplateRegistry
  2.  resolves "lbo"                    → TemplateSpec (path, hash, cell map, validation, post-recalc steps)
  3.  pre-flight: workspace.paths_for("project", "DemoDeal-Test")
        ├─ <workspace-root>/1. Projects/DemoDeal-Test/            ← must exist
        └─ <vault>/Projects/DemoDeal-Test/                     ← must exist
        (else exit 1 with `engine new-workspace project DemoDeal-Test` hint)
  4.  naming.next_output_path_for → <workspace-root>/1. Projects/DemoDeal-Test/3. Financials & analysis/
                                2. Valuation/Project_DemoDeal-Test_LBO_2026-05-26_v3.xlsx
                                    │
                                    ▼
  5.  excel_engine.run(spec, inputs, output_dir, output_filename):
        a. check input keys (required vs optional vs unknown)
        b. _verify_hash: sha256 of live .xlsx == registered hash, else TemplateHashMismatch
        c. shutil.copy2(template, out_path)              # fresh copy each run
        d. xw.App(visible=False); app.DisplayAlerts/AskToUpdateLinks/ScreenUpdating = False
        e. wb = app.books.open(out_path, update_links=False)
        f. for each input: _write_cell(wb, ref, value)   # named range → A1 fallback
        g. app.calculate()                                # full recalc
        h. post_recalc_hardcode loop:                     # circular-ref break
             read source (M103) → write to targets (S118, AC186, X191) → recalc
             if converge=true: re-read source; stop when |delta|<tol or max_iters
        i. for each output: _read_cell(wb, ref)
        j. evaluate validation rules; ValidationFailed on any false
        k. wb.save(); wb.close(); app.quit()             # finally-block teardown
                                    │
                                    ▼
  6.  audit.write(run)               → runs/audit.jsonl gets one line:
        { ts, run_id, template, version, template_hash, inputs_hash, outputs_hash,
          output_path, duration_ms, convergence_iters, status, notes }
                                    │
                                    ▼
  7.  naming.archive_supersedes      → prior same-(deal, skill, day) versions
                                       MOVED to <deal>/.../2. Valuation/00. OLD/
                                    │
                                    ▼
  8.  Return EngineRun to caller; CLI prints outputs dict.
```

## 5. The cell-map pattern (the durable contract per template)

`templates/templates.yaml` is the spec. One YAML entry per template:

```yaml
templates:
  <skill>:
    path: "os-templates/Project_x_<SKILL>_vN.xlsx"
    version: "N"
    description: "human-readable"

    inputs:                              # required — engine errors if missing
      logical_name: "Named_range"        # preferred — survives row inserts
      other_name:   "SheetName!A1"       # A1 form when no named range yet

    optional_inputs:                     # may be omitted — cell keeps current value
      scenario_x: "Scenario_X"

    outputs:                             # logical → cell ref (cell, range, or named range)
      irr_grid:             "LBO!Y187:AG195"   # 2D range → nested list
      output_summary_table: "LBO!B101:Q120"
      entry_ev:             "LBO!I76"
      stub_period:          "stub"             # named range

    post_recalc_hardcode:                # break circular refs by paste-as-value
      - source:   "Hardcode_source_M103"
        targets:  ["Hardcode_S118", "Hardcode_AC186", "Hardcode_X191"]
        converge: true                    # iterate to fixed-point
        tolerance: 0.0001
        max_iters: 10

    validation:                          # post-recalc sanity rules
      - rule:    "outputs.entry_ev > 0"
        message: "Entry FTEV must be positive"
```

### Cell-reference forms accepted

| Form | Example | When to use |
|---|---|---|
| Named range (workbook scope) | `"Acq_multiple"` | **Preferred.** Stable across row inserts. |
| Named range (sheet scope) | `"LBO!My_local_name"` | Rare; for sheet-local names. |
| A1 sheet-qualified | `"LBO!I25"` | Fallback when no named range exists. |
| A1 range | `"LBO!B101:Q120"` | 2D output; reads as nested list. |
| Quoted sheet | `"'Sheet With Spaces'!A1"` | Sheet names with spaces / `&`. |

Validation (`engine validate <name>`) is offline (openpyxl) so it runs in CI on
Linux without Excel installed. Every input/output/hardcode ref is resolved
against the live workbook's sheet list and defined-name list; any unresolved
ref fails the run before any Excel work starts.

## 6. The hash trail (template integrity)

At registry load, the engine computes `sha256(template_file)` and stores it
on `TemplateSpec.template_hash`. At run time, `_verify_hash()` re-hashes the
live file and compares. **Mismatch → `TemplateHashMismatch`, run rejected.**

This catches the case where a template was hand-edited (e.g. a row inserted,
breaking an A1-style cell map) but the cell map wasn't re-validated. After
intentional edits, run `engine validate <name>` to confirm the map still
resolves, then re-load the registry (next CLI invocation does this).

Override with `engine run --no-hash-check` if you must (not recommended).

## 7. Post-recalc hardcoding — the "circular break" pattern

Some templates have intentional circular references that the operator
resolves manually via paste-as-value. Example: the DemoDeal LBO's
exit-multiple sensitivity row at `S116:S120` is centred on `S118 = M103`
(where M103 is the *entry* multiple display, itself a `ROUND(...)` of
neighbouring cells). Linking `S118` directly to `M103` creates a cycle the
sensitivity grid can't tolerate.

The `post_recalc_hardcode` block in the cell map automates the manual fix:

1. Recalc once.
2. Read the `source` cell.
3. Write that value into each `target` cell.
4. Recalc again.
5. If `converge: true`, re-read `source`; if `|delta| > tolerance`, repeat
   from step 3. Cap at `max_iters` (default 10).

For the DemoDeal LBO this typically converges in 1-2 passes because `M103`
doesn't actually depend on `S118` in the formula graph — but iterating
defensively handles templates where it would.

## 7b. Client_FS operating-model pre-write (`client_fs`)

A dashboard LBO run is only as real as the operating model in the template's
`Client_FS` sheet. The run path optionally accepts a **`client_fs` block**
(CLI: `engine run lbo --inputs in.json --client-fs fs.json`; library:
`excel_engine.run(..., client_fs={...})`) that is written into the copied
workbook **before** the input cells, inside one manual-calc window
(`xlCalculationManual` during the writes, the workbook's **native** mode
restored before the single full recalc — never `xlCalculationAutomatic`,
which makes the IRR data table recalc continuously and hangs Excel).

```jsonc
{
  "dates":     ["2023-03-31", "..."],      // EXACTLY 10 ISO dates → Client_FS!J4:S4
  "rows":      {"6": [/* 10 numbers */]},  // row → J:S values (6=revenue, 13=EBITDA,
                                           // 25=D&A, 32=ΔNWC, 38=capex post-restructure)
  "zero_rows": [7, 8],                     // component rows blanked to 0.0
  "sheet":     "Client_FS"                 // optional (default)
}
```

Rules (enforced):

- **Units are verbatim.** The engine applies **no ×1e6** (or any) scaling —
  `Client_FS` holds full currency units, so the caller sends full values
  (`17900000.0`, not `17.9`). Note this differs from the LBO *input* cells
  (`acq_ebitda` etc.), which the template itself takes in millions.
- **Full window only.** `dates` must be exactly 10 (the `J:S` period window)
  and every row array must match its length — a partial write would leave
  stale columns silently poisoning the `OpModel_Link` SUMIFS date-join.
  Malformed blocks fail (`ClientFSBlockInvalid`, exit 1) before Excel opens.
- **Formula cells are refused.** If any target cell holds a formula the run
  aborts (`ClientFSFormulaCollision`, exit 2) before a single write — the
  `Client_FS` total rows (`r11/r20/r30/r36/r43`) are `=SUM()` formulas and
  must never be overwritten.
- The `dates` are the SUMIFS join keys `OpModel_Link` matches against the LBO
  timeline (the `EDATE` chain seeded by `First_FYE`) — keep them consistent
  with the `first_fye` input or the per-period rows silently read as zero.

Reference payload: `templates/inputs/lbo-client-fs-example.json` (the DemoCo
downside model). Schema doc: `templates/templates.yaml` → lbo →
"client_fs pre-write".

## 8. File / folder conventions (per workspace-write-policy, locked 2026-05-23)

### Per-deal output location

```
<workspace-root>/1. Projects/<Deal>/
└── 3. Financials & analysis/
    └── 2. Valuation/
        ├── Project_<Deal>_<SKILL>_<YYYY-MM-DD>_vN.xlsx   ← latest run
        └── 00. OLD/                                       ← per-deal archive
            ├── Project_<Deal>_<SKILL>_<YYYY-MM-DD>_v1.xlsx
            └── Project_<Deal>_<SKILL>_<YYYY-MM-DD>_v2.xlsx
```

- Output dir: `<Deal>/3. Financials & analysis/2. Valuation/`
- Filename: `Project_<Deal>_<SKILL>_<YYYY-MM-DD>_v<N>.xlsx`
- Skill UPPERCASE in the filename (`LBO`, `DCF`, `SENSITIVITY`, ...).
- On success, prior same-`(Deal, SKILL, Date)` versions MOVE to `00. OLD/`.
  Files are **never deleted** — CLAUDE.md §5.9 "safe deletion only".
- Different deals + different skills coexist in the same dir without
  collision (filename includes both).

**Workspace-policy alignment:** Real client mandates live under
`<workspace-root>/1. Projects/` per
`Topics/Architecture/workspace-write-policy.md`. Test / public-data
projects (`DemoTarget`, `DemoDeal-Test`) live under
`<vault>/Projects/` — distinct trees. Engine reads both via the
configured `external_project_paths` in `profile.md`.

### Template-itself versioning

When the template itself is superseded (e.g. v1 → v2 with named-range
additions), the prior version moves to **`os-templates/Archive/`** (not
per-deal `00. OLD/`, since the template isn't a deal artefact).

## 9. Workspace bootstrap — three-tier (#18, 2026-06-04)

The engine is workspace-aware across **three** types (`valuation/workspace.py`),
all rooted under `<workspace-root>/` per the operator profile
(`_claude/profile.md`):

| Type | Filesystem root | Vault counterpart | Output path |
|---|---|---|---|
| `project` | `<workspace-root>/1. Projects/<name>/` | `<vault>/Projects/<name>/` | `…/3. Financials & analysis/2. Valuation/Project_<name>_<SKILL>_<date>_vN.xlsx` |
| `bd` | `<workspace-root>/2. Business development/<name>/` | none (BD watch lives on `Companies/<X>.md`) | same `3. F&A/2. Valuation/Project_…` layout |
| `general` | `<workspace-root>/3. General/<name>/` | none | flat `<name>/<SKILL>/<name>_<SKILL>_<date>_vN.xlsx` (no `Project_` prefix) |

Only `project` has a vault counterpart, created atomically (file-system first;
if the vault copy fails the file-system side rolls back). bd/general are
file-system only. The engine never reads the vault/profile — the **caller**
supplies the filesystem root (the bridge passes it; the CLI falls back to the
profile-mirrored `workspace.DEFAULT_ROOTS`).

CLI:
```
engine new-workspace <type> <name>          # project ⇒ fs+vault; bd/general ⇒ fs only
engine list-workspaces [--type <type>]
engine workspace-status <type>:<name>        # exit 0 if scaffolded, 1 otherwise
engine run <skill> --workspace <type>:<name> [--workspace-root <path>]
```

`engine run` refuses to populate inputs unless the workspace is scaffolded
(project ⇒ both fs+vault); a half-scaffolded project (fs present, vault
missing) tells you to repair the vault side rather than re-run `new-workspace`
(which refuses an existing fs folder).

**Deprecated aliases** (kept for one release cycle): `--deal <Deal>` ⇒
`--workspace project:<Deal>`; `new-project` / `project-status`. `valuation/project.py`
is a thin shim over `workspace.py` (`ProjectPaths` aliases `Workspace`, with a
back-compat `.deal` property).

**Convergence with the bridge workspaces API.** `POST /api/workspaces`
(#5+#6+#6c) is the platform's primary workspace creator (it scaffolds both
sides). The engine CLI's `new-workspace` is the standalone path for engine-only
sessions; the two now share the same roots + path conventions. Minor follow-up
`#18-engine-roots-sync`: the engine's `DEFAULT_ROOTS` duplicate the profile
values (they match today; the bridge can pass `--workspace-root` to override).

## 10. Sensitivity routing (unchanged from v0.1)

The engine is sensitivity-agnostic — it just runs Excel against inputs you
give it. Routing happens at the **caller** layer (the slash command, the
bridge's central `before_llm_call` hook per #22, the dispatcher):

| Sensitivity | Input selection | Engine call | Output handling |
|---|---|---|---|
| `public` / `internal` | Cloud Claude | Engine populates locally | Narrative cloud Claude |
| `confidential` (bridge) | Local Ollama (Qwen3:14b) | Engine populates locally | Narrative local Ollama |
| `confidential` (post-Enterprise+ZDR) | Claude Enterprise | Engine populates locally | Narrative Claude Enterprise |
| `MNPI` (always) | Local Ollama | Engine populates locally | **Outputs never leave the machine.** Written only to `Projects/<Deal>/12 Outputs/` in the vault. |

Excel templates must never contain MNPI in their static content — they're
inputs-driven. The template itself is `internal`-tier.

## 11. Adding a new template — checklist for next session

This foundation supports DCF, sensitivity, 3-statement, football field,
audit-model out of the box. Each follows the same recipe (and will be
wired as part of #19 once #21 SKILL.md migration lands):

1. **Operator** places the template at `os-templates/Project_x_<SKILL>_date.xlsx`.

2. **Survey it** (no input from operator beyond the file):
   ```bash
   python "<repo>/engine/scripts/survey_lbo.py"  # adapt or copy
   ```
   Document: section row boundaries, hardcoded inputs (orange convention?),
   existing named ranges, errors, CapIQ junk, hidden sheets, cross-refs.

3. **Operator review** (parallel — they answer):
   - Which cells are user inputs vs derived defaults?
   - Where are the output cells the IC memo quotes?
   - Any circular-break hardcoding ritual you do manually?
   - Hidden sheets — vestigial or load-bearing?
   - Cleanup: strip CapIQ junk? add named ranges?

4. **Build v2** (programmatic, via the `template_ops` helpers):
   - Strip junk via `template_ops.strip_capiq_names`.
   - Remove orphan named ranges via `template_ops.remove_defined_names`.
   - Remove vestigial sheets via `template_ops.remove_sheet`.
   - Add named ranges via `template_ops.add_named_ranges_bulk`.
   - Save as `Project_x_<SKILL>_v2.xlsx` alongside v1.

5. **Register in `templates/templates.yaml`** — copy the LBO block as a
   starting point; replace inputs/outputs/post-recalc-hardcode.

6. **`engine validate <skill>`** — offline ref resolution must pass.

7. **Sanity-test fixture** — operator supplies one historical deal where
   they trust the model's IRR/MOIC. Add as
   `tests/test_<skill>_integration.py`, gate behind `pytest.mark.integration`,
   wire to skip when xlwings not installed.

8. **First live run** — `engine run <skill> --inputs <example>.json --workspace <type>:<name>`.
   Compare engine outputs to operator's manual-paste-special baseline.
   Lock the cell map.

9. **Plug the skill as SKILL.md** (per #21) — directory under
   `routines/skills/<skill>/` with `SKILL.md` frontmatter declaring
   sensitivity + cost ceiling, `scripts/<skill>.py` that calls the
   engine CLI, `references/`, `test_input.json`, `test_output.json`. The
   skill's job is: pick inputs, call engine, narrate outputs, cite
   sources, save to `Corporate Finance/<workspace>/<deal>/...` per
   workspace-write-policy.

10. **Commit** — engine repo gets the new cell map + tests; SKILL.md
    lives in `routines/skills/`; operator commits the v2 template under
    their own backup discipline.

## 12. Install / run (Windows-only at runtime)

```powershell
# Windows PowerShell, from <repo>\engine\
cd "<repo>\engine"

# Create venv with Windows Python
& "C:\Users\<you>\AppData\Local\Programs\Python\Python313\python.exe" -m venv .venv

# Activate + install
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Verify xlwings can talk to Excel
python -c "import xlwings as xw; wb = xw.Book(); wb.close(); print('xlwings OK')"

# Now use the engine
engine list
engine validate lbo
engine new-workspace project DemoDeal-Test
engine run lbo --inputs templates/inputs/lbo-DemoDeal-example.json --workspace project:DemoDeal-Test
engine audit-tail -n 5
```

The non-xlwings test suite runs on any Python 3.13 (Linux included):

```bash
PYTHONPATH=. python -m pytest
# 66 passed, 1 skipped (the xlwings-integration test) — v0.2.0 baseline
```

## 13. Things that broke / didn't work (so future sessions don't re-try)

| # | Failed | Why | Works instead |
|---|---|---|---|
| 1 | `audit.py` importing `EngineRun` from `excel_engine.py` | Created an import cycle when `excel_engine.py` started growing | Moved dataclasses to `models.py`, both audit and excel_engine import from there |
| 2 | `_build_spec` as `@staticmethod` calling `cls._hash_file` | `cls` not in scope inside a staticmethod | Either make it `@classmethod` OR call `TemplateRegistry._hash_file()` explicitly. Picked the explicit form. |
| 3 | Test sabotaging `shutil.copytree` by call-count | `copytree` recurses internally per subdir, so call-count fires inside the first top-level copy | Sabotage based on `src == vault_template_path`, not call count |
| 4 | A1 refs without sheet prefix (`"I25"` rather than `"LBO!I25"`) | Ambiguous which sheet | Cell-map validation rejects them; offline `validate` flags pre-run |
| 5 | Conservative `is_orange()` colour heuristic in surveys | Excel theme/indexed colours don't expose RGB on read | Restrict the colour check to fills with `fgColor.rgb` set; document in survey scripts |

(Cross-reference: parent repo's `HANDOFF.md` §5 and `HANDOFF-2026-05-26-PM.md`
catalogue the equivalent list for the routines + dashboard repos.)

## 14. Provenance + decisions log

- **2026-05-08** — repo scaffold (`v0.1.0-scaffold`). Architecture decided
  (xlwings, not pure Python). Module skeletons + pyproject + empty templates
  registry. Awaiting templates.

- **2026-05-19** — foundation v0.2.0 wired end-to-end with LBO as first
  skill. Cleanup of LBO template (CapIQ junk + hidden sheet + orphan named
  range), 28 named ranges added on v2, full cell map + validation +
  post-recalc convergence + per-deal archive policy + atomic
  project-bootstrap CLI + 66 passing unit tests. Awaiting operator review
  of v2 + Windows venv install + xlwings smoke + first live run to lock
  the cell map.

- **2026-05-24** — Phase 0 lock (`e895d6e` in vault). Anthropic SKILL.md
  spec adopted as standard; LBO is the migration pilot per #21. LBO SKILL.md
  template + 8-step migration plan staged at
  `<repo>\1. OS Structure\drafts\`. Until #21 lands, engine skills
  run as direct CLI invocations outside the `routines/skills/` tree.

- **2026-05-26** — workspace-write-policy alignment noted in §8 + §9; the
  bridge's `POST /api/workspaces` endpoint now offers an alternative path
  to two-track atomic bootstrap (#5+#6+#6c). Engine CLI's `new-project`
  remains for engine-only sessions; longer-term convergence tracked under
  #18 in OUTSTANDING. Real client mandates land at
  `<workspace-root>/1. Projects/`, not `<workspace-root>/` (pre-2026-05-22
  path now obsolete).
