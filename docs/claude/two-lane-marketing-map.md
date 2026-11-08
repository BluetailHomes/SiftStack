# Two-lane marketing map (design only, 2026-11-07)

**Status: PAPER DESIGN. Nothing here is built.** No DataSift presets, tags, lists, or sequences exist for Lane B yet. No code or Actions variables were changed. Every build step below needs Kaycie's explicit go, and so do cap changes and new numbers. Don't flip any dry-run gate (`PM_TEXTS_LIVE`, `EXTRA_TEXTS_LIVE`, `AM_START_LIVE`, `TOPUP_LIVE`, `P1_ENRICH_LIVE`) as part of this work.

Rahaf-facing version (plain language, with a flow diagram): Claude Doc "How Bluetail Reaches More Owners: The Two-Lane Plan", https://claude.ai/artifact/FtA2X32zGRT3LFMne6vvNU

## Why

This follows Ty's Day 2 marketing session. Pick a list, pair channels with it, and layer them to reach as close to 100% of the list as possible. Bulk SMS goes first, then calls to the people who didn't reply, then mail to whoever is left. Ty's per-channel contact rates are his estimates. Bluetail measures its own numbers in a 2-week test (see Measurement below).

Bulk can't go into the blitz itself, for three reasons:
- At 150 records, the blitz already fills the SMS lines.
- Bulk replies would collide with drip enrollment.
- Rahaf's time is the scarce input. See [rahaf-supply-constrained audit] in memory.

## The two lanes

A record is in **one lane at a time**. Tags decide which one. Statuses stay with Lane A.

### Lane A: 5-day blitz (unchanged)

The flow, which `pm-text-automation.md` documents:
1. `weekly_topup.py` runs on Sunday and loads presets 01/02, which hold Batch, SMS Line A–E, and primary phone.
2. am-start sets Prospecting and starts the drip.
3. The morning text goes out automatically.
4. Rahaf calls and leaves voicemails.
5. The 4:30 voicemail texts go out.
6. The audit and the drip exit guard run.
7. When the blitz finishes, the record gets `P1 Hold – Post-Sequence`.

In the 2026-11-09 week this is 150 records, at `MAX_PER_LINE = 30` across 5 DRIP_LINES. That's 150 morning texts a day, so **Lines A–E are full**.

### Lane B: bulk SMS → power dialer → mail/deep

1. **Bulk SMS round.** One text per eligible owner, sent from dedicated bulk numbers (never DRIP_LINES).
2. **Power dial.** Owners who don't reply within the wait window go to a smrtPhone power-dial list, with at most 3 attempts.
3. **Mail or deep prospecting.** For owners still not reached, or with no usable phone.

Rules for Lane B:
- Lane B **never sets or changes a status**.
- A reply at any step becomes Rahaf's lead (tag `Bulk Reply`). It **never** goes into the blitz drip.
- `STOP – Review` and opt-outs follow the existing STOP policy (all seven FCC keywords, commit 6745121).

## Tags

Existing tags this design relies on: `Priority 1`, `Obituary`, `FTM`, `Batch YYYY-MM-DD`, `SMS Line A–E`, `Mail Only`, `Dial First`–`Dial Fourth`, and these two:
- `P1 Hold – Post-Sequence` (`11989878-10e7-49a9-a697-a5bbf28f324d`). This is the main entry into Lane B.
- `STOP – Review` (`f06c1d11-3132-475a-bd60-6f276b0d7da3`). Used to keep finished-blitz records out of Lane B when they had a real conversation or a do-not-call note. 2026-11-07: added to Thomas Wrenn ×2 (Text Only), Curtis Allen, Donnie Landes, Gurmej Gosal, Betty Fore (backup `output/account_backups/stop_review_before_2026-11-07.json`).

Proposed new tags (not created):

| Tag | Meaning |
|---|---|
| `Bulk SMS YYYY-MM-DD` | Got the bulk text on that date |
| `Bulk Dial` | Waited out the reply window with no reply; on the power-dial list |
| `Bulk Mail` | Done dialing with no contact, or no usable phone; goes to mail or deep prospecting |
| `Bulk Reply` | Replied or answered in Lane B; Rahaf's lead; all other Bulk tags come off |

## Presets (proposed folder "Bulk Lane"; not created)

| Preset | Filter |
|---|---|
| **B0 Bulk – Eligible** | Has (`P1 Hold – Post-Sequence`, or only `Dial Third`/`Dial Fourth` phones, or a second-wave rung not scheduled for the blitz). Status is empty or `prospecting` (no lead/dead status). Has no `Bulk *`, `STOP – Review`, `Text Only`, or `Mail Only` tag. Has at least one mobile not marked Wrong/Dead/DNC. A held record keeps its old `Batch *` / `SMS Line *` tags (KPI history), so those tags do **not** exclude it; a never-blitzed record still needs no `Batch *` tag. |
| **B1 Awaiting Reply** | `Bulk SMS` tag dated 1–5 days ago, and no `Bulk Reply` |
| **B2 Power Dial** | Has `Bulk Dial`, has `predictivecall_attempts` < 3, and no `Bulk Reply` |
| **B3 Mail / Deep** | Has `Bulk Mail`, or has `Bulk Dial` with 3+ attempts and no contact, or has no usable phone |
| **B4 Replies** | Has `Bulk Reply` |

Before building, check the filter keys against the live API. In particular, check that the call-attempt field and tag-date filters exist.

## Phone lines

- At 30/line/day, DRIP_LINES (A–E) are fully used by the blitz. **Bulk must not share them.** Per-line caps count every text the line sends.
- Kaycie bought **6 more smrtPhone numbers on 2026-11-06**. Recommendation: make them the dedicated Lane B bulk lines.
  - *Decision pending.*
  - Needs 10DLC registration on the campaign.
  - Warm up each number with a ramp of 20→30→40→50/day, gated per line on delivery, reply, and STOP rates. `src/sms_line_health.py` reports these and is read-only. Carrier filtering shows up as a low reply rate, not as failed sends. See the smrtphone-over-smarter-contact memory.
  - SMS Line tags only map to A–E today. Bulk lines need their own tag or line mapping rather than reusing the Line A–E round-robin.
- Before dialing, run `caller-reputation-monitor` on the numbers the power dialer uses.

## Code touch points (when approved)

1. **`weekly_topup.skip_reason()`** must skip any record with a tag starting `Bulk `. This is the most important change: without it, the Sunday top-up could pull Lane B records into the blitz. Today it skips status, `Mail Only`, `Batch *`, and `SMS Line*`.
2. **The SMS responder and the drip exit guard (`pm_text_sender.py`)** need two changes. On an inbound reply from a Lane B record, tag it `Bulk Reply` and remove `Bulk SMS*`/`Bulk Dial`/`Bulk Mail`. Never enroll the record in the drip.
3. **Line caps** need two changes. The cap logic (`MAX_PER_LINE`, `line_capacity()`, line-watch alerts at 20 then every 5) must cover the bulk numbers too, or the bulk sender needs its own per-line cap. Either way, every text on a line counts toward that line's cap.
4. **A bulk sender** needs a method decision. Options are smrtPhone bulk campaign or API loop. Whichever is chosen must respect weekdays only, 9 AM–7 PM Central, STOP handling, and the per-line cap.
5. **The KPI scorecard** should report Lane A and Lane B separately (reached, leads, STOP/wrong-number rate, Rahaf minutes per lead).

## Rollout (phased)

The 150-record week leaves Rahaf with little idle time, so the rollout goes in phases:

- **Now (week of 2026-11-09):** nothing new. Blitz only.
- **Phase 1: bulk SMS.**
  - Runs on `P1 Hold – Post-Sequence` records from the 6 new numbers, after 10DLC and warm-up.
  - It's automated, so only replies reach Rahaf.
  - Requires touch points 1–4.
- **Phase 2: power dialer.**
  - Starts only when the KPI scorecard's Gaps line shows real idle time. Then Rahaf gets a fixed power-dial block, with its length agreed with her.
  - Maximum 3 attempts per owner.
- **Phase 3: mail/deep.** For B3. The vendor and budget aren't decided.

## Measurement (2-week test per phase)

Compare the two lanes on these numbers:
- Owners reached (any reply or conversation) as a share of records worked
- Leads created
- STOP and wrong-number rate
- Rahaf minutes per lead
- Per-line delivery and reply rate

These numbers decide whether to raise per-line volume, add the power dialer, or hold.

## Open decisions (Kaycie)

1. **The 6 new numbers.** Dedicate them to Lane B bulk, or add them to the blitz rotation?
2. **B0 eligibility.** Post-Sequence hold only, or also Dial Third/Fourth-only records and second-wave rungs?
3. **Bulk sender method.** smrtPhone campaign or API loop?
4. **Power-dial cap and timing.** Is 3 attempts right, and how long a daily block for Rahaf?
5. **Phase 3.** Mail vendor and budget, or deep prospecting only?

The FTC DNC scrub was deferred with the risk accepted. It isn't reopened here.
