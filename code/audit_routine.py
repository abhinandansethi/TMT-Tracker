#!/usr/bin/env python3
"""Audit: every ledger row's `routine` flag must equal what the regex produces.
A disagreement is a defect in the regex or the row, never a judgment call to keep."""
import json, re, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
RX = json.loads((ROOT/"registry"/"sources.json").read_text())["classification"]["routine_regex"]

def is_routine(title):
    return bool(re.search(RX, title))

def main():
    items = json.loads((ROOT/"data"/"items.json").read_text())["items"]
    bad = [i for i in items if is_routine(i["title"]) != i.get("routine", False)]
    print(f"{len(items)-len(bad)}/{len(items)} rows agree with the routine regex")
    for i in bad:
        print(f"  MISMATCH recorded routine={i.get('routine')} -> regex={is_routine(i['title'])}  "
              f"{i['date']} {i['title'][:78]}")
    sys.exit(1 if bad else 0)

if __name__ == "__main__":
    main()
