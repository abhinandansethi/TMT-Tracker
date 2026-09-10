# Decision record — scheduled (unattended) runs, 2026-09-10

**Decided by:** Abhinandan Sethi, in the open, when asked directly.
**Options put:** human-triggered only (recommended) · a daily schedule, knowingly · decide later.
**Chosen:** a daily schedule, knowingly.

## What changes

A scan's definition may carry `schedule: {daily_at: "HH:MM", tz, set_by, set_on}`. When it does,
the service on the firm's VM enqueues one `run` for that scan per local day at that time — the
same `pipeline.scan.run run` a person would have started, with the same budgets, the same
politeness, the same reporting. Everything else stays human-triggered: creating a scan,
discovery and gating, promoting a Miscellaneous finding, the TMT India sweep.

Safeguards that ship with it, all in `server/jobs.py`:

* **Off unless set.** No scan has a schedule until a partner sets one in the dialog; a demo scan
  can never be scheduled; `TMT_SCHEDULER=0` switches the loop off machine-wide without touching
  any definition.
* **Visible.** The scan page says *Runs daily at 06:30 IST — set by abhi on 11 Sep*, and the
  landing page no longer claims nothing is scheduled.
* **Attributed.** Every scheduled job is stamped `scheduled_for` and `requested_by: schedule set
  by <who>` in the jobs table, so the audit log never shows a scheduled run as a person's request.
* **Bounded.** One run per scan per local day, however often the loop ticks or the service
  restarts; never while that scan already has a job queued or running.

## What it means for the legal position

The per-source legal analysis (`engine/audit/legal_analysis_2026-08-27.json`, the terms review
of 2026-09-03, and `docs/horizon-design.md` §1.3) was written on the footing that collection is
**occasional and human-initiated**. That footing mattered in two places: the IT Act s.43(e)/(f)
proportionality reasoning (load that is "supervised, on demand" rather than "unattended and
repetitive"), and the characterisation of the tracker as research use rather than a standing
crawl.

A daily unattended run of a scan's approved sources changes that footing for those sources. It
does not change the manners of the fetch — honest identifying User-Agent, robots.txt enforced
per request, ~1 request a second, the same caps — and one run a day of a handful of listing
pages is still a very small load. But "small" is a different argument from "human-initiated",
and the analysis should be re-read with that in mind before a scheduled scan is pointed at any
venue whose terms condition access on non-automated use.

**Action:** treat every source approved into a scheduled scan as needing the 2026-09-03 terms
read applied with "unattended daily" substituted for "human-triggered". The gate's ToS scan
already parks anti-automation language as *pending* for a human; that behaviour is now
load-bearing rather than belt-and-braces.

**Not scheduled, deliberately:** the TMT India sweep. Its 51 vetted sources were reviewed on the
human-triggered footing and it is the firm's flagship coverage; leaving it on *Update now* keeps
that analysis intact while the scan layer gains scheduling.
