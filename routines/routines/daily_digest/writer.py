"""Atomic writer for the daily digest.

Format on disk mirrors the morning brief: YAML frontmatter (with the
structured payload as a JSON scalar block) plus a human-readable
markdown body. The bridge endpoint parses the frontmatter; humans browse
the body.
"""

from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from pathlib import Path

from routines.daily_digest.schema import DailyDigest
from routines.shared.vault_writer import atomic_write

log = logging.getLogger(__name__)


def digest_path(vault_root: Path, the_date: date_cls) -> Path:
    return vault_root / "Routines" / "daily-digests" / f"{the_date.isoformat()}.md"


def write_digest(vault_root: Path, digest: DailyDigest, the_date: date_cls) -> Path:
    path = digest_path(vault_root, the_date)
    md = _render_markdown(digest, the_date)
    atomic_write(path, md, vault_root=vault_root)
    return path


def _render_markdown(digest: DailyDigest, the_date: date_cls) -> str:
    payload_json = json.dumps(digest.model_dump(), indent=2)
    fm = (
        "---\n"
        "type: daily-digest\n"
        "sensitivity: internal\n"
        f"date: {the_date.isoformat()}\n"
        f"generated: {digest.source}\n"
        "tags: [daily-digest, routines, auto-generated]\n"
        "data: |\n"
        + _indent(payload_json, "  ")
        + "\n---\n\n"
    )

    body_lines = [
        f"# Daily Digest · {digest.date}",
        "",
        f"_Source: {digest.source}_",
        "",
    ]

    if digest.activity:
        body_lines += ["## Routines today", ""]
        for r in digest.activity:
            body_lines.append(f"- **{r.text}** — _{r.sub}_")
        body_lines.append("")

    if digest.vaultChanges:
        body_lines += ["## Vault writes", ""]
        for r in digest.vaultChanges:
            body_lines.append(f"- `{r.text}` — _{r.sub}_")
        body_lines.append("")

    if digest.antonCloses:
        body_lines += [
            "## Anton closes",
            "",
            digest.antonCloses,
            "",
        ]

    return fm + "\n".join(body_lines)


def _indent(text: str, prefix: str) -> str:
    return "\n".join(prefix + line for line in text.splitlines())
