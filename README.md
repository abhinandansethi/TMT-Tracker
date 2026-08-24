# TMT Regulatory Radar - handover

A regulatory tracker for the TMT team. Detection is now deterministic and local: the firm
machine reaches every gov.in venue directly, so a registry-driven engine (`engine/`, zero LLM)
sweeps **43 live sources every two hours** under launchd and is the primary lane. The cloud
schedule became the cross-check.

Three strata are live plus a safety net:

- **telecom** (24 sources): TRAI 15 listings including all five standing-direction divisions and
  the sitewide RSS as a lag cross-check, DoT eServices circulars and the Act & Rules shelf,
  NCCS latest/SAS/ComSec/ITSARs, TEC What's New, TDSAT notices, PIB releases ministry-filtered.
- **technology and data** (10): MeitY gazettes, acts and policies, orders and notices through its
  public headless-WordPress JSON API; CERT-In advisories (CIAD), vulnerability notes (CIVN),
  security guidelines (CISG) and s.70B(6) directions; UIDAI circulars, notifications and OMs.
- **media** (8): MIB acts and policy, orders and notices, other communications, digital media
  wing, broadcasting wing; PRGI, CBFC, ASCI.
- **safety net** (1): the e-Gazette recent Extraordinary and Weekly panels, ministry-filtered.

**Dashboard (share this with the partner):**
https://claude.ai/code/artifact/a9fd1bea-260c-4aa2-9354-b251ccb8e873
Private to Abhi until shared from the page's share menu. Three tabs: Instruments (ledger,
attention strip, hover for the official title, click to expand), Coverage (per-source health,
blind spots), Signals (reported-but-unpublished instruments).

There are no tiers: every row is equal weight, what a document IS comes from its own type label,
and the only filter is a Routine chip that reveals the recurring drive tests, data releases and
sitting notices. The official title is one hover away and the full record one click away, which
is what keeps a table of 30-word statutory titles readable. Each row carries a short display
title plus one factual line saying what the instrument does. Documents that merely announce
another instrument are folded into it rather than listed twice. The page also carries the
pipeline state (registry, classification, validation gates, short-title map, rules shelf) inside
its embedded JSON, so a scheduled cloud session needs nothing but the artifact URL.

## Architecture - three lanes, minimal LLM

| Lane | What | LLM involvement |
|---|---|---|
| **A. Local engine** (`engine/`) **- primary** | Deterministic Python: fetch, per-source adapter parse, row-floor check, tripwires, validation gates, dedupe, classify, SQLite ledger plus append-only JSONL. launchd every 2 hrs on the firm machine, then `export` rewrites `data/items.json`. | **None (0%)** |
| **B. Cloud cross-check** (scheduled, `runbook/cloud-runbook.md`) | Secondary. Confirms the local ledger is fresh and alerts if it stalled, then runs the two lanes that need WebSearch/WebFetch: the e-Gazette lane (legally authoritative, covers regulator-site lag) and the signals lane (press, NGO and litigation watch for letter-form instruments). Output is flagged `needs_verification` or `UNPUBLISHED`, never silently merged. | Copier plus mechanical validator only, no judgment calls |
| **C. Human curation and memos** | Short titles, row lines, folds, gists, and client alerts. Memos are template fill: fixed structure, verbatim operative language from the instrument PDF, deadlines as stated. Interpretive lines are marked `[ASSOCIATE REVIEW]`. | No generative legal drafting |

## Why this won't miss things quietly

- **A broken parse is a failure, not silence.** Every source declares a `row_floor`; a page that
  parses below it is a FAILED source, never "no new items". The sweep exits non-zero, launchd
  fires a macOS notification, and two consecutive failures are flagged prominently.
- **Nothing is dropped without a reason.** A row that fails a validation gate (date parses
  against the source's declared formats, link on the source's allowed domains, title 8 to 300
  chars) goes to the `quarantine` table with the reason and the raw row. It never reaches the
  ledger and never vanishes.
- **Tripwires.** Sequential series are gap-checked: TRAI press releases (PR_No.NNofYYYY) and
  CERT-In CIAD, CIVN and CISG numbering. Document shelves are monotonic, so a shrink is a parse
  regression. Staleness wires fire when a venue that should move has not. A generic fallback
  parser cross-checks each adapter, and a structure fingerprint per source turns markup change
  into a WARN.
- **Cross-check sources.** TRAI What's New, TRAI open consultations and the TRAI RSS run with
  `role: crosscheck`. Anything they catch first means a primary parser missed it, and the sweep
  says so instead of quietly accepting the catch.
- **The selftest gates every deploy.** `tracker.py selftest` parses every live source's saved
  fixture in `engine/fixtures/`; all 43 must clear their floor with at least 70% of rows passing
  the gates. It runs offline and takes seconds.
- **No classification drift.** `tracker.py audit` asserts that every engine-classified row's
  routine flag equals what the current regex produces, and exits non-zero otherwise. The earlier
  three-tier scheme was removed precisely because it required judgment the rules could not
  reproduce: an audit found 9 of 102 rows disagreeing with their own rule table.
- **Regression against v1.** Engine v2 re-detected the full 102-item v1 telecom baseline with
  zero misses in either direction. Two findings are documented rather than papered over: v1's
  baseline titles were curated paraphrases, not verbatim copies of the venue text, and two rows
  were duplicates, now reconciled with `status: duplicate` and a `duplicate_of:<id>` flag.
- **Independent verification.** The baseline was cross-checked against a ground-truth list built
  purely from secondary sources (press, law-firm alerts, civil society): **18/18 caught**,
  including three instruments that exist only as unpublished letters (signals lane) and four that
  regulator portals had not listed (gazette lane).

## Known blind spots (on the dashboard, not hidden)

- **Letter-form and unpublished instruments never appear on official venues.** They were about
  40% of the telecom instruments in the July to August window and over 90% of the
  content-blocking layer. The signals lane is the only net for them, and they stay flagged
  unofficial and out of the instruments ledger.
- **MeitY's own gazette listing is 8+ months stale** (newest row 16-12-2025) while 2026 MeitY
  gazette instruments exist. The e-Gazette and PIB lanes are the real-time net for MeitY; the
  MeitY API is provenance and eventual posting.
- **e-Gazette coverage is the homepage panels only.** Full ministry-by-date search is an ASP.NET
  postback flow and is on the roadmap. The panel itself has gone stale for weeks before, so a
  staleness wire fires at 14 days.
- **dot.gov.in** main site serves a JS-only shell, mirrored by eServices, and drafts can appear
  there first (gazette and signals cover). **wpc.dot.gov.in** is dead. **saralsanchar.gov.in**
  sits behind a WAF. **TDSAT judgments and daily orders** are behind POST forms; only the notices
  page is swept.
- **The eServices Act-and-Rules shelf lags gazetting**: it listed neither the Network
  Authorisation Rules (20-07) nor the User Identification Rules (09-08) two weeks after they were
  gazetted. That gap is exactly why the gazette lane exists.
- **TRAI's sitewide RSS lags about 6 weeks** and is never a trigger, only a cross-check.
  **TEC What's New** has been dormant since 22-12-2025. **MIB's /en/ pages** time out
  intermittently; fetch retries cover it.
- **Watches**: DPB (no website yet) and OGAI (dev portal down) have no live source; they stand up
  as registry entries when either regulator publishes a real site.

## Folder map

    README.md                          this file
    engine/                            Lane A, the detection engine (see engine/README.md)
      tracker.py                       CLI: sweep, backfill, selftest, audit, import-baseline,
                                       export, health, fetch-pdfs
      radar/                           fetch, parse, validate, classify, tripwires, ledger, core
      registry_v2.json                 single source of truth: 43 live sources, per-source adapter
                                       config, validation gates, classification regexes
      fixtures/                        saved page captures, the offline selftest corpus
      ledger.db / ledger.jsonl         canonical SQLite store plus append-only mirror
      health.json                      per-source status from the last sweep
      run_sweep.sh                     launchd entry point: sweep, export, notify
      com.trilegal.tmtradar.plist      the 2-hourly schedule (install steps in its comment)
    data/items.json                    dashboard contract, rewritten by `tracker.py export`
    data/rules_shelf.json              Telecom Act 2023 rules shelf, carried in page state
    data/short_titles.json             deterministic display titles
    data/row_lines.json                one factual line per instrument
    data/folds.json                    announcements folded into the instrument they announce
    data/signals.json                  unpublished-instrument signals
    runbook/cloud-runbook.md           Lane B: exact steps every scheduled cloud run follows
    memos/memo_template.docx           fixed client-alert template
    memos/2026-08-10_TRAI_1601-series_client-alert_SAMPLE.docx   real sample
    dashboard/tmt-radar.html           offline copy of the dashboard (live copy = artifact URL)
    dashboard/tmt-atlas.html           venue atlas page, built from registry/atlas.json
    code/audit_routine.py              v1 classification audit over data/items.json
    code/merge_verify.py, build_dashboard.py, build_atlas.py    v1 build and verification scripts
    registry/sources.json              v1 registry, kept for the v1 audit and baseline provenance
    extractor/                         v1 Lane A extractor, superseded by engine/

## Operations

- **Scheduled (primary)**: launchd runs `engine/run_sweep.sh` every 2 hours and at load. Install:

      cp engine/com.trilegal.tmtradar.plist ~/Library/LaunchAgents/
      launchctl load ~/Library/LaunchAgents/com.trilegal.tmtradar.plist

  Uninstall with `launchctl unload` on the same path. Each run sweeps, then exports
  `data/items.json`. A macOS notification fires on a non-zero exit (a source FAILED) and on new
  substantive items. Logs: `engine/sweep.log` and `engine/launchd.log`.
- **After any run, read the health file**: `engine/.venv/bin/python engine/tracker.py health`.
  Tripwire WARNs (sequence gap, shelf shrink, staleness, drift) are recorded there and printed in
  the sweep log, but they do not by themselves change the exit code.
- **Before trusting any change** to an adapter, a selector or the registry, run
  `engine/.venv/bin/python engine/tracker.py selftest`. It must print "selftest passed"; then run
  `tracker.py audit`, then a scoped `tracker.py sweep --source <id>`.
- **On demand**: ask Claude "run a TMT Radar sweep now".
- **Instrument PDFs**: `engine/.venv/bin/python engine/tracker.py fetch-pdfs` archives every
  ledgered PDF into `instruments/` (gitignored). Memos always link the official PDF.
- **Backfill** a window after adding a source: `tracker.py backfill --since YYYY-MM-DD`
  (optionally `--source` or `--stratum`).
- **Git is initialised on this repo.** Commit every sweep-relevant change: registry edits, adapter
  changes, refreshed fixtures, and the ledger and health snapshots that a sweep produces.
  `engine/.venv/`, `instruments/` and `__pycache__/` are gitignored.

*Internal working tool. Every instrument must be verified against the gazette text before client
advice. Nothing here is legal advice.*
