# TMT Regulatory Radar - handover

A regulatory tracker for the TMT team. Detection is deterministic and local: the firm machine
reaches every gov.in venue directly, so a registry-driven engine (`engine/`, zero LLM) sweeps
**52 live sources on demand** — triggered by hand from the dashboard's **Update now** button or
`engine/run_sweep.sh`, never on a hidden schedule — and is the primary lane. A cloud session can
run the same sweep as a cross-check.

The engine sorts what it finds into **three lanes**:

- **Instruments** — documents that bind a client (rules, notifications, orders, press notes);
  each may trigger a client memo. 236 ledgered.
- **Judgments** — tribunal and court decisions, read for how the law is being applied rather than
  as a compliance deadline; reproduced under s.52(1)(q)(iv) of the Copyright Act 1957. 123 ledgered.
- **Signals** — real, useful information that is not itself a binding instrument (announcements,
  security bulletins, court diaries, and reported-but-unpublished instrument leads). Kept out of the
  instruments ledger and never memo-triggering; tagged `lane: signals` in the data feed, with the
  reported-but-unpublished leads highlighted on the Signals tab.

Coverage spans three strata (17 regulators, 52 venues live):

- **telecom** (28 venues): TRAI (16 listings incl. all five standing-direction divisions and the
  sitewide RSS as a lag cross-check), NCCS latest/SAS/ComSec/ITSARs, TEC circulars, gazette-notified
  standards, MTCTE essential requirements and What's New, TDSAT notices and orders, IN-SPACe, and
  the Ministry-of-Communications e-Gazette lane.
- **technology & data** (14): MeitY gazettes, acts & policies, orders & notices and guidelines via
  its public headless-WordPress JSON API; CERT-In advisories (CIAD), vulnerability notes (CIVN),
  security guidelines (CISG) and s.70B(6) directions; the MeitY e-Gazette lane; **CCI antitrust
  orders**, **CCPA orders & advisories**, **DPIIT FDI press notes**, and the **Supreme Court** and
  **Delhi High Court** judgment feeds (TMT-filtered).
- **media** (10): MIB acts & policy, orders & notices, other communications, digital-media wing,
  broadcasting wing and advisories; PRGI, CBFC, ASCI, and the Ministry-of-I&B e-Gazette lane.

UIDAI and the DoT eServices/Saral Sanchar venues were removed after a compliance review; UIDAI and
DoT instruments are recovered lawfully from the e-Gazette instead. Every source's legal basis —
both the access (scraping) question and the copyright (reproduction) question, reasoned separately —
is set out in **`docs/TMT-Radar-legal-basis.pdf`**. A partner pipeline can pull the feed through the
queryable connector (**`engine/radar_api.py`**, contract in **`docs/CONNECTOR.md`**).

**Dashboard (share this with the partner):**
https://claude.ai/code/artifact/a9fd1bea-260c-4aa2-9354-b251ccb8e873
Private to Abhi until shared from the page's share menu. Four tabs: Instruments (ledger, hover
for the official title, click to expand), Judgments (tribunal and court decisions), Coverage
(per-source health, blind spots), Signals (reported-but-unpublished instruments). An **Update now**
button sits by the "Last updated" stamp; in the sandboxed published page it opens the local
operator console, or POSTs to a partner pipeline endpoint if one is wired (see `docs/CONNECTOR.md`).

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
| **A. Local engine** (`engine/`) **- primary** | Deterministic Python: fetch, per-source adapter parse, row-floor check, tripwires, validation gates, dedupe, classify, SQLite ledger plus append-only JSONL. run on demand (the dashboard Update-now button or `run_sweep.sh`), then `export` rewrites `data/items.json`. | **None (0%)** |
| **B. Cloud cross-check** (scheduled, `runbook/cloud-runbook.md`) | Secondary. Confirms the local ledger is fresh and alerts if it stalled, then runs the two lanes that need WebSearch/WebFetch: the e-Gazette lane (legally authoritative, covers regulator-site lag) and the signals lane (press, NGO and litigation watch for letter-form instruments). Output is flagged `needs_verification` or `UNPUBLISHED`, never silently merged. | Copier plus mechanical validator only, no judgment calls |
| **C. Human curation and memos** | Short titles, row lines, folds, gists, and client alerts. Memos are template fill: fixed structure, verbatim operative language from the instrument PDF, deadlines as stated. Interpretive lines are marked `[ASSOCIATE REVIEW]`. | No generative legal drafting |

## Why this won't miss things quietly

- **A broken parse is a failure, not silence.** Every source declares a `row_floor`; a page that
  parses below it is a FAILED source, never "no new items". The sweep exits non-zero, the run
  surfaces it, and two consecutive failures are flagged prominently on the coverage page.
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
  fixture in `engine/fixtures/`; all 52 must clear their floor with at least 70% of rows passing
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
- **Court feeds are coarse-filtered by party name, not by subject.** The Supreme Court and Delhi
  High Court judgment titles carry only party names, so the tight TMT keyword filter has low
  recall — a genuinely TMT judgment whose parties are unnamed companies can slip past. Fine
  classification belongs downstream, on the order text, in the partner pipeline. The **Supreme
  Court** CAPTCHA-free surface is only a 2-item "Latest Orders" teaser (the full archive is
  CAPTCHA-gated and out of scope), so its yield is low by design.
- **Delhi High Court's Latest-Judgments feed was erroring at source on 27-08-2026** ("An error
  occurred while fetching data"). The adapter is verified against the court's healthy markup and
  the direct-PDF layer is live; the coverage page reads the venue FAILED until the court's own
  feed recovers — an honest source outage, not a parser break.
- **NCLAT is cleared but not yet collected (planned).** Its public listing renders a view frozen
  at 2021; recent orders sit behind a Drupal date-filter POST that needs an e-Gazette-style driver.
  Registered as a named blind spot on the coverage page.
- **MeitY's own gazette listing is 8+ months stale** (newest row 16-12-2025) while 2026 MeitY
  gazette instruments exist — so it reads QUIET, honestly. The MeitY e-Gazette lane and PIB are
  the real-time net; the MeitY API is provenance and eventual posting. (MeitY, DPIIT and the SC
  feed all sit behind an Akamai WAF that 403s crawler-signature UAs; the engine's honest
  `(compatible; TMTRegulatoryRadar/…)` identifier is served normally — no browser spoof. See
  Principle 2 in the legal-basis PDF.)
- **DoT venues were removed after the compliance review.** `dot.gov.in` serves a JS-only shell,
  `wpc.dot.gov.in` is dead, and `saralsanchar.gov.in` sits behind a WAF; DoT instruments are
  recovered from the e-Gazette (Ministry of Communications) and PIB instead.
- **TRAI's sitewide RSS lags about 6 weeks** and is never a trigger, only a cross-check.
  **TEC What's New** has been dormant since 22-12-2025. **MIB's /en/ pages** time out
  intermittently; fetch retries cover it.
- **Watches**: DPB (no website yet) and OGAI (dev portal down) have no live source; they stand up
  as registry entries when either regulator publishes a real site.

## Folder map

    README.md                          this file
    engine/                            Lane A, the detection engine (see engine/README.md)
      tracker.py                       CLI: sweep, backfill, selftest, audit, compliance,
                                       import-baseline, export, health, fetch-pdfs
      radar/                           fetch, parse, validate, classify, tripwires, ledger, core
      registry_v2.json                 single source of truth: 52 live sources, per-source adapter
                                       config, validation gates, classification regexes
      fixtures/                        saved page captures, the offline selftest corpus
      ledger.db / ledger.jsonl         canonical SQLite store plus append-only mirror
      health.json                      per-source status from the last sweep
      run_sweep.sh                     launchd entry point: sweep, export, notify
      radar_api.py                     queryable connector API for a partner pipeline (docs/CONNECTOR.md)
    data/items.json                    dashboard contract, rewritten by `tracker.py export`
    data/rules_shelf.json              Telecom Act 2023 rules shelf, carried in page state
    data/short_titles.json             deterministic display titles
    data/row_lines.json                one factual line per instrument
    data/folds.json                    announcements folded into the instrument they announce
    data/signals.json                  unpublished-instrument signals
    docs/TMT-Radar-legal-basis.pdf     per-source legal basis: access + copyright, reasoned apart
    docs/CONNECTOR.md                  partner-pipeline integration contract (API / feed / MCP)
    engine/radar_api.py                read-only localhost connector over the same ledger
    pipeline/pipeline.py               reference client-alert pipeline: feed -> client match -> draft emails
    pipeline/clients.json              sample client roster (watch-lists); replace with the firm's
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

- **Manual trigger (primary)**: there is no schedule. A sweep runs only when a human asks for one —
  the dashboard's **Update now** button, or `engine/run_sweep.sh` on the firm machine, or asking
  Claude "run a TMT Radar sweep now". Each run sweeps, then exports `data/items.json` and rebuilds
  the dashboard. This is deliberate: unattended crawling is what turns an arguable technical breach
  into an evidential one, so collection is always a supervised, on-demand act. Logs: `engine/sweep.log`.
- **Connector for a partner pipeline**: `engine/.venv/bin/python engine/radar_api.py` serves a
  read-only localhost API (health, sources, instruments, judgments, digest) that reads the same
  ledger; the boundary contract is `docs/CONNECTOR.md`.
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
