"""Self-improvement loop.

Walks Claude Code session logs to detect recurring follow-up questions
("what about capex history?" asked after several company-profile runs),
clusters them via local Ollama embeddings, and writes a proposal
markdown the operator reviews + applies. Templates evolve over time
based on real operator-feedback patterns — no auto-mutation.

Weekly cadence (scheduled task). Plus a manual `learn note` escape
hatch for explicit "this should always be there" signals.

Pipeline:
    scan      → walk ~/.claude/projects/*/*.jsonl, detect follow-ups
                (regex patterns + adjacency to assistant outputs).
                Append FeedbackEvent rows to runs/learning-events.jsonl.
    cluster   → embed events via nomic-embed-text, greedy-cluster on
                cosine similarity. Returns clusters with ≥ threshold.
    propose   → for each cluster, qwen3:14b drafts a "what should change
                in which template" recommendation. Output markdown lands
                at Routines/learning/<date>-template-evolution.md.

Operator reviews the proposal in Obsidian; applies the suggested change
to the relevant template by hand (or via a Claude session). The
proposal file gets tagged 'applied' or 'rejected' by the operator —
that audit trail is what gives the loop its memory.
"""

from routines.learning.schema import FeedbackEvent, FeedbackCluster, ProposalDoc
from routines.learning.detect import scan_session_logs, classify_event
from routines.learning.cluster import cluster_events
from routines.learning.propose import build_proposal

__all__ = [
    "FeedbackEvent", "FeedbackCluster", "ProposalDoc",
    "scan_session_logs", "classify_event",
    "cluster_events", "build_proposal",
]
