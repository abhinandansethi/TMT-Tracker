#!/usr/bin/env python3
"""TMT Radar — operator console.

A local control panel with working buttons: sweep on demand, run the selftest,
rebuild the dashboard. Serves on 127.0.0.1 only, because these endpoints run
commands and must never be reachable from the network.

    engine/.venv/bin/python engine/console.py      then open http://127.0.0.1:8787

Why this exists rather than a button on the published dashboard: that page runs
in a sandbox that cannot reach gov.in or this machine, so a button there could
never start a scrape. The person who needs a trigger is sitting at this Mac.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, List, Optional

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PY = sys.executable
PORT = 8787
IST = timezone(timedelta(hours=5, minutes=30))

# A sweep older than this is called out on the page. Chosen to be just over the
# two-hourly cycle, so one skipped run shows up rather than passing unnoticed.
STALE_AFTER_HOURS = 3

_lock = threading.Lock()
_job: Dict[str, object] = {"name": None, "running": False, "lines": [], "started": None,
                           "finished": None, "code": None}


def _run(name: str, args: List[str]) -> None:
    """Run a tracker command, streaming its output into the job buffer."""
    with _lock:
        _job.update(name=name, running=True, lines=[], started=datetime.now(IST).isoformat(timespec="seconds"),
                    finished=None, code=None)
    try:
        proc = subprocess.Popen([PY, str(HERE / "tracker.py"), *args],
                                cwd=str(HERE), stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip("\n")
            if "NotOpenSSL" in line or "warnings.warn" in line:
                continue
            with _lock:
                _job["lines"].append(line)  # type: ignore[union-attr]
        proc.wait()
        code = proc.returncode
        # a sweep that found something is only useful once the data file is rebuilt
        if name == "sweep" and code == 0:
            with _lock:
                _job["lines"].append("")  # type: ignore[union-attr]
                _job["lines"].append("rebuilding dashboard data...")  # type: ignore[union-attr]
            subprocess.run([PY, str(HERE / "tracker.py"), "export"], cwd=str(HERE),
                           capture_output=True, text=True)
            build = subprocess.run([PY, str(ROOT / "code" / "build_dashboard_v2.py")],
                                   cwd=str(ROOT), capture_output=True, text=True)
            with _lock:
                for ln in (build.stdout or "").splitlines():
                    _job["lines"].append(ln)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001 — surfaced in the console, never swallowed
        code = 1
        with _lock:
            _job["lines"].append(f"console error: {type(e).__name__}: {e}")  # type: ignore[union-attr]
    with _lock:
        _job.update(running=False, finished=datetime.now(IST).isoformat(timespec="seconds"), code=code)


def start(name: str, args: List[str]) -> bool:
    with _lock:
        if _job["running"]:
            return False
    threading.Thread(target=_run, args=(name, args), daemon=True).start()
    return True


def status() -> dict:
    health_path = HERE / "health.json"
    health = json.loads(health_path.read_text()) if health_path.exists() else {"sources": {}}
    srcs = health.get("sources", {})
    counts = {"OK": 0, "WARN": 0, "FAILED": 0}
    problems = []
    for sid, s in srcs.items():
        counts[s.get("status", "OK")] = counts.get(s.get("status", "OK"), 0) + 1
        if s.get("status") != "OK":
            problems.append({"id": sid, "status": s.get("status"),
                             "note": "; ".join(s.get("notes", []))[:220]})

    last = health.get("generated")
    age_h: Optional[float] = None
    if last:
        try:
            age_h = (datetime.now(IST) - datetime.fromisoformat(last)).total_seconds() / 3600
        except ValueError:
            age_h = None

    items_path = ROOT / "data" / "items.json"
    stats, window = {}, {}
    if items_path.exists():
        d = json.loads(items_path.read_text())
        stats, window = d.get("stats", {}), d.get("window", {})

    with _lock:
        job = {"name": _job["name"], "running": _job["running"], "code": _job["code"],
               "started": _job["started"], "finished": _job["finished"],
               "lines": list(_job["lines"])[-400:]}  # type: ignore[arg-type]

    return {"last_sweep": last, "age_hours": age_h, "stale": (age_h is None or age_h > STALE_AFTER_HOURS),
            "stale_after": STALE_AFTER_HOURS, "counts": counts, "problems": problems,
            "stats": stats, "window": window, "job": job,
            "dashboard": str(ROOT / "dist" / "tmt-radar-v2.html")}


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>TMT Radar — console</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{
  --ground:#EEF1F3;--paper:#FDFEFE;--sunk:#F3F6F8;--ink:#14181B;--body:#2E353A;
  --mute:#5A646B;--faint:#8A939A;--rule:#D8DEE2;--rule-soft:#E7ECEF;
  --navy:#00506F;--ok:#1B6B4A;--ok-soft:#E6F1EB;--warn:#7E6410;--warn-soft:#F6F0DC;
  --bad:#8A2B1C;--bad-soft:#F7E9E6;
  --sans:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --serif:'Iowan Old Style','Palatino Linotype',Palatino,Georgia,serif;
}
@media (prefers-color-scheme:dark){:root{
  --ground:#0E1216;--paper:#171C21;--sunk:#1E252B;--ink:#E9EDF0;--body:#C4CCD2;
  --mute:#98A3AB;--faint:#707B83;--rule:#2C353C;--rule-soft:#232B31;
  --navy:#7FB2D2;--ok:#5FBE93;--ok-soft:#14261E;--warn:#D2AE52;--warn-soft:#2A2415;
  --bad:#E08A76;--bad-soft:#2E1A16;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--ground);color:var(--body);font-family:var(--sans);
  font-size:15px;line-height:1.55;-webkit-font-smoothing:antialiased}
.sheet{max-width:900px;margin:0 auto;background:var(--paper);min-height:100vh;padding:0 clamp(18px,4vw,48px) 64px}
header{padding:34px 0 0}
.eyebrow{font-family:var(--mono);font-size:10px;letter-spacing:.2em;text-transform:uppercase;color:var(--navy);font-weight:600}
h1{font-family:var(--serif);font-weight:400;font-size:34px;margin:10px 0 0;color:var(--ink);letter-spacing:-.01em}
.sub{margin:8px 0 0;color:var(--mute);font-size:15px;max-width:60ch}

/* freshness */
.fresh{margin-top:26px;border-left:3px solid var(--ok);background:var(--ok-soft);padding:16px 20px 18px}
.fresh.stale{border-left-color:var(--bad);background:var(--bad-soft)}
.fresh .k{font-family:var(--mono);font-size:10px;letter-spacing:.18em;text-transform:uppercase;font-weight:600;color:var(--ok)}
.fresh.stale .k{color:var(--bad)}
.fresh .v{font-family:var(--serif);font-size:23px;color:var(--ink);margin-top:6px}
.fresh .m{font-size:13.5px;color:var(--mute);margin-top:4px}

/* buttons */
.acts{margin-top:26px;display:flex;gap:12px;flex-wrap:wrap}
button{font-family:var(--sans);font-size:14px;font-weight:600;cursor:pointer;
  border:1px solid var(--navy);background:var(--navy);color:var(--paper);
  padding:11px 20px;transition:opacity .12s}
button.ghost{background:transparent;color:var(--navy)}
button:hover:not(:disabled){opacity:.84}
button:disabled{opacity:.4;cursor:not-allowed}
button:focus-visible{outline:2px solid var(--navy);outline-offset:2px}

/* stats */
.grid{margin-top:30px;display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));
  gap:1px;background:var(--rule-soft);border:1px solid var(--rule-soft)}
.cell{background:var(--paper);padding:14px 16px}
.cell .k{font-family:var(--mono);font-size:9.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--faint);font-weight:600}
.cell .v{font-family:var(--mono);font-size:24px;color:var(--ink);margin-top:4px;font-variant-numeric:tabular-nums}
.cell .v.ok{color:var(--ok)} .cell .v.warn{color:var(--warn)} .cell .v.bad{color:var(--bad)}

h2{font-family:var(--serif);font-weight:500;font-size:19px;color:var(--ink);margin:34px 0 0}
.log{margin-top:12px;background:var(--sunk);border:1px solid var(--rule-soft);padding:14px 16px;
  font-family:var(--mono);font-size:12px;line-height:1.62;color:var(--body);
  max-height:340px;overflow:auto;white-space:pre-wrap;word-break:break-word}
.log .new{color:var(--ok);font-weight:600}
.log .warn{color:var(--warn)} .log .bad{color:var(--bad);font-weight:600}
.idle{color:var(--faint)}
.prob{margin-top:12px;border-left:3px solid var(--warn);background:var(--warn-soft);padding:12px 16px;
  font-size:13.5px}
.prob b{font-family:var(--mono);font-size:12px;color:var(--ink)}
.hint{margin-top:22px;font-size:13.5px;color:var(--mute);max-width:64ch}
.hint code{font-family:var(--mono);font-size:12.5px;background:var(--sunk);border:1px solid var(--rule-soft);padding:1px 5px}
footer{margin-top:40px;padding-top:14px;border-top:1px solid var(--rule);
  font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}
</style></head><body>
<div class="sheet">
  <header>
    <div class="eyebrow">Operator console · local only</div>
    <h1>TMT Radar</h1>
    <p class="sub">Trigger a sweep when you need one. The partner's dashboard is read-only by
    design, so the controls live here.</p>
  </header>

  <div id="fresh" class="fresh"><div class="k">Checking</div><div class="v">…</div></div>

  <div class="acts">
    <button id="b-sweep">Check all sources now</button>
    <button id="b-test" class="ghost">Run selftest</button>
    <button id="b-build" class="ghost">Rebuild dashboard</button>
  </div>

  <div class="grid" id="grid"></div>
  <div id="problems"></div>

  <h2>Output</h2>
  <div class="log" id="log"><span class="idle">Nothing running. Press a button above.</span></div>

  <p class="hint">A sweep takes about four and a half minutes: it paces itself to roughly one
  request per second per host so it never hammers a government server. When it finishes the
  local dashboard is rebuilt automatically at <code>dist/tmt-radar-v2.html</code>. Updating the
  partner's shared link is a separate step, since only Claude can republish it.</p>

  <footer>127.0.0.1 only · runs commands · never expose this port</footer>
</div>
<script>
const $ = s => document.querySelector(s);
let running = false;

function fmtAge(h){
  if (h === null || h === undefined) return 'never';
  if (h < 1) return Math.round(h*60) + ' minutes ago';
  if (h < 48) return h.toFixed(1) + ' hours ago';
  return Math.floor(h/24) + ' days ago';
}
function paintLog(job){
  const el = $('#log');
  if (!job.name && !job.lines.length){
    el.innerHTML = '<span class="idle">Nothing running. Press a button above.</span>';
    return;
  }
  const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
  el.innerHTML = job.lines.map(l => {
    const e = l.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
    if (/^\s*NEW\s/.test(l)) return '<span class="new">' + e + '</span>';
    if (/^\s*WARN\s/.test(l)) return '<span class="warn">' + e + '</span>';
    if (/^\s*(FAIL|SELFTEST FAILED|AUDIT FAILED)/.test(l)) return '<span class="bad">' + e + '</span>';
    return e;
  }).join('\n') + (job.running ? '\n<span class="idle">running…</span>' : '');
  if (atBottom) el.scrollTop = el.scrollHeight;
}
async function tick(){
  let s;
  try { s = await (await fetch('/api/status')).json(); } catch { return; }

  const f = $('#fresh');
  f.className = 'fresh' + (s.stale ? ' stale' : '');
  f.innerHTML = '<div class="k">' + (s.stale ? 'Data is stale' : 'Up to date') + '</div>'
    + '<div class="v">Last checked ' + fmtAge(s.age_hours) + '</div>'
    + '<div class="m">' + (s.stale
        ? 'Older than ' + s.stale_after + ' hours. Run a check before relying on this.'
        : 'All ' + (s.counts.OK||0) + ' sources reported in.') + '</div>';

  $('#grid').innerHTML = [
    ['Instruments', (s.stats.total||0), ''],
    ['Substantive', (s.stats.substantive||0), ''],
    ['Sources OK', (s.counts.OK||0), 'ok'],
    ['Warnings', (s.counts.WARN||0), (s.counts.WARN? 'warn':'')],
    ['Failed', (s.counts.FAILED||0), (s.counts.FAILED? 'bad':'')],
  ].map(([k,v,c]) => '<div class="cell"><div class="k">'+k+'</div><div class="v '+c+'">'+v+'</div></div>').join('');

  $('#problems').innerHTML = s.problems.length
    ? s.problems.map(p => '<div class="prob"><b>'+p.id+'</b> — '+p.status+'<br>'+p.note+'</div>').join('')
    : '';

  running = s.job.running;
  ['#b-sweep','#b-test','#b-build'].forEach(id => $(id).disabled = running);
  $('#b-sweep').textContent = running && s.job.name === 'sweep' ? 'Checking…' : 'Check all sources now';
  paintLog(s.job);
}
async function go(path){
  if (running) return;
  running = true;
  ['#b-sweep','#b-test','#b-build'].forEach(id => $(id).disabled = true);
  await fetch(path, {method:'POST'});
  tick();
}
$('#b-sweep').onclick = () => go('/api/sweep');
$('#b-test').onclick  = () => go('/api/selftest');
$('#b-build').onclick = () => go('/api/build');
tick();
setInterval(tick, 1500);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 — http.server's required casing
        if self.path == "/":
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
        elif self.path == "/api/status":
            self._send(200, json.dumps(status()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        routes = {"/api/sweep": ("sweep", ["sweep"]),
                  "/api/selftest": ("selftest", ["selftest"]),
                  "/api/build": ("build", ["export"])}
        if self.path not in routes:
            self._send(404, b"not found", "text/plain")
            return
        name, args = routes[self.path]
        ok = start(name, args)
        self._send(200 if ok else 409, json.dumps({"started": ok}).encode(), "application/json")

    def log_message(self, *_args) -> None:
        pass  # the console shows its own output; keep the terminal clean


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"TMT Radar console  →  http://127.0.0.1:{PORT}")
    print("bound to localhost only; Ctrl-C to stop")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
