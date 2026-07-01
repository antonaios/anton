"""Ticker-multiples skill — lightweight Yahoo/OpenBB market-snapshot quick-look.

Renamed from the unbuilt SESSION-27B ``comps-pull`` (per
``COMPS-REDESIGN-2026-06-01.md``). A dashboard quick-look / data helper that
returns the CURRENT trading multiples for one or more public tickers. It is
deliberately FIREWALLED from the valuation Comps template, the precedent-
transactions tracker, and the deal Valuation folder: it never writes a
workbook, never touches ``Sectors/`` or the tracker, and (by default) never
writes the vault at all. The deliverable IS the returned snapshot.

The skill reuses the existing markets adapter Protocol (``routines.markets``)
via ``routines.markets.comps.build_comps`` — no provider plumbing is
duplicated here. See ``scripts/ticker_multiples.py`` for the Pydantic
input/output and the build logic, and ``SKILL.md`` for the §14 contract.
"""
