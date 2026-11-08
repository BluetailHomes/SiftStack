# Ty/Tyler deep-prospecting SOP vs. our tooling (2026-11-07)

Pulled from the DataSift 4-week workshop transcripts in `output/transcripts/`. Timestamps are `[date hh:mm:ss]`. The captions are auto-generated, so names are garbled in places (e.g. "Trussell" = Trestle). Read this before building the Jackson Obituary track or restarting probate supply. Nothing in it is built yet.

## Headline

**Ty runs the same skill we have.** His Oct 27 demo prompt was "run the Deep Prospecting skill version 5", using the SmartSkip bulk workflow and the Trestle API key, then pasting the results to the message board and making a PDF [10-27 00:46:44]. The engine is `deep-prospecting-v5`. What we don't have yet is everything around it: the DataSift presets that pick records, the write-back, the scheduling, and the outreach that follows.

Why it's worth doing: owner-skipped obituary and probate records reach about 5–10% of people; deep-prospected ones reach 47–70% [10-13 01:51:03, 10-29 00:45:16, 10-22 01:11:20]. Ty expects lead volume to "probably triple" [10-29 00:45:36]. Our probate-era contact rate was 6.0%.

## Their workflow, in order

1. **Select records (DataSift preset).** The preset lives in a "prospecting" folder [10-27 00:43:56]:
   - Includes: obituary flag; last obituary date within about 2 years; **at least 3 months since the obituary** [10-27 00:44:45–51]. "3 months is the sweet spot", set as a rolling preset rule, not a manual wait [10-29 00:49:29].
   - Excludes: low/negative equity, recently sold, under contract/closed. Optionally excludes records with a probate filing, which leaves "obituary filed, probate never done" — the ~70% of cases where there's "usually an underlying issue" [10-29 00:44:48, 00:50:46–00:51:43].
   - Properties owned = 1 [10-27 00:44:59]. Ty also warns against filtering to a single owner on title, since a surviving spouse may still be on the deed [10-29 01:34:17].
2. **Probate: PR becomes the owner.** Make the personal representative from the filing the owner, using their mailing address, with a message-board note [10-22 00:17:14, 10-29 01:14:43]. This is done by hand today with DataSift's new **Edit Owners** feature (launched about Oct 28). Owner update/swap/merge is expected in the public API around **Nov 16–20** [10-27 00:54:41, 10-29 00:56:03]. For SiftMap probates (no case number), look up the PR by name in court records [10-13 01:53:06].
3. **Skip trace.**
   - Ty: SmartSkip bulk on the decedent or the PR [10-29 01:31:23].
   - Tyler, for all county first-to-market records: DataSift skip, then SmartSkip, then DirectSkip (15¢, IDI data), all run right away. Each preset feeds the next ("Ready to Skip DataSift" → SmartSkip → DirectSkip → ready to market). Each provider adds about 12–15% to contact rate [10-22 00:18:21–00:20:31, 01:12:36].
4. **Claude processes the file.**
   - Claude formats the SmartSkip output and labels each relationship. Tyler uses a Python name-gender library, e.g. "sibling" + female name → Sister [10-22 01:23:18].
   - Claude picks the heirs to contact and drops neighbors and tenants [10-22 01:18:39].
   - Claude researches the obituary and builds the heir map. It works out whether the owner or the spouse died [10-27 00:46:44], checks for a trust (a successor trustee can sell without probate) [10-27 01:51:18], and throws out wrong-person numbers [10-27 01:52:19].
5. **Trestle.** Unique numbers only.
   - Ty keeps only Dial 1 and Dial 2; "some come back with 16 heirs, we only needed 4" [10-27 02:01:25–02:03:24].
   - Tyler keeps all numbers (about 30 per record) and dials the ones Trestle says are live [10-22 01:10:55, 01:17:07].
   - Bulk lists aren't worth Trestling (30,000 records ≈ $2K, little gain) [10-22 00:59:03].
6. **Write back to DataSift** (via the API, all at once) [10-27 00:48:25–00:50:35, 10-29 00:52:45]:
   - Status **Deep Prospecting**, a **Deep Prospecting** tag, and assignment to the caller working the flow.
   - Phones on the record, each tagged relationship + dial tier + source, e.g. "Son Dial First SmartSkip".
   - One message-board post:
     - the owner/decedent at the top;
     - "children of X", each with name and last 4 digits of their phone;
     - in-laws and mailing addresses;
     - the call order and decedent check ("do this before you dial");
     - date of death.
     - Call notes go on the message board, never in tags [10-22 00:24:18–01:11:20].
   - The PDF research pack as a property file.
   - Custom fields: date of death, tax-delinquent amount/years, foreclosure date [10-29 01:17:50].
   - First-to-market records also get an FTM tag and a week tag ("week 34, 2026") [10-22 00:17:29, 00:23:27].
7. **Automate.** About 70 new obituaries a month in Ty's account. A daily end-of-day job on Fly.io runs the flow and posts a Slack notification [10-27 00:51:59].
   - Test it on 10 records first to check for hallucination [10-27 00:53:18].
   - Batch size: up to about 20 per session, one subagent per property. Opus on extra-high/max, no Ultracode [10-27 00:46:01–00:47:53, 01:19:56].
8. **Outreach.**
   - **Calling:** click-to-call, not a dialer [10-22 00:58:44]. Callers read the heir map and overview first and use personal details for rapport [10-29 01:17:13]. Call order:
     - Ty: the heir "most likely handling the house", e.g. the son 2 miles away [10-27 01:53:31].
     - Tyler: owner first, then spouse before children, Dial First numbers first within each person [10-22 01:25:16].
   - **Openers:** never ask for the deceased.
     - "Hey Tom, do you have any plans in selling that property at 123 Main Street?" [10-29 01:16:28]
     - "I'm pretty sure you're associated with X… trying to reach them about this property… can you help me out?" [10-22 01:27:40]
   - **Cadence:** initial attempt + 3 follow-ups, then a monthly rehash (attempts 4–6/7). Probates every 45 days. "A lot of deals out of that 5th and 6th call attempt" [10-22 01:03:04–01:03:33].
   - **Mail:**
     - Obituary heirs get handwritten mail every 30 days for 12 months [10-29 00:31:51, 00:37:41].
     - Sequence: a branded card, then a "Dear ___ Family" letter with **no dollar offer** (legal risk) [10-22 00:33:19].
     - Tyler mails obituary records immediately, weekly while mail attempts = 0 [10-22 00:29:48–00:30:16].
   - **Throughput:** about 100 deep-prospected records per caller per week ≈ "a deal or two" [10-22 00:58:10].
9. **Exhausted records.** Records where every number is wrong or dead (5–9%) move to deep prospecting through a status change. "Level 1" means every relative's number from all providers plus Trestle, done up front. "Level 2" means social-media and LinkedIn hunting [10-22 00:20:31–00:27:07].

**Cost:** 30–50¢ per record all in [10-22 00:57:36, 10-27 00:53:34]. The `deep-prospecting-v5` skill measured about $0.24.

## Where they disagree with each other

| Question | Ty | Tyler |
|---|---|---|
| When to work an obituary | Wait 3+ months, then deep prospect | Mail and skip immediately |
| Phones to upload | Dial 1–2 only | All (~30), dial the live ones |
| Skip providers | SmartSkip (+ Trestle) | DataSift + SmartSkip + DirectSkip (+ Trestle) |
| Owner swap | Edit Owners now, API from ~Nov 20 | VA edits on intake |

## Compared with what we have

| Piece | Ty/Tyler | Us | Gap |
|---|---|---|---|
| Heir engine | deep-prospecting-v5 | Same skill installed; SmartSkip/Tracerfy/Trestle creds in `.env` | None |
| Extra skip provider | DirectSkip (Tyler) | Tracerfy gap-fill only | Optional; v5 measured SmartSkip at 89% hit, 93% of relatives with phones |
| Selection preset | Obituary flag, 3+ mo, equity/sold exclusions, optional no-probate | Obituary track planned as plain tagged records, never loaded | Build the preset |
| Probate owner swap | Edit Owners UI → API | `datasift-owner-phone-write-api` writes phones only; no owner-swap endpoint confirmed | Manual until the public API ships, or test the internal endpoint |
| Write-back | Status + tag + phone tags + message board + PDF + custom fields | Phone write and tags proven (P1 enrichment); message board/file upload not yet confirmed via API | Confirm the board and file endpoints |
| In-repo code | — | `src/deep_prospector.py` is a stub (`_run_level_1` "would call tracerfy here"); `src/obituary_enricher.py` has Haiku pick heirs from prose, which v5 replaced | Wrap v5 rather than extend these |
| Scheduler | Fly.io daily + Slack | GitHub Actions via the Cloudflare dispatcher (5 cron slots used) | Reuse an existing trigger or run weekly |
| Heir opener | Property-first, never ask for the deceased | Rahaf's script is built for P1 owners | Needs an heir opener |
| Mail | Handwritten monthly for 12 months | No mail vendor (two-lane Phase 3 open) | Open |
