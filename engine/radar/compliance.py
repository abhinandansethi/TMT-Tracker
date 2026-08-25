"""Dated evidence of every site's own crawl policy.

Whether robots.txt implies authorisation is unsettled in India. What is not in doubt is
that a firm which can produce the file as it stood on the day it ran, for every host it
touched, is in a materially different position from one reconstructing it afterwards.

This writes a timestamped snapshot: the raw bytes, a hash, and the verdict for the exact
paths the tracker fetches. Run it periodically; the snapshots are the audit trail, and a
diff between two of them is how a site changing its mind gets noticed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List
from urllib.parse import urlparse

import requests
import urllib3

from .fetch import UA
from .robots import RobotsDisallowed, check

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def snapshot(registry: dict, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d")
    raw_dir = out_dir / f"robots_{stamp}"
    raw_dir.mkdir(exist_ok=True)

    # host -> the source paths we actually fetch from it
    hosts: Dict[str, List[dict]] = {}
    for s in registry["sources"]:
        if s.get("status") not in ("live", "suspended", "needs_decision"):
            continue
        u = urlparse(s["url"])
        hosts.setdefault(f"{u.scheme}://{u.netloc}", []).append(s)

    report = {"taken": datetime.now().isoformat(timespec="seconds"), "hosts": {}}
    for origin, srcs in sorted(hosts.items()):
        url = origin + "/robots.txt"
        entry: dict = {"url": url, "sources": len(srcs)}
        try:
            r = requests.get(url, headers=UA, timeout=20, verify=False)
            body = r.text if "html" not in r.headers.get("content-type", "").lower() else ""
            entry["status"] = r.status_code
            entry["content_type"] = r.headers.get("content-type", "")
            entry["sha256"] = hashlib.sha256(r.content).hexdigest()[:16]
            entry["bytes"] = len(r.content)
            if body:
                (raw_dir / f"{urlparse(origin).netloc}.robots.txt").write_text(body)
                entry["saved"] = True
                entry["rules"] = [ln for ln in body.splitlines()
                                  if ln.strip().lower().startswith(("user-agent", "allow", "disallow"))][:40]
            else:
                entry["saved"] = False
                entry["note"] = "served HTML rather than a robots file"
        except Exception as e:  # noqa: BLE001
            entry["status"] = None
            entry["error"] = f"{type(e).__name__}: {str(e)[:90]}"

        # the only question that matters: our exact paths
        verdicts = {}
        for s in srcs:
            try:
                check(s["url"])
                verdicts[s["id"]] = "allowed"
            except RobotsDisallowed as e:
                verdicts[s["id"]] = f"REFUSED: {e}"
        entry["paths"] = verdicts
        entry["all_allowed"] = all(v == "allowed" for v in verdicts.values())
        report["hosts"][origin] = entry

    path = out_dir / f"robots_snapshot_{stamp}.json"
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False))
    return {"report": report, "json": path, "raw": raw_dir}
