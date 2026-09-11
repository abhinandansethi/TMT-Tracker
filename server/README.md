# The service — Delta Scanner on one machine

Everything the tracker did across GitHub Actions, Vercel functions, Vercel hosting and a
personal access token, as **one Python process on the firm's own VM**. No GitHub in the
runtime path, no Vercel, nothing on anyone's laptop, no personal token anywhere.

    uvicorn server.app:app --host 127.0.0.1 --port 8080

| Used to be | Now |
|---|---|
| Vercel static hosting | this process serves `dist/`, rebuilt after every job |
| Vercel Edge middleware (Basic Auth) | the same Basic Auth here — `AUTH_USERS`, one `user:password` per line |
| `api/*.js` functions (Node twins of the Python pipeline) | `/api/*` routes calling the pipeline modules directly (`server/assist.py`) |
| GitHub Actions running `pipeline.scan.run …` and the sweep | a job queue and one worker inside the process (`server/jobs.py`), running **the same commands** |
| GitHub as the audit trail | a commit to the **local** git repository after every job |

What did not change, on purpose: the files (`scans/`, `data/scans/`, `engine/health.json`), the
builders, the CLI commands and their exit codes, the page's JavaScript (it posts to the same
`/api` paths and reads the same status shape), and the rule that **nothing runs unless a person
pressed a button** — with one deliberate exception. A scan whose definition carries `schedule`
(`{daily_at, tz, set_by, set_on}`, set in the dialog, off by default) is run once a day by the
scheduler thread in `server/jobs.py`: one run per scan per local day, never while a job for it
is already queued, never for a demo scan, stamped `scheduled_for` and attributed to the schedule
in the jobs table so it is never mistaken for a person's request. `TMT_SCHEDULER=0` switches the
loop off machine-wide. The decision and its legal implications are recorded in
`engine/audit/scheduling_decision_2026-09-10.md`; the TMT India sweep is not scheduled.

## Install on Ubuntu

From a laptop that has run `az login`, one command does all of the below and puts TLS in front:

    bash server/deploy/push.sh Work WORK_GROUP tmt-radar.centralindia.cloudapp.azure.com

By hand:

    sudo git clone <the repo> /opt/tmt-radar          # or rsync a checkout there
    cd /opt/tmt-radar && sudo DOMAIN=radar.example.com bash server/deploy/install.sh
    # then open https://<domain>/setup in a browser with the one-time setup code the installer printed

`DOMAIN` is optional; without it the service answers on the VM's IP over plain HTTP, which is
fine inside a private network and not fine on the public internet — give it a name and certbot
puts TLS in front. The installer is idempotent: `git pull` and run it again to update.

## The environment file

`/var/lib/tmt-radar/settings.env` is the only place secrets live (0600, owned by the service).
Nobody needs a terminal to fill it: on first run `/setup` (gated by a one-time code the
installer prints) takes the OpenAI key — verified against the pipeline's model before it is
saved — and the first login, which becomes the admin; afterwards the admin's **Logins** page
(`/admin`) adds or removes partners' logins and replaces the key. Every change is written
atomically and applied in-process, so no restart. Editing by hand still works; then restart.
It is not in `/etc` because the unit's `ProtectSystem=full` makes `/etc` read-only to the
service, and this file is the service's to write.

    OPENAI_API_KEY=sk-…
    AUTH_USERS="abhi:…
    priya:…"
    TMT_SCAN_MODEL=            # empty = the code's default (gpt-5.6-luna)
    TMT_SCAN_MODEL_STRONG=
    TMT_NO_COMMIT=0            # 1 disables the audit-trail commits

Adding a partner is one line in `AUTH_USERS` and `systemctl restart tmt-radar`. A partner needs
nothing but the URL and their pair.

## Jobs

`POST /api/scans {action, scan_id, scan?, finding?, no_discover?}` and `POST /api/sweep` enqueue
a job and answer 202 at once. Jobs run one at a time in the order asked, as a subprocess of the
exact CLI the workflows ran — `pipeline.scan.run create|run|delete|promote|dismiss` or the sweep
chain (`tracker.py sweep` → `export` → `brief.py --limit 40`) — then the pages are rebuilt and
the changed files committed locally. `POST /api/scans {action:"status", scan_id?, workflow?}`
returns the newest jobs in the shape the page already reads; `GET /api/jobs/<id>/log` is the run
log, which is what the page's "open the run log" link now opens.

Exit codes are the CLI's: 0 succeeded · 1 a source FAILED after the results were written (the
results are on disk and rebuilt; the job says *failure* so the card goes red and the log names
the source) · 2 refused before doing anything (the definition was wrong; the log says how).

The queue is SQLite at `server/jobs.db`; logs are `server/logs/<job>.log`. A job that was in
progress when the service stopped is marked *cancelled* on the next start — it did not finish,
and the service says so rather than leaving it "running" for ever.

## Model routes

`/api/propose`, `/api/discover`, `/api/propose-filter`, `/api/ask`, `/api/draft` — all in
`server/assist.py`, all calling `pipeline/scan/common.structured` and the pipeline's own modules,
so the prompt the browser gets is the prompt the pipeline uses. `ask` and `draft` read the ledger
row and the stored text from disk; every passage the model quotes is verified as a substring of
that text or discarded, and a draft's trailer is appended in code if the model forgot it.

`/api/health` is the one unauthenticated route: `{ok, service, worker}` and nothing else.

## Running it locally

    AUTH_USERS="abhi:1234" TMT_NO_COMMIT=1 engine/.venv/bin/uvicorn server.app:app --port 8080

Without `OPENAI_API_KEY` the model routes answer 501 and a scan job fails at the preflight with
the sentence naming the fix — which is the honest behaviour, not a bug.
