"""BD (business development) watch + decay routines.

Plan v3 §6.9 Phase 5. Walks Companies/<X>.md for any with `bd_state` set,
calculates days-since-last-contact, flags entries past their state-specific
threshold. Surfaces in morning brief.
"""
