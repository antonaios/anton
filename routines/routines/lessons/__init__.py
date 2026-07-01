"""Cross-project Lessons Learned routine — semantic memory promotion across deals.

Walks every project's ``13 Lessons Learned.md``, extracts both individual
lessons and explicit "patterns worth promoting" entries, then proposes
additions to ``Registers/Lessons.md`` (the cross-project register).
Where 2+ projects share a theme, BERTopic clusters them as a cross-
project pattern.

Plan v3 §7d / W9+. Module is functional today against a single project
(Mode A — extract flagged patterns) but only fully blooms once 2-3
closed projects exist (Mode B — cross-project clustering).
"""
