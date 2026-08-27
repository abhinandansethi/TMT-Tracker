# Client-alert pipeline

The downstream step that turns the tracker's feed into client-ready drafts:

```
sources → engine → feed (data/items.json or the connector API) → [pipeline] → client drafts
```

For every new instrument or judgment, it decides **which clients care** and drafts an **alert
email a partner reviews and sends**. Everything is deterministic and local — no client data
leaves the machine, and nothing is ever sent.

## Run

```bash
python3 pipeline/pipeline.py --since 2026-06-01
```

- reads `data/items.json` by default; add `--api` to pull the live connector instead
  (`GET /v1/digest` + `/v1/judgments`, see `docs/CONNECTOR.md`).
- writes one `pipeline/drafts/<client>.md` per client with matches, plus `drafts/index.html`
  to review them all in a browser.

## How matching works

`pipeline/clients.json` is the roster. Each client declares what it watches:

```json
{ "id": "roblox", "name": "Roblox", "sector": "…",
  "watch": { "regulators": ["MeitY","CERT-In","MIB","CCPA","DPIIT","e-Gazette"],
             "strata": ["tech_data","media"],
             "keywords": ["online.?gaming","dark.?pattern","\\bDPDP\\b","children","intermediar", …] } }
```

An item reaches a client only when a **subject keyword matches** — the client is shown a
development because of what it is *about*, never merely because it came from a regulator it
follows (that alone would dump every tariff order and cable-TV petition on it). The watched
regulator then enriches the "why this is on your radar" line. Keywords are precise,
multi-word regex terms to keep false positives low; tuning them per client is the firm's job.

Swap `clients.json` for the firm's real roster (or generate it from the CRM). Client names and
match rules stay on the firm machine.

## The drafting step

The draft is **template fill** — the same discipline the memos use: heading, what-it-does line,
effective date, the official document link, and the citation, under a bold **DRAFT — verify
against the official text before advising; not sent** banner. A firm that wants a partner-voiced
narrative in its own house style plugs its model in at the one marked point in `pipeline.py`
(`draft_markdown`), over the identical matched facts — the matching and citations stay
deterministic and auditable either way.

## The boundary

This pipeline is the reference for *your* side of `docs/CONNECTOR.md`. The tracker guarantees
the feed is complete and every item carries its document link and citation; the pipeline decides
relevance and drafts. A partner still reads every draft and verifies each instrument against the
gazette text before it reaches a client.
