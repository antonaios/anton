# Third-Party Notices

ANTON is distributed under the MIT License (see `LICENSE`). It uses and, in one
case, ports logic from third-party open-source software. This file records
attributions and the licensing considerations that informed the choice of an
MIT license for this repository.

## 1. Ported source code - microsoft/markitdown (MIT)

`routines/routines/shared/pptx_to_markdown.py` is a **standalone port** of the
PPTX-conversion logic from Microsoft's
[markitdown](https://github.com/microsoft/markitdown) `_pptx_converter.py`.
markitdown is **not** taken as a runtime dependency - only the converter logic
(position-sorted shape iteration, table/chart extraction, speaker-notes block)
was lifted and adapted.

markitdown is MIT-licensed:

```
MIT License

Copyright (c) Microsoft Corporation.

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction ... [standard MIT terms]
```

Because markitdown is MIT, reproducing its copyright + permission notice here
satisfies its only redistribution condition, and ANTON can itself be MIT.

## 2. OpenBB Platform (AGPL-3.0) - optional, un-vendored pip dependency

The markets adapter can optionally use the
[OpenBB Platform](https://github.com/OpenBB-finance/OpenBB) SDK
(`pip install -e .[markets]`). **OpenBB is licensed AGPL-3.0.**

Important characteristics for licensing:

* OpenBB source is **NOT redistributed in this repository.** It is an optional
  dependency the end user installs themselves. Without the `[markets]` extra,
  the adapter falls back to a deterministic stub provider and OpenBB is never
  imported.
* Because it is not vendored here, this MIT repo does not redistribute AGPL
  code.

> **Operator flag (review before hosting).** The AGPL-3.0 copyleft has a
> network-use clause (Section 13): if you ever run ANTON as a *hosted network
> service* with the OpenBB extra installed and active, AGPL may require you to
> offer the corresponding source of the combined work to your users. For local
> single-operator use this does not trigger. If you move to a hosted/multi-user
> deployment, take a deliberate look at the OpenBB dependency boundary first.

## 3. Deliberately EXCLUDED external engines

Two heavier orchestration / valuation engines are invoked by ANTON over
process or HTTP boundaries and are **deliberately NOT included** in this mirror:

* **Synapse (AGPL)** - invoked as an external service. Not vendored; not
  published here. Treated as a documented external dependency.
* **MetaGPT** - invoked over a subprocess/HTTP boundary as an external engine.
  Not vendored; not published here.

Keeping these out of the repository is both a licensing decision (avoid
redistributing AGPL source under an MIT repo) and an architecture decision
(they sit behind clean boundaries).

## 4. Key runtime dependencies (permissive licenses)

The bridge (`routines`) and engine declare these primary dependencies; all are
permissively licensed (MIT / BSD / Apache-2.0 / PSF):

| Package | Typical license | Used for |
|---|---|---|
| fastapi | MIT | HTTP bridge for the dashboard |
| uvicorn | BSD-3-Clause | ASGI server |
| pydantic (via fastapi) | MIT | request/response models |
| click | BSD-3-Clause | CLI entry points |
| requests | Apache-2.0 | HTTP client |
| pyyaml | MIT | config / frontmatter |
| python-frontmatter | MIT | note frontmatter parsing |
| watchdog | Apache-2.0 | HiNotes file watcher |
| python-docx | MIT | DOCX export ingest |
| pypdf / pypdfium2 | BSD / Apache-2.0 | PDF intake |
| python-pptx | MIT | PPTX intake (see port note above) |
| openpyxl | MIT | workbook writes |
| networkx | BSD-3-Clause | vault wikilink graph |
| apscheduler | MIT | bridge-embedded scheduler |
| sqlalchemy | MIT | scheduler job store |
| cryptography | Apache-2.0 / BSD | Fernet credential encryption |
| pywin32 | PSF | Windows DPAPI key-wrap (Windows only) |
| anthropic | MIT | cloud Claude API fallback lane |
| streamlit | Apache-2.0 | localhost dashboard MVP |
| pandas / numpy | BSD-3-Clause | engine numerics |
| xlwings | BSD-3-Clause | Excel template driving (Windows only) |

Optional extras (`[learning]`, `[recall]`) pull bertopic, umap-learn, hdbscan,
sentence-transformers and a cross-encoder reranker - all permissively licensed
(MIT / BSD), but heavy; not required for the bridge hot path.

> Verify exact license texts of pinned versions before any commercial
> redistribution. This table reflects the well-known license of each project as
> of this snapshot.
