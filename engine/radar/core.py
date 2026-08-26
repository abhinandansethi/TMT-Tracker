"""Sweep orchestration. One source at a time, every outcome recorded, exit code honest.

Per-source pipeline:
    fetch -> parse (adapter) -> floor check -> tripwires -> gates -> classify -> ledger

Source result statuses. "OK" is never a default: it has to be earned by evidence, because
a coverage page that shows green for a venue it is silently getting nothing from is worse
than one that shows red.

    OK      fetched, parsed >= floor, and rows survived the gates
    QUIET   fetched and parsed fine, but every row predates the tracked window. Provable,
            not assumed: the source records newest_visible, the date of the newest item
            actually on the page, so "nothing new" is a claim you can check.
    EMPTY   fetched, yet nothing survived parsing and gating. Never green: either the
            venue really is bare or the adapter has quietly stopped matching.
    WARN    a tripwire fired (drift, staleness, sequence, monotonic, crosscheck catch)
    FAILED  fetch error, undeclared host, or parse below floor
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
        urls += [f"{base}{sep}{param}={n}" for n in range(start, start + max_pages)]
        return urls[:max_pages]
    if scheme == "path_page":
        template, start = pg["template"], pg.get("start", 1)
        return [template.format(page=n) for n in range(start, start + max_pages)]
    return [base]


def _field(row: dict, field: str) -> str:
    if field.startswith("extra."):
        return str((row.get("extra") or {}).get(field[6:], ""))
    return str(row.get(field, ""))


def _row_included(row: dict, source: dict) -> bool:
    """Deterministic scope filters. A filtered row is out of subject scope by design, so it
    is counted and reported but never quarantined as if it were malformed.

    row_exclude matters for the Gazette: a ministry is not a subject. Ministry of
    Communications covers both telecommunications and the Department of Posts, and Post
    Office regulations are not TMT."""
    exc = source.get("row_exclude")
    if exc:
        blob = " ".join(_field(row, f) for f in exc.get("fields", ["title"]))
        if re.search(exc["regex"], blob, re.I):
            return False
    flt = source.get("row_filter")
    if not flt:
        return True
    return bool(re.search(flt["regex"], _field(row, flt.get("field", "title")), re.I))


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

            # Sources that cannot be reached by fetching a URL (session + postback flows)
            # run their own driver. Everything downstream is identical.
            driver = source["parser"].get("driver")
            if driver:
                from .drivers import DRIVERS
                all_rows = DRIVERS[driver](source, self.today)
                rows_total = len(all_rows)
                floor_err = tw.check_floor(rows_total, source)
                if floor_err:
                    raise FetchError(floor_err)
                n_generic = 0
                fp = None
                seen_dates = sorted(str(r["date"]) for r in all_rows if r.get("date"))
                newest_visible = seen_dates[-1] if seen_dates else None
                seq_state = state.get("seq_state", {})
                first_run = not state.get("last_run")
                ing = self._ingest(all_rows, source, backfill_since, first_run)
                fresh = ing["fresh"]
                if ing["activated"]:
                    info.append(f"first sweep: {ing['activated']} item(s) ledgered as activation_baseline")
                if ing["pre_window"]:
                    info.append(f"{ing['pre_window']} row(s) predate window_start")
                if ing["filtered"]:
                    info.append(f"{ing['filtered']} row(s) outside this source's subject filter")
                owned = self.ledger.count_for(sid) + self.ledger.sightings_for(sid)
                if rows_total == 0:
                    status = "EMPTY"
                    notes.append("driver returned no rows at all")
                elif owned == 0 and ing["filtered"] == rows_total:
                    status = "FILTERED"
                    info.append(f"all {rows_total} row(s) belong to other subjects")
                elif owned == 0:
                    status = "QUIET"
                    info.append(f"nothing in scope; newest seen {newest_visible or 'undated'}")
                if notes and status == "OK":
                    status = "WARN"
                self.ledger.set_state(sid, last_run=now_ist(), last_status=status,
                                      last_note="; ".join(notes)[:500] or None,
                                      consecutive_failures=0, last_row_count=rows_total,
                                      fingerprint=state.get("fingerprint"),
                                      seq_state=seq_state, newest_visible=newest_visible)
                st = self.ledger.get_state(sid)
                self.health[sid] = {"status": status, "rows_seen": rows_total, "new": fresh,
                                    "ledgered_total": self.ledger.count_for(sid),
                                    "newest_visible": st.get("newest_visible"),
                                    "notes": notes, "info": info, "checked": now_ist(),
                                    "consecutive_failures": st.get("consecutive_failures", 0)}
                return

            for page_url in _page_urls(source, max_pages):
                r = get(page_url, tolerant_tls=source.get("tolerant_tls", False),
                        extra_headers=source.get("http_headers"),
                        source_id=sid, allowed_domains=source.get("allowed_domains"))
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

            # proof of life: the newest thing this venue is actually showing right now
            seen_dates = sorted(str(r["date"]) for r in all_rows if r.get("date"))
            newest_visible = seen_dates[-1] if seen_dates else None

            first_run = not state.get("last_run")
            ing = self._ingest(all_rows, source, backfill_since, first_run)
            fresh = ing["fresh"]
            if ing["activated"]:
                info.append(f"first sweep: {ing['activated']} visible item(s) ledgered as activation_baseline")
            if ing["pre_window"]:
                info.append(f"{ing['pre_window']} row(s) predate window_start — not ledgered")
            if ing["filtered"]:
                info.append(f"{ing['filtered']} row(s) outside this source's subject filter")
            if source.get("role") == "crosscheck" and fresh and not backfill_since:
                notes.append(f"crosscheck caught {fresh} item(s) the primary sources missed")

            # Earn the status from evidence; never default to OK. "Contributed" means this
            # source has either ledgered an instrument or confirmed one another source found
            # (a sighting) — a cross-listing venue that only ever confirms is still working.
            owned = self.ledger.count_for(sid)
            sightings = self.ledger.sightings_for(sid)
            contributed = owned + sightings
            window_start = self.registry.get("window_start")
            in_window = bool(newest_visible and window_start and newest_visible >= window_start)

            if rows_total == 0:
                status = "EMPTY"
                notes.append("fetched, but the page yielded no rows at all")
            elif contributed == 0 and ing["filtered"] == rows_total and rows_total:
                status = "FILTERED"
                info.append(f"all {rows_total} row(s) belong to other subjects; nothing in scope yet")
            elif contributed == 0 and not in_window:
                status = "QUIET"
                info.append(f"nothing inside the tracked window; newest item this venue shows "
                            f"is {newest_visible or 'undated'}")
            elif contributed == 0:
                status = "EMPTY"
                notes.append(f"parsed {rows_total} row(s), newest {newest_visible}, which is inside "
                             f"the window, yet nothing has ever reached the ledger — adapter or gates")
            if notes and status == "OK":
                status = "WARN"
            self.ledger.set_state(sid, last_run=now_ist(), last_status=status,
                                  last_note="; ".join(notes)[:500] or None,
                                  consecutive_failures=0, last_row_count=rows_total,
                                  fingerprint=fp or state.get("fingerprint"),
                                  seq_state=seq_state, newest_visible=newest_visible)
        except (FetchError, Exception) as e:  # noqa: BLE001 — recorded, never swallowed
            status = "FAILED"
            fails = state.get("consecutive_failures", 0) + 1
            notes.append(f"{type(e).__name__}: {str(e)[:200]}")
            if fails >= 2:
                notes.append(f"SOURCE DOWN {fails} CONSECUTIVE RUNS")
            self.ledger.set_state(sid, last_run=now_ist(), last_status="FAILED",
                                  last_note="; ".join(notes)[:500],
                                  consecutive_failures=fails)
        st = self.ledger.get_state(sid)
        self.health[sid] = {"status": status, "rows_seen": rows_total, "new": fresh,
                            "ledgered_total": self.ledger.count_for(sid),
                            "newest_visible": st.get("newest_visible"),
                            "notes": notes, "info": info, "checked": now_ist(),
                            "consecutive_failures": st.get("consecutive_failures", 0)}

    # ---------------------------------------------------------------------- ingest
    def _ingest(self, rows: List[dict], source: dict, backfill_since: Optional[str],
                first_run: bool = False):
        sid, fresh, activated, pre_window = source["id"], 0, 0, 0
        filtered = sighted = 0
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
                filtered += 1  # out of subject scope by design, e.g. another ministry
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
                ok, page_date = self._pib_page_info(url, pib_filter, sid,
                                                    source.get("allowed_domains"))
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
            seq_key = self._seq_key(clean, source)
            if seq_key:
                holder = self.ledger.known_seq(sid, seq_key)
                if holder:
                    self.ledger.sight(holder, sid, str(clean.get("url") or ""))
                    sighted += 1
                    continue
            norm_key = (norm_title(str(clean["title"])),
                        str(clean["date"]) if clean.get("date") else "")
            url_key = self.ledger.canon_url(str(clean.get("url") or ""))
            holder = self._idx.get(norm_key)
            if holder is None and url_key.split("?")[0].endswith(".pdf"):
                # only URLs specific enough to be an identity (see Ledger.url_index)
                holder = self._url_idx.get(url_key)
            if holder:
                self.ledger.sight(holder, sid, str(clean.get("url") or ""))
                sighted += 1
                continue
            # The crosscheck horizon exists to stop a lagging feed from minting stale items,
            # not to stop it confirming known ones — so it is applied only after the identity
            # lookup above has had its chance to record a sighting.
            if horizon and (not clean.get("date") or str(clean["date"]) < horizon):
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
                # Routine means recurring administrative output: drive tests, lab
                # designations, statistics releases. That is the signals definition, so it
                # belongs there rather than sitting in the instruments ledger behind a
                # toggle the reader has to know to distrust.
                "lane": "signals" if cl.is_routine(title, self.routine_rx)
                        else source.get("lane", "instruments"),
                "first_seen": now_ist(),
                "status": ("backfill" if backfill_since
                           else "activation_baseline" if first_run else "new"),
            }
            self.ledger.insert(rec)
            self._idx[norm_key] = iid
            from .ledger import is_generic_doc_url
            if url_key and not is_generic_doc_url(url_key):
                self._url_idx.setdefault(url_key, iid)
            if rec["status"] == "new":
                self.new_items.append(rec)
                fresh += 1
            else:
                activated += 1
        return {"fresh": fresh, "activated": activated, "pre_window": pre_window,
                "filtered": filtered, "sighted": sighted}

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

    def _pib_page_info(self, url: str, ministries: List[str], sid: str,
                       allowed_domains: Optional[List[str]] = None):
        """PIB's only feed is all-ministries and dateless; both facts live solely on the
        release page. Deterministic: fetch once, substring-match the ministry list and
        regex the Posted-On date. An unreachable page quarantines the row — never a
        silent drop."""
        if url in self._pib_cache:
            return self._pib_cache[url]
        ok, page_date = False, None
        try:
            text = get(url, source_id=sid, allowed_domains=allowed_domains).text
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
        by = lambda st: [k for k, h in self.health.items() if h["status"] == st]  # noqa: E731
        filtered_s = by("FILTERED")
        failed, empty, warned, quiet = by("FAILED"), by("EMPTY"), by("WARN"), by("QUIET")
        substantive = [i for i in self.new_items if not i["routine"]]
        print(f"sweep: {len(self.health)} sources | {len(self.new_items)} new "
              f"({len(substantive)} substantive) | {len(failed)} FAILED, {len(empty)} EMPTY, "
              f"{len(warned)} WARN, {len(quiet)} quiet")
        for i in substantive:
            print(f"  NEW   {i['date']}  {i['regulator']:<8} {i['title'][:100]}")
        for k in warned:
            print(f"  WARN  {k}: {'; '.join(self.health[k]['notes'])[:180]}")
        for k in empty:
            print(f"  EMPTY {k}: {'; '.join(self.health[k]['notes'])[:180]}")
        for k in failed:
            print(f"  FAIL  {k}: {'; '.join(self.health[k]['notes'])[:180]}")
        prov = self.provenance()
        if prov["undeclared"]:
            print(f"  PROVENANCE BREACH: {len(prov['undeclared'])} request(s) to undeclared hosts")
            for u in prov["undeclared"][:5]:
                print(f"    {u['source_id']} -> {u['host']}")
        # EMPTY is a failure of evidence, not a quiet week: exit non-zero so it is seen.
        return 1 if (failed or empty or prov["undeclared"]) else 0

    def provenance(self) -> dict:
        """Every host contacted this run, attributed to the source that caused it, checked
        against the registry. The coverage page tells a partner where the tracker looks;
        this is what makes that statement auditable instead of a claim."""
        from .fetch import fetch_log
        declared = set()
        for s in self.registry["sources"]:
            for d in s.get("allowed_domains", []):
                declared.add(d.lower())
        by_source: Dict[str, dict] = {}
        undeclared = []
        for sid, host, url in fetch_log:
            h = host.lower().split(":")[0]
            rec = by_source.setdefault(sid, {"hosts": set(), "requests": 0})
            rec["hosts"].add(h)
            rec["requests"] += 1
            if not any(h == d or h.endswith("." + d) for d in declared):
                undeclared.append({"source_id": sid, "host": h, "url": url})
        return {"requests": len(fetch_log),
                "by_source": {k: {"hosts": sorted(v["hosts"]), "requests": v["requests"]}
                              for k, v in sorted(by_source.items())},
                "hosts": sorted({h for v in by_source.values() for h in v["hosts"]}),
                "undeclared": undeclared}

    def write_health(self, path: Path) -> None:
        """Merge, never replace: a scoped sweep must not erase the other sources' health,
        which would read as 56 venues having silently disappeared."""
        merged = {}
        if path.exists():
            try:
                merged = json.loads(path.read_text()).get("sources", {})
            except (ValueError, OSError):
                merged = {}
        merged.update(self.health)
        path.write_text(json.dumps(
            {"generated": now_ist(), "sources": merged, "provenance": self.provenance()},
            indent=1, ensure_ascii=False))
