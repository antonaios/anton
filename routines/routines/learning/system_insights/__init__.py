"""Dream Cycle Phase 5 — system self-reflection routine (#73).

ANTON's existing ``routines/learning/`` (BERTopic via #40) analyses what the
**operator** asked — clusters operator queries, surfaces template-evolution
signals. Nothing analyses what the **system itself did**. Schedule misses,
hook failures, budget overruns, retry spirals, slow-skill outliers, audit
anomalies — all observable in JSONL but unsurfaced unless the operator goes
looking.

This routine closes that gap by running weekly (Sun 18:00 Europe/London,
sandwich before Mon 06:30 morning-brief), reading six telemetry sources
(audit JSONL + audit_index.db + scheduler history + llm_calls.jsonl +
budget incidents + audit_failures.jsonl), clustering / aggregating
anomalies, and surfacing them as operator-gated proposals at
``Routines/system-insights/<YYYY-WW>-<topic-slug>.md`` with frontmatter
``kind: system-insight`` (post-#58 approval tier).

Operator-gated by design — NEVER auto-routes, NEVER silently mutates vault
state. The promotion contract (#8 + #58) holds. This is what distinguishes
ANTON's adoption of Phase 5 from Thoth's original (Thoth phases 1-4
auto-mutate the knowledge store; ANTON rejects that path).

See ``evaluations/THOTH-EVALUATION-2026-05-28.md`` §"Dream Cycle Phase 5"
for the pattern source.
"""

from routines.learning.system_insights.analyse import (
    InsightProposal,
    analyse_window,
)
from routines.learning.system_insights.readers import (
    TelemetryEvent,
    read_all_sources,
    read_audit_db,
    read_audit_failures,
    read_audit_jsonl,
    read_budget_incidents,
    read_llm_calls,
    read_scheduler_history,
)
from routines.learning.system_insights.writer import (
    write_proposal,
    proposal_path_for,
)

__all__ = [
    "TelemetryEvent",
    "InsightProposal",
    "read_all_sources",
    "read_audit_jsonl",
    "read_audit_db",
    "read_scheduler_history",
    "read_llm_calls",
    "read_budget_incidents",
    "read_audit_failures",
    "analyse_window",
    "write_proposal",
    "proposal_path_for",
]
