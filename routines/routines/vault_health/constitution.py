"""Constitution integrity check (#claudemd-restructure, 2026-06-11).

The constitution was restructured from ``_claude/CLAUDE.md`` into a vault-root
``CLAUDE.md`` plus per-tree rule files. Safety-critical sections (§4
sensitivity tiers, §5 never-list) are pinned by SHA-256 hashes recorded in
``_claude/constitution-manifest.json`` — this check verifies, on every
vault-health run, that:

* the manifest exists and parses;
* every ``expected_files`` entry exists in the vault;
* every ``expected_anchors`` id is present in the root ``CLAUDE.md``
  (SKILL.md cross-refs link these anchors — they are load-bearing);
* the §4 / §5 section hashes match the manifest. A mismatch means the
  safety text changed WITHOUT a same-commit manifest bump → CRITICAL.
  Never-list edits must be intentional, reviewed events.

Section extraction contract (mirrors the manifest's ``extraction_rule``):
a section is its ``## N.`` heading line through the line before the next
line starting with ``## ``, LF-joined, UTF-8, no trailing newline.

Read-only — never writes the vault.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

MANIFEST_REL = "_claude/constitution-manifest.json"


@dataclass
class Finding:
    severity: str  # "CRITICAL" (the only severity today — this is a safety gate)
    check: str     # "manifest" | "file" | "anchor" | "section-hash"
    detail: str


def section_hash(lines: list[str], number: str) -> str | None:
    """SHA-256 of section ``number`` per the manifest extraction rule.

    Returns None if the ``## <number>.`` heading is absent.
    """
    heading = re.compile(rf"^## {re.escape(number)}\. ")
    start = None
    for i, line in enumerate(lines):
        if heading.match(line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## "):
            end = j
            break
    text = "\n".join(lines[start:end])
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scan(vault: Path) -> list[Finding]:
    """Verify the constitution set against the manifest. Empty list = green."""
    findings: list[Finding] = []

    manifest_path = vault / MANIFEST_REL
    if not manifest_path.is_file():
        return [Finding("CRITICAL", "manifest", f"{MANIFEST_REL} missing")]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [Finding("CRITICAL", "manifest", f"{MANIFEST_REL} unreadable: {exc}")]

    for rel in manifest.get("expected_files", []):
        if not (vault / rel).is_file():
            findings.append(Finding("CRITICAL", "file", f"expected file missing: {rel}"))

    root = vault / manifest.get("constitution", "CLAUDE.md")
    if not root.is_file():
        findings.append(
            Finding("CRITICAL", "file", f"constitution missing: {manifest.get('constitution', 'CLAUDE.md')}")
        )
        return findings

    text = root.read_text(encoding="utf-8")
    lines = text.splitlines()

    for anchor in manifest.get("expected_anchors", []):
        if f'<a id="{anchor}"></a>' not in text:
            findings.append(
                Finding("CRITICAL", "anchor", f"load-bearing anchor missing from root: #{anchor}")
            )

    for number, expected in (manifest.get("sections") or {}).items():
        actual = section_hash(lines, number)
        if actual is None:
            findings.append(
                Finding("CRITICAL", "section-hash", f"§{number} heading not found in root CLAUDE.md")
            )
        elif actual != expected:
            findings.append(
                Finding(
                    "CRITICAL",
                    "section-hash",
                    f"§{number} hash mismatch — safety text changed without a same-commit "
                    f"manifest bump (expected {expected[:12]}…, got {actual[:12]}…)",
                )
            )

    return findings


def render_report(findings: list[Finding]) -> str:
    if not findings:
        return "# Constitution integrity\n\nGREEN — manifest, files, anchors, and §4/§5 hashes all verify.\n"
    out = [
        "# Constitution integrity — CRITICAL",
        "",
        f"{len(findings)} finding(s). Safety sections changed or constitution set broken",
        "without a same-commit manifest bump. Investigate before trusting the rules.",
        "",
    ]
    for f in findings:
        out.append(f"- **{f.severity}** [{f.check}] {f.detail}")
    out.append("")
    return "\n".join(out)
