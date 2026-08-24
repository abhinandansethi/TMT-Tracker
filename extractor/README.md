# Lane A — local extractor (0% LLM)

Deterministic pipeline: fetch → parse (fixed selectors + regex) → validate → dedupe → tier
(rule table) → append `ledger.jsonl` → write `health.json`. No language model anywhere.

Run it on any firm machine with normal internet access. The cloud assistant's sandbox cannot
reach gov.in domains, so this lane is the fully-deterministic complement to the cloud sweep
(Lane B), which uses the assistant only as a fetch-and-copy transport with mechanical validation.

## Setup (once)

    cd "TMT Tracker/extractor"
    pip3 install -r requirements.txt
    python3 tracker.py sweep          # first run seeds the ledger

## Schedule

macOS (`crontab -e`):

    0 8-20/2 * * 1-5  cd "$HOME/Desktop/TMT Tracker/extractor" && /usr/bin/python3 tracker.py sweep >> sweep.log 2>&1
    0 9 * * 6,0       cd "$HOME/Desktop/TMT Tracker/extractor" && /usr/bin/python3 tracker.py sweep >> sweep.log 2>&1

The process exits non-zero when any source fails, so cron's mail/alerting fires — zero silent failures.

## Commands

    python3 tracker.py sweep                    # all live sources
    python3 tracker.py sweep --source trai_directions
    python3 tracker.py fetch-pdfs               # mirror instrument PDFs into ../instruments/
    python3 tracker.py health                   # last per-source status

## Notes

- The registry (`../registry/sources.json`) is the single source of truth — URLs, date formats,
  allowed domains, default tiers, quirks. Adding a stratum = flipping `status` to `live`.
- `parse_listing` is a generic row extractor (date + same-domain link per row). If a site
  changes markup, fix it here once; the registry documents each site's known quirks.
- Politeness: 1s delay between sources; browser User-Agent (some gov WAFs reject bare clients).
- `dot.gov.in`, `saralsanchar.gov.in` need a headless browser (JS shell / WAF) — Phase 2 here;
  covered meanwhile by eServices + gazette + signals lanes.
