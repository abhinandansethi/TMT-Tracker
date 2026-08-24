"""TMT Regulatory Radar — extractor engine v2.

Deterministic, registry-driven, zero-LLM detection pipeline:

    fetch -> parse (per-source adapter) -> validate (gates) -> dedupe -> classify -> ledger

Design invariants (the fool-proof properties):
  1. NO SILENT FAILURE. A fetch error, an empty parse, or a parse below the source's
     row_floor is a FAILED source, never "no new items". The sweep exits non-zero.
  2. NO GUESSING. A row that fails a validation gate goes to quarantine with a reason,
     never into the ledger and never dropped.
  3. LOUD DRIFT. Every source records a structure fingerprint; markup change -> WARN.
     A generic fallback parser cross-checks the configured adapter; if the fallback
     sees materially more rows, the adapter is flagged as drifting.
  4. TRIPWIRES. Sources with sequential numbering (TRAI PRs, CERT-In advisories) are
     checked for gaps: a gap means a missed instrument, and the sweep says so.
  5. APPEND-ONLY. SQLite is the canonical store; every insert is mirrored to
     ledger.jsonl. History is never rewritten.
"""

__version__ = "2.0.0"
