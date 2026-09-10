"""Setup and admin pages — the service configures itself from the browser.

/setup  first run only: no login exists yet. Asks for the one-time setup code the installer
        printed (TMT_SETUP_TOKEN), the OpenAI key, and the first login, which becomes the admin.
        Writes the settings file and applies it in-process; the code is deleted once used.
/admin  the admin's page: add or remove a partner's login, replace the OpenAI key. Every other
        login gets a 403 that says who manages logins.

The key is verified against the model the pipeline uses before it is saved, so a wrong key is
refused here with the model's own error rather than discovered on the first scan. Neither page
ever shows a stored password or more than the last four characters of the key.
"""
from __future__ import annotations

import hmac
import html
import os
import urllib.parse
from typing import Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from server import settings

router = APIRouter()

CSS = """
<link rel="preconnect" href="https://fonts.googleapis.com"><link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&family=Spectral:wght@300;400&display=swap">
<style>
:root{--ink:#111315;--mute:#4E555A;--faint:#7C848A;--paper:#fff;--ground:#E4E8EA;--panel:#F4F6F7;--rule:#D9DEE1;
--serif:'Spectral',Georgia,serif;--sans:'IBM Plex Sans',system-ui,sans-serif;--mono:'IBM Plex Mono',ui-monospace,monospace}
*{box-sizing:border-box}body{margin:0;background:var(--ground);font-family:var(--sans);color:var(--ink);font-size:13.5px;line-height:1.5}
.sheet{max-width:720px;margin:0 auto;background:var(--paper);min-height:100vh;padding:0 0 60px}
.mast{background:#16232B;padding:18px 40px;color:#fff}.wordmark{font-family:var(--serif);font-size:24px}.wordmark b{font-weight:500}
.wordmark a{color:#fff;text-decoration:none}
main{padding:34px 40px}.crumb{font-family:var(--mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--faint);margin-bottom:12px}
h1{font-family:var(--serif);font-weight:400;font-size:30px;line-height:1.15;margin:0 0 10px}h2{font-family:var(--serif);font-weight:400;font-size:20px;margin:34px 0 8px}
p{margin:0 0 12px;color:var(--mute);max-width:560px}label{display:block;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin:16px 0 5px}
input[type=text],input[type=password]{width:100%;max-width:480px;padding:9px 11px;border:1px solid var(--rule);border-radius:6px;font:inherit;background:#fff}
input:focus{outline:none;border-color:var(--ink)}.hint{font-size:12px;color:var(--faint);margin-top:4px}
.btn{appearance:none;cursor:pointer;background:#fff;color:var(--ink);border:1px solid var(--rule);border-radius:6px;padding:8px 14px;font:inherit;margin-top:18px}
.btn.primary{background:var(--ink);color:#fff;border-color:var(--ink)}.btn.small{padding:3px 9px;font-size:12px;margin:0}
.notice{background:#EEF6EE;border-left:3px solid #2E7D32;padding:10px 14px;margin:0 0 18px;color:#1B4D1E}
.problem{background:#FBEFE8;border-left:3px solid #B3411E;padding:10px 14px;margin:0 0 18px;color:#6B2A12;white-space:pre-wrap}
table{border-collapse:collapse;margin-top:8px}td{padding:7px 14px 7px 0;border-bottom:1px solid var(--panel)}td.u{font-family:var(--mono)}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);border:1px solid var(--rule);border-radius:20px;padding:1px 8px;margin-left:8px}
.row{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}.row>div{flex:1;min-width:200px}
</style>"""


def _page(title: str, crumb: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex"><title>{html.escape(title)} · TMT Regulatory Radar</title>{CSS}</head><body><div class="sheet">
<div class="mast"><div class="wordmark"><a href="/">TMT <b>Regulatory Radar</b></a></div></div>
<main><div class="crumb">{html.escape(crumb)}</div>{body}</main></div></body></html>""")


async def _form(request: Request) -> Dict[str, str]:
    ctype = (request.headers.get("content-type") or "").lower()
    if not ctype.startswith("application/x-www-form-urlencoded"):
        raise HTTPException(415, "Send a form.")
    site = request.headers.get("sec-fetch-site") or ""
    if site and site not in ("same-origin", "none"):
        raise HTTPException(403, f"Cross-site request refused ({site}).")
    raw = await request.body()
    if len(raw) > 64_000:
        raise HTTPException(413, "Form too large.")
    q = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: (v[0] if v else "") for k, v in q.items()}


def check_key_shape(key: str) -> str:
    """Catch the wrong thing in the (masked) key field before OpenAI is asked — a pasted error
    message, a sentence, a blank. An OpenAI key is one token starting with sk-."""
    if not key:
        return "The OpenAI key is empty."
    if " " in key or "\n" in key or not key.startswith("sk-") or len(key) < 20:
        head = key[:12] + ("…" if len(key) > 12 else "")
        return f"That is not an OpenAI key — the field received “{head}”. A key is one token starting with sk-; use Show to see what is in the field."
    return ""


SHOW = '<button type="button" class="btn small" style="margin-left:8px" onclick="var i=document.getElementById(\'{id}\');i.type=i.type===\'password\'?\'text\':\'password\';this.textContent=i.type===\'password\'?\'Show\':\'Hide\'">Show</button>'


def _verify_key(key: str) -> str:
    """'' when the key can use the pipeline's model, else the model's own error, one line."""
    try:
        from openai import OpenAI
        from pipeline.scan import common
        OpenAI(api_key=key, timeout=20, max_retries=0).models.retrieve(common.MODEL)
        return ""
    except Exception as e:  # noqa: BLE001 — shown to the person, verbatim
        return str(e).strip().splitlines()[0][:400] if str(e).strip() else type(e).__name__


def _configured() -> bool:
    from server.app import _pairs
    return bool(_pairs())


# ------------------------------------------------------------------------------ /setup
def _setup_form(err: str = "", vals: Dict[str, str] = None) -> HTMLResponse:
    v = vals or {}
    e = f'<div class="problem">{html.escape(err)}</div>' if err else ""
    force = '<label style="text-transform:none;letter-spacing:0;font-family:var(--sans);font-size:13px;color:var(--mute)"><input type="checkbox" name="force" value="1"> Save the key even though it could not be verified just now</label>' if err.startswith("The key") else ""
    return _page("Set up", "Scans › Set up", f"""
<h1>Set this server up</h1>
<p>Nothing is configured yet. This page is only here until the first login exists — after that it is gone and logins are managed from <span style="font-family:var(--mono)">/admin</span>.</p>{e}
<form method="post" action="/setup" autocomplete="off">
<label for="token">Setup code</label><input type="text" id="token" name="token" value="{html.escape(v.get('token',''))}" required>
<div class="hint">The one-time code the installer printed (Abhi has it). It is deleted once used.</div>
<label for="key">OpenAI API key</label><div class="row" style="align-items:center"><input type="password" id="key" name="key" required style="flex:1">{SHOW.format(id="key")}</div>
<div class="hint">Checked against the model the pipeline uses before anything is saved. Stored only on this machine, in the service's settings file.</div>
<h2>Your login — this becomes the admin</h2>
<div class="row"><div><label for="user">Username</label><input type="text" id="user" name="user" value="{html.escape(v.get('user',''))}" required></div>
<div><label for="pw">Password</label><input type="password" id="pw" name="pw" required></div>
<div><label for="pw2">Again</label><input type="password" id="pw2" name="pw2" required></div></div>
<div class="hint">At least 10 characters; no double quote, backslash or line break. Partners get their own logins from the admin page.</div>
{force}
<button class="btn primary" type="submit">Save and open the radar</button>
</form>""")


@router.get("/setup", response_class=HTMLResponse)
async def setup_get():
    if _configured():
        return RedirectResponse("/", status_code=303)
    if not os.environ.get("TMT_SETUP_TOKEN"):
        return _page("Set up", "Scans › Set up", "<h1>No setup code on this server</h1><p>This server has no login and no setup code, so nothing can be configured from here. The installer writes a code into the settings file; ask Abhi.</p>")
    return _setup_form()


@router.post("/setup", response_class=HTMLResponse)
async def setup_post(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    token = os.environ.get("TMT_SETUP_TOKEN") or ""
    f = await _form(request)
    if not token or not hmac.compare_digest(f.get("token", "").strip().encode(), token.encode()):
        return _setup_form("That setup code is not the one on this server.", f)
    user, pw, pw2, key = f.get("user", "").strip(), f.get("pw", ""), f.get("pw2", ""), f.get("key", "").strip()
    for err in (settings.check_user(user), settings.check_password(pw), "" if pw == pw2 else "The two passwords differ.", check_key_shape(key)):
        if err:
            return _setup_form(err, f)
    verified = False
    if f.get("force") != "1":
        bad = _verify_key(key)
        if bad:
            return _setup_form("The key could not be verified: " + bad, f)
        verified = True
    current = settings.read()
    current.update({"OPENAI_API_KEY": key, "AUTH_USERS": settings.pairs_to([(user, pw)]), "TMT_ADMIN_USER": user, "TMT_SETUP_TOKEN": ""})
    try:
        settings.write(current)
    except OSError as e:
        return _setup_form(f"The settings file could not be written ({e}). The service user must own {settings.ENV_FILE}.", f)
    settings.apply(current)
    saved = ("Saved. The key works with the pipeline's model and your login exists." if verified
             else "Saved without verifying the key — if it is wrong, the first scan and the model routes will say so. Replace it on the admin page.")
    return _page("Set up", "Scans › Set up", f"""<div class="notice">{saved}</div>
<h1>Ready</h1><p>Open the radar — the browser will ask for the username and password you just created. Partners' logins are added on the admin page, linked from the landing page.</p>
<a class="btn primary" href="/">Open the radar</a> <a class="btn" href="/admin">Admin page</a>""")


# ------------------------------------------------------------------------------ /admin
def _require_admin(request: Request) -> str:
    from server.app import require_user
    user = require_user(request)
    admin = os.environ.get("TMT_ADMIN_USER") or ""
    if not admin or not hmac.compare_digest(user.encode(), admin.encode()):
        who = admin or "the person who set the server up"
        raise HTTPException(403, f"Only {who} manages logins and the key on this server.")
    return user


def _admin_page(user: str, notice: str = "", err: str = "") -> HTMLResponse:
    cur = settings.read()
    pairs = settings.pairs_from(cur.get("AUTH_USERS") or os.environ.get("AUTH_USERS") or "")
    rows = "".join(
        f'<tr><td class="u">{html.escape(u)}{"<span class=tag>admin</span>" if u == user else ""}</td><td>'
        + ("" if u == user else f'<form method="post" action="/admin" style="margin:0"><input type="hidden" name="action" value="remove"><input type="hidden" name="user" value="{html.escape(u)}"><button class="btn small" type="submit">Remove login</button></form>')
        + "</td></tr>" for u, _ in pairs)
    from pipeline.scan import common
    n = f'<div class="notice">{html.escape(notice)}</div>' if notice else ""
    e = f'<div class="problem">{html.escape(err)}</div>' if err else ""
    return _page("Admin", "Scans › Admin", f"""
<h1>Logins and the key</h1>
<p>One login per partner. A password is shown once, when you set it; there is no way to read one back — set a new one instead.</p>{n}{e}
<h2>Logins</h2><table>{rows}</table>
<form method="post" action="/admin" autocomplete="off"><input type="hidden" name="action" value="add">
<div class="row"><div><label for="nu">Username</label><input type="text" id="nu" name="user" required></div>
<div><label for="np">Password</label><input type="text" id="np" name="pw" required></div></div>
<div class="hint">At least 10 characters; no double quote, backslash or line break. Setting an existing username replaces its password.</div>
<button class="btn" type="submit">Add or reset login</button></form>
<h2>OpenAI key</h2>
<p>Currently <span style="font-family:var(--mono)">{html.escape(settings.key_hint(os.environ.get("OPENAI_API_KEY") or ""))}</span> · model <span style="font-family:var(--mono)">{html.escape(common.MODEL)}</span>.</p>
<form method="post" action="/admin" autocomplete="off"><input type="hidden" name="action" value="key">
<label for="nk">New key</label><div class="row" style="align-items:center"><input type="password" id="nk" name="key" required style="flex:1">{SHOW.format(id="nk")}</div>
<div class="hint">Verified against the model before it replaces the old one.</div>
<button class="btn" type="submit">Replace key</button></form>
<p style="margin-top:34px"><a href="/">← All scans</a></p>""")


@router.get("/admin", response_class=HTMLResponse)
async def admin_get(user: str = Depends(_require_admin)):
    return _admin_page(user)


@router.post("/admin", response_class=HTMLResponse)
async def admin_post(request: Request, user: str = Depends(_require_admin)):
    f = await _form(request)
    cur = settings.read()
    if not cur.get("AUTH_USERS"):
        cur["AUTH_USERS"] = os.environ.get("AUTH_USERS") or ""
    cur.setdefault("OPENAI_API_KEY", os.environ.get("OPENAI_API_KEY") or "")
    cur.setdefault("TMT_ADMIN_USER", os.environ.get("TMT_ADMIN_USER") or user)
    pairs = settings.pairs_from(cur["AUTH_USERS"])
    action = f.get("action")
    if action == "add":
        u, pw = f.get("user", "").strip(), f.get("pw", "")
        err = settings.check_user(u) or settings.check_password(pw)
        if err:
            return _admin_page(user, err=err)
        replaced = any(x == u for x, _ in pairs)
        pairs = [(x, p) for x, p in pairs if x != u] + [(u, pw)]
        notice = f"Password for {u} replaced." if replaced else f"Login added for {u}. Give them the URL, the username and the password."
    elif action == "remove":
        u = f.get("user", "").strip()
        if u == user:
            return _admin_page(user, err="You cannot remove your own login.")
        if not any(x == u for x, _ in pairs):
            return _admin_page(user, err=f"No login named {u}.")
        pairs = [(x, p) for x, p in pairs if x != u]
        notice = f"Login {u} removed — it stops working now."
    elif action == "key":
        key = f.get("key", "").strip()
        shape = check_key_shape(key)
        if shape:
            return _admin_page(user, err=shape)
        bad = _verify_key(key)
        if bad:
            return _admin_page(user, err="The key could not be verified, so the old one stays: " + bad)
        cur["OPENAI_API_KEY"] = key
        notice = f"Key replaced ({settings.key_hint(key)})."
    else:
        raise HTTPException(400, "Unknown action.")
    cur["AUTH_USERS"] = settings.pairs_to(pairs)
    try:
        settings.write(cur)
    except OSError as e:
        return _admin_page(user, err=f"The settings file could not be written ({e}). Nothing changed.")
    settings.apply(cur)
    return _admin_page(user, notice=notice)
