# Pre-Tulum Stabilization Tracker

**Deadline:** Friday, October 30, 2026. **Kaycie away:** Sat Oct 31 – Wed Nov 4 (fully unplugged). **Rahaf solo:** Mon Nov 2 – Wed Nov 4. **Kaycie back:** Thu Nov 5.

This file is the source of truth for the sprint, not CLAUDE.md. How to use it:

- One tracker item per Claude Code conversation. Start with "read `docs/pre-tulum-tracker.md`, work on item N".
- Before ending a session, update that item's status, evidence, blocker and next action, and add a line to the session log.
- Status values: `not started`, `in progress`, `blocked`, `verified`, `deferred`.
- Keep personnel/performance notes and credentials out of this file. They stay in private memory.

## Vacation operating rules (Nov 2–4)

**Rahaf continues:** seller conversations, callbacks, follow-ups, qualification, requesting photos, CRM documentation.

**Paused:** new offers, new negotiations needing Kaycie's approval, new purchase agreements, nonessential system changes.

**Final state Fri 10-30 ~5:45 PM CT (verified):** `AM_START_LIVE` true, `EXTRA_TEXTS_LIVE` false, `PM_TEXTS_LIVE` false, `TOPUP_LIVE` absent; all 5 Morning Texts drips off with 0 running (API `running_count`). Texting next week is manual by Rahaf (handoff v6, sent by Kaycie). No DataSift drips until support explains the 10/26 delay.

**Priority 1 launches:** **Mon 11-02: the pinned 45-owner batch is the intended launch (Kaycie, 10-30), still on HOLD until she authorizes each step** (LAUNCH_READINESS.md C1–C5). Variables verified by Kaycie 10-30: `AM_START_LIVE` false, `EXTRA_TEXTS_LIVE` false, `PM_TEXTS_LIVE` true, `TOPUP_LIVE` and `FRIDAY_DRIP_STOP_LIVE` not present. No batch expansion; the 75-owner plan is dead. Running Week 2 drips finish Fri 10-30 as scheduled. *(Superseded 10-27 line: "Mon 11-02 cohort: NO LAUNCH".)*

**Update 10-30:** Kaycie asked for a 45-owner Nov 2 batch on the existing cadence. The technical checks PASS read-only (pinned dry run: 45 of 45, 9 per line on A–E, 0 unreadable). **Revised late 10-30: Kaycie's preferred plan is to launch the pinned 45 on Mon 11-02, NOT yet authorized.** It needs her explicit go on each step in `output/p1_launch_readiness_2026-10-30/LAUNCH_READINESS.md` §7, plus answers to its 7 pre-launch questions, all before she leaves Sat 10-31. Without that, nothing launches and there's no retry (Rahaf's Plan B). The code is local branch `p1-launch-2026-11-02` (0d4ed07, unpushed).

**Still handled:** existing commitments, inbound replies, opt-outs/STOP. Sellers needing an offer or approval are documented for the Nov 5 return queue.

## Items

Priority order: 1 → 6. SMS reliability first, then the Week 1 P1 records.

### 1. SMS reliability

| Field | |
|---|---|
| Owner | Kaycie + Claude |
| Status | in progress (audit done 2026-10-27, fixes awaiting approval) |
| Evidence | Read-only audit 10-27 (GitHub run logs + DataSift `owner.sms.sent` logs). pm-texts ran LIVE on every dispatched weekday: 10-21 sent 35/fail 2 (landlines), 10-22 26/0, 10-26 23/0, 10-27 35/0. extra-texts: dry run 10-22; LIVE by 10-26 (4/0) and 10-27 (41/0). am-start LIVE: 10-26 set 72, 10-27 set 2. No send outside weekday 9–7 CT. **Line cap broken:** SMS Lines A/B/C sent 41/37/33 on 10-26 and 27/25/18 on 10-27, because the morning drip's later steps now fire ~4:40 PM, the same minutes as extra-texts, after its cap snapshot. Fri 10-23 had no dispatch (day-number bug, fixed 10-25); Fri 10-30 is the first Friday under the fix. Drip exit guard fails intermittently on DataSift HTTP 429. "Sent" = smrtPhone API accepted; delivery status is not available (DataSift `status` is null). |
| Blocker | Fix plan (10-27 evening) awaiting Kaycie's approval. Commit e50d69e (local, unpushed): Friday prep, Monday-only capacity-gated am-start, STOP policy, audit reliability. 10-27 overage on A/C was extra-texts (12/16) plus drip steps (13/7); drips alone max 13/line. 20/line is internal policy; provider limit unverified. 13 failed drips (9 properties, "no primary phone") are terminal and none are in the 11-02 plan. 9 AM move: recommend 9:05 CT after one clean Monday. |
| Next action | Fix plan 10-27: all 39 running drips finish Fri ~4:38 PM, so none send on the weekend. The 20 duplicates were a 4:32 PM voicemail text followed by drip step 2 eight minutes later, from a different line with a different message. Preset 01 is empty, and the 10-25 top-up ran as a dry run (TOPUP_LIVE empty). Before Friday: pause extra-texts (EXTRA_TEXTS_LIVE, needs approval); pm-texts skips records with a drip step due today; bounded 429 retry in `drip_exit_guard.search_records`; remove the stale re-enroll step. After Tulum: build the shared per-line budget (send log + drip steps due today). |
| Decisions 10-27 | `EXTRA_TEXTS_LIVE` stays false (no restart and no drip-reservation logic before Tulum). `AM_START_LIVE` stays false until Kaycie approves a validated Monday cohort. The 11-02 start stays at 9:50 AM CT, and the KPI report schedule is unchanged. `FRIDAY_DRIP_STOP_LIVE` stays unset. The 13 failed drips are terminal: report them, don't retry. STOP suppresses the requesting number; ambiguous identity is held for review, not suppressed owner-wide. |
| Top-up schedule | **Friday 6 PM CT from 2026-11-13** (Kaycie, 11-07). Sunday 11-08 still runs once (worker `onDate` entry), then Fridays only. Cron `0 0,23 * * FRI,SAT,SUN,MON`; drop SUN,MON after 11-08. Friday run starts with the drip check (report-only unless `FRIDAY_DRIP_STOP_LIVE`). `weekly-topup.yml` has no GitHub cron. |
| Exclusions (uncommitted, 10-27) | Hard exclusions in pm-texts (no automated text), extra-texts, am-start, top-up plan search + `skip_reason`, and Monday `validate`: STOP – Review `f06c1d11…` (5 records), P1 Hold – Post-Sequence `11989878…` (28; enrollment only), and Aram Damirjian (owner `52f78fea…`, no direct outreach). Validate and live am-start fail closed if either tag is missing, renamed or unreadable. Validate also excludes a record whose primary number or owner appears twice in the batch. Text Only does not block texts; it is a dialer rule, so enforcement there is manual. Live read-only test 10-27: 5/5 STOP – Review and 28/28 Hold excluded, Aram blocked, 3 Text Only records still textable, and 0 tagged records in the top-up search; 34/34 probe records in a scratch validate were excluded. Kyuhong/Julie/Chereena (Batch 10-19) stay ineligible after their drips finish (assigned, status, Batch/Line tags, drip history); regression test added. Preset 01/02 don't yet carry the two tags in `must_not` (DataSift write, needs approval). |
| Decisions 10-27 (late) | Sunday 6 PM CT top-up stays; no Friday switch before the trip. Safety-only ship plan approved to prepare, not to push: local branch `sms-safety-only` (worktree in scratchpad) = origin/main + `841c151` (e50d69e minus dispatcher/weekly-topup.yml schedule and audit monitoring) + `2d2c6e1` (exclusions) + optional `2418863` (audit alerts). Excludes 70301c2 (KPI) and 3e1ab2f (wholesale). 447 tests pass; Sunday dry run on the branch: 45 planned, exclusions firing. Preset 01/02 PATCH approved in principle: read-only diff shows only `filters.must.must_not.any_tags` changes (5 → 7, adds the two review tags); awaiting final approval. Aram Do Not Market tag deferred until it's verified not to suppress his realtor contact. Text Only stays a Lead Management dialer rule; Wrenn and Soler remain manual no-call. Read-only 10-27: all 39 running P1 drips have owner opt_out/dnc false and a non-DNC primary; none carry STOP – Review/Hold or are Aram. |
| Shipped 10-28 (~2 AM CT) | Preflight passed: ambiguous STOP blocks the number and holds the record in every sender (tests in test_friday_safeguards, test_extra_texts, test_drip_stop, test_unmatched_reply); push was a fast-forward of exactly 3 commits / 8 files; Slack webhook verified by the 10-27 audit post; 447 tests pass on `d33d6d6`. **origin/main = `d33d6d6`** (841c151, 2d2c6e1, d33d6d6 on top of 1de376c). Preset 01/02 PATCHed and read back: `must_not.any_tags` = original 5 + STOP – Review + P1 Hold, no other field changed (before snapshot: scratchpad `preset_before_snapshot_2026-10-28.json`). **Not done:** production dry-run dispatches (blocked by Claude's permission classifier); Kaycie to dispatch by hand or allow. Worker not redeployed; variables untouched. Open gap: STOP detection depends on the responder's LLM classifier (no keyword fallback); an outage longer than its look-back leaves a STOP unmarked. First responder run on `d33d6d6`: 40689056245 (12:35Z 10-28); runs up to 12:30Z were still on `1de376c`. |
| 10-28 morning | **STOP keyword fallback** committed `d8bf27d` on local branch `stop-keyword-fallback` (safety worktree, on top of `d33d6d6`); not pushed, awaiting approval. A whole-message opt-out ("STOP", "Unsubscribe", "Stop texting me", ...) is marked DNC on the replying number when the classifier fails or mislabels it; it is always flagged for a human, and the review/hold policy is unchanged. 27 new tests; 474 pass. **Last-seen variable values (10-27 run logs):** PM_TEXTS_LIVE true, EXTRA_TEXTS_LIVE **true**, AM_START_LIVE **true**, TOPUP_LIVE empty (10-25). The token can't read variables, so Kaycie checks them by hand. **Superseded by Kaycie's own check 10-30:** AM_START_LIVE false, EXTRA_TEXTS_LIVE false, PM_TEXTS_LIVE true, TOPUP_LIVE / FRIDAY_DRIP_STOP_LIVE not present. Expected at 9:50: a one-time false "audit for 10-27 missing" alert, because the 10-27 audit ran before the marker code existed. |

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
| Status | verified: handoff v6 (Google Doc 1KKVwu0Q…) sent to Rahaf by Kaycie 10-30; v2–v5 titled SUPERSEDED |
| Evidence | Google Doc "Rahaf — plan for Mon Nov 2 – Wed Nov 4 (FINAL 10-30, not yet shared)", owner-only, read back: https://docs.google.com/document/d/1wrZaEMIA3-NJcUme7EKtGGhQ8gCxbOn8yyHMs9NqQbY/edit. Older docs 1yaPaGIO…, 1h3tEs…, 1y_u5…, 1j3RPQp7… renamed "SUPERSEDED — do not share" (not trashed). Source `output/p1_launch_readiness_2026-10-30/RAHAF_HANDOFF.md`: Monday 10:30 check, "How statuses start texts", seller work first, training in the final paid hour of her shift. LAUNCH_READINESS.md: ordered go/no-go checklist (A decisions, B pre-checks, C1–C5 production steps with checks, no-go paths). TRAINING_READINESS.md: final paste-ins for 01, 02 and tracker rows 2/3/5. |
| Blocker | Kaycie's go/no-go (LAUNCH_READINESS.md checklist). Intended outcome: launch the 45 on 11/2; HOLD stays until she approves each step. B1 passed 10-30. Nothing executed. |
| Next action | Kaycie: answer A1–A5 (compliance/SMS items are unresolved, not approved), B2 after 4:45 PM Fri, approve or decline C1–C4 before Sat 10/31; paste the TRAINING_READINESS.md corrections; share doc 1wrZaEMI… last (C5). If stopped after C2, leave AM_START_LIVE false (it is false now). |

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

### P1 launch prerequisites (from the 10-30 readiness check)

- Owner-level suppression in `weekly_topup.py`: **built** on branch `p1-launch-2026-11-02` (0d4ed07), not pushed.
- Add `validate` and the `topup_ready_*` upload to `weekly-topup.yml`: **built** on the same branch, not pushed.
- Pinned batch (`--uuids-file`), and validate reads the Batch tag, not the plan file: **built** on the same branch.
- Decide whether the 90-day not_interested cool-off is owner-wide.
- National/MO DNC: Kaycie follows DataSift's outreach recommendations and isn't requesting a separate scrub for this batch. Whether the registry, the MO no-call list, TCPA consent and A2P 10DLC apply is not independently verified; that's Kaycie's decision.
- **Superseded late 10-30: Kaycie prefers the Nov 2 launch, pending approval (see LAUNCH_READINESS.md).** Earlier note: Branch, batch CSV, pinned dry run, plan, validation results and rollback are preserved in the worktree and `output/p1_launch_readiness_2026-10-30/` as the starting point for Nov 5 decisions. A later launch re-runs the pinned dry run and validate for the new date.
- No P1 strategy work before 11/5 (Kaycie, 10-30).

### SMS safety gaps (post-trip work, from the 10-28 production check)

None is fixed yet. Each needs a design and Kaycie's approval before any change.

1. **Opt-outs inside longer or profane messages aren't reliably marked DNC.** The responder sends them to a person as `unclear`. The d8bf27d fallback catches only a whole-message STOP. Example: 5412 College Ave, 10-28, marked by hand.
2. **Voicemail texts and drip texts can reach the same owner minutes apart, on different numbers.** On 10-28, 9 owners got a voicemail text at 4:34 PM and then a drip text at 4:41–4:42 PM.
3. **Manual texts can overlap with drip sends.** Rahaf's manual texts and the drip reached the same owner on the same day, including Jose at 6832 Cleveland on the same number.
4. **Duplicate drip records can text the same number twice.** An owner with two dripped properties gets two texts on one number (Nancy Caroline, Line C, 10-28).
5. **The 20-a-day per-line cap must count every sending source.** pm-texts checks the cap at 4:30 PM, but the drip's later steps fire at about 4:41 PM and manual texts aren't planned for. Line A reached exactly 20 on 10-28 (7 manual + 13 drip).

### OpenRouter per-task model routing (post-trip, architecture approved 10-29)

Kaycie approved the design but nothing is built. Today, `LLM_BACKEND=openrouter` in `src/config.py` switches every task to a single model. The plan is to let each `LLM_MODELS` entry name its own model, e.g. `openrouter:<model>`. The OpenRouter key works. As of 10-29 the account has $40 in credits with about $33 left.

- Claude stays the default for every task until that task has been tested.
- Benchmark cheap models against saved examples on accuracy, structured-output reliability, latency and actual token cost. Candidate tasks: `ocr_extraction`, `entity_research`, `obituary_support`, `situation_summary`.
- These stay on their current approved workflows: identity matching (`obituary_match`), seller-facing messaging (the SMS responder), compliance decisions and offer authorization.
- When entity research comes back ambiguous, a person reviews it.
- Add per-task cost tracking and a simple rollback (one config line per task).
- Production routing changes only after Kaycie approves the benchmark results.

### Post-Tulum priorities (documented 10-30, nothing started)

1. Five-day cadence vs. power dialing.
2. A larger rolling eligible pool.
3. Geographic filtering.
4. Probate and obituary sourcing layered into P1. **Changed 2026-11-07 (Kaycie): follow Ty's process.** Probate/obituary records go to deep prospecting (heirs or the PR found and made the owner, then their numbers worked), never straight to calling. `weekly_topup.skip_reason()` now holds back `Obituary`, `Probate`, `Pre-Probate/Deceased` and `Obit *` tags, so the top-up and P1 enrich skip them. The deep-prospecting track itself is not built yet.
5. Phone quality and enrichment.
6. Smarter Contact.
7. Centralized owner-level eligibility (one gate for every outreach path).
8. P1 architecture.

## Session log

- 2026-10-27 — Audit of auto-loaded context; slim CLAUDE.md brought to main; tracker created.
- 2026-10-27 — Item 1 read-only SMS audit: sends verified, per-line cap exceeded on Lines A–C (drip firing at 4:40 PM), Friday dispatch untested; no changes made.
- 2026-10-27 — Item 1 fix planning (read-only): weekend scan clean, duplicates explained, top-up/am-start vacation state checked; plan awaiting approval.
- 2026-10-27 — Committed e50d69e (not pushed, Worker not deployed). Read-only overage attribution, failed-drip check, 9 AM evaluation; EXTRA_TEXTS_LIVE must be off before 4:30 PM 10-28.
- 2026-10-27 — P1 exclusions (STOP – Review, Post-Sequence Hold, Aram) added to every SMS enrollment path and verified live read-only; top-up schedule verified as Sunday 6 PM CT in production; 531 tests pass. Uncommitted; nothing deployed.
- 2026-10-28 — Item 1: STOP keyword fallback committed locally (d8bf27d, unpushed, 474 tests); last-seen variable values read from 10-27 logs; today's verification plan written. 4:30 PM run review pending.
- 2026-10-28 — Production check of 10-28 (read-only): pm-texts LIVE sent 14, failed 0; extra-texts DRY RUN (14 planned, 0 sent); am-start ran nothing (not Monday). DataSift recorded 64 texts: 14 voicemail texts, 40 from the morning drip at 4:41–4:42 PM, and 10 manual texts from Rahaf. Line A had 20 (7 manual + 13 drip): at the cap, not over. 0 sends to STOP – Review, Aram or DNC; Nadine (1021 S Emery) carries P1 Hold, which blocks enrollment only. 5412 College Ave (owner 34139b06) sent a profane opt-out at 6:49 PM; the drip was removed but the number is NOT marked DNC; a person must mark it. 9 owners got a voicemail text and a drip text about 7 min apart. 7:15 PM audit posted. d8bf27d is still unpushed.
- 2026-10-28 — Approved DNC fix: phone 3478327732 (Conrrado Perez, owner 34139b06, 5412 College Ave, property f168aa5a) moved from CORRECT to CORRECT_DNC. An opt-out note was added to the record's message board. On read-back, owner opt_out/dnc are unchanged (False), tags are unchanged, and the record has no running drips. The production STOP policy now reads it as a confirmed owner STOP. No other record changed. Five SMS safety gaps were added to the Nov 5 queue.
- 2026-10-29 — Vacation-readiness audit (read-only, no DataSift writes). P1 universe is 1,209 records, not ~1,122; P1 tag = union of the 14 lists. 432 textable unassigned, but 80 sit only in lists outside the top-up's 7 RUNGS (77 Bad Credit + OOS 19.3, 3 Estate Sale 13.9) and 168 sit outside preset 01's 8 lists. The niche lists (1.2–9.6) have 0 textable left. Rahaf's cold P1 pool (42 records / 61 D1–D2 numbers) runs out by Mon 11/2 or Tue 11/3; after that it's follow-ups and tasks (101 open, ~36 overdue). A 45-record batch visible to preset 01 (44 from 18.9, 1 from 10.7; Lines A–E, 9 each) was staged as a scratchpad CSV for 11/9 or later. It was NOT launched, and Mon 11/2 stays NO LAUNCH. Phase 2 changes await Kaycie's approval.
- 2026-10-29 — New priority: stage 100+ P1 owners (staging only). Built `output/p1_staging/p1_staging_100_2026-10-29.csv` read-only: 100 unique owners and primaries (96 from 18.9, 3 Estate Sale 13.9, 1 from 10.7) plus 20 alternates; 97 operationally eligible, 3 candidates (Estate Sale isn't visible to preset 01); 0 cleared for activation. Proposed as two 50-record cohorts, because 100 at once fills every line's 20/day cap with drip texts alone and about doubles call load. Mon 11/2 is still NO LAUNCH; the earliest possible start is cohort A on Mon 11/9 after approval. The Nov 2–4 plan puts sellers first. Gaps found: Ryan Barker (Hot Lead) and Aristeo Delangel (P1 Cold Lead) have no open task.
- 2026-10-29 — Entity-owner research pilot, checkpoint 1 (read-only: no DataSift writes, no paid calls). There's no standalone DataSift "Entity Research" skill. Entity research lives in Deep Prospecting v5 (`references/entity-owners-enformion-business.md`: Enformion BusinessV2, then a reverse-address unmask, then the Secretary of State) and Buyer Prospector (SOS research guide, H/M/L confidence). No Enformion key is in `.env`. Re-classified the 1,209-record P1 pull: 449 records / 311 owner names are non-person (LLC 318/220, trusts 35/31, corp+other 19/18, gov/nonprofit 25, lender 10, IRA custodian 9, incomplete 8, and 25 records / 13 owners that are real people misfiled in the company field). About 63 entity records were classified after 10/16 and may lack the `Entity Owner` tag, which needs a read-only verify. Proposed a 25-owner / 37-record pilot (scratchpad). Awaiting Kaycie's approval.
- 2026-10-29 — Entity-owner pilot, checkpoint 2 (read-only: no DataSift writes, no paid lookups, no outreach; about $0.004 spent). Report: `output/entity_pilot_2026-10-29/REPORT.md`. Tag audit is resumable and covers 1,208 of 1,209 records (1 returns 404). **56 entity records (51 owners) are missing `Entity Owner`**: none is typed person, so the top-up can't pick them, but presets that rely only on the tag can expose them to manual dials. **11 records of real people wrongly carry the tag.** Proposed corrections for the 13 misfiled individuals are in `corrections_proposed_misfiled_owners.csv` (classification kept separate from eligibility; DNC/opt-out preserved; not executed). The recorder pilot graded 24 of 25 owners (Landmark wasn't researched because of a recorder crash): VERIFIED 3, LIKELY 13, AMBIGUOUS 3, NOT FOUND 5. That's a 12.5% verified rate, 67% at least LIKELY, and about 8 min of review per owner. 0 verified contacts and 0 DataSift numbers matched until a paid lookup is approved. Recommendation: an "Entity Research Needed" status instead of the blanket exclusion, plus MO SOS as a second source, then scale to LLCs and corps first. **Stopped for Kaycie's review.** Pending approval: tag fixes, a SmartSkip/Trestle run on the 16 LIKELY+ persons ($10 cap), and the SOS step.
- 2026-10-29 — Entity pilot, phase 2.
  - **Wrote `Entity Owner` to 56 entity records.** Recheck clean; tagged count 371→427; read-back OK on all 56. Rollback is in `output/entity_pilot_2026-10-29/tag_write_manifest_COMMITTED.json`.
  - **Held the tag removal on the 11 individual records.** The tag is their only exclusion, so removing it makes them P1-visible and one becomes top-up-eligible. Proposed pairing the removal with a hold tag; that needs separate approval.
  - **MO SOS is blocked** by a Cloudflare bot challenge (not bypassed). Aggregator search yielded little (about $0.02).
  - **Paid lookups not started** (they're gated on SOS). Funnel: 24 researched, 16 representatives, 3 corroborated, 0 contacts.
  - **Waiting on Kaycie** to decide how to do the SOS checks.
- 2026-10-29 — Entity pilot, phase 3: the 3 paid lookups, the hold audit and the SOS sheet.
  - **Paid lookups (3 VERIFIED owners only):** SmartSkip (about $0.48) plus Trestle (about $0.6–0.95). Pilot total is about $1.1–1.5 of the $10 cap.
    - 1 owner has a number independently associated with them.
    - 1 owner is low confidence: the vendors conflict and the numbers are household lines.
    - 1 owner has no match.
    - No CRM changes, no outreach, no DNC scrub. Details in `paid_lookup/paid_lookup_results.md`.
  - **Hold audit (read-only):** `hold_audit/HOLD_PROPOSAL.md`.
    - Proposed an "Entity Classification Hold" tag, excluded in 103 presets plus `outreach_block` and the top-up search.
    - Dry run: 0 of 11 records match anywhere.
    - Found 3 presets that already expose some of the 11 today (they don't reference `Entity Owner`).
    - Removing `Entity Owner` without the hold would put 1 record into the Sunday top-up.
  - **MO SOS manual pilot:** instructions and an 18-row sheet are ready for Kaycie or Rahaf, outside Rahaf's lead hours.
  - Nothing applied. **Waiting on Kaycie's approval of the hold implementation.**
- **2026-10-30, P1 strategy audit (read-only):** `output/p1_strategy_audit_2026-10-30/strategy_recommendation.md`.
  - Recommends adding supply first (Jackson probate as Queue 1), then one owner/phone eligibility gate, then the power dialer as a hybrid for first-touch.
  - No bulk SMS on cold lists.
  - Pilot designs and 7 decision requests are for after 11-05.
  - Nothing changed. Nov 2 NO LAUNCH stands.
- 2026-10-30 — Monday P1 readiness check, read-only: no DataSift writes, no variable changes, no pushes. The production dry-run top-up planned 75 owners (15/line). Passes: unique owners and phones, equity, entity names, DataSift DNC flags. Fails: 5 owners on owner-level suppression (1 running drip, 1 new_lead, 2 not_interested from 10/26, 1 unexplained prior attempt); capacity (cap 9/line = 45); and the production weekly-topup never runs validate, so am-start has no marker. FTC scrub not done. **NO LAUNCH stands for 11/2.** Training check: Drive access OK, script and Voice Memos steps present, Skool password unconfirmed, Drive wording conflicts listed. Two-outcome handoff drafted. Reports in `output/p1_launch_readiness_2026-10-30/`.
- 2026-10-30 — Full-pool launch-readiness check (read-only; no DataSift writes, no variable or code changes). 497 rung records → **312 owners pass every safeguard** (310 reachable by the top-up); 185 held, 0 unreadable. National DNC scrub not done and production top-up lacks owner-level suppression (replay: 7 of its 45 picks fail — 2 recent sales, 2 declined elsewhere, 3 active elsewhere) → **NO LAUNCH 11/2**. 75 exceeds capacity; largest compliant batch is 45, earliest 11/9 after DNC scrub, suppression fix and approval. All 35 running drips finish today. Fallback = RAHAF_HANDOFF.md Outcome B. Report: `output/p1_launch_readiness_2026-10-30/LAUNCH_READINESS.md`.
- 2026-10-30 — Nov 2 45-owner batch prepared (no DataSift writes, no variable changes, nothing pushed). Branch `p1-launch-2026-11-02` (0d4ed07, base origin/main): owner-level suppression, pinned `--uuids-file`, validate reads the Batch tag, validate step and marker upload in `weekly-topup.yml`, timeout 45 min; 451 tests pass. Pinned dry run: 45 of 45 ready, A–E 9 each, 0 skipped, 0 unreadable, about 12 min. **Technical PASS; launch HOLD pending Kaycie's approval.** Reports: `LAUNCH_READINESS.md` (rewritten), `TRAINING_READINESS.md` (paste-ready Drive corrections; Drive not edited). Handoff (no-batch version) created as a private Google Doc, owner-only, not shared. Post-Tulum priorities added above.
- 2026-10-30 — Final: Kaycie confirmed **HOLD, no Nov 2 batch**; nothing pushed, written or toggled. Skool access confirmed. Share-ready handoff doc created (owner-only, not shared); old draft trashed. TRAINING_READINESS.md corrections made definitive (no new P1 batch) with doc links. Running drips (35, read-only check): all at their final step, last text today ~4:41 PM CT, then complete; no weekend or 11/2+ drip sends. No P1 strategy work until 11/5.
- 2026-10-30 (late) — Kaycie revised: HOLD isn't her preferred outcome. The preferred outcome is the pinned 45 launching Mon 11-02, but it isn't authorized yet. RAHAF_HANDOFF.md is rewritten as one document: a 10:30 AM Monday check in preset 01 (Batch 2026-11-02 records in Prospecting means Plan A, otherwise Plan B), calls first, follow-through midday, training 5–6 PM CT, and Rahaf never sets Prospecting. LAUNCH_READINESS.md is reframed and gains 7 pre-launch questions; the DST check on the am-start crons is OK. BATCH_READINESS.md is marked superseded. TRAINING_READINESS.md paste-ins moved to 5–6 PM. Nothing pushed, written or toggled.
- 2026-10-30 (final) — Handoff finalized per Kaycie: training moved to "the final paid hour of your scheduled shift" everywhere (no clock time confirmed); morning order = fresh seller calls, promised callbacks, active leads; "How statuses start texts" added (read-only check of 27 sequences: only Prospecting + SMS Line A–E tag starts a drip; New Lead = task/card/assign only; SiftLine moves never set Prospecting). Google Doc replaced (new id 1yaPaGIO…, owner-only, read back; old trashed). LAUNCH_READINESS.md gains the ordered go/no-go checklist; verified production am-start fails closed without an 11/2 marker. Nothing pushed, written, toggled or shared.
- 2026-10-30 (review draft) — Per Kaycie: handoff schedule set to explicit times (10–1 calling/outreach with fresh conversations + promised callbacks first; 1–5 existing leads, eligible legacy outreach, photos, follow-up, offer prep; 5–6 training with Voice Memos + tracker). Monday check now covers a partial start (work the Prospecting records only; leave no-status batch records alone; report counts). Never-Prospecting rule kept. New Google Doc 1h3tEs-Rfhpn1aMqhEaJ6XY4SOqqfcESQWjps_GywYqo created, owner-only, read back, NOT shared; previous doc not trashed. Verified read-only that all 45 batch records carry the Priority 1 tag, so the existing drip exit guard covers them. Batch stays on HOLD; nothing pushed, written or toggled.
- 2026-10-30 (final v3) — Handoff additions per Kaycie: (1) A new section 'Four analyzed properties: discovery calls, not offers'. These are seller-discovery and Intuitive Sales practice calls. Rahaf explores motivation, timeline and price flexibility, gives no numbers, takes notes, and flags promising ones for Nov 5. The four are Montgall (DataSift d94a93d5…) and E 73rd (8cf4823f…), each with an exact link. Spruce and Quincy have NO DataSift record: a read-only scan of all 6,199 properties found neither address. They are framed as listing-agent calls, and Rahaf must not create records. No agent name or phone is on file. (2) Plan A: Tue/Wed continue the normal five-day cadence for records started Monday; Thu/Fri continue under normal procedures after Kaycie is back; never manually start or restart held records. (3) A 'something goes wrong' example now covers automatic texts that continue after STOP or after a record leaves Prospecting. The opt-out and don't-troubleshoot steps are unchanged. New doc 1tRpMpfoXNcjK5hvejM5JzXg7gj-L1eaS9B7ZA59Dowg is owner-only and read back clean. 1wrZaEMIA3 was renamed SUPERSEDED, not trashed. NOT shared. Nothing pushed, written to DataSift or toggled.
- 2026-10-30 (final draft v2) — Per Kaycie's corrections: the Monday check had a bug, because preset 01 excludes Prospecting and can never show started records. Rahaf now counts preset 02 (started) and preset 01 (held, left untouched), works only the started records, and never moves records into Prospecting. 'Your day' blocks now set priorities, not limits (seller calls and eligible legacy outreach at any time). The opt-out steps were verified against drip_exit_guard + stop_policy: mark DNC, then status Not Interested (guard removes the drip within ~5 min), then note, then Nov 5 summary. Other problems: stop, document, no settings/automation changes or troubleshooting. Formatting: nested lists flattened, filename example de-escaped. New Google Doc 1j3RPQp7WiGS9QtYZ6gnuq2DowRJvSBVwFBDRVHZmcY0, owner-only, read back, NOT shared. Earlier docs (1y_u5…, 1h3tEs…, 1yaPaGIO…) not trashed. LAUNCH_READINESS.md lines 56/222 match. Batch stays on HOLD; nothing pushed, written or toggled.
- 2026-10-30 (implementation) — Kaycie verified the variables by hand: AM_START_LIVE false, EXTRA_TEXTS_LIVE false, PM_TEXTS_LIVE true, TOPUP_LIVE and FRIDAY_DRIP_STOP_LIVE not present (B1 PASSED in LAUNCH_READINESS.md). Read-only bypass check: no GitHub Environments, no org variables (user account), and live sends need `vars.X == "true"`. The real bypass paths are a person setting Prospecting + SMS Line tag in DataSift (native drips) and local `--send` runs; added to LAUNCH_READINESS.md. Kaycie made the pinned 45 the intended launch, so the handoff, readiness and training paste-ins were reframed (batch = the plan, Plan B only if it didn't start). New handoff doc 1wrZaEMIA3-NJcUme7EKtGGhQ8gCxbOn8yyHMs9NqQbY is owner-only and read back; the 4 older drafts were renamed SUPERSEDED. Nothing pushed, written to DataSift, toggled or shared.
- 2026-10-30 (close) — Handoff v6 created (Kaycie's edits kept, training time unspecified, 10 AM preset check + 10:15 recheck, manual texts + second-number morning texts), v5 renamed SUPERSEDED; Kaycie sent v6 to Rahaf. Read-only checks at ~5:45 PM CT: all 5 P1 Morning Texts drips active=false, running_count 0 (completed 55, failed 13); the DataSift list page "Active" column (35) is not a running count. Kaycie set PM_TEXTS_LIVE=false; AM_START_LIVE true, EXTRA_TEXTS_LIVE false, TOPUP_LIVE absent (screenshot). Dani follow-up sent.
- 2026-11-06 — Built `src/p1_enrich.py` + step 2b in `weekly-topup.yml`: Trestle-scores and Tracerfy-traces SiftMap auto-added P1 records so Friday's top-up can batch them. Read-only dry run: 63 records to score, 66 to trace (122 owners, 195 phones, ~$5), 461 LLC/trust records reported not traced. 12 new tests; 556 pass. Uncommitted, nothing paid or written; live needs P1_ENRICH_LIVE + TRESTLE_API_KEY/TRACERFY_API_KEY secrets and Kaycie's go.
- 2026-11-07 — P1 enrich committed + pushed alone (5179e34 on origin/main; parked local commits still unpushed). Live backlog run stopped at scoring: Trestle key in `.env` returns 403 AUTHENTICATION_FAILED. Tracerfy queue 221705 already paid (59 owners, 48 credits); nothing written to DataSift. Script now scores before tracing and takes `--tracerfy-queue`. Next: new Trestle key (.env + GitHub secret), then `python src/p1_enrich.py --live --tracerfy-queue 221705`.
- 2026-11-07 — **18 open VS Code sessions consolidated.** Each session wrote a handoff to `docs/session-handoffs/siftstack-*.md`; the merged list is in `docs/session-handoffs/CONSOLIDATED_2026-11-07.md` (urgent items, blockers, legal flags, repo state, grouped decisions). Top items:
  - 3614 E 73rd: the owner's "$165K, no negotiation" text from 10/27 has had no human follow-up.
  - The Sun 11/8 top-up runs origin/main code only. The local deceased skip and the Lines F–K worktree are not live.
  - Mon 11/9 is the first automated drip day (Morning Texts A–E turned on 11/06). Check send times.
  - Push 6745121 before 11/25 by cherry-picking onto origin/main; local main has diverged by 6 and 5 commits.
  - Probate scraper returns 0 (two runs).
  - GitHub token gives 401; Trestle key gives 403.
  - db's `last_run.json` and `seen_ids.json` were restored after its dry run.
  - Nothing committed, pushed, toggled or written to DataSift.
- 2026-11-07 (evening) — Synced local main to origin/main (backup branch `backup/local-edits-2026-11-07`), pushed 5 commits through 27213ad, Trestle header fix a48a466, P1 enrich backlog live (115 owners, 89 dialable, ~$9.18). Ported the probate/obituary deep-prospecting gate into `weekly_topup.skip_reason()` and these doc edits. Weekly top-up moving to Friday 6 PM CT after the Sun 11-08 run.
