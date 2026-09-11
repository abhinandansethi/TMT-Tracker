"""The service's settings file, read and written by the service itself.

/var/lib/tmt-radar/settings.env is a systemd EnvironmentFile: KEY=VALUE lines, a double-quoted
value may span lines. It is the only place secrets live. It sits in the service's own state
directory, not /etc, because the unit's ProtectSystem makes /etc read-only and this file is
rewritten by the service itself. Setup and the admin page write it from the browser so
that nobody needs a terminal to give the service its key or to add a partner's login; every
write is applied to os.environ at once (the pipeline reads the environment at call time and jobs
copy it per subprocess), so no restart is needed for a change to take effect.

Usernames are [A-Za-z0-9._-]; passwords may not contain a double quote, a backslash or a newline —
the three characters systemd would reinterpret inside a quoted value. Passwords are stored as
given (the same AUTH_USERS the Vercel middleware read), so the file's 0600 mode is the protection.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Tuple

ENV_FILE = Path(os.environ.get("TMT_ENV_FILE") or "/var/lib/tmt-radar/settings.env")
USER_RE = re.compile(r"^[A-Za-z0-9._-]{1,40}$")
KEYS = ("OPENAI_API_KEY", "AUTH_USERS", "TMT_ADMIN_USER", "TMT_SETUP_TOKEN",
        "TMT_SCAN_MODEL", "TMT_SCAN_MODEL_STRONG", "TMT_ASK_MODEL", "TMT_NO_COMMIT", "TMT_SCHEDULER")

HEADER = """# Delta Scanner — service environment. Written by the service's setup and admin pages;
# editing by hand also works (then: systemctl restart tmt-radar). Owned by the service user, mode 0600.
# AUTH_USERS: one login per partner, user:password per line, inside the quotes.
# TMT_ADMIN_USER: the one login that may open /admin and manage the others.
# Model names empty = the code's default (gpt-5.6-luna). TMT_NO_COMMIT=1 stops the audit-trail commits.
"""


def read(path: Path = None) -> Dict[str, str]:
    """Parse the file the way systemd does for the subset we write: KEY=VALUE, '#' comments,
    a double-quoted value that may run over several lines."""
    p = path or ENV_FILE
    out: Dict[str, str] = {}
    if not p.exists():
        return out
    text = p.read_text(encoding="utf-8")
    i, n = 0, len(text)
    while i < n:
        j = text.find("\n", i); j = n if j < 0 else j
        line = text[i:j]; i = j + 1
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1); k = k.strip(); v = v.lstrip()
        if v.startswith('"'):
            end = v.find('"', 1)
            if end >= 0:
                out[k] = v[1:end]
            else:                       # quoted value continues on following lines
                buf = v[1:]
                while i <= n:
                    j = text.find("\n", i); j = n if j < 0 else j
                    seg = text[i:j]; i = j + 1
                    end = seg.find('"')
                    if end >= 0:
                        buf += "\n" + seg[:end]; break
                    buf += "\n" + seg
                    if i >= n: break
                out[k] = buf
        else:
            out[k] = v.rstrip()
    return out


def _quote(v: str) -> str:
    return '"' + v + '"' if ("\n" in v or " " in v or not v) else v


def write(values: Dict[str, str], path: Path = None) -> None:
    """Rewrite the whole file from `values` (every known key, empty when absent), atomically, 0600."""
    p = path or ENV_FILE
    body = HEADER + "".join(f"{k}={_quote(values.get(k) or '')}\n" for k in KEYS)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".tmt-radar.env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try: os.unlink(tmp)
        except OSError: pass
        raise


def apply(values: Dict[str, str]) -> None:
    """Make the running process see what was just written (and what jobs will inherit)."""
    for k in KEYS:
        v = values.get(k) or ""
        if v:
            os.environ[k] = v
        else:
            os.environ.pop(k, None)


def pairs_from(s: str) -> List[Tuple[str, str]]:
    out = []
    for entry in re.split(r"[\n,]", s or ""):
        entry = entry.strip()
        if ":" in entry:
            name, pw = entry.split(":", 1)
            if name.strip() and pw:
                out.append((name.strip(), pw))
    return out


def pairs_to(pairs: List[Tuple[str, str]]) -> str:
    return "\n".join(f"{u}:{p}" for u, p in pairs)


def check_user(name: str) -> str:
    if not USER_RE.match(name or ""):
        return "A username is 1–40 letters, digits, dots, dashes or underscores."
    return ""


def check_password(pw: str) -> str:
    if len(pw or "") < 10:
        return "A password needs at least 10 characters."
    if any(c in pw for c in '"\\\n\r'):
        return "A password may not contain a double quote, a backslash or a line break."
    return ""


def key_hint(key: str) -> str:
    """What the page may show of a key: whether it is set and its last four characters."""
    return f"set · ends …{key[-4:]}" if key and len(key) >= 8 else ("set" if key else "not set")


if __name__ == "__main__" and "--selftest" in os.sys.argv:
    import io
    d = Path(tempfile.mkdtemp()); p = d / "env"
    write({"OPENAI_API_KEY": "sk-abc12345", "AUTH_USERS": "abhi:p w:1\npriya:x y z 10", "TMT_ADMIN_USER": "abhi"}, p)
    assert oct(p.stat().st_mode & 0o777) == "0o600"
    r = read(p)
    assert r["OPENAI_API_KEY"] == "sk-abc12345", r
    assert r["AUTH_USERS"] == "abhi:p w:1\npriya:x y z 10", repr(r["AUTH_USERS"])
    assert r["TMT_ADMIN_USER"] == "abhi" and r["TMT_SETUP_TOKEN"] == "", r
    assert pairs_from(r["AUTH_USERS"]) == [("abhi", "p w:1"), ("priya", "x y z 10")]
    assert pairs_to(pairs_from(r["AUTH_USERS"])) == r["AUTH_USERS"]
    # the installer's hand-written shape parses too
    p.write_text('OPENAI_API_KEY=\nAUTH_USERS="a:b\nc:d"\nTMT_NO_COMMIT=0\n')
    r = read(p); assert r["AUTH_USERS"] == "a:b\nc:d" and r["TMT_NO_COMMIT"] == "0" and r["OPENAI_API_KEY"] == "", r
    assert check_user("abhi.s") == "" and check_user("ab hi") and check_user("") 
    assert check_password("short") and check_password('ten"chars!!') and check_password("tenchars!!") == ""
    assert key_hint("") == "not set" and key_hint("sk-abcdefgh") == "set · ends …efgh"
    # apply: set and unset
    apply({"TMT_SETUP_TOKEN": "t"}); assert os.environ["TMT_SETUP_TOKEN"] == "t"
    apply({}); assert "TMT_SETUP_TOKEN" not in os.environ
    print("settings selftest: PASS")
