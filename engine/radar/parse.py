"""Per-family parsing strategies. Each strategy is small, explicit and fixture-tested.
A strategy returns raw rows: {date?, title, url, page_url?, extra{}} — validation happens
downstream in validate.py, never here.

Strategies:
    trai_views       TRAI Drupal category pages (.item-list li with .title-number)
    trai_grid        TRAI press-release grid (.views-view-grid__item)
    html_table       generic table: configurable row/title/link/date sub-selectors
    tr_with_doc      malformed-table sites (TDSAT): any <tr> holding a doc link + a date
    link_shelf       undated document shelves (eServices Act & Rules): the doc links ARE the rows
    rss              plain RSS/Atom feeds
    egazette_recent  e-Gazette homepage "Recent" panels with ministry filter
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional
from urllib.parse import urljoin

import warnings

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

from .dates import extract_date

warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

Row = Dict[str, object]

DOC_EXT = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip")

ARIA_TITLE = re.compile(r"Download (?:PDF|file) for (.+?)(?:\s*[-–]\s*\([^)]*\))?,?\s*opens in new tab\s*$", re.I)


def _aria_title(a) -> Optional[str]:
    m = ARIA_TITLE.search(a.get("aria-label", "") or "")
    return _clean(m.group(1)) if m else None


def _clean(t: str) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()


def _abs(base: str, href: str) -> str:
    return urljoin(base, (href or "").strip())


def _first_doc_link(el, base: str) -> Optional[str]:
    best = None
    for a in el.select("a[href]"):
        href = _abs(base, a["href"])
        if href.lower().split("?")[0].endswith(DOC_EXT):
            return href
        if best is None and not href.lower().startswith(("javascript:", "mailto:", "#")):
            best = href
    return best


# ---------------------------------------------------------------- trai_views
def parse_trai_views(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    rows: List[Row] = []
    for li in soup.select("ul.item-list > li"):
        title_el = li.select_one(".title-number .field-content")
        if title_el is None:
            continue
        date_el = li.select_one(".release-date .field-content")
        title = _clean(title_el.get_text(" "))
        date = extract_date(date_el.get_text(" ") if date_el else "", cfg["date_formats"])
        pdf = None
        for a in li.select("a[href]"):
            href = _abs(base, a["href"])
            if href.lower().split("?")[0].endswith(".pdf"):
                pdf = href
                break
        detail = li.select_one(".title-number a[href]")
        rows.append({"date": date, "title": title, "url": pdf or (detail and _abs(base, detail["href"])) or "",
                     "page_url": _abs(base, detail["href"]) if detail else None})
    return rows


# ---------------------------------------------------------------- trai_grid
def parse_trai_grid(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    rows: List[Row] = []
    for item in soup.select(".views-view-grid__item"):
        title_el = item.select_one(".views-field-title .field-content")
        if title_el is None:
            continue
        date_el = item.select_one(".views-field-field-date .field-content")
        title = _clean(title_el.get_text(" "))
        date = extract_date(date_el.get_text(" ") if date_el else "", cfg["date_formats"])
        pdf, seq = None, None
        a = item.select_one("a[href]")
        if a is not None:
            pdf = _abs(base, a["href"])
            label = a.get("aria-label", "") or ""
            m = re.search(r"Press Release No\.?\s*(\d+)", label) or \
                re.search(r"PR[._ ]?No[._ ]?(\d+)of(\d{4})", pdf)
            if m:
                seq = int(m.group(1))
        rows.append({"date": date, "title": title, "url": pdf or "", "extra": {"seq": seq}})
    return rows


# ---------------------------------------------------------------- html_table
def parse_html_table(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    p = cfg["parser"]
    rows: List[Row] = []
    for tr in soup.select(p.get("row_selector", "table tbody tr")):
        cells = tr.find_all(["td", "th"], recursive=False) or tr.find_all(["td", "th"])
        if not cells or tr.find("th") and not tr.find("td"):
            continue  # header row
        row_text = _clean(tr.get_text(" "))
        title = None
        if p.get("title_selector"):
            el = tr.select_one(p["title_selector"])
            title = _clean(el.get_text(" ")) if el else None
        if not title and p.get("title_cells") and len(cells) > max(p["title_cells"]):
            parts = [_clean(cells[i].get_text(" ")) for i in p["title_cells"]]
            title = " - ".join(x for x in parts if x)
        if not title and p.get("title_cell") is not None and len(cells) > p["title_cell"]:
            title = _clean(cells[p["title_cell"]].get_text(" "))
        if not title:
            continue
        date_text = row_text
        if p.get("date_cell") is not None and len(cells) > p["date_cell"]:
            date_text = _clean(cells[p["date_cell"]].get_text(" "))
        date = extract_date(date_text, cfg["date_formats"]) or extract_date(row_text, cfg["date_formats"])
        src_type = None
        if p.get("type_cell") is not None and len(cells) > p["type_cell"]:
            src_type = _clean(cells[p["type_cell"]].get_text(" "))
        link = None
        if p.get("link_selector"):
            el = tr.select_one(p["link_selector"])
            link = _abs(base, el["href"]) if el and el.has_attr("href") else None
        if link is None:
            link = _first_doc_link(tr, base)
        rows.append({"date": date, "title": title, "url": link or "",
                     "extra": {"src_type": src_type} if src_type else {}})
    return rows


# ---------------------------------------------------------------- tr_with_doc
def parse_tr_with_doc(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    rows: List[Row] = []
    seen = set()
    for tr in soup.find_all("tr"):
        doc = None
        for a in tr.find_all("a", href=True):
            href = _abs(base, a["href"])
            if href.lower().split("?")[0].endswith(DOC_EXT):
                doc = href
                break
        if doc is None:
            continue
        text = _clean(tr.get_text(" "))
        date = extract_date(text, cfg["date_formats"])
        # title = row text minus serial number and trailing date
        title = re.sub(r"^\d+\s+", "", text)
        title = re.sub(r"\s*\d{1,2}[-./]\d{1,2}[-./]\d{4}\s*$", "", title).strip()
        key = (title.lower(), date)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"date": date, "title": title, "url": doc})
    return rows


# ---------------------------------------------------------------- regex_rows
def parse_regex_rows(content: bytes, cfg: dict, base: str) -> List[Row]:
    """For malformed HTML that tree parsers collapse (TDSAT never re-opens <tr>).
    The source declares a row_regex with named groups href/title/date_raw; rows are
    matched on the raw bytes, deterministically."""
    html = content.decode("utf-8", errors="ignore")
    rx = re.compile(cfg["parser"]["row_regex"], re.S | re.I)
    rows: List[Row] = []
    for m in rx.finditer(html):
        g = m.groupdict()
        title = _clean(re.sub(r"<[^>]+>", " ", g.get("title") or ""))
        if g.get("title2"):
            t2 = _clean(re.sub(r"<[^>]+>", " ", g["title2"]))
            title = f"{title}: {t2}" if title else t2
        date = extract_date(_clean(g.get("date_raw") or ""), cfg["date_formats"]) or \
            extract_date(title, cfg["date_formats"])
        extra = {}
        if g.get("seq"):
            digits = re.findall(r"\d+", g["seq"])
            if digits:
                extra["seq"] = int(digits[-1])
        rows.append({"date": date, "title": title, "url": _abs(base, g.get("href") or ""),
                     "extra": extra})
    return rows


DDMMYYYY = re.compile(r"[-_](\d{2})(\d{2})(\d{4})(?:_\d+)?\.pdf$", re.I)


def _date_from_href(href: str, cfg: dict) -> Optional[str]:
    """TRAI PDF filenames embed the date (Regulation_DDMMYYYY.pdf) — a deterministic
    date for otherwise-undated shelf rows.

    A filename is not authority. TRAI ships real typos (Standing_Direction_03122028.pdf,
    uploaded in September 2024), so a date that has not happened yet is treated as no date
    rather than a wrong one: the instrument still gets tracked, just undated."""
    if not cfg["parser"].get("date_from_href"):
        return None
    m = DDMMYYYY.search(href.split("?")[0])
    if not m:
        return None
    d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    from datetime import date as _d, timedelta as _td
    try:
        got = _d(y, mo, d)
    except ValueError:
        return None
    if not (2000 <= y <= 2100) or got > _d.today() + _td(days=45):
        return None
    return got.isoformat()


# ---------------------------------------------------------------- link_shelf
def parse_link_shelf(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    p = cfg["parser"]
    scope = soup.select_one(p["scope_selector"]) if p.get("scope_selector") else soup
    if scope is None:
        return []
    rows: List[Row] = []
    seen = set()
    include = re.compile(p["href_include"]) if p.get("href_include") else None
    for a in scope.select("a[href]"):
        href = _abs(base, a["href"])
        if not href.lower().split("?")[0].endswith(DOC_EXT):
            continue
        if include and not include.search(href):
            continue
        title = _aria_title(a) or _clean(a.get_text(" "))
        if len(title) < 8 or re.match(r"(?i)^download", title):  # icon/size-only link text
            title = _aria_title(a) or _clean(a.parent.get_text(" "))[:300]
        # strip listing junk that rides along in shelf rows: trailing dates and file sizes
        title = re.sub(r"\s*\d{1,2}/\d{1,2}/\d{4}\s*", " ", title)
        title = re.sub(r"\s*\(?\d+(\.\d+)?\s*(KB|MB)\)?\s*$", "", title, flags=re.I).strip(" ,;:-")
        if href in seen:
            continue
        seen.add(href)
        # shelves are usually undated; a date may ride along in the row text or filename
        date = extract_date(_clean(a.parent.get_text(" ")), cfg["date_formats"]) or \
            _date_from_href(href, cfg)
        rows.append({"date": date, "title": title, "url": href, "extra": {"undated_shelf": date is None}})
    return rows


# ---------------------------------------------------------------- rss
def parse_rss(content: bytes, cfg: dict, base: str) -> List[Row]:
    import feedparser
    feed = feedparser.parse(content)
    rows: List[Row] = []
    for e in feed.entries:
        date = None
        for k in ("published_parsed", "updated_parsed"):
            t = e.get(k)
            if t:
                date = f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"
                break
        rows.append({"date": date, "title": _clean(e.get("title", "")),
                     "url": e.get("link", ""), "extra": {}})
    return rows


# ---------------------------------------------------------------- meity_api
def parse_meity_api(content: bytes, cfg: dict, base: str) -> List[Row]:
    """MeitY's Next.js frontend reads a public headless-WordPress API:
    /cms/wp-json/document/documents?type=...&limit=N&page=N&sort=acf&order=DESC
    Fully machine-readable JSON — the v1 'headless_required' constraint is obsolete."""
    import json as _json
    data = _json.loads(content.decode("utf-8", errors="ignore"))
    posts = data if isinstance(data, list) else data.get("posts", [])
    rows: List[Row] = []
    for post in posts:
        acf = post.get("acf_data") or {}
        title = _clean(acf.get("title") or post.get("post_title") or "")
        date = extract_date(str(acf.get("date") or ""), cfg["date_formats"]) or \
            extract_date(str(post.get("post_date") or ""), ["YYYY-MM-DD"])
        url = ""
        files = acf.get("file") or []
        if files and isinstance(files, list):
            f0 = files[0] or {}
            pdf = f0.get("pdf") or {}
            url = (pdf.get("url") if isinstance(pdf, dict) else "") or f0.get("external_link") or ""
        rows.append({"date": date, "title": title, "url": url,
                     "extra": {"category": acf.get("select_documents_type")}})
    return rows


# ---------------------------------------------------------------- uidai_rsc
def parse_uidai_rsc(content: bytes, cfg: dict, base: str) -> List[Row]:
    """UIDAI's Next.js App Router returns its React flight payload to a plain GET with
    an 'RSC: 1' header; the document list rides inside as "pdfDetails":{"data":[...]}.
    Deterministic: locate the marker, JSON-decode the array."""
    import json as _json
    txt = content.decode("utf-8", errors="ignore")
    marker = '"pdfDetails":{"data":'
    i = txt.find(marker)
    if i < 0:
        # some renders nest the payload as an escaped JSON string — unescape and retry
        txt = txt.replace('\\"', '"')
        i = txt.find(marker)
    if i < 0:
        return []
    docs, _ = _json.JSONDecoder().raw_decode(txt[i + len(marker):])
    rows: List[Row] = []
    for d in docs:
        title = _clean(str(d.get("title") or d.get("name") or ""))
        date = extract_date(str(d.get("updated_date") or ""), cfg["date_formats"]) or \
            extract_date(title, cfg["date_formats"])
        rows.append({"date": date, "title": title, "url": str(d.get("file_url") or ""),
                     "extra": {"category": d.get("type")}})
    return rows


# ---------------------------------------------------------------- psn_rows
def parse_psn_rows(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    """DoT eServices topic pages (RoW, satellite): div.psn-container rows inside a
    scoped view — a PDF anchor wrapping p.psnDate (badge + date span) and p.psn-name.
    Scope strictly: a sitewide What's-New ticker on every page mimics the row shape."""
    p = cfg["parser"]
    scope = soup.select_one(p["scope_selector"]) if p.get("scope_selector") else soup
    if scope is None:
        return []
    rows: List[Row] = []
    for box in scope.select(".psn-container"):
        a = box.select_one("a[href]")
        name = box.select_one("p.psn-name")
        date_el = box.select_one(".policy-circular-presentation")
        badge = box.select_one(".badge")
        if a is None or name is None:
            continue
        rows.append({
            "date": extract_date(date_el.get_text(" ") if date_el else "", cfg["date_formats"]),
            "title": _clean(name.get_text(" ")),
            "url": _abs(base, a["href"]),
            "extra": {"src_type": _clean(badge.get_text(" ")) if badge else None},
        })
    return rows


# ---------------------------------------------------------------- inspace_api
def parse_inspace_api(content: bytes, cfg: dict, base: str) -> List[Row]:
    """IN-SPACe's ServiceNow portal answers anonymous JSON GETs; the publications list
    is hardcoded as JS object literals inside a widget's client_script."""
    # the widget JS rides inside a JSON string, so its quotes arrive escaped
    txt = content.decode("utf-8", errors="ignore").replace('\\"', '"')
    rx = re.compile(r'\{\s*title:\s*"((?:[^"\\]|\\.)*)"\s*,\s*belowline:\s*"((?:[^"\\]|\\.)*)"'
                    r'\s*,\s*url:\s*"((?:[^"\\]|\\.)*)"\s*,\s*category:\s*"((?:[^"\\]|\\.)*)"')
    rows: List[Row] = []
    for m in rx.finditer(txt):
        title, below, url, category = (x.replace('\\"', '"').replace("\\/", "/") for x in m.groups())
        rows.append({"date": extract_date(below, cfg["date_formats"]),
                     "title": _clean(title), "url": _abs(base, url),
                     "extra": {"src_type": _clean(category)}})
    return rows


# ---------------------------------------------------------------- tec_er_api
def parse_tec_er_api(content: bytes, cfg: dict, base: str) -> List[Row]:
    """MTCTE portal's get_er_list: a flat JSON array where every 6 consecutive elements
    are one Essential Requirement record."""
    import json as _json
    data = _json.loads(content.decode("utf-8", errors="ignore"))
    rows: List[Row] = []
    for i in range(0, len(data) - 5, 6):
        product, er_num, _orig, start, _end, _status = (str(x or "") for x in data[i:i + 6])
        if not product:
            continue
        title = f"{product} ({er_num})" if er_num else product
        url = f"https://www.mtcte.tec.gov.in/filedownload?name={er_num}.pdf" if er_num else ""
        rows.append({"date": extract_date(start, cfg["date_formats"]),
                     "title": _clean(title), "url": url, "extra": {}})
    return rows


# ---------------------------------------------------------------- egazette_recent
def parse_egazette_recent(soup: BeautifulSoup, cfg: dict, base: str) -> List[Row]:
    rows: List[Row] = []
    for table in soup.find_all("table"):
        head = _clean(table.get_text(" "))[:80].lower()
        panel = "extraordinary" if "extra ordinary" in head or "extraordinary" in head else \
                ("weekly" if "weekly gazettes" in head else None)
        if panel is None:
            continue
        for tr in table.find_all("tr"):
            cells = [_clean(td.get_text(" ")) for td in tr.find_all("td")]
            if len(cells) < 3:
                continue
            text = " ".join(cells)
            date = extract_date(text, cfg["date_formats"])
            if not date:
                continue
            ministry, subject = cells[0], cells[1]
            gid = next((c for c in cells if re.match(r"^[A-Z]{2,4}-", c) or re.search(r"\d{6,}", c)), "")
            rows.append({"date": date, "title": f"{ministry}: {subject}".strip(": "),
                         "url": base, "extra": {"panel": panel, "ministry": ministry, "gazette_id": gid}})
    return rows


STRATEGIES = {
    "trai_views": parse_trai_views,
    "trai_grid": parse_trai_grid,
    "html_table": parse_html_table,
    "tr_with_doc": parse_tr_with_doc,
    "link_shelf": parse_link_shelf,
    "psn_rows": parse_psn_rows,
    "egazette_recent": parse_egazette_recent,
}


def parse(source: dict, content: bytes, base: str) -> List[Row]:
    strategy = source["parser"]["strategy"]
    if strategy == "rss":
        return parse_rss(content, source, base)
    if strategy == "regex_rows":
        return parse_regex_rows(content, source, base)
    if strategy == "meity_api":
        return parse_meity_api(content, source, base)
    if strategy == "uidai_rsc":
        return parse_uidai_rsc(content, source, base)
    if strategy == "inspace_api":
        return parse_inspace_api(content, source, base)
    if strategy == "tec_er_api":
        return parse_tec_er_api(content, source, base)
    soup = BeautifulSoup(content, "lxml")
    return STRATEGIES[strategy](soup, source, base)


# ------------------------------------------------- generic fallback (drift check)
def generic_row_count(content: bytes, source: dict, base: str) -> int:
    """v1-style heuristic: rows = elements holding both a date and a link. Used ONLY to
    cross-check the configured strategy — if this sees materially more rows than the
    adapter parsed, the adapter is flagged as drifting."""
    soup = BeautifulSoup(content, "lxml")
    n = 0
    seen = set()
    for el in soup.select("tr, li, .views-row, .views-view-grid__item, article"):
        text = _clean(el.get_text(" "))
        if len(text) < 12:
            continue
        if not extract_date(text, source["date_formats"]):
            continue
        if not el.select_one("a[href]"):
            continue
        key = text[:120]
        if key in seen:
            continue
        seen.add(key)
        n += 1
    return n


def structure_fingerprint(content: bytes, source: dict) -> str:
    """Cheap structural signature of the listing: strategy + shape of the first parsed
    region. Change = markup drift = WARN (not FAILED — the parse gates decide that)."""
    import hashlib
    soup = BeautifulSoup(content, "lxml")
    sel = {"trai_views": "ul.item-list > li", "trai_grid": ".views-view-grid__item",
           "html_table": source["parser"].get("row_selector", "table tbody tr"),
           "tr_with_doc": "tr", "link_shelf": "a[href]", "rss": "item",
           "regex_rows": "table", "meity_api": "body", "uidai_rsc": "body",
           "psn_rows": ".psn-container", "inspace_api": "body", "tec_er_api": "body",
           "egazette_recent": "table"}[source["parser"]["strategy"]]
    first = soup.select_one(sel)
    sig = ""
    if first is not None:
        sig = ",".join(sorted({c.name for c in first.find_all(True)[:30]}))
    return hashlib.sha1(f"{source['parser']['strategy']}|{sig}".encode()).hexdigest()[:12]
