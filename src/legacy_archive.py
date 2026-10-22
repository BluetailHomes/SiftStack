"""Archive the "Bluetail 1.0" legacy leads in DataSift (decided 2026-10-14).

Everything tagged `Bluetail 1.0` that is NOT (a) on a Jackson Priority 1 /
Obituary / Buyer Prospecting list, (b) in an active status, (c) on a SiftLine
board, or (d) carrying an open task gets:

  * tag  `Archived 2026-10`  +  one reason tag by segment
        duplicate   -> `Archive - Duplicate Copy`   (SiftStack courthouse copies)
        dormant     -> `Archive - Dormant Market`   (Albuquerque v1 lists)
        unfiltered  -> `Archive - Unfiltered Data`  (everything else)
  * status `close_out` when the record has NO status (records that already
    carry Not Interested / Dead Lead etc. keep their status, tags only)

`close_out` is excluded by every one of Ty's 95 presets, so archived records
drop out of all funnels with no preset edits. Nothing is deleted. Lists stay.

Phases (dry run unless --commit):
  plan      read-only counts per segment
  dnc       mark the 7 STOP/"do not contact" senders (audit STOP_Texts tab)
            as *_DNC phone status on their owner
  test      ONE record: scoped add-tags with a throwaway tag, status PATCH,
            read-back, and a records-index visibility check; then cleans up
  archive   all three segments (or --segment duplicate|dormant|unfiltered)
  presets   create the "ZZ LEGACY TRIAGE" folder + presets
  verify    read-only: counts of archived tags and close_out
  boards    remove legacy SiftLine cards. --scope archived (default: only
            already-archived records), terminal (+ kept records whose card sits
            in a Lost Deal / Dead Lead / Not Interested / Deep Dived column; those
            records are archived too), or all (every legacy card; every record
            archived). Records on a Jackson P1 / Obituary / Buyer list are never
            touched. A removed card is read back from the record.

Usage:
  python src/legacy_archive.py --phase plan
  python src/legacy_archive.py --phase dnc --commit
  python src/legacy_archive.py --phase test --commit
  python src/legacy_archive.py --phase archive --commit
  python src/legacy_archive.py --phase presets --commit
  python src/legacy_archive.py --phase verify

Auth: REISIFT_API_KEY in the environment or in .env (Open API key).

API facts this relies on (verified in DataSift's own scripts, 2026-08/09):
  * POST /api/internal/property/add-tags/ scopes ONLY by query.must. A
    top-level `properties` list is ignored and the tag applies account-wide,
    so `properties` is always nested INSIDE query.must here, and every call
    is additionally scoped by the segment filter. Tags are passed by TITLE.
  * The records-filter index catches up ~1-2 minutes after a bulk call.
  * A real opt-out is a phone STATUS of DNC / CORRECT_DNC / WRONG_DNC written
    through POST /api/internal/owner/{uuid}/upsert-phones/ with the full
    phone payload (number, type, tags, status, is_connected, verified).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
sys.stdout.reconfigure(encoding="utf-8")
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://apiv2.reisift.io"
ROOT = Path(__file__).resolve().parent.parent
AUDIT_XLSX = ROOT / "audit_2026-10-01" / "SiftStack_FTM_Import_Audit_2026-10-01.xlsx"
REPORT_DIR = ROOT / "output" / "account_backups"

LEGACY_TAG = "Bluetail 1.0"
COURTHOUSE_TAG = "Courthouse Data"
ARCHIVED_TAG = "Archived 2026-10"
REASON = {
    "duplicate": "Archive - Duplicate Copy",
    "dormant": "Archive - Dormant Market",
    "unfiltered": "Archive - Unfiltered Data",
}
TEST_TAG = "ZZ Archive Test"
ARCHIVE_STATUS = "close_out"
TRIAGE_FOLDER = "ZZ LEGACY TRIAGE"

# statuses that mean "a conversation is in flight" -> never archived
ACTIVE = ["new_lead", "No Contact New Lead", "Cold Lead", "Warm Lead", "Hot Lead",
          "Ghosting Lead", "follow_up", "lead", "prospecting", "underwrite", "push_deal",
          "refer_lead", "under_contract", "transactions", "listed"]
BOARDS = ("Lead Management", "Acquisitions", "Transactions")
KEEP_LIST_PREFIXES = ("Jackson - Priority 1",)
KEEP_LISTS = ("Obituary", "Buyer Prospecting - Bluetail Markets")
V1_MARK = "| v1 |"
V1_KEEP_PREFIX = "KC |"          # Jackson County list, NOT a dormant market

PAGE, CHUNK, PAUSE = 200, 100, 1.0


# ----------------------------------------------------------------- client
class ApiError(Exception):
    pass


class Api:
    def __init__(self, key: str, min_interval: float = 0.35):
        self.key, self.min_interval, self._last, self.calls = key, min_interval, 0.0, 0

    def call(self, path, method="GET", body=None, override=None, timeout=90):
        gap = self.min_interval - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()
        self.calls += 1
        data = json.dumps(body).encode() if body is not None else None
        headers = {"authorization": "Api-Key " + self.key, "accept": "application/json",
                   "user-agent": "SiftStack legacy_archive/1.0"}
        if data is not None:
            headers["content-type"] = "application/json"
        if override:
            headers["x-http-method-override"] = override
        req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    t = r.read().decode()
                    return json.loads(t) if t.strip().startswith(("{", "[")) else t
            except urllib.error.HTTPError as e:
                txt = e.read().decode(errors="replace")
                if e.code == 429 and attempt < 5:
                    m = re.search(r"available in (\d+)\s*second", txt, re.I)
                    time.sleep(min((int(m.group(1)) + 1) if m else 5 * (attempt + 1), 300))
                    continue
                if e.code in (500, 502, 503, 504) and attempt < 3:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError(f"HTTP {e.code} {method} {path}: {txt[:300]}")
        raise ApiError(f"gave up: {method} {path}")

    def get_all(self, path):
        out, url, guard = [], path, 0
        while url and guard < 500:
            guard += 1
            r = self.call(url[len(API):] if url.startswith(API) else url)
            if isinstance(r, list):
                return out + r
            rows = r.get("results") if r.get("results") is not None else r.get("data")
            out.extend(rows or [])
            url = r.get("next") if isinstance(r.get("next"), str) else None
        return out

    def count(self, must: dict) -> int:
        r = self.call("/api/internal/property/", "POST",
                      {"limit": 1, "offset": 0, "query": {"must": must}}, override="GET")
        return int(r.get("count") or 0) if isinstance(r, dict) else 0

    def rows(self, must: dict) -> list[dict]:
        out, off = [], 0
        while True:
            r = self.call("/api/internal/property/", "POST",
                          {"limit": PAGE, "offset": off, "query": {"must": must}}, override="GET")
            d = r.get("results") if isinstance(r, dict) else r
            d = d or []
            out.extend(d)
            if len(d) < PAGE:
                return out
            off += PAGE


def load_key() -> str:
    k = os.environ.get("REISIFT_API_KEY", "").strip()
    if not k and (ROOT / ".env").exists():
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if line.startswith("REISIFT_API_KEY="):
                k = line.split("=", 1)[1].strip().strip('"').strip("'")
    if not k:
        sys.exit("REISIFT_API_KEY not set (env or .env)")
    return k


# ---------------------------------------------------------------- context
class Ctx:
    def __init__(self, api: Api, commit: bool):
        self.api, self.commit = api, commit
        self.lists = {l["title"]: l["uuid"] for l in api.get_all("/api/internal/list/?limit=999")}
        self.tags = {t["title"]: t["uuid"] for t in
                     api.get_all("/api/internal/tag/?offset=0&limit=10000&ordering=title")}
        self.boards = {b["title"]: b["uuid"] for b in
                       api.get_all("/api/internal/siftline/board/?offset=0&limit=999")}
        self.log: list[str] = []

    def say(self, s: str):
        print(s)
        self.log.append(s)

    # -- building blocks
    def lid(self, pred):
        return [u for t, u in self.lists.items() if pred(t)]

    @property
    def keep_lists(self):
        return self.lid(lambda t: t.startswith(KEEP_LIST_PREFIXES) or t in KEEP_LISTS)

    @property
    def v1_dormant(self):
        return self.lid(lambda t: V1_MARK in t and not t.startswith(V1_KEEP_PREFIX))

    def base(self) -> dict:
        """Legacy records that are archive-eligible (nothing in flight)."""
        must_not = {"any_lists": self.keep_lists,
                    "any_property_status": ACTIVE,
                    "any_boards": [self.boards[b] for b in BOARDS if b in self.boards]}
        return {"any_tags": [self.tags[LEGACY_TAG]], "tasks_incomplete": [0, 0],
                "must_not": must_not}

    def segment(self, name: str) -> dict:
        m = json.loads(json.dumps(self.base()))
        ch = self.tags.get(COURTHOUSE_TAG)
        if name == "duplicate":
            m["all_tags"] = [ch]
        elif name == "dormant":
            m["any_lists"] = self.v1_dormant
            m["must_not"]["any_tags"] = [ch]
        elif name == "unfiltered":
            m["must_not"]["any_tags"] = [ch]
            m["must_not"]["any_lists"] = m["must_not"]["any_lists"] + self.v1_dormant
        else:
            raise ValueError(name)
        return m

    def ensure_tag(self, title: str) -> str | None:
        if title in self.tags:
            return self.tags[title]
        if not self.commit:
            self.say(f"  (dry) would create tag {title!r}")
            return None
        self.api.call("/api/internal/tag/", "POST", {"title": title})
        self.tags = {t["title"]: t["uuid"] for t in
                     self.api.get_all("/api/internal/tag/?offset=0&limit=10000&ordering=title")}
        if title not in self.tags:
            raise ApiError(f"tag {title!r} not found after create")
        self.say(f"  created tag {title!r}")
        return self.tags[title]

    def ensure_status_active(self, title: str) -> bool:
        """Built-in statuses can be inactive; an inactive one is rejected on PATCH."""
        rows = self.api.get_all("/api/internal/status/?limit=1000")
        row = next((r for r in rows if r.get("title") == title), None)
        if not row:
            self.say(f"  !! status {title!r} does not exist"); return False
        if row.get("is_active"):
            return True
        if not self.commit:
            self.say(f"  (dry) would activate status {title!r}"); return True
        self.api.call(f"/api/internal/status/{row['uuid']}/", "PATCH", {"is_active": True})
        back = next(r for r in self.api.get_all("/api/internal/status/?limit=1000") if r["uuid"] == row["uuid"])
        self.say(f"  activated status {title!r}: is_active={back.get('is_active')}")
        return bool(back.get("is_active"))

    def add_tags(self, must: dict, uuids: list[str], titles: list[str]) -> int:
        """Scoped bulk tag: segment filter AND explicit uuid chunk, both inside query.must."""
        done = 0
        for i in range(0, len(uuids), CHUNK):
            chunk = uuids[i:i + CHUNK]
            body = {"query": {"must": {**must, "properties": chunk}, "ordering": ["-list_count"]},
                    "tags": titles}
            assert "properties" in body["query"]["must"]      # never top-level
            r = self.api.call("/api/internal/property/add-tags/", "POST", body)
            n = (r.get("count") or 0) if isinstance(r, dict) else 0
            done += n
            time.sleep(PAUSE)
        return done

    def wait_index(self, must: dict, want: int, label: str, tries=12) -> int:
        got = -1
        for _ in range(tries):
            got = self.api.count(must)
            if got == want:
                break
            time.sleep(10)
        self.say(f"  index: {label} = {got} (wanted {want})")
        return got


# ------------------------------------------------------------------ phases
def phase_plan(ctx: Ctx):
    ctx.say(f"legacy tag on {ctx.api.count({'any_tags': [ctx.tags[LEGACY_TAG]]})} records")
    ctx.say(f"archive-eligible (base): {ctx.api.count(ctx.base())}")
    total = 0
    for name in REASON:
        n = ctx.api.count(ctx.segment(name))
        total += n
        no_status = ctx.api.count({**ctx.segment(name),
                                   "must_not": {**ctx.segment(name)["must_not"],
                                                "any_property_status": ACTIVE + [
                                                    "not_interested", "Dead Lead", "dnc", "opt_out",
                                                    "close_out", "lost_deal", "closed", "sold",
                                                    "buyer", "buyer_found", "buyer_lost"]}})
        ctx.say(f"  segment {name:10} {n:5} records  ({no_status} with no status -> close_out)")
    ctx.say(f"  segments total {total}")
    kept = ctx.api.count({"any_tags": [ctx.tags[LEGACY_TAG]],
                          "must_not": {"any_lists": ctx.keep_lists}}) - ctx.api.count(ctx.base())
    ctx.say(f"kept (active / on board / open task): ~{kept}")
    for t in (ARCHIVED_TAG, *REASON.values()):
        ctx.say(f"  tag {t!r}: {'exists' if t in ctx.tags else 'will be created'}")


def phase_dnc(ctx: Ctx):
    import openpyxl
    ws = openpyxl.load_workbook(AUDIT_XLSX, read_only=True)["STOP_Texts"]
    rows = list(ws.iter_rows(values_only=True))[1:]
    for ouid, name, ts, frm, msg, *_ in rows:
        o = ctx.api.call(f"/api/internal/owner/{ouid}/")
        last10 = re.sub(r"\D", "", str(frm))[-10:]
        hit = [p for p in (o.get("phones") or [])
               if re.sub(r"\D", "", str(p.get("number", "")))[-10:] == last10]
        if not hit:
            ctx.say(f"  {name}: STOP number not on owner -> SKIPPED, do by hand")
            continue
        p = hit[0]
        cur = (p.get("status") or "UNKNOWN").upper()
        new = {"CORRECT": "CORRECT_DNC", "WRONG": "WRONG_DNC"}.get(cur, "DNC")
        if cur.endswith("DNC"):
            ctx.say(f"  {name}: already {cur}")
            continue
        payload = {"number": re.sub(r"\D", "", str(p["number"])),
                   "type": (p.get("type") or "UNKNOWN").upper(),
                   "tags": [(t.get("title") if isinstance(t, dict) else t) for t in (p.get("tags") or [])],
                   "status": new,
                   "is_connected": True if p.get("is_connected") is None else p.get("is_connected"),
                   "verified": bool(p.get("verified", False))}
        if not ctx.commit:
            ctx.say(f"  (dry) {name}: ...{last10[-4:]} {cur} -> {new}")
            continue
        ctx.api.call(f"/api/internal/owner/{ouid}/upsert-phones/", "POST", {"phones": [payload]})
        o2 = ctx.api.call(f"/api/internal/owner/{ouid}/")
        p2 = [q for q in o2.get("phones", []) if re.sub(r"\D", "", str(q.get("number", "")))[-10:] == last10]
        got = (p2[0].get("status") if p2 else "MISSING")
        ok = (got or "").upper() == new and len(o2.get("phones", [])) == len(o.get("phones", []))
        ctx.say(f"  {name}: ...{last10[-4:]} {cur} -> {got}  {'OK' if ok else '!! MISMATCH'}")
        if o2.get("opt_out") is False:
            try:
                ctx.api.call(f"/api/internal/owner/{ouid}/", "PATCH", {"opt_out": True})
                o3 = ctx.api.call(f"/api/internal/owner/{ouid}/")
                ctx.say(f"      owner opt_out flag -> {o3.get('opt_out')}")
            except ApiError as e:
                ctx.say(f"      owner opt_out flag not writable ({str(e)[:80]}); phone status is the record of opt-out")


def phase_test(ctx: Ctx):
    must = ctx.segment("duplicate")
    rows = [r for r in ctx.api.rows({**must}) if not r.get("status")][:1]
    if not rows:
        ctx.say("no status-less duplicate record found for the test")
        return
    rec = rows[0]
    u = rec["uuid"]
    ctx.say(f"test record {u} status={rec.get('status')!r} tags={rec.get('tags')}")
    if not ctx.commit:
        ctx.say("  (dry) would: create test tag, scoped add-tags, PATCH status, read back, index check, remove test tag")
        return
    if not ctx.ensure_status_active(ARCHIVE_STATUS):
        ctx.say("  !! archive status unavailable; stopping"); return
    tid = ctx.ensure_tag(TEST_TAG)
    try:
        n = ctx.add_tags(must, [u], [TEST_TAG])
        ctx.say(f"  add-tags returned count={n}")
        wide = ctx.wait_index({"any_tags": [tid]}, 1, "records carrying the test tag account-wide")
        if wide != 1:
            ctx.say("  !! scoping check failed; stopping before any further write")
            return
        ctx.api.call(f"/api/internal/property/{u}/", "PATCH", {"status": ARCHIVE_STATUS})
        full = ctx.api.call(f"/api/internal/property/{u}/")
        ctx.say(f"  record read-back: status={full.get('status')!r}, tags={full.get('tags')}")
        ctx.wait_index({"properties": [u], "any_property_status": [ARCHIVE_STATUS]}, 1,
                       "index sees close_out on the test record")
        ctx.say("  test record left at close_out (it is a duplicate copy slated for archive anyway)")
    finally:
        ctx.api.call("/api/internal/property/remove-tags/", "POST",
                     {"query": {"must": {"any_tags": [tid]}}, "tags": [TEST_TAG]})
        ctx.wait_index({"any_tags": [tid]}, 0, "test tag removed")


def phase_archive(ctx: Ctx, only: str | None):
    if not ctx.ensure_status_active(ARCHIVE_STATUS):
        ctx.say("  !! archive status unavailable; stopping"); return
    ctx.ensure_tag(ARCHIVED_TAG)
    for name, reason in REASON.items():
        if only and name != only:
            continue
        ctx.ensure_tag(reason)
        must = ctx.segment(name)
        rows = ctx.api.rows(must)
        uuids = [r["uuid"] for r in rows]
        need_status = [r["uuid"] for r in rows if not r.get("status")]
        ctx.say(f"segment {name}: {len(uuids)} records, {len(need_status)} need status -> {ARCHIVE_STATUS}")
        if not ctx.commit:
            continue
        tagged = ctx.add_tags(must, uuids, [ARCHIVED_TAG, reason])
        ctx.say(f"  add-tags submitted for {tagged}")
        done = 0
        for i, u in enumerate(need_status, 1):
            try:
                ctx.api.call(f"/api/internal/property/{u}/", "PATCH", {"status": ARCHIVE_STATUS})
                done += 1
            except ApiError as e:
                ctx.say(f"  status PATCH failed {u}: {str(e)[:100]}")
            if i % 250 == 0:
                ctx.say(f"  status: {i}/{len(need_status)}")
        ctx.say(f"  status set on {done}")
        ctx.wait_index({"any_tags": [ctx.tags[reason]]}, len(uuids), f"{reason!r} tag count")


TIER_01 = ["new_lead", "Warm Lead", "Hot Lead"]
TIER_02 = ["Cold Lead", "not_interested", "follow_up", "Ghosting Lead", "close_out"]
PRESET_GROUPS = {"KC": ["Jackson", "Clay", "Platte", "Cass", "Knox"],
                 "OK": ["Oklahoma", "Tulsa"],
                 "NM": ["Bernalillo", "Sandoval"]}
RECALL_TAG = "Legacy Recall 2026-10"


def phase_presets(ctx: Ctx):
    folders = ctx.api.get_all("/api/internal/filter-preset-folder/?type=properties&limit=999")
    fol = next((f for f in folders if f["title"] == TRIAGE_FOLDER), None)
    # account uuid comes from any existing preset's stored filter
    acct = None
    for f in folders:
        ps = ctx.api.get_all(f"/api/internal/filter-preset-folder/{f['uuid']}/filter-preset/?limit=999")
        if ps:
            acct = (ctx.api.call(f"/api/internal/filter-preset/{ps[0]['uuid']}/").get("filters") or {}).get("account")
            break
    eng = ctx.tags.get(ENGAGED_TAG)
    if not eng:
        ctx.say(f"!! tag {ENGAGED_TAG!r} missing - run --phase requalify --commit first")
        return
    recall = ctx.ensure_tag(RECALL_TAG) if ctx.commit else ctx.tags.get(RECALL_TAG)
    presets = []
    for g, counties in PRESET_GROUPS.items():
        for title, extra in ((f"01 {g} – Call Now", {"any_property_status": TIER_01}),
                             (f"02 {g} – Nurture", {"any_property_status": TIER_02}),
                             (f"03 {g} – Exhausted", None)):
            must = {"all_tags": [eng], "any_county": counties, "must_not": {}}
            if recall:
                must["must_not"]["any_tags"] = [recall]
            if extra:
                must.update(extra)
            else:
                must["must_not"]["any_property_status"] = TIER_01 + TIER_02
            if not must["must_not"]:
                del must["must_not"]
            presets.append((title, must))
    if not ctx.commit:
        ctx.say(f"(dry) folder {TRIAGE_FOLDER!r}: {'exists' if fol else 'create'}; "
                f"tag {RECALL_TAG!r}: {'exists' if recall else 'create (not in counts)'}")
        for title, must in presets:
            ctx.say(f"  {title}: {ctx.api.count(must)}")
        return
    if not fol:
        fol = ctx.api.call("/api/internal/filter-preset-folder/", "POST",
                           {"title": TRIAGE_FOLDER, "type": "properties", "permissions": []})
        ctx.say(f"created folder {TRIAGE_FOLDER!r}")
    have = {p["title"] for p in ctx.api.get_all(f"/api/internal/filter-preset-folder/{fol['uuid']}/filter-preset/?limit=999")}
    for title, must in presets:
        if title in have:
            ctx.say(f"  skip {title} (exists)")
            continue
        body = {"title": title, "folder": fol["uuid"], "quick_filter": False,
                "filters": {"must": must, "account": acct}, "type": "properties"}
        got = ctx.api.call("/api/internal/filter-preset/", "POST", body)
        back = ctx.api.call(f"/api/internal/filter-preset/{got['uuid']}/")
        ok = (back.get("filters") or {}).get("must") == must
        ctx.say(f"  created {title}: read-back {'OK' if ok else '!! differs'}; matches {ctx.api.count(must)} records")


TERMINAL_COLUMNS = ("Lost Deal", "Dead Lead", "Not Interested Anymore", "Deep Dived")
REASON_BOARD = "Archive - Unfiltered Data"
FINISH_TAG = "Legacy - Finish Out"      # live-column records: card removed, NOT archived, kept visible
CLOSED_STATUSES = ["not_interested", "Dead Lead", "dnc", "opt_out", "close_out", "lost_deal", "closed", "sold"]


def _cards(ctx: Ctx, prop: str) -> list[dict]:
    r = ctx.api.call(f"/api/internal/siftline/property/{prop}/card/")
    return (r.get("results") if isinstance(r, dict) else r) or []


def _remove_card(ctx: Ctx, card: dict, prop: str) -> bool:
    """DELETE /siftline/board/column/{col}/card/{card}/ — verified live 2026-10-14."""
    col = (card.get("column") or {}).get("uuid")
    ctx.api.call(f"/api/internal/siftline/board/column/{col}/card/{card['uuid']}/", "DELETE")
    return True


def phase_boards(ctx: Ctx, scope: str):
    boards = ctx.api.get_all("/api/internal/siftline/board/?offset=0&limit=999")
    B = [b["uuid"] for b in boards]
    b10, arch = ctx.tags[LEGACY_TAG], ctx.tags.get(ARCHIVED_TAG)
    rows = ctx.api.rows({"any_tags": [b10], "any_boards": B, "must_not": {"any_lists": ctx.keep_lists}})
    archived = {r["uuid"] for r in ctx.api.rows({"any_tags": [arch], "any_boards": B})} if arch else set()
    plan = []   # (row, card, archive_record?)
    finish = set()
    for r in rows:
        for c in _cards(ctx, r["uuid"]):
            col = (c.get("column") or {}).get("title", "")
            is_arch = r["uuid"] in archived
            if is_arch:
                plan.append((r, c, False))
            elif col in TERMINAL_COLUMNS and scope in ("terminal", "all"):
                plan.append((r, c, True))
            elif scope == "all":
                plan.append((r, c, False))
                finish.add(r["uuid"])
    if scope == "all":
        # kept legacy records with an open task but no card belong to the finish-out set too
        for r in ctx.api.rows({"any_tags": [b10], "tasks_incomplete": [1, None],
                               "must_not": {"any_lists": ctx.keep_lists, **({"any_tags": [arch]} if arch else {})}}):
            if r["uuid"] not in {x["uuid"] for x, _, a in plan if a}:
                finish.add(r["uuid"])
    import collections
    tally = collections.Counter(((c.get("board") or {}).get("title"), (c.get("column") or {}).get("title"), "archive+remove" if a else "remove") for _, c, a in plan)
    for (bt, col, what), n in sorted(tally.items(), key=lambda x: str(x[0])):
        ctx.say(f"  {n:4}  {bt:16} | {col:34} | {what}")
    to_archive = sorted({r["uuid"] for r, _, a in plan if a})
    finish = sorted(finish - set(to_archive))
    ctx.say(f"scope={scope}: {len(plan)} cards to remove on {len({r['uuid'] for r, _, _ in plan})} records; "
            f"{len(to_archive)} records also get archived; {len(finish)} tagged {FINISH_TAG!r} (kept, visible)")
    if not ctx.commit:
        return
    if finish:
        ctx.ensure_tag(FINISH_TAG)
        n = ctx.add_tags({"any_tags": [b10], "must_not": {"any_lists": ctx.keep_lists}}, finish, [FINISH_TAG])
        ctx.say(f"  finish-out tag submitted for {n}")
        ctx.wait_index({"any_tags": [ctx.tags[FINISH_TAG]]}, len(finish), f"{FINISH_TAG!r} tag count")
        _repoint_finish_preset(ctx)
    if to_archive:
        if not ctx.ensure_status_active(ARCHIVE_STATUS):
            return
        ctx.ensure_tag(ARCHIVED_TAG); ctx.ensure_tag(REASON_BOARD)
        must = {"any_tags": [b10], "must_not": {"any_lists": ctx.keep_lists}}
        n = ctx.add_tags(must, to_archive, [ARCHIVED_TAG, REASON_BOARD])
        ctx.say(f"  add-tags submitted for {n}")
        done = 0
        for u in to_archive:
            ctx.api.call(f"/api/internal/property/{u}/", "PATCH", {"status": ARCHIVE_STATUS})
            done += 1
        ctx.say(f"  status -> {ARCHIVE_STATUS} on {done}")
    removed, failed = 0, 0
    for r, c, _ in plan:
        try:
            _remove_card(ctx, c, r["uuid"]); removed += 1
        except ApiError as e:
            failed += 1
            ctx.say(f"  !! could not remove card {c['uuid']} on {r['uuid']}: {str(e)[:80]}")
            if failed >= 3:
                ctx.say("  stopping: repeated failures"); break
    ctx.say(f"  removed {removed} cards, failed {failed}")
    left = sum(len(_cards(ctx, u)) for u in sorted({r["uuid"] for r, _, _ in plan})[:25])
    ctx.say(f"  read-back: cards remaining on first 25 touched records = {left}")
    ctx.say(f"  legacy (non-current) records still on a board: "
            f"{ctx.api.count({'any_tags': [b10], 'any_boards': B, 'must_not': {'any_lists': ctx.keep_lists}})}")


def _repoint_finish_preset(ctx: Ctx):
    """'01. Active - finish out' -> records carrying the finish-out tag whose status is still open."""
    folders = ctx.api.get_all("/api/internal/filter-preset-folder/?type=properties&limit=999")
    fol = next((f for f in folders if f["title"] == TRIAGE_FOLDER), None)
    if not fol:
        ctx.say("  !! triage folder missing; preset not re-pointed"); return
    ps = ctx.api.get_all(f"/api/internal/filter-preset-folder/{fol['uuid']}/filter-preset/?limit=999")
    row = next((p for p in ps if p["title"] == "01. Active - finish out"), None)
    if not row:
        ctx.say("  !! '01. Active - finish out' missing; not re-pointed"); return
    cur = ctx.api.call(f"/api/internal/filter-preset/{row['uuid']}/")
    filt = dict(cur.get("filters") or {})
    must = {"any_tags": [ctx.tags[FINISH_TAG]], "must_not": {"any_property_status": CLOSED_STATUSES}}
    filt["must"] = must
    ctx.api.call(f"/api/internal/filter-preset/{row['uuid']}/", "PATCH", {"filters": filt})
    back = ctx.api.call(f"/api/internal/filter-preset/{row['uuid']}/")
    ok = (back.get("filters") or {}).get("must") == must
    ctx.say(f"  re-pointed '01. Active - finish out': read-back {'OK' if ok else '!! differs'}; matches {ctx.api.count(must)} records")


FINISH_LIST = "Legacy - Finish Out"


def phase_finishlist(ctx: Ctx):
    """Put every record tagged FINISH_TAG on a static list of the same name (Rahaf's work queue)."""
    tid = ctx.tags.get(FINISH_TAG)
    if not tid:
        ctx.say(f"  !! tag {FINISH_TAG!r} missing; run --phase boards first"); return
    rows = ctx.api.rows({"any_tags": [tid]})
    uuids = [r["uuid"] for r in rows]
    ctx.say(f"{len(uuids)} records carry {FINISH_TAG!r}")
    if FINISH_LIST not in ctx.lists:
        if not ctx.commit:
            ctx.say(f"  (dry) would create list {FINISH_LIST!r} and add {len(uuids)} records"); return
        ctx.api.call("/api/internal/list/", "POST", {"title": FINISH_LIST})
        ctx.lists = {l["title"]: l["uuid"] for l in ctx.api.get_all("/api/internal/list/?limit=999")}
        ctx.say(f"  created list {FINISH_LIST!r}")
    lid = ctx.lists[FINISH_LIST]
    already = ctx.api.count({"any_lists": [lid]})
    ctx.say(f"  list currently holds {already}")
    if not ctx.commit:
        return
    done = 0
    for i in range(0, len(uuids), CHUNK):
        chunk = uuids[i:i + CHUNK]
        body = {"query": {"must": {"any_tags": [tid], "properties": chunk}, "ordering": ["-list_count"]},
                "lists": [FINISH_LIST]}
        r = ctx.api.call("/api/internal/property/add-lists/", "POST", body)
        done += (r.get("count") or 0) if isinstance(r, dict) else 0
        time.sleep(PAUSE)
    ctx.say(f"  add-lists submitted for {done}")
    ctx.wait_index({"any_lists": [lid]}, len(uuids), f"{FINISH_LIST!r} list count")
    full = ctx.api.call(f"/api/internal/property/{uuids[0]}/")
    ctx.say(f"  record read-back: {FINISH_LIST!r} on first record = {FINISH_LIST in (full.get('lists') or [])}")


SHORTLIST_STREETS = ("4207 E 35Th St", "4106 Norledge Ave", "4840 Blue Ridge Blvd",
                     "9200 E 66Th Ter", "505 Ne Olympic Ct", "3462 S 151St Ave E")


def _bulk(ctx: Ctx, route: str, must: dict, uuids: list[str], key: str, titles: list[str]) -> int:
    done = 0
    for i in range(0, len(uuids), CHUNK):
        chunk = uuids[i:i + CHUNK]
        body = {"query": {"must": {**must, "properties": chunk}, "ordering": ["-list_count"]}, key: titles}
        r = ctx.api.call(route, "POST", body)
        done += (r.get("count") or 0) if isinstance(r, dict) else 0
        time.sleep(PAUSE)
    return done


def phase_shortlist(ctx: Ctx):
    """Keep only SHORTLIST_STREETS in the finish-out set; archive the rest."""
    tid, lid = ctx.tags.get(FINISH_TAG), ctx.lists.get(FINISH_LIST)
    rows = ctx.api.rows({"any_tags": [tid]})
    keep, drop = [], []
    for r in rows:
        full = ctx.api.call(f"/api/internal/property/{r['uuid']}/")
        st = ((full.get("address") or {}).get("street") or "").strip()
        (keep if st in SHORTLIST_STREETS else drop).append((r["uuid"], st, full.get("status")))
    ctx.say(f"finish-out set: {len(rows)} -> keep {len(keep)}, archive {len(drop)}")
    for u, st, s in keep: ctx.say(f"  keep    {st:28} {s}")
    missing = set(SHORTLIST_STREETS) - {st for _, st, _ in keep}
    if missing:
        ctx.say(f"  !! shortlist streets not found: {missing}"); return
    if not ctx.commit:
        return
    if not ctx.ensure_status_active(ARCHIVE_STATUS): return
    ctx.ensure_tag(ARCHIVED_TAG); ctx.ensure_tag(REASON_BOARD)
    du = [u for u, _, _ in drop]
    must = {"any_tags": [tid]}
    ctx.say(f"  archived-tags submitted for {_bulk(ctx, '/api/internal/property/add-tags/', must, du, 'tags', [ARCHIVED_TAG, REASON_BOARD])}")
    n = 0
    for u in du:
        ctx.api.call(f"/api/internal/property/{u}/", "PATCH", {"status": ARCHIVE_STATUS}); n += 1
    ctx.say(f"  status -> {ARCHIVE_STATUS} on {n}")
    try:
        ctx.say(f"  removed from list for {_bulk(ctx, '/api/internal/property/remove-lists/', must, du, 'lists', [FINISH_LIST])}")
    except ApiError as e:
        ctx.say(f"  !! remove-lists not available ({str(e)[:60]}); records stay on the list but lose the tag")
    ctx.say(f"  finish-out tag removed for {_bulk(ctx, '/api/internal/property/remove-tags/', must, du, 'tags', [FINISH_TAG])}")
    ctx.wait_index({"any_tags": [tid]}, len(keep), f"{FINISH_TAG!r} tag count")
    ctx.wait_index({"any_lists": [lid]}, len(keep), f"{FINISH_LIST!r} list count")
    ctx.wait_index({"any_tags": [tid], "must_not": {"any_property_status": CLOSED_STATUSES}}, len(keep), "'01. Active - finish out' preset")
    full = ctx.api.call(f"/api/internal/property/{du[0]}/")
    ctx.say(f"  read-back on an archived one: status={full.get('status')!r}, finish tag present={FINISH_TAG in (full.get('tags') or [])}, on list={FINISH_LIST in (full.get('lists') or [])}")


ENGAGED_TAG = "Legacy - Engaged"
ENGAGED_STATUSES = {"new_lead", "Cold Lead", "Warm Lead", "Hot Lead"}
CALL_NOW_STATUSES = {"new_lead", "Warm Lead", "Hot Lead"}
P1_TAG = "Priority 1"


ANSWERED_MIN_SECS = 60


def _phone(n) -> str:
    return "".join(c for c in str(n or "") if c.isdigit())[-10:]


def _status_pair(e: dict) -> tuple[str, str]:
    st = ((e.get("payload") or {}).get("property") or {}).get("status") or []
    if isinstance(st, list):
        return (st[0] if len(st) > 1 else ""), (st[-1] if st else "")
    return "", st


def _signals(logs: list[dict]) -> tuple[list[str], str, str, str]:
    """(engagement signals, last two-way contact date, last not_interested date, status before the archive) from a record's event log.

    An answered call counts only if it ran >= ANSWERED_MIN_SECS, or the same number got a Correct mark, or the
    record's status changed that day: smrtPhone logs voicemails as answered calls, and duration is 0 on many of them.
    """
    sig, last, ni, touched = set(), "", "", False
    calls, correct, status_days, pre_archive, archived_at = [], set(), set(), "", ""
    for e in logs:
        t, ts, pl = e.get("event_type") or "", (e.get("timestamp") or "")[:10], e.get("payload") or {}
        if t == "owner.call.answered":
            c = pl.get("call") or {}
            calls.append((_phone(c.get("phone")), ts, int(c.get("duration") or 0)))
            touched = True
        elif t == "owner.sms.received":
            sig.add("inbound text"); last = max(last, ts); touched = True
        elif t.startswith(("owner.call.", "owner.sms.")):
            touched = True
        elif t == "owner.phone.status.updated":
            ph = pl.get("owner") or {}
            st = ph.get("status") or []
            if (st[-1] if isinstance(st, list) and st else st) == "CORRECT":
                correct.add(_phone(ph.get("phone")))
        elif t == "property.status.updated":
            was, st = _status_pair(e)
            if st == ARCHIVE_STATUS:
                if ts >= archived_at:
                    archived_at, pre_archive = ts, was
                continue
            status_days.add(ts)
            if st in ENGAGED_STATUSES:
                sig.add(f"status was {st}")
            if st == "not_interested":
                ni = max(ni, ts)
    for ph, ts, secs in calls:
        if secs >= ANSWERED_MIN_SECS or ph in correct or ts in status_days:
            sig.add("answered call"); last = max(last, ts)
    if ni and touched:
        sig.add("not interested")
    return sorted(sig), last, ni, pre_archive


def phase_requalify(ctx: Ctx):
    """Read-only: find legacy records with a Correct phone and real seller engagement, propose a tier."""
    import csv
    rows = ctx.api.rows({"any_tags": [ctx.tags[LEGACY_TAG]], "any_phone_status": ["CORRECT"]})
    ctx.say(f"  legacy records with a CORRECT phone: {len(rows)}")
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - 30 * 86400))
    out, skipped = [], {}
    for i, r in enumerate(rows, 1):
        if i % 100 == 0:
            print(f"  ... {i}/{len(rows)}")
        uid, a = r["uuid"], r.get("address") or {}
        street = a.get("street") or ""
        short = street in SHORTLIST_STREETS
        full = ctx.api.call(f"/api/internal/property/{uid}/")
        logs = (ctx.api.call(f"/api/internal/property/{uid}/logs/?limit=250&offset=0") or {}).get("results") or []
        sig, last, ni, pre = _signals(logs)
        status, tags, owner = full.get("status") or "", full.get("tags") or [], full.get("owner") or {}
        tier_status = pre if status == ARCHIVE_STATUS and pre else status
        st = status if status in ENGAGED_STATUSES else ""
        if st:
            sig = sorted(set(sig) | {f"status was {st}"})
        why = ("owner DNC/opt-out" if owner.get("dnc") or owner.get("opt_out") else
               "sold" if status == "sold" else
               "already Priority 1" if P1_TAG in tags else
               "no engagement" if not sig and not short else "")
        if why:
            skipped[why] = skipped.get(why, 0) + 1
            continue
        tier = ("01 Call Now" if tier_status in CALL_NOW_STATUSES else
                "02 Nurture (NI < 30d, wait)" if tier_status == "not_interested" and ni > cutoff else
                "02 Nurture" if tier_status in ("Cold Lead", "not_interested", "follow_up", "Ghosting Lead") else
                "02 Nurture (signals)" if tier_status == ARCHIVE_STATUS and ({"inbound text", "answered call"} & set(sig)) else
                "03 Exhausted")
        dnc_phone = any(p.get("status") == "CORRECT" and p.get("do_not_call") for p in owner.get("phones") or [])
        out.append({"uuid": uid, "street": street, "city": a.get("city"), "state": a.get("state"), "zip": a.get("zip5"),
                    "status": status, "pre_archive_status": pre, "tier": tier, "signals": "; ".join(sig) or "(shortlist)",
                    "last_contact": last, "last_not_interested": ni, "shortlist": "yes" if short else "",
                    "archived": "yes" if ARCHIVED_TAG in tags else "", "phone_flagged_dnc": "yes" if dnc_phone else ""})
    for k, v in sorted(skipped.items()):
        ctx.say(f"  skipped - {k}: {v}")
    ctx.say(f"  engaged: {len(out)}")
    for t in sorted({o['tier'] for o in out}):
        ctx.say(f"    {t}: {sum(o['tier'] == t for o in out)}")
    ctx.say(f"    of which currently archived: {sum(bool(o['archived']) for o in out)}; shortlist present: {sum(bool(o['shortlist']) for o in out)}/6")
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORT_DIR / f"legacy_requalify_{time.strftime('%Y%m%dT%H%M%S')}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(out[0]) if out else ["uuid"])
        w.writeheader(); w.writerows(sorted(out, key=lambda o: (o["tier"], o["state"] or "", o["zip"] or "")))
    ctx.say(f"  csv: {path}")
    if not ctx.commit or not out:
        return
    ctx.ensure_tag(ENGAGED_TAG)
    must = {"any_tags": [ctx.tags[LEGACY_TAG]]}
    ctx.say(f"  engaged tag submitted for {_bulk(ctx, '/api/internal/property/add-tags/', must, [o['uuid'] for o in out], 'tags', [ENGAGED_TAG])}")
    arch = [o["uuid"] for o in out if o["archived"]]
    ctx.say(f"  archive tags removed for {_bulk(ctx, '/api/internal/property/remove-tags/', must, arch, 'tags', [ARCHIVED_TAG, *REASON.values()])}")
    srows = ctx.api.get_all("/api/internal/status/?limit=1000")
    live = {s["title"].lower(): s["title"] for s in srows}
    active = {s["title"] for s in srows if s.get("is_active")}
    changes = []
    for o in out:
        pre = o["pre_archive_status"]
        if o["status"] != ARCHIVE_STATUS or not pre:
            continue
        to = live.get(pre.lower()) or (pre if "_" in pre or pre == "closed" else None)
        if not to:
            res = "no live status"
        elif to not in active:
            res = "inactive status, not restored"
        else:
            try:
                ctx.api.call(f"/api/internal/property/{o['uuid']}/", "PATCH", {"status": to}); res = "ok"
            except ApiError as e:
                res = f"error {str(e)[:80]}"
        changes.append({"uuid": o["uuid"], "street": o["street"], "from": ARCHIVE_STATUS, "to": to or pre,
                        "result": res, "flag": "check" if to in ("closed", "lost_deal") else ""})
    cpath = REPORT_DIR / f"legacy_requalify_commit_{time.strftime('%Y%m%dT%H%M%S')}.csv"
    with cpath.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["uuid", "street", "from", "to", "result", "flag"])
        w.writeheader(); w.writerows(changes)
    for res in sorted({c["result"] for c in changes}):
        ctx.say(f"  status restore - {res}: {sum(c['result'] == res for c in changes)}")
    for c in changes:
        if c["flag"]:
            ctx.say(f"  check: {c['street']:28} -> {c['to']}")
    ctx.say(f"  commit log: {cpath}")
    ctx.wait_index({"any_tags": [ctx.tags[ENGAGED_TAG]]}, len(out), f"{ENGAGED_TAG!r} tag count")
    ok = next((c for c in changes if c["result"] == "ok"), None)
    if ok:
        full = ctx.api.call(f"/api/internal/property/{ok['uuid']}/")
        tg = full.get("tags") or []
        ctx.say(f"  read-back {ok['street']}: status={full.get('status')!r}, archived tag gone={ARCHIVED_TAG not in tg}, engaged tag={ENGAGED_TAG in tg}")


def phase_verify(ctx: Ctx):
    for t in (ARCHIVED_TAG, *REASON.values(), TEST_TAG):
        if t in ctx.tags:
            ctx.say(f"  {t!r}: {ctx.api.count({'any_tags': [ctx.tags[t]]})}")
    ctx.say(f"  close_out status: {ctx.api.count({'any_property_status': [ARCHIVE_STATUS]})}")
    ctx.say(f"  legacy still archive-eligible and untagged: "
            f"{ctx.api.count({**ctx.base(), 'must_not': {**ctx.base()['must_not'], **({'any_tags': [ctx.tags[ARCHIVED_TAG]]} if ARCHIVED_TAG in ctx.tags else {})}})}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phase", required=True, choices=["plan", "dnc", "test", "archive", "presets", "verify", "boards", "finishlist", "shortlist", "requalify"])
    ap.add_argument("--scope", choices=["archived", "terminal", "all"], default="archived")
    ap.add_argument("--segment", choices=list(REASON), default=None)
    ap.add_argument("--commit", action="store_true", help="write; default is a dry run")
    a = ap.parse_args()
    ctx = Ctx(Api(load_key()), a.commit)
    ctx.say(f"== legacy_archive --phase {a.phase} {'COMMIT' if a.commit else 'DRY RUN'} ==")
    {"plan": lambda: phase_plan(ctx), "dnc": lambda: phase_dnc(ctx), "test": lambda: phase_test(ctx),
     "archive": lambda: phase_archive(ctx, a.segment), "presets": lambda: phase_presets(ctx),
     "verify": lambda: phase_verify(ctx), "boards": lambda: phase_boards(ctx, a.scope), "finishlist": lambda: phase_finishlist(ctx), "shortlist": lambda: phase_shortlist(ctx), "requalify": lambda: phase_requalify(ctx)}[a.phase]()
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORT_DIR / f"legacy_archive_{a.phase}_{time.strftime('%Y%m%dT%H%M%S')}.log"
    out.write_text("\n".join(ctx.log), encoding="utf-8")
    ctx.say(f"api calls: {ctx.api.calls}; log: {out}")


if __name__ == "__main__":
    main()
