"""Sweep orchestration. One source at a time, every outcome recorded, exit code honest.

Per-source pipeline:
    fetch -> parse (adapter) -> floor check -> tripwires -> gates -> classify -> ledger

Source result statuses:
    OK      fetched, parsed >= floor, no tripwire noise
    WARN    parsed fine but a tripwire fired (drift, staleness, sequence, monotonic, crosscheck)
    FAILED  fetch error or parse below floor — consecutive_failures increments
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

from . import classify as cl
from . import parse as ps
from . import tripwires as tw
from . import validate as vl
from .fetch import FetchError, get
from .ledger import Ledger, item_id, norm_title, now_ist


def _page_urls(source: dict, max_pages: int) -> List[str]:
    """Page-1-first URL list for backfill; sweep uses only the first entry."""
    pg = source.get("pagination") or {}
    base = source["url"]
    scheme = pg.get("scheme", "none")
    if scheme == "query_page":
        param, start = pg.get("param", "page"), pg.get("start", 1)
        sep = "&" if "?" in base else "?"
        urls = [base] if pg.get("first_unparameterised", True) else []
        n0 = start if urls else 0
        urls += [f"{base}{sep}{param}={n}" for n in range(n0, n0 + max_pages)]
        return urls[:max_pages]
    if scheme == "path_page":
        template, start = pg["template"], pg.get("start", 1)
        return [template.format(page=n) for n in range(start, start + max_pages)]
    return [base]


def _row_included(row: dict, source: dict) -> bool:
    """Deterministic include-filter (e.g. e-Gazette ministry match). A filtered row is
    out of scope by design — not quarantined."""
    flt = source.get("row_filter")
    if not flt:
        return True
    field = flt.get("field", "title")
    if field.startswith("extra."):
        val = str((row.get("extra") or {}).get(field[6:], ""))
    else:
        val = str(row.get(field, ""))
    return bool(re.search(flt["regex"], val, re.I))


class Sweeper:
    def __init__(self, registry: dict, ledger: Ledger, today: Optional[date] = None):
        self.registry = registry
        self.ledger = ledger
        self.today = today or date.today()
        self.routine_rx, self.deadline_rx = cl.make_classifier(registry)
        self.health: Dict[str, dict] = {}
        self.new_items: List[dict] = []
        # identity indexes recomputed from stored rows: survive norm-rule changes
        self._idx = ledger.norm_index()
        self._url_idx = ledger.url_index()

    # ------------------------------------------------------------------ one source
    def sweep_source(self, source: dict, backfill_since: Optional[str] = None) -> None:
        sid = source["id"]
        state = self.ledger.get_state(sid)
        notes: List[str] = []       # anomalies: a human should look
        info: List[str] = []        # expected behaviour, recorded for the audit trail
        status = "OK"
        rows_total, fresh = 0, 0
        try:
            max_pages = (source.get("pagination") or {}).get("max_pages", 30) if backfill_since else 1
            all_rows: List[dict] = []
            content = b""
            for page_url in _page_urls(source, max_pages):
                r = get(page_url, tolerant_tls=source.get("tolerant_tls", False))
                content = r.content if not all_rows else content
                rows = ps.parse(source, r.content, page_url)
                if not rows and all_rows:
                    break  # ran off the end during backfill
                all_rows.extend(rows)
                if backfill_since:
                    dated = [str(x["date"]) for x in rows if x.get("date")]
                    if dated and max(dated) < backfill_since:
                        break
                else:
                    break  # sweep = page 1 only
            rows_total = len(all_rows)

            floor_err = tw.check_floor(rows_total, source) if not backfill_since else None
            if floor_err:
                raise FetchError(floor_err)

            # tripwires (page-1 content only)
            n_generic = ps.generic_row_count(content, source, source["url"]) \
                if source["parser"]["strategy"] not in ("rss",) else 0
            for note in (tw.check_drift(rows_total, n_generic),
                         tw.check_staleness(all_rows, source, self.today),
                         tw.check_monotonic(rows_total, source, state.get("last_row_count"))):
                if note:
                    notes.append(note)
            known_seqs = self.ledger.seqs_for(sid, str(self.today.year)) \
                if source.get("tripwire", {}).get("kind") == "sequence" else set()
            seq_alerts, seq_state = tw.check_sequence(all_rows, source, state.get("seq_state", {}),
                                                     self.today, known_seqs)
            notes.extend(seq_alerts)

            fp = ps.structure_fingerprint(content, source) if content else None
            if fp and state.get("fingerprint") and fp != state["fingerprint"]:
                notes.append(f"structure fingerprint changed {state['fingerprint']} -> {fp}")

            first_run = not state.get("last_run")
            fresh, activated, pre_window = self._ingest(all_rows, source, backfill_since, first_run)
            if activated:
                info.append(f"first sweep: {activated} visible item(s) ledgered as activation_baseline")
            if pre_window:
                info.append(f"{pre_window} row(s) predate window_start — not ledgered")
            if source.get("role") == "crosscheck" and fresh and not backfill_since:
                notes.append(f"crosscheck caught {fresh} item(s) the primary sources missed")

            if notes:
                status = "WARN"
            self.ledger.set_state(sid, last_run=now_ist(), last_status=status,
                                  last_note="; ".join(notes)[:500] or None,
                                  consecutive_failures=0, last_row_count=rows_total,
                                  fingerprint=fp or state.get("fingerprint"),
                                  seq_state=seq_state)
        except (FetchError, Exception) as e:  # noqa: BLE001 — recorded, never swallowed
            status = "FAILED"
            fails = state.get("consecutive_failures", 0) + 1
            notes.append(f"{type(e).__name__}: {str(e)[:200]}")
            if fails >= 2:
                notes.append(f"SOURCE DOWN {fails} CONSECUTIVE RUNS")
            self.ledger.set_state(sid, last_run=now_ist(), last_status="FAILED",
                                  last_note="; ".join(notes)[:500],
                                  consecutive_failures=fails)
        self.health[sid] = {"status": status, "rows_seen": rows_total, "new": fresh,
                            "notes": notes, "info": info, "checked": now_ist(),
                            "consecutive_failures": self.ledger.get_state(sid).get("consecutive_failures", 0)}

    # ---------------------------------------------------------------------- ingest
    def _ingest(self, rows: List[dict], source: dict, backfill_since: Optional[str],
                first_run: bool = False):
        sid, fresh, activated, pre_window = source["id"], 0, 0, 0
        window_start = self.registry.get("window_start")
        crosscheck = source.get("role") == "crosscheck"
        # Lagging feeds and aggregators only act on recent rows: their old rows are
        # title-variant re-renderings of items the primary sources already ledgered.
        horizon = None
        if crosscheck and not backfill_since:
            from datetime import timedelta
            horizon = (self.today - timedelta(days=source.get("crosscheck_horizon_days", 14))).isoformat()
        for row in rows:
            if not _row_included(row, source):
                continue
            clean, reason = vl.gate(row, source, self.today)
            if clean is None:
                # undated shelf rows and crosscheck feeds quarantine quietly; primary
                # listings quarantine loudly via health notes
                self.ledger.quarantine(sid, reason, row)
                continue
            pib_filter = source.get("pib_ministry_filter")
            if pib_filter:
                url = str(clean.get("url") or "")
                if self.ledger.known_url(sid, url):
                    continue
                ok, page_date = self._pib_page_info(url, pib_filter, sid)
                if not ok:
                    continue
                if page_date and not clean.get("date"):
                    clean["date"] = page_date
            if backfill_since and clean.get("date") and str(clean["date"]) < backfill_since:
                continue
            if (not backfill_since and window_start and clean.get("date")
                    and str(clean["date"]) < window_start):
                pre_window += 1  # archive rows visible on page 1 stay out of the tracked window
                continue
            if horizon and (not clean.get("date") or str(clean["date"]) < horizon):
                continue  # crosscheck feeds only act inside their horizon
            seq_key = self._seq_key(clean, source)
            if seq_key:
                holder = self.ledger.known_seq(sid, seq_key)
                if holder:
                    self.ledger.sight(holder, sid, str(clean.get("url") or ""))
                    continue
            norm_key = (norm_title(str(clean["title"])),
                        str(clean["date"]) if clean.get("date") else "")
            url_key = self.ledger.canon_url(str(clean.get("url") or ""))
            holder = self._idx.get(norm_key) or \
                (self._url_idx.get(url_key) if url_key.split("?")[0].endswith(".pdf") else None)
            if holder:
                self.ledger.sight(holder, sid, str(clean.get("url") or ""))
                continue
            iid = item_id(str(clean["title"]), clean.get("date") and str(clean["date"]))
            title = str(clean["title"])
            flags = list(source.get("item_flags", []))
            if crosscheck:
                flags.append("caught_by_crosscheck")
            rec = {
                "id": iid,
                "date": clean.get("date") and str(clean["date"]),
                "title": title,
                "url": str(clean.get("url") or ""),
                "page_url": clean.get("page_url"),
                "source_id": sid,
                "regulator": source.get("regulator"),
                "stratum": source.get("stratum"),
                "type": cl.derive_type(title, source,
                                       (clean.get("extra") or {}).get("src_type")),
                "routine": cl.is_routine(title, self.routine_rx),
                "deadline": cl.extract_deadline(title, self.deadline_rx, source.get("date_formats", [])),
                "flags": flags,
                "seq": seq_key,
                "first_seen": now_ist(),
                "status": ("backfill" if backfill_since
                           else "activation_baseline" if first_run else "new"),
            }
            self.ledger.insert(rec)
            self._idx[norm_key] = iid
            if url_key:
                self._url_idx.setdefault(url_key, iid)
            if rec["status"] == "new":
                self.new_items.append(rec)
                fresh += 1
            else:
                activated += 1
        return fresh, activated, pre_window

    @staticmethod
    def _seq_key(clean: dict, source: dict) -> Optional[str]:
        """Identity key for numbered series: 'YYYY:NN'. Number from the parser's extra.seq
        or from the URL (PR_No.NNofYYYY)."""
        if not source.get("tripwire") or source["tripwire"].get("kind") != "sequence":
            return None
        seq = (clean.get("extra") or {}).get("seq")
        url = str(clean.get("url") or "")
        m = re.search(r"No\.?_?\s*(\d+)\s*of\s*(\d{4})", url, re.I)
        year = m.group(2) if m else (str(clean.get("date"))[:4] if clean.get("date") else None)
        if seq is None and m:
            seq = int(m.group(1))
        if seq is None or year is None:
            return None
        return f"{year}:{int(seq)}"

    # ---------------------------------------------------- PIB page enrichment
    _pib_cache: Dict[str, tuple] = {}
    _PIB_DATE = re.compile(r"Posted On:\s*(\d{1,2})\s+([A-Z]{3})[A-Za-z]*\s+(\d{4})", re.I)
    _MON = {m: i for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

    def _pib_page_info(self, url: str, ministries: List[str], sid: str):
        """PIB's only feed is all-ministries and dateless; both facts live solely on the
        release page. Deterministic: fetch once, substring-match the ministry list and
        regex the Posted-On date. An unreachable page quarantines the row — never a
        silent drop."""
        if url in self._pib_cache:
            return self._pib_cache[url]
        ok, page_date = False, None
        try:
            text = get(url).text
            ok = any(m.lower() in text.lower() for m in ministries)
            m = self._PIB_DATE.search(text)
            if m and m.group(2).lower()[:3] in self._MON:
                from datetime import date as _d
                try:
                    page_date = _d(int(m.group(3)), self._MON[m.group(2).lower()[:3]],
                                   int(m.group(1))).isoformat()
                except ValueError:
                    page_date = None
        except FetchError as e:
            self.ledger.quarantine(sid, f"PIB release page unreachable, cannot confirm ministry: {e}",
                                   {"url": url})
        self._pib_cache[url] = (ok, page_date)
        return ok, page_date

    # ----------------------------------------------------------------------- run
    def run(self, only: Optional[str] = None, stratum: Optional[str] = None,
            backfill_since: Optional[str] = None) -> int:
        live = [s for s in self.registry["sources"] if s.get("status") == "live"]
        for s in live:
            if only and s["id"] != only:
                continue
            if stratum and s.get("stratum") != stratum:
                continue
            self.sweep_source(s, backfill_since)
        failed = [k for k, h in self.health.items() if h["status"] == "FAILED"]
        warned = [k for k, h in self.health.items() if h["status"] == "WARN"]
        substantive = [i for i in self.new_items if not i["routine"]]
        print(f"sweep: {len(self.health)} sources | {len(self.new_items)} new "
              f"({len(substantive)} substantive) | {len(failed)} FAILED, {len(warned)} WARN")
        for i in substantive:
            print(f"  NEW   {i['date']}  {i['regulator']:<8} {i['title'][:100]}")
        for k in warned:
            print(f"  WARN  {k}: {'; '.join(self.health[k]['notes'])[:180]}")
        for k in failed:
            print(f"  FAIL  {k}: {'; '.join(self.health[k]['notes'])[:180]}")
        return 1 if failed else 0

    def write_health(self, path: Path) -> None:
        path.write_text(json.dumps(
            {"generated": now_ist(), "sources": self.health}, indent=1, ensure_ascii=False))
