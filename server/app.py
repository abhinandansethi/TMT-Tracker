"""TMT Regulatory Radar — the whole thing as one service on one machine.

    uvicorn server.app:app --host 127.0.0.1 --port 8080

What this replaces, and with what:
  Vercel static hosting  -> this process serves dist/ (rebuilt after every job)
  Vercel Edge middleware -> HTTP Basic Auth here, same AUTH_USERS semantics as middleware.js
  editing env by hand    -> /setup (first run) and /admin (logins, key) write the settings file
  Vercel functions       -> /api/* routes calling the pipeline modules directly (server/assist.py)
  GitHub Actions         -> a job queue and one worker in this process (server/jobs.py)
  GitHub as audit trail  -> a commit to the LOCAL git repository after every job

What it deliberately keeps: the same files (scans/, data/scans/, engine/health.json), the same
builders, the same CLI commands with the same exit codes, and the rule that nothing runs unless
a person asked for it. The page's JavaScript is unchanged — it posts to the same /api paths and
reads the same status shape it read from GitHub.
"""
from __future__ import annotations

import base64
import hmac
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server import admin, assist, jobs, settings  # noqa: E402

DIST = ROOT / "dist"
REALM = 'Basic realm="Delta Scanner", charset="UTF-8"'

app = FastAPI(title="Delta Scanner", docs_url=None, redoc_url=None, openapi_url=None)
JOBS = jobs.Jobs()


# ------------------------------------------------------------------------------ auth
def _pairs() -> list:
    """Every configured user:password pair, from the same three forms middleware.js accepts:
    AUTH_USER/AUTH_PASS, AUTH_USERS (one pair per line or comma), AUTH_USER_2..9/AUTH_PASS_2..9.
    The password keeps everything after the FIRST colon, spaces included."""
    out = []
    u, p = os.environ.get("AUTH_USER"), os.environ.get("AUTH_PASS")
    if u and p:
        out.append((u, p))
    for entry in re.split(r"[\n,]", os.environ.get("AUTH_USERS") or ""):
        entry = entry.strip()
        if ":" in entry:
            name, pw = entry.split(":", 1)
            if name.strip() and pw:
                out.append((name.strip(), pw))
    for n in range(2, 10):
        u, p = os.environ.get(f"AUTH_USER_{n}"), os.environ.get(f"AUTH_PASS_{n}")
        if u and p:
            out.append((u, p))
    return out


def require_user(request: Request) -> str:
    pairs = _pairs()
    if not pairs:
        # Fail closed. There is deliberately no starter pair: a short password in a file in front
        # of a client roster is weakly closed, not closed.
        if os.environ.get("TMT_SETUP_TOKEN"):
            raise HTTPException(503, "This server is not set up yet. Open /setup with the setup code.")
        raise HTTPException(503, f"This server has no logins configured. Set AUTH_USERS (user:password per line) in {settings.ENV_FILE} and restart.")
    header = request.headers.get("authorization") or ""
    if not header.startswith("Basic "):
        raise HTTPException(401, "Ask Abhi for your username and password.", headers={"WWW-Authenticate": REALM})
    try:
        decoded = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        raise HTTPException(401, "Ask Abhi for your username and password.", headers={"WWW-Authenticate": REALM})
    if ":" not in decoded:
        raise HTTPException(401, "Ask Abhi for your username and password.", headers={"WWW-Authenticate": REALM})
    user, pw = decoded.split(":", 1)
    # Compare EVERY pair, no early exit, so the time taken does not say which username exists.
    # Usernames are case-insensitive (Abhi and abhi are one login); passwords are not.
    ok = False; matched = user
    for u, p in pairs:
        a = hmac.compare_digest(user.lower().encode(), u.lower().encode()); b = hmac.compare_digest(pw.encode(), p.encode())
        if a and b:
            ok = True; matched = u
    if not ok:
        raise HTTPException(401, "Ask Abhi for your username and password.", headers={"WWW-Authenticate": REALM})
    return matched


# ------------------------------------------------------------------------------ helpers
def _refused(e: assist.Refused) -> JSONResponse:
    return JSONResponse({"ok": False, "message": e.message}, status_code=e.status)


async def _json(request: Request) -> dict:
    if not (request.headers.get("content-type") or "").lower().startswith("application/json"):
        raise HTTPException(415, "Send Content-Type: application/json.")
    site = request.headers.get("sec-fetch-site") or ""
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, f"Cross-site request refused ({site}).")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Request body is not valid JSON.")
    if not isinstance(body, dict):
        raise HTTPException(400, "Request body must be a JSON object.")
    return body


@app.exception_handler(HTTPException)
async def _http_exc(request: Request, exc: HTTPException):
    # One shape everywhere: {ok:false, message}. The page reads `message` and shows it.
    return JSONResponse({"ok": False, "message": exc.detail}, status_code=exc.status_code, headers=exc.headers or {})


# ------------------------------------------------------------------------------ jobs: scans + sweep
@app.post("/api/scans")
async def api_scans(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    err, v = jobs.validate_scan_request(body)
    if err:
        raise HTTPException(400, err)
    if v["action"] == "status":
        wf = v.get("workflow")
        kind = "sweep" if wf == "sweep" else "scan"
        runs = JOBS.runs(kind=kind, scan_id=v["scan_id"] if kind == "scan" else None, limit=10)
        out = {"ok": True, "runs": runs, "actionsUrl": "/api/jobs"}
        if v["scan_id"]:
            out["scan_id"] = v["scan_id"]
        if wf:
            out["workflow"] = wf
        if not runs:
            out["message"] = "No run recorded yet for this scan on this server."
        return out
    # Who set a schedule is a fact the SERVER knows (the authenticated user), so it is stamped
    # here, never trusted from the page — a partner cannot record a schedule in someone else's name.
    if v["action"] == "create" and isinstance(v["scan"], dict) and isinstance(v["scan"].get("schedule"), dict):
        v["scan"]["schedule"] = dict(v["scan"]["schedule"], set_by=user, set_on=jobs.now_iso())
    title = f"Scan {v['action']} {v['scan_id']}"
    args = {"scan": v["scan"], "no_discover": v["no_discover"], "finding": v["finding"],
            "url": v.get("url"), "decision": v.get("decision"), "note": v.get("note"), "clients": v.get("clients")}
    job = JOBS.enqueue("scan", v["action"], v["scan_id"], args, title, requested_by=user)
    verb = {"create": "Scan queued", "run": "Run queued", "delete": "Scan removal queued",
            "promote": "Promotion queued", "dismiss": "Dismissal queued", "legal": "Decision queued",
            "clients": "Client roster queued"}[v["action"]]
    return JSONResponse({"ok": True, "message": f"{verb} — it runs on this server now; the page picks the result up itself.",
                         "scan_id": v["scan_id"], "job_id": job["id"], "actionsUrl": job["html_url"]}, status_code=202)


@app.post("/api/sweep")
async def api_sweep(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    wanted = body.get("workflow", "sweep")
    if wanted != "sweep":
        raise HTTPException(400, "Only the sweep runs from this button on the server; briefs run as part of it.")
    job = JOBS.enqueue("sweep", "sweep", None, {}, "Sweep TMT India", requested_by=user)
    return JSONResponse({"ok": True, "message": "Sweep queued — every source is checked, then the page rebuilds itself.",
                         "job_id": job["id"], "actionsUrl": job["html_url"]}, status_code=202)


@app.get("/api/jobs")
async def api_jobs(user: str = Depends(require_user)):
    return {"ok": True, "runs": JOBS.runs(limit=50)}


@app.get("/api/jobs/{jid}/log", response_class=PlainTextResponse)
async def api_job_log(jid: str, user: str = Depends(require_user)):
    if not re.match(r"^[a-f0-9]{12}$", jid):
        raise HTTPException(400, "bad job id")
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    head = f"{j['display_title']} · {j['status']}{' · ' + j['conclusion'] if j['conclusion'] else ''} · queued {j['created_at']}\n"
    return head + "\n" + (JOBS.log_text(jid) or "(no log yet)")


# ------------------------------------------------------------------------------ model-backed routes
@app.post("/api/propose")
async def api_propose(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    try:
        return await run_in_threadpool(assist.propose, body.get("description"))
    except assist.Refused as e:
        return _refused(e)


@app.post("/api/discover")
async def api_discover(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    jur = body.get("jurisdiction")
    if jur is None and isinstance(body.get("jurisdictions"), list):
        js = [j for j in body["jurisdictions"] if isinstance(j, str) and j.strip()]
        if len(set(j.upper() for j in js)) > 1:
            raise HTTPException(400, "Send one jurisdiction per call; the page merges the answers.")
        jur = js[0] if js else None
    try:
        return await run_in_threadpool(assist.discover_one, body.get("intent"), jur, body.get("topics") or [], body.get("industries") or [])
    except assist.Refused as e:
        return _refused(e)


@app.post("/api/resolve")
async def api_resolve(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    try:
        return await run_in_threadpool(assist.resolve_source, body.get("query"), body.get("intent") or "", body.get("jurisdictions") or [])
    except assist.Refused as e:
        return _refused(e)


@app.post("/api/gate")
async def api_gate(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    try:
        return await run_in_threadpool(assist.gate_one, body.get("url"), body.get("intent") or "", body.get("jurisdictions") or [])
    except assist.Refused as e:
        return _refused(e)


@app.post("/api/discover/stream")
async def api_discover_stream(request: Request, user: str = Depends(require_user)):
    """Discovery as server-sent events: a line per search, per venue, then the filtered answer.
    X-Accel-Buffering: no keeps nginx from holding the events back until the end."""
    body = await _json(request)
    jur = body.get("jurisdiction")
    gen = assist.discover_stream(body.get("intent"), jur, body.get("topics") or [], body.get("industries") or [])
    return StreamingResponse(gen, media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/jobs/{jid}/tail")
async def api_job_tail(jid: str, user: str = Depends(require_user)):
    """The last lines of a job's log with its state — what the page shows while a scan is built."""
    if not re.match(r"^[a-f0-9]{12}$", jid):
        raise HTTPException(400, "bad job id")
    j = JOBS.get(jid)
    if not j:
        raise HTTPException(404, "no such job")
    text = JOBS.log_text(jid, tail=60) or ""
    return {"ok": True, "id": jid, "status": j["status"], "conclusion": j["conclusion"], "scan_id": j.get("scan_id"),
            "action": j.get("action"), "lines": [ln for ln in text.splitlines() if ln.strip()][-60:]}


@app.post("/api/propose-filter")
async def api_propose_filter(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    try:
        return await run_in_threadpool(assist.propose_filter, body.get("intent"), body.get("topics") or [])
    except assist.Refused as e:
        return _refused(e)


@app.post("/api/ask")
async def api_ask(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    try:
        return await run_in_threadpool(assist.ask, body.get("scan"), body.get("dev"), body.get("question"), body.get("client"))
    except assist.Refused as e:
        return _refused(e)


@app.post("/api/draft")
async def api_draft(request: Request, user: str = Depends(require_user)):
    body = await _json(request)
    try:
        return await run_in_threadpool(assist.draft, body.get("scan"), body.get("dev"), body.get("kind") or "email", body.get("client"))
    except assist.Refused as e:
        return _refused(e)


@app.get("/api/health")
async def api_health():
    """Unauthenticated on purpose: a load balancer or a partner checking 'is it up' needs no
    password for that, and it leaks nothing — no scan names, no counts."""
    return {"ok": True, "service": "tmt-radar", "worker": bool(JOBS._thread and JOBS._thread.is_alive())}


# ------------------------------------------------------------------------------ the pages
@app.middleware("http")
async def _headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.get("/")
async def home(user: str = Depends(require_user)):
    p = DIST / "scans.html"
    if not p.exists():
        raise HTTPException(503, "The pages have not been built yet. Run: python code/build_dashboard_v2.py && python code/build_scans.py")
    return FileResponse(p, media_type="text/html")


class _AuthedStatic(StaticFiles):
    """dist/ behind the same gate as everything else — the client roster is in those files."""
    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            req = Request(scope, receive)
            try:
                require_user(req)
            except HTTPException as e:
                await JSONResponse({"ok": False, "message": e.detail}, status_code=e.status_code, headers=e.headers or {})(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


def _ensure_built() -> None:
    if not (DIST / "scans.html").exists():
        py = jobs.PY
        for b in ("code/build_dashboard_v2.py", "code/build_scans.py"):
            subprocess.call([py, b], cwd=str(ROOT))


# /setup and /admin are routes, so they go in before the catch-all mount of dist/.
app.include_router(admin.router)

_ensure_built()
DIST.mkdir(exist_ok=True)
app.mount("/", _AuthedStatic(directory=str(DIST), html=True), name="dist")


@app.on_event("startup")
async def _start():
    JOBS.start()


@app.on_event("shutdown")
async def _stop():
    JOBS.stop()
