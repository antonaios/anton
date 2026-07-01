"""Stale-gate sweep (#steal-kocoro P3).

Scheduler-driven sweep that finds runs stuck on a human-approval step and,
fail-closed at a long horizon, auto-cancels them. See :mod:`routines.stale_gate.sweep`.
"""
