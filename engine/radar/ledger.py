"""Canonical store: SQLite (stdlib) with an append-only JSONL mirror.
History is never rewritten; an item is inserted once and only its status may change.
Cross-listings land in `sightings` (the same instrument seen on another venue)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional

IST = timezone(timedelta(hours=5, minutes=30))

SCHEMA = """
CREATE TABLE IF NOT EXISTS items(
  id TEXT PRIMARY KEY,
  date TEXT,
  title TEXT NOT NULL,
  url TEXT,
  page_url TEXT,
  source_id TEXT NOT NULL,
  regulator TEXT,
  stratum TEXT,
  type TEXT,
  routine INTEGER NOT NULL DEFAULT 0,
  deadline TEXT,
  flags TEXT NOT NULL DEFAULT '[]',
  seq TEXT,
  lane TEXT NOT NULL DEFAULT 'instruments',
  meta TEXT NOT NULL DEFAULT '{}',
  first_seen TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'new'
);
CREATE INDEX IF NOT EXISTS idx_items_lane ON items(lane);
CREATE INDEX IF NOT EXISTS idx_items_seq ON items(source_id, seq);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);
CREATE TABLE IF NOT EXISTS sightings(
  item_id TEXT NOT NULL,
  source_id TEXT NOT NULL,
  url TEXT,
  seen TEXT NOT NULL,
  PRIMARY KEY(item_id, source_id)
);
CREATE TABLE IF NOT EXISTS source_state(
  source_id TEXT PRIMARY KEY,
  last_run TEXT,
  last_status TEXT,
  last_note TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  last_row_count INTEGER,
  fingerprint TEXT,
  seq_state TEXT NOT NULL DEFAULT '{}',
  newest_visible TEXT
);
CREATE TABLE IF NOT EXISTS quarantine(
  at TEXT NOT NULL,
  source_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  raw TEXT
);
"""


def now_ist() -> str:
    return datetime.now(IST).isoformat(timespec="seconds")


def norm_title(t: str) -> str:
    """Punctuation-insensitive: the same instrument appears across venues with smart
    quotes, dashes, case and spacing variants — none of that is identity."""
    t = t.strip().lower()
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


GENERIC_STEMS = {"notice", "notices", "order", "orders", "circular", "circulars",
                 "document", "doc", "file", "pdf", "advisory", "notification", "letter",
                 "download", "attachment", "public notice", "press release"}


def is_generic_doc_url(canon: str) -> bool:
    """Is this link's filename too generic to identify a document? Registries that upload
    every notice as NOTICE.pdf make the URL a location, not an identity."""
    stem = canon.split("?")[0].rstrip("/").rsplit("/", 1)[-1]
    stem = re.sub(r"\.(pdf|docx?|xlsx?|zip)$", "", stem, flags=re.I)
    stem = re.sub(r"[_\-]+", " ", stem).strip().lower()
    return stem in GENERIC_STEMS or len(stem) < 8


def item_id(title: str, date: Optional[str]) -> str:
    return hashlib.sha1((norm_title(title) + (date or "")).encode()).hexdigest()[:10]


# Columns added after the first ledgers were created. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so each is applied by hand, additively —
# history is never rebuilt.
MIGRATIONS = [
    ("items", "seq", "TEXT"),
    ("items", "lane", "TEXT NOT NULL DEFAULT 'instruments'"),
    ("items", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    ("source_state", "newest_visible", "TEXT"),
]


class Ledger:
    def __init__(self, db_path: Path, jsonl_path: Path):
        self.db = sqlite3.connect(db_path)
        self._migrate()
        self.db.executescript(SCHEMA)
        self.jsonl = jsonl_path

    def _migrate(self) -> None:
        for table, column, decl in MIGRATIONS:
            exists = self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                continue  # fresh database: SCHEMA creates it complete
            cols = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.db.commit()

    # ------------------------------------------------------------- items
    def known(self, iid: str) -> bool:
        return self.db.execute("SELECT 1 FROM items WHERE id=?", (iid,)).fetchone() is not None

    def insert(self, rec: Dict) -> None:
        self.db.execute(
            "INSERT INTO items(id,date,title,url,page_url,source_id,regulator,stratum,type,"
            "routine,deadline,flags,seq,lane,meta,first_seen,status) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["id"], rec.get("date"), rec["title"], rec.get("url"), rec.get("page_url"),
             rec["source_id"], rec.get("regulator"), rec.get("stratum"), rec.get("type"),
             int(bool(rec.get("routine"))), rec.get("deadline"),
             json.dumps(rec.get("flags", [])), rec.get("seq"),
             rec.get("lane", "instruments"), json.dumps(rec.get("meta") or {}, ensure_ascii=False),
             rec["first_seen"], rec.get("status", "new")))
        self.db.commit()
        with self.jsonl.open("a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def norm_index(self) -> Dict:
        """{(normalised_title, date): id} recomputed from stored titles — identity survives
        normalisation-rule changes across engine versions, unlike stored ids."""
        idx = {}
        for iid, title, d in self.db.execute("SELECT id, title, date FROM items"):
            idx[(norm_title(title), d or "")] = iid
        return idx

    def seqs_for(self, source_id: str, year: str) -> set:
        """Sequence numbers ever recorded for this source in this year."""
        out = set()
        for (s,) in self.db.execute(
                "SELECT seq FROM items WHERE source_id=? AND seq LIKE ?", (source_id, year + ":%")):
            try:
                out.add(int(s.split(":")[1]))
            except (IndexError, ValueError):
                pass
        return out

    def known_seq(self, source_id: str, seq: str) -> Optional[str]:
        """Item id already holding this sequence key (e.g. '2026:83') for this source.
        Numbered series use the number as identity — titles vary across renderings."""
        row = self.db.execute("SELECT id FROM items WHERE source_id=? AND seq=?",
                              (source_id, seq)).fetchone()
        return row[0] if row else None

    @staticmethod
    def canon_url(url: str) -> str:
        u = (url or "").strip().replace("%20", " ")
        u = re.sub(r"^https?://(www\.)?", "", u)
        return u.rstrip("/").lower()

    def url_index(self) -> Dict:
        """{canonical_url: id} for URLs specific enough to BE an identity.

        A shared PDF link usually means one document listed at two venues, which is exactly
        the cross-listing we want to collapse. But some registries reuse a filename for
        unrelated documents: TDSAT serves 14 different notices as 'NOTICE.pdf'. Treating
        that as identity silently swallows genuinely new instruments, the worst failure this
        tracker can have. So a URL is admitted as an identity only when it is unambiguous:
        it must not already point at two differently-titled documents, and its filename must
        not be a bare generic word."""
        seen: Dict[str, set] = {}
        first: Dict[str, str] = {}
        for iid, u, title in self.db.execute(
                "SELECT id, url, title FROM items WHERE url != ''"):
            cu = self.canon_url(u)
            if not cu or is_generic_doc_url(cu):
                continue
            seen.setdefault(cu, set()).add(norm_title(title))
            first.setdefault(cu, iid)
        return {cu: first[cu] for cu, titles in seen.items() if len(titles) == 1}

    def known_url(self, source_id: str, url: str) -> bool:
        """Has this exact link already been ledgered or sighted for this source? Used to
        short-circuit sources (PIB) whose rows need a page fetch before they can be keyed."""
        if not url:
            return False
        return (self.db.execute("SELECT 1 FROM items WHERE source_id=? AND url=?",
                                (source_id, url)).fetchone() is not None
                or self.db.execute("SELECT 1 FROM sightings WHERE source_id=? AND url=?",
                                   (source_id, url)).fetchone() is not None)

    def sight(self, iid: str, source_id: str, url: str) -> bool:
        """Record a cross-listing. True if this sighting is new."""
        cur = self.db.execute(
            "INSERT OR IGNORE INTO sightings(item_id,source_id,url,seen) VALUES(?,?,?,?)",
            (iid, source_id, url, now_ist()))
        self.db.commit()
        return cur.rowcount > 0

    def quarantine(self, source_id: str, reason: str, raw: Dict) -> None:
        self.db.execute("INSERT INTO quarantine(at,source_id,reason,raw) VALUES(?,?,?,?)",
                        (now_ist(), source_id, reason, json.dumps(raw, ensure_ascii=False)[:2000]))
        self.db.commit()

    # ------------------------------------------------------- source state
    def sightings_for(self, source_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM sightings WHERE source_id=?",
                               (source_id,)).fetchone()[0]

    def count_for(self, source_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM items WHERE source_id=? AND status != 'duplicate'",
            (source_id,)).fetchone()[0]

    def get_state(self, source_id: str) -> Dict:
        row = self.db.execute(
            "SELECT last_run,last_status,last_note,consecutive_failures,last_row_count,"
            "fingerprint,seq_state,newest_visible FROM source_state WHERE source_id=?",
            (source_id,)).fetchone()
        if row is None:
            return {"consecutive_failures": 0, "seq_state": {}}
        return {"last_run": row[0], "last_status": row[1], "last_note": row[2],
                "consecutive_failures": row[3], "last_row_count": row[4],
                "fingerprint": row[5], "seq_state": json.loads(row[6] or "{}"),
                "newest_visible": row[7]}

    def set_state(self, source_id: str, **kw) -> None:
        st = self.get_state(source_id)
        st.update(kw)
        self.db.execute(
            "INSERT INTO source_state(source_id,last_run,last_status,last_note,"
            "consecutive_failures,last_row_count,fingerprint,seq_state,newest_visible) "
            "VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id) DO UPDATE SET last_run=excluded.last_run,"
            "last_status=excluded.last_status,last_note=excluded.last_note,"
            "consecutive_failures=excluded.consecutive_failures,"
            "last_row_count=excluded.last_row_count,fingerprint=excluded.fingerprint,"
            "seq_state=excluded.seq_state,newest_visible=excluded.newest_visible",
            (source_id, st.get("last_run"), st.get("last_status"), st.get("last_note"),
             st.get("consecutive_failures", 0), st.get("last_row_count"),
             st.get("fingerprint"), json.dumps(st.get("seq_state", {})),
             st.get("newest_visible")))
        self.db.commit()

    # ----------------------------------------------------------- queries
    def all_items(self) -> List[Dict]:
        cols = ["id", "date", "title", "url", "page_url", "source_id", "regulator", "stratum",
                "type", "routine", "deadline", "flags", "seq", "lane", "meta", "first_seen", "status"]
        out = []
        for r in self.db.execute(f"SELECT {','.join(cols)} FROM items ORDER BY date, id"):
            d = dict(zip(cols, r))
            d["routine"] = bool(d["routine"])
            d["flags"] = json.loads(d["flags"])
            d["meta"] = json.loads(d.get("meta") or "{}")
            out.append(d)
        return out

    def count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
