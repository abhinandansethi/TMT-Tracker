# engine - Lane A, the local detection engine (0% LLM)

Registry-driven, deterministic, fixture-tested. This is the primary detection lane: the firm
machine reaches every gov.in venue directly, so launchd runs a full sweep of all 43 live sources
every 2 hours. No language model is involved at any point.

Design invariants live in `radar/__init__.py`. In one line each: no silent failure, no guessing,
loud drift, tripwires, append-only.

## Architecture

    launchd (StartInterval 7200, RunAtLoad)
       |
       v
    run_sweep.sh ---> tracker.py sweep ---> tracker.py export ---> ../data/items.json
       |                    |
       |                    +--> health.json      (per-source status, notes, info)
       |                    +--> ledger.db        (canonical SQLite)
       |                    +--> ledger.jsonl     (append-only mirror)
       |
       +--> osascript notification on non-zero exit, or on new substantive items

    per source, config from registry_v2.json:

    fetch.get      ->  parse.parse    ->  floor      ->  tripwires   ->  validate.gate
    browser UA         adapter by         parsed <       sequence,       date / domain /
    2 retries          strategy           row_floor      monotonic,      title length
    politeness 1.2s                       = FAILED,      staleness,      pass -> on
    TLS downgrade                         exits 1        drift,          fail -> quarantine
    once, loudly                                         fingerprint     with a reason

                   ->  dedupe          ->  classify        ->  ledger.insert
                       seq key,            routine regex,      items table + JSONL
                       norm title+date,    type rules,         cross-listings ->
                       canonical pdf url   deadline regex      sightings table

`radar/` modules: `fetch` (HTTP), `parse` (adapters plus the generic fallback and the structure
fingerprint), `validate` (gates), `classify` (routine, type, deadline), `tripwires`, `ledger`
(SQLite plus JSONL, identity indexes), `core` (the `Sweeper`, one source at a time), `dates`
(format-declared date extraction only).

## Run it

Interpreter is the venv the launchd job uses:

    engine/.venv/bin/python engine/tracker.py <cmd>

Dependencies: `requests`, `urllib3`, `beautifulsoup4`, `lxml`, `feedparser`. Nothing else, and
stdlib `sqlite3` for the ledger.

## Commands and exit codes

| Command | What | Exit |
|---|---|---|
| `selftest` | Parse every live source's saved fixture offline. Floors must hold and at least 70% of parsed rows must pass the gates (unless the source declares `undated_ok`). A missing fixture is a failure. | 0 pass, 1 any breach |
| `sweep [--source ID] [--stratum S]` | Page 1 of every live source, full pipeline. | 0 if no source FAILED, 1 if any did |
| `backfill --since YYYY-MM-DD [--source ID] [--stratum S]` | Walks pagination back to the date. Floors are not enforced during backfill. | as sweep |
| `audit` | Every engine-classified row's routine flag must equal what the current regex produces. Curated v1 baseline and duplicate rows are exempt. | 0 agree, 1 any disagreement |
| `import-baseline` | One-off, idempotent: seeds the ledger from `../data/items.json` so the first sweep does not re-announce history. | 0 |
| `export` | Rewrites `../data/items.json` in the dashboard contract. Curated fields (gist, hand-set types) are preserved verbatim; engine rows are appended. | 0 |
| `health` | Prints the last `health.json`. | 0 |
| `fetch-pdfs` | Archives every ledgered PDF into `../instruments/`, skipping ones already there. | 0 all OK, 1 any download failed |

**WARN does not change the exit code.** A tripwire firing (sequence gap, shelf shrink, staleness,
drift, fingerprint change) is recorded in `health.json` and printed as a `WARN` line, but only a
FAILED source makes the process exit non-zero. Read `health.json` after a run, do not rely on the
notification alone.

## Adapter strategies

One strategy per site family, selected by `parser.strategy` in the registry:

| Strategy | Used for |
|---|---|
| `trai_views` | TRAI Drupal category pages: `ul.item-list > li` with `.title-number` and `.release-date`. |
| `trai_grid` | TRAI press-release grid: `.views-view-grid__item`, PR number read from the download aria-label or the PDF filename. |
| `html_table` | Generic table with configurable `row_selector`, `title_cell` or `title_selector`, `date_cell`, `type_cell`, `link_selector`. The workhorse: DoT eServices, NCCS, TEC, MIB, CBFC. |
| `tr_with_doc` | Malformed tables: any `<tr>` holding a document link plus a date, serial number and trailing date stripped from the title. |
| `link_shelf` | Undated document shelves where the doc links are the rows: TRAI standing directions, the eServices Act & Rules shelf, PRGI, ASCI. Title from aria-label, date from row text or a `Direction_DDMMYYYY.pdf` filename. |
| `regex_rows` | Markup a tree parser collapses (TDSAT never re-opens `<tr>`, CERT-In legacy servlets). Rows matched on raw bytes with a declared `row_regex` using named groups `href`, `title`, `title2`, `date_raw`, `seq`. |
| `rss` | Plain RSS or Atom via feedparser: PIB, TRAI sitewide. |
| `meity_api` | MeitY's public headless-WordPress JSON API (`/cms/wp-json/document/documents?type=...`). Fully machine-readable; the v1 `headless_required` constraint is obsolete. |
| `uidai_rsc` | UIDAI's Next.js App Router flight payload: plain GET with an `RSC: 1` header, documents ride in `"pdfDetails":{"data":[...]}`, unescaped and JSON-decoded. |
| `egazette_recent` | e-Gazette homepage Recent Extraordinary and Weekly panels, with a ministry `row_filter`. |

A strategy returns raw rows `{date?, title, url, page_url?, extra{}}` and validates nothing.
Validation belongs to `validate.py`, always.

## Adding a source

1. **Registry entry** in `registry_v2.json` under `sources`: `id`, `stratum`, `regulator`, `name`,
   `url`, `status: "live"`, `parser` (`strategy` plus its selectors and `row_floor`),
   `date_formats` (only the formats that site actually uses, so a stray number can never be read
   as a date), `allowed_domains`, `default_type`, and optionally `pagination`, `type_rules`,
   `type_map`, `tripwire`, `stale_after_days`, `row_filter`, `item_flags`, `http_headers`,
   `tolerant_tls`, `role: "crosscheck"`, and a `quirks` note recording what you learned the hard
   way.
2. **Fixture**: capture the live page into `fixtures/<id>.<ext>`, extension matching what the
   strategy consumes (`.html`, `.json` for `meity_api` and RSC payloads, `.xml` for feeds):

       curl -sL -A "Mozilla/5.0" "<url>" -o engine/fixtures/<id>.html
       curl -sL -A "Mozilla/5.0" -H "RSC: 1" "<url>" -o engine/fixtures/<id>.json   # UIDAI

3. **Set `row_floor`** below the row count a healthy page 1 actually shows, with a little slack,
   never above it. The floor is the difference between "this venue published nothing" and "our
   parser broke".
4. `tracker.py selftest` until the source parses above floor with its rows passing the gates.
5. `tracker.py sweep --source <id>`, then check `health.json` and the `quarantine` table for rows
   that were gated out for a bad reason.
6. `tracker.py backfill --since YYYY-MM-DD --source <id>` to fill the window.
7. Commit the registry entry, the adapter change if any, and the fixture together.

## Identity and dedupe

Three keys, checked in this order, so the same instrument seen on several venues stays one item:

1. **Sequence key** for numbered series: `"<year>:<n>"` per source, from the parser's `extra.seq`
   or a `No.NNofYYYY` in the URL. Numbers are identity for TRAI PRs and CERT-In series because
   their titles vary between renderings.
2. **Normalised title plus ISO date**: lowercased, every non-alphanumeric run collapsed to a
   space. Punctuation-insensitive on purpose, since the same instrument appears with smart
   quotes, dashes, case and spacing variants. The item id is `sha1(norm_title + date)[:10]`.
3. **Canonical PDF URL**: scheme, `www.`, trailing slash and case stripped, `%20` decoded. Only
   applied when the link is a PDF.

A hit on any key records a **sighting** (`item_id`, `source_id`, `url`) instead of inserting a
second item. The title and URL indexes are recomputed from stored rows at the start of each
sweep, so identity survives a change to the normalisation rules across engine versions. Nothing
is ever rewritten: an item is inserted once, only its `status` field distinguishes `baseline`,
`activation_baseline`, `backfill`, `new` and `duplicate`.

A source's first sweep ledgers everything visible as `activation_baseline` rather than announcing
it as new. Rows older than `window_start` are counted and skipped, not ledgered.

## Gates, quarantine, tripwires

Gates (`validate.gate`): title 8 to 300 chars after whitespace collapse; link on the source's
`allowed_domains`; a date that parses against the source's declared formats, is not more than 45
days in the future and not before 2000. Undated rows pass only where the source declares
`undated_ok` (document shelves). A failure writes `{at, source_id, reason, raw}` to the
`quarantine` table. Rows removed by a `row_filter` are out of scope by design and are not
quarantined.

Tripwires (`tripwires.py`): `floor` (below it means FAILED), `sequence` (a number missing from
both the page and the ledger is a missed instrument), `monotonic` (shelves only grow), `staleness`
(`stale_after_days`), `drift` (the generic date-plus-link fallback parser sees materially more
rows than the adapter), and the structure fingerprint (a hash of the first parsed region's tag
shape; a change is a WARN, never a silent pass).

## The fixture-refresh rule

**Whenever a site changes its markup, re-capture the fixture and rerun the selftest.** A drift or
fingerprint WARN, a floor breach, or a suddenly empty parse all mean the same thing: the saved
fixture no longer represents the live page, so the selftest is now testing history.

    1. re-capture:  curl ... -o engine/fixtures/<id>.html
    2. fix the adapter or the registry selectors until it parses
    3. engine/.venv/bin/python engine/tracker.py selftest      # must pass for ALL sources
    4. engine/.venv/bin/python engine/tracker.py sweep --source <id>
    5. commit the fixture and the fix in the same commit

Never hand-edit a fixture to make the selftest pass. A fixture is evidence of what the venue
served, and a doctored one turns the regression harness into decoration. `fixtures/` also holds
probe captures for venues that are not live sources (POST-form pages, API shells kept as
evidence); the selftest only looks for fixtures named after live source ids.
