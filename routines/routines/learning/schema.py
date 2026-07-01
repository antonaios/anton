"""Schemas for the self-improvement (learning) routine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class FeedbackEvent:
    """One detected follow-up question — the operator asking for something
    that wasn't in a deliverable they just received.

    Two sources:
      - scan:  inferred from session logs (heuristic detector)
      - note:  explicit operator entry via `learn note "..."`
    """
    timestamp: str                        # ISO; when the user said it (or now() for manual)
    text: str                             # the verbatim follow-up
    source: str                           # "scan" | "note"
    session_id: Optional[str] = None
    prior_artifact: Optional[str] = None  # path of the deliverable they were reacting to
    prior_artifact_kind: Optional[str] = None  # "company-profile" | "ic-memo" | etc, when classifiable
    classification: Optional[str] = None  # detector's best guess of theme: "capex" | "ownership" | ...
    operator_target: Optional[str] = None  # vault path the operator wants this added to


@dataclass
class FeedbackCluster:
    """A group of FeedbackEvents that look thematically similar."""
    theme: str                            # short label, e.g. "capex history and projections"
    events: list[FeedbackEvent] = field(default_factory=list)
    centroid_text: Optional[str] = None   # representative phrasing for the cluster
    artifact_kinds: list[str] = field(default_factory=list)   # which deliverables the events followed

    @property
    def size(self) -> int:
        return len(self.events)


@dataclass
class ProposalDoc:
    """Output of the propose step — a markdown file the operator reviews
    in Obsidian and applies (or doesn't).
    """
    generated_at: datetime
    clusters: list[FeedbackCluster] = field(default_factory=list)
    markdown_path: Optional[str] = None   # vault-relative
