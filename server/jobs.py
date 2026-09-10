"""The job runner: what GitHub Actions used to be, on one machine.

A job is exactly one of the commands the workflows ran — `pipeline.scan.run create|run|delete|
promote|dismiss`, or the sweep chain — executed as a subprocess with the same environment, its
log kept, its SUMMARY captured, and the pages rebuilt afterwards. Jobs run ONE AT A TIME, in the
order they were asked for, which is the same serialisation the workflows' concurrency groups
gave us and the same reason: two runs writing one scan's ledger at once would corrupt it.

Why a subprocess and not an import: the CLI is the tested surface. Its argument validation, its
exit codes (0 ok · 1 a source FAILED after results were written · 2 refused) and its
--summary-out contract are what the workflow relied on, and a partner reading a job log here
sees exactly what they would have seen on the Actions page.

Why SQLite and not a queue service: one VM, one worker, a few jobs a day. The table is the audit
log of who asked for what and what happened; the results themselves stay in the same files the
whole system has always used (scans/, data/scans/, engine/health.json), and every completed
job is also committed to the LOCAL git repository, so `git log` on the VM remains the history it
was on GitHub — with no GitHub.

Nothing here is scheduled. A job exists because a person pressed a button. The compliance
position of the whole tracker rests on that (docs/horizon-design.md §1.3); if a schedule is ever
wanted, it is a deliberate decision to be taken in the open, not a cron line added here.
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
PY = str(ROOT / "engine" / ".venv" / "bin" / "python") if (ROOT / "engine" / ".venv" / "bin" / "python").exists() else sys.executable
DB_PATH = Path(os.environ.get("TMT_JOBS_DB") or ROOT / "server" / "jobs.db")
LOG_DIR = Path(os.environ.get("TMT_JOB_LOGS") or ROOT / "server" / "logs")
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,59}$")
FINDING_RE = re.compile(r"^[a-f0-9]{10}$")
RESERVED_IDS = {"schema", "tmt-india"}
ACTIONS = {"create", "run", "delete", "promote", "dismiss"}
MAX_SCAN_JSON = 60_000           # the same cap the workflow input had; a definition is small


def now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------------------ storage
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id            TEXT PRIMARY KEY,
  kind          TEXT NOT NULL,          -- 'scan' | 'sweep'
  action        TEXT NOT NULL,          -- create|run|delete|promote|dismiss | sweep
  scan_id       TEXT,
  display_title TEXT NOT NULL,
  args_json     TEXT NOT NULL,
  status        TEXT NOT NULL,          -- queued | in_progress | completed
  conclusion    TEXT NOT NULL DEFAULT '',  -- '' | success | failure | cancelled
  exit_code     INTEGER,
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  log_path      TEXT,
  summary_json  TEXT,
  requested_by  TEXT
);
CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created_at DESC);
"""


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["html_url"] = f"/api/jobs/{d['id']}/log"
    return d


# ------------------------------------------------------------------------------ the queue
class Jobs:
    def __init__(self):
        self.db = _db()
        self.lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # A process that died mid-job leaves 'in_progress' rows behind. They did not finish; say so.
        with self.lock:
            n = self.db.execute("UPDATE jobs SET status='completed', conclusion='cancelled', updated_at=? "
                                "WHERE status='in_progress'", (now_iso(),)).rowcount
            self.db.commit()
        if n:
            print(f"[jobs] {n} job(s) were in progress when the service last stopped — marked cancelled", flush=True)

    # ---- enqueue -------------------------------------------------------------------------
    def enqueue(self, kind: str, action: str, scan_id: Optional[str], args: dict,
                display_title: str, requested_by: Optional[str] = None) -> dict:
        jid = uuid.uuid4().hex[:12]
        t = now_iso()
        with self.lock:
            self.db.execute(
                "INSERT INTO jobs(id,kind,action,scan_id,display_title,args_json,status,created_at,updated_at,requested_by) "
                "VALUES(?,?,?,?,?,?,'queued',?,?,?)",
                (jid, kind, action, scan_id, display_title, json.dumps(args), t, t, requested_by))
            self.db.commit()
            row = _row(self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
        self._wake.set()
        return row

    def get(self, jid: str) -> Optional[dict]:
        with self.lock:
            r = self.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
        return _row(r) if r else None

    def runs(self, kind: Optional[str] = None, scan_id: Optional[str] = None, limit: int = 10) -> list:
        """Newest first, in the shape the pages already read (status / conclusion / created_at /
        updated_at / html_url / display_title) — the same seven fields the GitHub answer had."""
        q, p = "SELECT * FROM jobs", []
        conds = []
        if kind:
            conds.append("kind=?"); p.append(kind)
        if scan_id:
            conds.append("scan_id=?"); p.append(scan_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at DESC LIMIT ?"; p.append(int(limit))
        with self.lock:
            rows = self.db.execute(q, p).fetchall()
        return [{k: r[k] for k in ("id", "status", "conclusion", "created_at", "updated_at", "display_title", "scan_id", "action")}
                | {"html_url": f"/api/jobs/{r['id']}/log"} for r in rows]

    def log_text(self, jid: str, tail: int = 400) -> str:
        j = self.get(jid)
        if not j or not j.get("log_path") or not Path(j["log_path"]).exists():
            return ""
        lines = Path(j["log_path"]).read_text(errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    # ---- the worker ------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="tmt-jobs", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set(); self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            with self.lock:
                r = self.db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at ASC LIMIT 1").fetchone()
            if not r:
                self._wake.wait(timeout=5); self._wake.clear()
                continue
            self._run_one(dict(r))

    def _set(self, jid: str, **fields) -> None:
        fields["updated_at"] = now_iso()
        with self.lock:
            self.db.execute("UPDATE jobs SET " + ", ".join(f"{k}=?" for k in fields) + " WHERE id=?",
                            [*fields.values(), jid])
            self.db.commit()

    def _run_one(self, job: dict) -> None:
        jid = job["id"]; args = json.loads(job["args_json"])
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOG_DIR / f"{jid}.log"
        self._set(jid, status="in_progress", log_path=str(log_path))
        summary_path = LOG_DIR / f"{jid}.summary.json"
        env = dict(os.environ)
        env.pop("TMT_SCAN_DRY_RUN", None)          # a job is never a dry run; that flag is for tests
        env.setdefault("PYTHONUNBUFFERED", "1")
        code, summary = 1, None
        with open(log_path, "w") as log:
            log.write(f"# job {jid} · {job['display_title']} · queued {job['created_at']}\n")
            try:
                if job["kind"] == "scan":
                    code, summary = self._scan_job(job, args, env, log, summary_path)
                else:
                    code = self._sweep_job(env, log)
                self._rebuild(job["kind"], env, log)
                self._commit(job, summary, env, log)
            except Exception as e:                       # the runner's own fault, not the command's
                log.write(f"\n[runner] failed: {type(e).__name__}: {e}\n"); code = 97
        concl = "success" if code == 0 else "failure"
        # exit 1 means "a source FAILED after the results were written" — the workflow committed
        # and then flagged it. Same here: results are on disk and rebuilt; the job says failure so
        # the page shows the red card and the log names the source.
        self._set(jid, status="completed", conclusion=concl, exit_code=code,
                  summary_json=json.dumps(summary) if summary else None)

    # ---- the two job kinds ----------------------------------------------------------------
    def _scan_job(self, job: dict, args: dict, env: dict, log, summary_path: Path):
        action = job["action"]
        cmd = [PY, "-m", "pipeline.scan.run", action]
        tmp_def = None
        if action == "create":
            tmp_def = LOG_DIR / f"{job['id']}.definition.json"
            tmp_def.write_text(json.dumps(args["scan"], ensure_ascii=False))
            cmd += ["--from-json", str(tmp_def)]
            if args.get("no_discover"):
                cmd.append("--no-discover")
        else:
            cmd += ["--id", job["scan_id"]]
        if action in ("promote", "dismiss"):
            cmd += ["--finding", args["finding"]]
        cmd += ["--summary-out", str(summary_path)]
        log.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n"); log.flush()
        code = subprocess.call(cmd, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
        summary = None
        if summary_path.exists():
            try:
                summary = json.loads(summary_path.read_text() or "{}")
            except Exception:
                summary = None
        return code, summary

    def _sweep_job(self, env: dict, log) -> int:
        """The sweep chain, step for step as .github/workflows/sweep.yml ran it. A sweep that
        reports source failures still exports and rebuilds — that is the point of recording
        failures in health rather than aborting on them."""
        steps = [
            ([PY, "engine/tracker.py", "sweep"], True),
            ([PY, "engine/tracker.py", "export"], False),
            ([PY, "pipeline/brief.py", "--limit", "40"], True),
        ]
        worst = 0
        for cmd, tolerate in steps:
            log.write("$ " + " ".join(shlex.quote(c) for c in cmd) + "\n"); log.flush()
            rc = subprocess.call(cmd, cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
            if rc != 0:
                log.write(f"[runner] step exited {rc}" + (" — tolerated, continuing\n" if tolerate else " — stopping\n"))
                if not tolerate:
                    return rc
                worst = max(worst, 1)
        return worst

    # ---- after the command: the pages, then the audit trail ------------------------------
    def _rebuild(self, kind: str, env: dict, log) -> None:
        builders = ["code/build_dashboard_v2.py", "code/build_scans.py"] if kind == "sweep" else ["code/build_scans.py"]
        for b in builders:
            log.write(f"$ {PY} {b}\n"); log.flush()
            subprocess.call([PY, b], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)

    def _commit(self, job: dict, summary: Optional[dict], env: dict, log) -> None:
        """Every job that changed something is a commit in the LOCAL repository. This is the
        audit trail GitHub used to be — who asked for what, when, and what the files looked like
        after — kept with no remote at all. TMT_NO_COMMIT=1 turns it off for a scratch install."""
        if os.environ.get("TMT_NO_COMMIT") == "1" or not (ROOT / ".git").exists():
            return
        paths = ["scans", "data/scans"] if job["kind"] == "scan" else ["engine/ledger.db", "engine/ledger.jsonl", "engine/health.json", "data", "pipeline/brief_cache.json"]
        present = [p for p in paths if (ROOT / p).exists()]
        if not present:
            return
        git = lambda *a: subprocess.call(["git", *a], cwd=str(ROOT), env=env, stdout=log, stderr=subprocess.STDOUT)
        git("add", "-A", "--", *present)
        if subprocess.call(["git", "diff", "--cached", "--quiet"], cwd=str(ROOT)) == 0:
            log.write("[runner] nothing to commit\n"); return
        who = job.get("requested_by") or "a partner"
        msg = f"{job['display_title']} · {now_iso()} · requested by {who}"
        git("-c", "user.name=tmt-radar", "-c", "user.email=tmt-radar@localhost", "commit", "-q", "-m", msg)


# ------------------------------------------------------------------------------ validation
def validate_scan_request(body: dict) -> tuple:
    """Shape checks only; run.py is the validator of the definition itself and says exactly what
    is wrong in the job log. Returns (error_message | None, normalised dict)."""
    action = body.get("action")
    if action not in ACTIONS | {"status"}:
        return f"action must be one of: {', '.join(sorted(ACTIONS | {'status'}))}.", None
    scan_id = body.get("scan_id")
    scan = body.get("scan")
    if action == "create":
        if not isinstance(scan, dict):
            return "create needs the scan definition in `scan`.", None
        if scan_id is None:
            scan_id = scan.get("id") or _slug(scan.get("name") or "")
        scan = dict(scan, id=scan_id)
        if len(json.dumps(scan)) > MAX_SCAN_JSON:
            return f"the definition is longer than {MAX_SCAN_JSON} characters.", None
    if action != "status":
        if not isinstance(scan_id, str) or not ID_RE.match(scan_id):
            return "scan_id must match ^[a-z0-9][a-z0-9-]{1,59}$.", None
        if scan_id in RESERVED_IDS:
            return f"'{scan_id}' is reserved.", None
    finding = body.get("finding")
    if action in ("promote", "dismiss"):
        if not isinstance(finding, str) or not FINDING_RE.match(finding):
            return f"{action} needs `finding`, a 10-character hex id.", None
    no_discover = body.get("no_discover")
    if no_discover is not None and not isinstance(no_discover, bool):
        return "no_discover must be true or false.", None
    return None, {"action": action, "scan_id": scan_id, "scan": scan, "finding": finding,
                  "no_discover": bool(no_discover), "workflow": body.get("workflow")}


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:60] or "scan"
