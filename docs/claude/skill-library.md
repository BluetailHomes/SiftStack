<!-- Moved verbatim from CLAUDE.md on 2026-10-27 to keep CLAUDE.md small. -->
# REI Skill Library (DataSift plugins, bluetail-* skills)

## REI Skill Library — three separate things, don't conflate them

This project touches three distinct layers. Keeping them separate matters because they have different owners, different update mechanisms, and different rules about who may edit what:

1. **DataSift's official REI Skill Library** (below) — generic, DataSift-maintained Claude Skills for real estate investing workflows (market research, comps, rehab, deal analysis, CRM setup, call coaching, ops). Lives entirely outside this repo, installed as a Claude Code plugin from DataSift's own marketplace. Auto-updates from upstream. Anyone can install/update it; this repo has no ability to edit it and doesn't try to.
2. **Bluetail's protected `bluetail-*` skills** — Bluetail-specific customizations, kept deliberately separate from DataSift's upstream library so an upstream update can never silently overwrite them. **Correction (2026-09-25): these do exist** — a 2026-09-21 search of this repo/machine found nothing, but a later session showed a full `bluetail-*` skill set available (`bluetail-rehab-estimator`, `bluetail-real-estate-comping`, `bluetail-deep-prospecting`, `bluetail-outbound-messaging`, `bluetail-sift-crm-workflow`, `bluetail-propstream-data-workflow`, `bluetail-first-market-county-data`, `bluetail-buyer-prospector`, `bluetail-phone-validator`, `bluetail-playbook-creator`, `bluetail-probate-property-finder`, `bluetail-sift-market-research`, and `bluetail-sequential-presets`) — these carry real, Bluetail-specific production logic that diverges materially from the generic DataSift versions (e.g. `bluetail-rehab-estimator` implements "Bluetail Deal Analyzer v3" with property-identity guardrails, seller NEED logic, fee tiers, and a NO-GO template; `bluetail-outbound-messaging` and `bluetail-sift-crm-workflow` reference Bluetail's actual active markets and real SiftLine board names). They live somewhere outside this repo's own file tree and outside this machine's local `~/.claude` config — likely an account- or Project-level Claude skill store rather than a plugin marketplace, since they weren't found by either of those search methods. **Still do not merge, restore, replace, or otherwise modify anything under this name** without first confirming with Kaycie exactly where the canonical source lives and how it's meant to be updated.
3. **The SiftStack Python codebase** (`src/`, this repo) — the actual scraping/enrichment/Apify Actor pipeline documented everywhere else in this file. Completely independent of both skill layers above; installing or updating DataSift's skill library has zero effect on it, and vice versa.

### 1. DataSift's Official REI Skill Library (27 skills, as of 2026-09-21)

Distributed as a Claude Code plugin marketplace, not the old downloadable `.skill`/`.plugin` ZIPs (see "Prior distribution model" below for how this used to work). Install everything with:

```bash
claude plugin marketplace add DataSift-Ty-Personal/SiftStack
claude plugin install siftstack-all@siftstack
```

This installs to the **global** Claude Code plugin cache (`~/.claude/plugins/`), scoped to the user, not this repo — nothing under `src/` or this project's `.claude/` directory is touched by it. Smaller per-division bundles are also available (`siftstack-coaching`, `siftstack-crm`, `siftstack-deal-analysis`, `siftstack-market-intel`, `siftstack-operations`) if the full 27-skill install isn't wanted. A visual org chart of the full DataSift agent/skill roster (including planned/gated ones beyond just these 27 shipped skills) is published at `learn.datasift.ai/agent-org-chart`.

| Skill | Category | What It Does |
|---|---|---|
| `siftstack-core` | Setup | SiftStack doctor and setup — what works right now, what needs a key, what it costs, what to do instead |
| `sift-market-research` | Market Intelligence | Sift Market Finder research for real estate wholesaling with comprehensive market analysis |
| `first-market-county-data` | Market Intelligence | Find WHERE to pull county distress lists (probate, foreclosure, tax sale, tax delinquent, eviction, code violation, divorce) for any US county |
| `buyer-prospector` | Market Intelligence | Build a real estate buyers list for any US county from a nationwide buyer database, categorize entities (LLC/Trust/Corp), research decision-makers for skip tracing |
| `real-estate-comping` | Deal Analysis | AI-powered property valuation and comparable sales analysis for wholesaling |
| `rehab-estimator` | Deal Analysis | Estimate rehab costs for investment properties |
| `deal-analyzer` | Deal Analysis | End-to-end deal analysis: photos → comps → rehab estimate → offer strategy |
| `comp-package` | Deal Analysis | Build a boundary-filtered comparable sales package for one subject property, delivered as an Excel workbook |
| `deep-prospecting-v5` | Deal Analysis | Identify heirs/decision-makers and exactly who must sign to sell when an owner is deceased or skip tracing fails |
| `probate-property-finder` | Deal Analysis | Find real property owned by probate decedents from just the case filing (PR number, decedent name, executor) — no address needed |
| `phone-validator` | Operations | Score/validate phone numbers via Trestle's phone_intel API, generate DataSift-ready CSVs with phone tags |
| `sequential-presets` | Operations | Design, build, and manage sequential marketing filter presets in DataSift |
| `playbook-creator` | Operations | Create playbooks, SOPs, and Scribe-style step guides with process maps and agent-executable SOP files |
| `caller-reputation-monitor` | Operations | Keep outbound cold-calling numbers out of carrier "Spam Likely / Scam Likely" labels |
| `candidate-intake` | Operations | Aggregate job applicants from many channels into one scored master sheet, review the ranked list, send screening outreach |
| `contractor-call-sheet` | Operations | Turn a vetted contractor/vendor directory into a one-page printable call sheet of top picks by trade |
| `dispo-deal-blast` | Operations | Text one named wholesale deal to the cash buyers whose own deed history says they buy at that price; hand every reply to a human |
| `team-hiring` | Operations | Plan, post for, interview, and onboard a remote REI team (data manager, prospector, lead manager, acquisitions manager, dispo) |
| `text-touch-builder` | Operations | Generate a 4-text-touch SMS sequence for hottest DataSift records, personalized and varied per record |
| `vendor-directory-builder` | Operations | Build a vetted, filterable local service-provider directory by mining a community source |
| `sift-sequences` | CRM | Create, manage, and ideate Sift sequences (automations) |
| `sift-operations` | CRM | Comprehensive REI Sift operations encyclopedia — sequences, presets, SiftLine boards, drip campaigns, events, tasks, filters, tags, skip tracing |
| `account-blueprint` | CRM | Build a DataSift account matching the DataSift reference account structure over the API, with a read-back on every object created |
| `closer-coach` | Coaching & Performance | Pull acquisitions/closing calls from SmrtPhone, transcribe with tonality notes, grade against the DataSift closer rubric |
| `cold-call-coach` | Coaching & Performance | Pull cold-call recordings from SmrtPhone, transcribe with tonality notes, grade against the DataSift cold-calling rubric |
| `lead-manager-coach` | Coaching & Performance | Pull lead-management follow-up/qualification calls from SmrtPhone, transcribe, grade against the DataSift lead manager rubric |
| `kpi-engine` | Coaching & Performance | Pull and grade cold-calling/lead-gen KPIs straight from DataSift — dials, answer/conversation/contact rates, dispositions |

13 of these (`sift-market-research`, `first-market-county-data`, `buyer-prospector`, `real-estate-comping`, `rehab-estimator`, `deal-analyzer`, `deep-prospecting-v5`, `probate-property-finder`, `phone-validator`, `sequential-presets`, `sift-sequences`, `sift-operations`, `playbook-creator`) are the same skills a prior version of this file documented as "13 Skills" hosted locally in this repo at `Skills for REI/improved/`. That local directory and the skill-authoring/verification work described below **predate this repo's move to consuming DataSift's upstream marketplace** and no longer exist in this repo — DataSift now owns and maintains the skill content directly. The following two subsections are kept as historical record of that prior, locally-hosted version; they have not been re-verified against the current upstream release and should not be assumed still accurate.

<details>
<summary>Historical record — prior locally-hosted 13-skill version (pre-migration)</summary>

**Cross-Skill Verified Consistency** (as of the local version):
- Phone tiers: 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- Preset folders: "00 Niche Sequential Marketing" (12 presets), "01. Bulk Sequential Marketing" (9 presets)
- Sequence count: 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- Comp adjustments: Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- Financing defaults: HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- DOD sanity: MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- Notice types: 7 total (foreclosure, tax_sale, tax_delinquent, probate, eviction, code_violation, divorce)

**Key Corrections Made During Optimization (April 2026):**
- Hardcoded credentials removed from sift-market-research (had email/password in SKILL.md)
- Bedroom adjustment corrected from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- HML points corrected from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- Linux paths fixed in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- Preset names aligned across 3 skills to match `niche_sequential.py` source code
- Transfer tax labeled as Tennessee-specific in deal-analyzer with state reference table for top 10 states
- "Substantial renovation" defined in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

**Prior distribution model** (superseded by the plugin-marketplace install above):
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```
</details>

### 2. Bluetail's protected `bluetail-*` skills

Not present anywhere in this repo or on this machine as of 2026-09-21 (see the search note in the intro above). Treat as a placeholder section until their actual location is confirmed — **do not merge, restore, replace, or otherwise modify anything under this name without first locating the real source**, and do not assume the DataSift install above has any relationship to them beyond sharing the general "SiftStack" project name.

### 3. The SiftStack Python codebase

Everything else in this file — `src/`, the Apify Actor, the enrichment pipeline, the 9-county scraper infrastructure. See the rest of this document.
