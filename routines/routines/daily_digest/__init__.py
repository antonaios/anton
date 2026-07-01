"""Daily digest — EOD wrap-up sibling to the morning brief.

Walks the day's audit logs + vault writes, produces a short reflective
close via local Ollama qwen3:14b, and lands the digest at
``Routines/daily-digests/<date>.md``. Same structural pattern as
``routines.morning_brief``.
"""
