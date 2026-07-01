"""Deal tracker routine — plan §7B.

Maintains an Excel workbook (`Projects/_Trackers/M&A Deals.xlsx`) with one row
per M&A deal. Schema mirrors the user's existing Mergermarket-style template
(26 columns: announced/completed dates, target/bidder/seller details + sectors
+ countries, EV, financials, multiples, deal description, GBP equivalent).

Two ingestion paths:
    1. **Manual:** `deal-tracker add --url <url>` or `--text <pasted-news>` —
       extracts structured deal record via Ollama, appends to workbook
    2. **Auto-fed by sector-news:** post-synthesis, the sector-news pipeline
       can flag items that look like M&A announcements and call back into
       deal-tracker for extraction (Phase 2 — wire-up after first manual runs)

The workbook is treated as **append-only** by the routine. Operator can edit
manually in Excel; routine never overwrites existing rows. Idempotency by
(announced_date, target_company) — duplicate adds are flagged, not duplicated.

Out of scope (Phase 2):
    - Real-time scraping of M&A news sites (RNS feeds, etc.)
    - Cross-check against existing Companies/<X>.md pages with relationship
      updates ("seen as buyer in deal X, seen as target in deal Y")
    - Sector page comp-data updates (write a row to `Sectors/<X>.md`'s
      precedent transactions)
    - Telegram alert on notable multiples
"""
