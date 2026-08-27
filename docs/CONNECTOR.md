# Connecting your pipeline to TMT Radar

Your side runs a pipeline: pull new instruments from here, match each against your client
list and context, and draft a client-alert email for the partner to review. This document is
the contract for the boundary between us. Nothing here reaches your clients — it emits the raw
material your workflow turns into a draft.

There are three ways to consume the data, in increasing order of directness. A working reference of the whole downstream — match the feed to a client roster and draft alert emails — ships in `pipeline/` (`python3 pipeline/pipeline.py`); adapt it or read it as the contract.

There are three ways to consume the data, in increasing order of directness.

## 1. The queryable API (recommended)

Run it next to the engine:

```bash
engine/.venv/bin/python engine/radar_api.py          # http://127.0.0.1:8788, localhost only
```

It reads the same ledger the dashboard shows, reloaded on every request, so it is always as
fresh as the last sweep. All endpoints are `GET`, all return JSON, and the feed is read-only.

| Endpoint | Purpose |
|---|---|
| `GET /v1/health` | liveness, last-sweep time, staleness, counts |
| `GET /v1/sources` | the coverage list: every live source, its footing and freshness |
| `GET /v1/instruments?…` | binding instruments, filterable |
| `GET /v1/judgments?…` | tribunal and court decisions, same filters |
| `GET /v1/signals?…` | non-binding leads: security bulletins, court diaries, unpublished-instrument signals |
| `GET /v1/items/{id}` | one item, full payload |
| `GET /v1/digest?since=YYYY-MM-DD` | everything new since a date, grouped by regulator |
| `GET /v1/openapi.json` | the machine-readable contract |

Filters on the list endpoints: `regulator`, `stratum` (telecom / tech_data / media), `type`,
`since`, `until`, `has_deadline=1`, `q` (free text), `limit`.

Your daily job is usually one call:

```
GET /v1/digest?since=2026-08-20
```

## 2. The static feed in the repo / on the site

If you would rather pull from git or the published page than run the API, the same data is in
`data/items.json` (committed on every sweep) and embedded in the dashboard HTML under
`<script id="tracker-data">`. The `items[]` array carries the same fields as the API payload
(minus the derived `summary`/`citation`, which you can rebuild or take from the API).

## 3. Drive the whole thing from a routine

Point your scheduler at `engine/run_sweep.sh` (sweep, export, rebuild), then read the feed.

## The item payload — what a draft email needs

Every instrument and judgment comes back in this shape:

```json
{
  "id": "09a96cbe62",
  "title": "Notification of Telecommunications (User Identification) Rules, 2026",
  "short_title": "Telecommunications (User Identification) Rules, 2026",
  "summary": "In force 21 Aug 2026 · amends s.56, Telecommunications Act 2023 · action required",
  "regulator": "e-Gazette",
  "type": "rules",
  "lane": "instruments",
  "stratum": "telecom",
  "date": "2026-08-21",
  "effective_date": "2026-08-21",
  "deadline": null,
  "impact": "Actionable",
  "amends": "Sub-section (2) of section 56 of the Telecommunications Act, 2023 (44 of 2023)",
  "gazette_id": "CG-DL-E-21082026-275657",
  "part_section": "Part II-Section 3-Sub-Section (i)",
  "parties": null,
  "document_url": "https://…",
  "source_page_url": "https://…",
  "citation": "Source: e-Gazette, Notification of …, 21 Aug 2026. Retrieved from …"
}
```

- **`short_title`** and **`summary`** are the two lines a partner scans: what it is, and why it
  matters now. Both are deterministic — no model produced them.
- **`document_url`** opens the instrument; **`source_page_url`** is the official listing it was
  published on. A gazette entry's **`document_url`** is the stable e-Gazette PDF
  (`egazette.gov.in/WriteReadData/<year>/<n>.pdf`, built from the Gazette ID); its permanent
  citation is the **`gazette_id`**, carried alongside.
- **`effective_date`** / **`deadline`** are the dates your workflow keys client obligations off.
- **`citation`** is a ready-to-paste provenance line.

## The two rules of the boundary

1. **The coverage list is the whole world.** `GET /v1/sources` is the complete set of places we
   fetch from. The pipeline must not infer instruments from anywhere not on that list — if it is
   not in the feed, it was not collected, by design.

2. **Every instrument must be verified against the gazette text before client advice.** The feed
   is a monitoring signal and a first draft's raw material, not a legal opinion. The draft email
   your pipeline produces is for a partner to check and send, never to auto-send.

## Enriching the feed with LLM briefs (optional)

The deterministic brief is composed from an instrument's *metadata* — it can say
"amends s.56 of the Telecommunications Act, 2023", but it can't say what the new s.56
actually *requires*, because that lives in the PDF body the engine never reads.

`pipeline/brief.py` reads that body. For each actionable instrument it fetches the
document, extracts the text, and asks Claude (Anthropic SDK, `claude-opus-5` by default)
for a short, factual, lawyer-facing brief — what the instrument does, plus a one-line
compliance implication and a confidence flag. Briefs cache to `pipeline/brief_cache.json`
keyed by item id; the dashboard build embeds them, and the Clients tab shows the LLM brief
(marked **AI brief · <confidence> · verify**) in place of the metadata brief.

```bash
python3 pipeline/brief.py --dry-run --limit 3   # fetch + extract + show the prompt, no API call, no credentials
python3 pipeline/brief.py                        # brief the actionable instruments (needs credentials)
engine/.venv/bin/python code/build_dashboard_v2.py   # rebuild so the dashboard embeds them
```

It is **additive and server-side only**: the published dashboard can't call an LLM, so
briefs are baked in here. If credentials are absent, a document can't be fetched, or the
model declines, that item simply keeps its deterministic brief — nothing breaks. Credentials
resolve the standard SDK way (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, or an `ant auth
login` profile); no key is stored. Set `TMT_BRIEF_MODEL=claude-sonnet-5` (or `-haiku-4-5`)
to run a large batch cheaper. The brief is a monitoring aid — it stays under the same
verify-against-the-official-text rule (boundary rule 2); it is never legal advice.

## Wiring the dashboard "Update now" button to your pipeline

The published dashboard cannot fetch government sites from its sandbox, so its **Update now**
button, by default, opens the local operator console. To make it trigger your pipeline instead,
set a global before the page's script runs (e.g. via your embedding wrapper):

```html
<script>window.TMT_CONFIG = { pipelineEndpoint: "https://your-system/hooks/tmt-refresh" };</script>
```

The button then `POST`s `{ "action": "sweep", "source": "tmt-radar-dashboard" }` to that
endpoint. Your pipeline runs the sweep and republishes; the page reloads to the new data.

## Making every document open in-browser (`docProxy`)

A few government servers send PDFs with a forced-download header (`Content-Disposition:
attachment`), and one (TEC MTCTE) also mislabels them `application/octet-stream`, which no
hosted viewer can preview. On the public preview those download. When you serve the dashboard
through your own infrastructure alongside the connector, point it at the connector's document
proxy and **every** document — MTCTE included — opens in the tab instead of downloading:

```html
<script>window.TMT_CONFIG = { docProxy: "https://your-host/connector" };</script>
```

`GET {docProxy}/v1/doc?u=<doc url from the feed>` fetches the file server-side and re-serves it
`application/pdf; inline`. It only proxies URLs that already appear in the feed (SSRF guard).

## MCP

If your workflow speaks MCP, wrap the API: each endpoint above maps to one read-only tool
(`list_instruments`, `get_digest`, `get_item`, `list_sources`). The API is stateless and
localhost, so a thin MCP shim over `http://127.0.0.1:8788` is all it takes.
