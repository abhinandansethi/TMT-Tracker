# TMT Regulatory Radar — handover

A regulatory tracker for the TMT team. Stratum 1 (**telecom**) is live and verified; the other
strata (technology & data, media) are fully mapped in the registry and activate by flipping a
flag, not by rebuilding.

**Dashboard (share this with the partner):**
https://claude.ai/code/artifact/a9fd1bea-260c-4aa2-9354-b251ccb8e873
Private to Abhi until shared from the page's share menu. Three tabs: Instruments (tiered
ledger, attention strip, hover for the official title, click to expand), Coverage (per-source
health, blind spots, strata not yet live), Signals (reported-but-unpublished instruments).

Visual system comes from the Claude Design canvas: Spectral for instrument titles, IBM Plex
Sans for interface, IBM Plex Mono for data; navy #003F62 accent, ochre for consultation tier,
alarm red for anything urgent or unverified. There are no tiers: every row is equal weight,
what a document IS comes from its own type label (Rules, Direction, Circular, Consultation,
Draft, Notification, Release, Notice), and the only filter is a Routine chip that reveals the
recurring drive tests, data releases and sitting notices. The
official title of every instrument is one hover away and the full record one click away,
which is what keeps a table of 30-word statutory titles readable. Each row carries a short
display title plus one factual line saying what the instrument does, so four authorisation
rules notified on the same day read as four different things. Documents that merely announce
another instrument are folded into it rather than listed twice.

The page also carries the pipeline state (source registry, tier rules, validation gates,
short-title map, rules shelf) inside its embedded JSON, so a scheduled sweep needs nothing
but the artifact URL.

## Architecture — three lanes, minimal LLM

| Lane | What | LLM involvement |
|---|---|---|
| **A. Local extractor** (`extractor/`) | Deterministic Python: fetch → parse → validate → dedupe → rule-table tier → ledger. Cron on a firm machine. | **None (0%)** |
| **B. Cloud sweep** (scheduled, `runbook/cloud-runbook.md`) | Fresh cloud session every 2 hrs (08:00–20:00 IST Mon–Fri) + 09:00 IST weekends. Needed because the cloud sandbox cannot reach gov.in directly — the model acts only as fetch-and-copy transport; every row passes a mechanical validation gate (date format, domain whitelist, title length) before the ledger; tiers come from the same rule table as Lane A. Updates the dashboard, drafts memos for new tier-1 items. | Copier + validator only — no judgment calls |
| **C. Cross-check lanes** | e-Gazette recent-lists (daily) for instruments regulator portals lag on; news-signals sweep (Mon/Fri) for letter-form instruments that are never published. Output is flagged (`gazette copy pending` / `UNPUBLISHED`) — never silently merged. | Copier only |

Memos are **template fill**: fixed structure, verbatim operative language from the instrument
PDF, deadlines as stated. Interpretive lines are marked `[ASSOCIATE REVIEW]`. No generative
legal drafting.

## Why this won't miss things quietly

- **Zero silent failures** — every source reports OK/FAILED each run; Lane A exits non-zero on
  any failure; Lane B flags a source down twice consecutively on the dashboard and in the alert.
- **No classification drift** — `code/audit_routine.py` asserts that every ledger row's routine
  flag equals what the regex produces, and exits non-zero otherwise. Run it after any sweep.
  The earlier three-tier scheme was removed precisely because it required judgment the rules
  could not reproduce: an audit found 9 of 102 rows disagreeing with their own rule table.
- **Sequential-number tripwire** — TRAI PRs are numbered (PR_NoNNofYYYY); a numbering gap = a
  missed item. Backfill confirmed PRs 65–114/2026 complete.
- **Independent verification** — the baseline was cross-checked against a ground-truth list
  built purely from secondary sources (press, law-firm alerts, civil society): **18/18 caught**,
  including three instruments that exist only as unpublished letters (signals lane) and four
  that regulator portals had not listed (gazette lane).
- **Known blind spots are on the dashboard, not hidden**: dot.gov.in main site serves a JS-only
  shell (mirrored by eServices; drafts can appear there first — gazette/signals cover);
  wpc.dot.gov.in dead; saralsanchar.gov.in behind a WAF; TDSAT judgments behind POST forms;
  TRAI's RSS lags ~6 weeks (never used as trigger); the eServices Act-and-Rules shelf listed
  neither the Network Authorisation Rules (20-07) nor the User Identification Rules (09-08) two
  weeks after gazetting — exactly the gap the gazette lane exists for. Letter-form instruments
  (~40% of the July–Aug window) can never appear on official venues — the signals lane surfaces
  them as leads, clearly labelled unofficial.

## Folder map

    README.md                     ← this file
    registry/sources.json         ← single source of truth: 49 venues, classification, validation gates
    data/items.json               ← verified baseline ledger (102 items, 1 Jun–24 Aug 2026)
    data/rules_shelf.json         ← Telecom Act 2023 rules shelf (41, carried in page state)
    data/short_titles.json        ← deterministic display titles for the ledger
    data/row_lines.json           ← one factual line per instrument, shown under the title
    data/folds.json               ← announcements folded into the instrument they announce
    data/signals.json             ← unpublished-instrument signals (3)
    code/audit_routine.py         ← asserts the ledger agrees with the classification regex
    extractor/                    ← Lane A: tracker.py + cron instructions (0% LLM)
    runbook/cloud-runbook.md      ← Lane B: exact steps every scheduled run follows
    memos/memo_template.docx      ← fixed client-alert template
    memos/2026-08-10_TRAI_1601-series_client-alert_SAMPLE.docx  ← real sample (TRAI 1601 direction)
    dashboard/tmt-radar.html      ← offline copy of the dashboard (live copy = artifact URL above)

## Operations

- **Scheduled**: two cloud schedules are active — “TMT Radar — weekday sweep” (every 2 hrs,
  08:00–20:00 IST, Mon–Fri) and “TMT Radar — weekend sweep” (09:00 IST Sat–Sun). Push + email
  alerts fire only on new substantive items or a source failure.
- **On demand**: ask Claude “run a TMT Radar sweep now”.
- **Instrument PDFs**: the cloud cannot mirror binaries from gov.in; run
  `python3 extractor/tracker.py fetch-pdfs` on a firm machine to build a local archive in
  `instruments/`. Memos always link the official PDF.
- **Add the next stratum**: in `registry/sources.json` flip the chosen sources from
  `planned` to `live`. Tech & data note: MeitY is a JS SPA (needs headless browsing or
  sitemap-diffing — plan in registry quirks); CERT-In and UIDAI are trivial. Media note: MIB is
  clean Drupal — easiest next stratum mechanically.
- **Watches**: DPB (no website yet) and OGAI (dev portal down) are weekly DNS watches — the
  tracker alerts when either regulator stands up a real site.

*Internal working tool. Every instrument must be verified against the gazette text before
client advice. Nothing here is legal advice.*
