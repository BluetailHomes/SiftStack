# Pre-Tulum Stabilization Tracker

**Deadline:** Friday, October 30, 2026. **Kaycie away:** Sat Oct 31 – Wed Nov 4 (fully unplugged). **Rahaf solo:** Mon Nov 2 – Wed Nov 4. **Kaycie back:** Thu Nov 5.

This file is the source of truth for the sprint, not CLAUDE.md. How to use it:

- One tracker item per Claude Code conversation. Start with "read `docs/pre-tulum-tracker.md`, work on item N".
- Before ending a session, update that item's status, evidence, blocker and next action, and add a line to the session log.
- Status values: `not started`, `in progress`, `blocked`, `verified`, `deferred`.
- Keep personnel/performance notes and credentials out of this file. They stay in private memory.

## Vacation operating rules (Nov 2–4)

**Rahaf continues:** seller conversations, callbacks, follow-ups, qualification, requesting photos, CRM documentation.

**Paused:** new offers, new negotiations needing Kaycie's approval, new purchase agreements, new Priority 1 launches, nonessential system changes.

**Still handled:** existing commitments, inbound replies, opt-outs/STOP. Sellers needing an offer or approval are documented for the Nov 5 return queue.

## Items

Priority order: 1 → 6. SMS reliability first, then the Week 1 P1 records.

### 1. SMS reliability

| Field | |
|---|---|
| Owner | Kaycie + Claude |
| Status | not started |
| Evidence | Jobs: `pm-text-sender.yml` (am-start 9:50, voicemail texts 4:30, extra-texts, line-watch, call-watch, 7:15 audit), `sms-auto-responder.yml` (5-min poll), `drip-exit-guard.yml`; all triggered by the Cloudflare Worker in `dispatcher/`. Unmatched-reply + STOP→DNC handling merged to main Oct 26–27 (`c35d3b3`, `1bd4abe`). Live gate values (`PM_TEXTS_LIVE`, `EXTRA_TEXTS_LIVE`, `AM_START_LIVE`) and run history since Oct 26 **not yet checked**. Details: `docs/claude/pm-text-automation.md`, `docs/sms_reply_rules.md`. |
| Blocker | Unknown until run history and delivery statuses are pulled. |
| Next action | Pull workflow run history Oct 26–now + smrtPhone delivery statuses; verify each path: AM texts, PM voicemail texts, per-line cap (20/day), inbound replies, STOP/DNC. Define a manual fallback Rahaf can run if a job fails Nov 2–4. |

### 2. Priority 1 lifecycle

| Field | |
|---|---|
| Owner | Claude (trace), Kaycie (decide routing) |
| Status | not started |
| Evidence | Week 1: 50 records, Oct 19–23. Week 2: 72 records, Oct 26–30. Cadence and rungs: `docs/claude/jackson-build-sheet.md`. |
| Blocker | Unknown where records go after the 5-day sequence. |
| Next action | Trace the Week 1 records in DataSift (status, tags, lists, drip/sequence membership) and list any that are stranded. Then the same for Week 2 before Friday. |

### 3. Lead management / SiftLine

| Field | |
|---|---|
| Owner | Claude (audit), Kaycie (decisions) |
| Status | not started |
| Evidence | No project doc yet. Known concerns: crowded New Lead (Unqualified) stage, Legacy Triage, Rahaf's Records page filters. |
| Blocker | — |
| Next action | Audit board routing and stage counts; flag urgent sellers buried in the backlog so they are in the Nov 2–4 plan or the Nov 5 queue. Write concise findings to `docs/claude/lead-management-siftline.md` (Wednesday). |

### 4. KPI accuracy

| Field | |
|---|---|
| Owner | Claude |
| Status | not started |
| Evidence | Scorecard `src/kpi_scorecard.py` (built Oct 26), feeds the 7:15 PM audit and the Sunday top-up summary. Rahaf dials in smrtPhone as "Britney". 150-dial target stays unchanged. |
| Blocker | — |
| Next action | Reconcile smrtPhone outbound calls (Britney/Rahaf) against DataSift call logs for Oct 26–28; fix any gap in the scorecard and the 7:15 audit. |

### 5. Coaching report

| Field | |
|---|---|
| Owner | Claude, Kaycie reviews |
| Status | not started |
| Evidence | Cold-call coach daily report (branch `cold-call-coach-cloud`, commit `533f6d7`, not yet on main). |
| Blocker | Depends on item 4: system failures must be identified before anything is called a performance issue. |
| Next action | Make the report separate verified performance issues from system failures, with supportive, actionable wording. |

### 6. Vacation handoff

| Field | |
|---|---|
| Owner | Kaycie (approve), Claude (draft), Rahaf (run) |
| Status | not started |
| Evidence | Operating rules above. |
| Blocker | Items 1–3 shape what Rahaf can rely on. |
| Next action | Draft a Nov 2–4 day-by-day operating plan (what runs automatically, what Rahaf checks, what to do if a job fails) and a Nov 5 return queue template. |

### 0. Claude Code workspace cleanup

| Field | |
|---|---|
| Owner | Claude |
| Status | verified (minimal scope) |
| Evidence | Slim CLAUDE.md (~13 KB, was ~150 KB) cherry-picked onto main as `5b2c3f2`; this tracker created. |
| Blocker | — |
| Next action | Optional: split SMS/KPI docs out of `docs/claude/pm-text-automation.md` only if it doesn't delay item 1. **Deferred until after Tulum:** memory cleanup, skill deduplication, further CLAUDE.md trimming. |

## Nov 5 return queue

Sellers needing offers/approvals, and anything else Rahaf parks for Kaycie, go here (address/record link + one line of context; no personnel notes).

- _(empty)_

## Session log

- 2026-10-27 — Audit of auto-loaded context; slim CLAUDE.md brought to main; tracker created.
