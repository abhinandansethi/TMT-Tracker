"""Source drivers: venues whose content cannot be reached by fetching a URL.

Most sources are a GET away. Two are not, and both matter enough to be worth the code.

  egazette_search  The Gazette of India is the legally authoritative venue and the only
                   one where the right to reproduce comes from statute (Copyright Act
                   s.52(1)(q)(i)) rather than a site's goodwill. Reaching its ministry
                   search needs an ASP.NET session token carried in the URL path, a
                   Referer header (without which every deep page bounces to error.aspx),
                   and an ImageButton postback submitted with .x/.y coordinates.

  tdsat_post       TDSAT's judgments and daily orders are POST-only. s.52(1)(q)(iv)
                   expressly permits reproducing judgments and orders of a tribunal, which
                   makes them the cleanest category in the whole registry.

Both are deliberately kept in one place: they are the fragile parts of the system, and
when a site changes they should break here rather than in the generic pipeline.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Dict, List, Optional

import requests
import urllib3
from bs4 import BeautifulSoup

from .fetch import UA, FetchError, POLITENESS, fetch_log
from .robots import check as robots_check

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TIMEOUT = 60
Row = Dict[str, object]

MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _session(source_id: str) -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    s.verify = False
    return s


def _log(source_id: str, url: str) -> None:
    robots_check(url)
    fetch_log.append((source_id, url.split("/", 3)[2], url))


# ------------------------------------------------------------------ e-Gazette
def egazette_search(source: dict, today: date) -> List[Row]:
    """Ministry-and-month search against the Gazette of India.

    Returns one row per gazette entry, carrying the official Gazette ID. That ID is the
    citation: it is permanent and is what a lawyer would actually cite, whereas the URL
    here is a session-scoped path that expires."""
    sid = source["id"]
    cfg = source["parser"]
    ministry = str(cfg["ministry_id"])
    months = int(cfg.get("months_back", 2))

    s = _session(sid)
    _log(sid, "https://egazette.gov.in/")
    home = s.get("https://egazette.gov.in/", timeout=TIMEOUT)
    home.raise_for_status()
    m = re.search(r"\(S\([a-z0-9]+\)\)", home.url)
    if not m:
        raise FetchError("e-Gazette did not issue a session token")
    base = f"https://egazette.gov.in/{m.group(0)}"
    search_url = f"{base}/SearchMinistry.aspx"

    rows: List[Row] = []
    seen_ids = set()
    # walk back a couple of months so an instrument published near a month boundary is
    # not missed on the first of the month
    probe = today.replace(day=1)
    for _ in range(months):
        _log(sid, search_url)
        page = s.get(search_url, headers={"Referer": home.url}, timeout=TIMEOUT)
        page.raise_for_status()
        form = {i.get("name"): (i.get("value") or "")
                for i in BeautifulSoup(page.text, "lxml").find_all("input") if i.get("name")}
        data = {k: v for k, v in form.items() if k.startswith("__")}
        data.update({
            "ddlMinistry": ministry,
            "ddlmonth": str(probe.month),
            "ddlyear": str(probe.year),
            "rdb_Option": "0",                      # month/year wise
            "ImgSubmitDetails.x": "12",             # ASP.NET ImageButton needs coordinates
            "ImgSubmitDetails.y": "10",
        })
        _log(sid, search_url)
        res = s.post(search_url, data=data, headers={"Referer": search_url}, timeout=TIMEOUT)
        res.raise_for_status()
        rows.extend(_parse_gazette_grid(res.text, source, seen_ids))
        probe = (probe - timedelta(days=1)).replace(day=1)
    return rows


def _parse_gazette_grid(html: str, source: dict, seen_ids: set) -> List[Row]:
    soup = BeautifulSoup(html, "lxml")
    grid = soup.find("table", {"id": "gvGazetteList"})
    if grid is None:
        return []
    out: List[Row] = []
    for tr in grid.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 11:
            continue
        cell = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        gid = cell[9]
        if not gid or gid in seen_ids:
            continue
        seen_ids.add(gid)
        issue = _gaz_date(cell[7]) or _gaz_date(cell[8])
        subject, dept, office = cell[4], cell[2], cell[3]
        # The Gazette packs a structured annotation into the subject cell:
        #   "<instrument name>. ( New Gazette, Impacted Rule : ..., Applicable Section/Rule :
        #    ..., Date of Applicability : DD/MM/YYYY, Impact : Actionable ) (Department)"
        # The instrument name is the title; the annotation carries the effective date and an
        # Impact flag that a partner actually wants. Split them, so the title stays inside the
        # gate and the useful metadata is kept rather than discarded.
        title, ann = _split_gazette_subject(subject)
        if not title:
            title = f"{dept} gazette notification" if dept else "Gazette notification"
        extra = {"gazette_id": gid, "ministry": cell[1], "department": dept,
                 "office": office, "category": cell[5], "part_section": cell[6],
                 "publish_date": _gaz_date(cell[8])}
        extra.update(ann)
        out.append({"date": issue, "title": title[:280],
                    "url": source["url"], "extra": extra})
    return out


_GAZ_FIELDS = ("Impacted Rule", "Applicable Section/Rule", "Date of Applicability",
               "Impact", "New Gazette", "Amended by", "Amendment")


def _split_gazette_subject(subject: str):
    """Return (clean_instrument_name, {annotation fields}). The e-Gazette subject cell is
    "<name>. ( New Gazette, Impacted Rule : ..., Date of Applicability : DD/MM/YYYY, Impact
    : ... ) (Dept)". Field VALUES themselves contain commas and parentheses (a section
    citation like "Sub-section (2) of section 56 of the ... Act, 2023"), so each value runs
    up to the next known field label, not the next comma."""
    ann = {}
    m = re.search(r"\s*\(\s*(?:New Gazette|Amendment|Impacted Rule)\b", subject)
    title = subject[:m.start()].strip().rstrip(".") if m else subject.strip().rstrip(".")
    block = subject[m.start():] if m else ""
    nextfield = r"(?=\s*,?\s*(?:" + "|".join(re.escape(f) for f in _GAZ_FIELDS) + r")\s*:|\s*\)\s*(?:\(|$))"
    for key, field in (("Date of Applicability", "effective_date"),
                       ("Applicable Section/Rule", "applicable_rule"),
                       ("Impacted Rule", "impacted_rule"),
                       ("Impact", "impact")):
        mm = re.search(re.escape(key) + r"\s*:\s*(.+?)" + nextfield, block, re.S)
        if mm:
            val = re.sub(r"\s+", " ", mm.group(1)).strip()
            # the grid interleaves bare labels ("New Gazette", "Amendment"); strip trailing
            # punctuation, then a trailing label, then any punctuation it leaves behind
            for _ in range(3):
                val = re.sub(r"[,\s]+$", "", val)
                val = re.sub(r",?\s*(?:New Gazette|Amendment|Amended by)\s*$", "", val, flags=re.I)
            val = val.strip()
            if field == "effective_date":
                dm = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", val)
                val = f"{dm.group(3)}-{int(dm.group(2)):02d}-{int(dm.group(1)):02d}" if dm else None
            if val:
                ann[field] = val[:160]
    return title, ann


def _gaz_date(text: str) -> Optional[str]:
    m = re.search(r"(\d{1,2})-([A-Z][a-z]{2})-(\d{4})", text or "")
    if not m or m.group(2) not in MONTHS:
        return None
    try:
        return date(int(m.group(3)), MONTHS[m.group(2)], int(m.group(1))).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------- TDSAT
def tdsat_post(source: dict, today: date) -> List[Row]:
    """TDSAT judgments and daily orders, retrieved by a plain urlencoded POST over a date
    window. No session, cookie or CSRF token is involved."""
    sid = source["id"]
    cfg = source["parser"]
    url = source["url"]
    days = int(cfg.get("window_days", 30))
    start = today - timedelta(days=days)

    s = _session(sid)
    payload = dict(cfg.get("post_fields") or {})
    payload[cfg.get("from_field", "from_date1")] = start.strftime("%d/%m/%Y")
    payload[cfg.get("to_field", "to_date1")] = today.strftime("%d/%m/%Y")

    _log(sid, url)
    r = s.post(url, data=payload, timeout=TIMEOUT)
    r.raise_for_status()

    soup = BeautifulSoup(r.text, "lxml")
    rows: List[Row] = []
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue
        cell = [" ".join(td.get_text(" ", strip=True).split()) for td in tds]
        d = _dmy(cell[3]) or _dmy(" ".join(cell))
        if not d:
            continue
        case, party = cell[1], cell[2]
        if not case and not party:
            continue
        # item links are javascript handlers carrying a base64 filing number
        token = None
        for a in tr.find_all("a"):
            mm = re.search(r"\('([A-Za-z0-9+/=]{6,})'\)", a.get("href", "") or a.get("onclick", "") or "")
            if mm:
                token = mm.group(1)
                break
        view = cfg.get("view_url")
        link = f"{view}?filing_no={token}" if (view and token) else url
        rows.append({
            "date": d,
            "title": f"{case} — {party}" if case and party else (case or party),
            "url": link,
            "extra": {"case_no": case, "parties": party, "filing_token": token},
        })
    return rows


def _dmy(text: str) -> Optional[str]:
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{4})\b", text or "")
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1))).isoformat()
    except ValueError:
        return None


DRIVERS = {"egazette_search": egazette_search, "tdsat_post": tdsat_post}
