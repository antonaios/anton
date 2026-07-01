# Corporate finance deal-folder structure

A blank, ready-to-copy folder scaffold for running a single corporate-finance / sell-side
transaction. It mirrors the on-disk structure ANTON's workspace-write policy expects for a deal
(distinct from the **vault**, which is the cross-deal knowledge store).

**This ships as directories only** — every folder is kept by an empty `.gitkeep`. No documents,
spreadsheets, or templates are included: those are your own work product. Drop your blank house
templates (engagement letter, NDA, comps, model, fee proposal, …) into the matching folders.

## How to use

Copy the whole tree once per deal into your working area and rename the copy to the deal:

```powershell
Copy-Item -Recurse "templates\corporate-finance-deal-structure" "C:\Work\Project-Acme"
```

Then point ANTON's external project path at your working root (see `_claude/profile.example.md`
→ `external_project_paths`).

## The structure

```
0. Pitch
1. Process Management
   1. KYC & AML
   2. Engagement Letter
   3. Advisors & WGL
      NDA Advisers
         NDA Financial Advisers
         NDA Supporting Advisers
      WGL
   4. Timeline            (+ Archive)
   5. Process tracker
   6. IRL                 (+ Archive)
2. Kick off and weekly decks
   Meeting notes & agendas
3. Financials & analysis
   1. Operating Model
   2. Valuation
      00. OLD
      01. COMPS
         Deal tracker template
   3. Investment proposal
4. Marketing Materials
   1. Teaser
   2. Information Memorandum
   3. Management Presentations
5. Received from Client
6. DD & Advisors
7. Legal
   Process Letters
8. Buyers
   NDA Buyers
      Executed
      Template NDA
9. Dataroom
   VDR Index
```
