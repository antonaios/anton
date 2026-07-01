"""Extractors — one per source type.

Each extractor exports a single function:

    def gather(vault_root: Path, sector: str, *, since: date | None = None,
               ollama: OllamaClient | None = None) -> list[SectorExtract]

The CLI in `routines.sectors.cli` dispatches to these. Each extractor is
self-contained — no cross-extractor dependencies.
"""
