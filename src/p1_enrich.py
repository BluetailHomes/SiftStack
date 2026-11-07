"""Friday enrichment for Jackson P1 records that SiftMap auto-added without a usable phone.

Why this exists
  SiftMap's auto-add presets drop new records into the Jackson Priority 1 lists with DataSift's own
  skip trace phones but no Trestle tier tags, and it has no skip-trace option of its own. The
  weekly top-up only batches a record whose owner has a MOBILE tagged Dial First/Second
  (_best_primary), so an auto-added record is either left out (no phones) or tagged Mail Only
  (phones, none scored). This step runs in the Friday top-up workflow before `weekly_topup.py
  apply`, so the records it fixes go into that same Monday batch.

What it does, per record the top-up could batch (same rung lists, P1 tag, skip_reason() clean)
  - has a Dial First/Second mobile already: nothing.
  - has phones with no tier tag: scores them with Trestle phone_intel (+ litigator check) and
    writes the tier tag. The 2026-10-15 run used the same tiers (phone_validator.DEFAULT_TIERS).
  - has no phones, or every phone is scored and none is a Dial First/Second mobile: one Tracerfy
    trace on the owner, new numbers added (tag "tracerfy") and scored in the same run. The record
    is then tagged "P1 Enrich - Traced" so it is never traced (paid) again; if it still has no
    usable phone, the top-up tags it Mail Only as before.
  - LLC / trust / no first name: not traced (Tracerfy needs a person). Listed in the report.
  A record whose newly scored phones still give no usable mobile is traced the next Friday.

Modes
  python src/p1_enrich.py                   # dry run: counts + estimated cost, spends nothing
  python src/p1_enrich.py --live --slack    # Tracerfy + Trestle + owner phone writes

Env: REISIFT_API_KEY, TRESTLE_API_KEY, TRACERFY_API_KEY; SLACK_WEBHOOK_URL for --slack.

Safety rules
  - Without --live nothing is paid for and nothing is written.
  - Paid calls are capped per run (--max-trace owners, --max-score phones).
  - Owner phones are written with one PATCH per owner (the full list, re-read right before the
    write so nothing added since is lost), the response is checked, and the before-snapshot and
    result of every owner go to output/account_backups/p1_enrich_write_{date}.json, even on a crash.
  - A litigator-risk number gets only the "Litigator Risk - DNC" tag (never Dial First/Second) and
    DNC status, so a failed status write still can't make it a primary phone.
  - Status, assignment, and Batch/SMS Line tags are never touched; the top-up owns those.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid as uuidlib
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pm_text_sender import STATE_DIR, Sift, _best_primary, _env, _slack, ct_now  # noqa: E402
from weekly_topup import BACKUP_DIR, LIST_PREFIX, P1_TAG, RUNGS, _rows, add_tags, digits, ensure_tag, skip_reason  # noqa: E402

TIERS = (("Dial First", 81), ("Dial Second", 61), ("Dial Third", 41), ("Dial Fourth", 21), ("Drop", 0))
TIER_TAGS = {t for t, _ in TIERS}
LITIGATOR_TAG = "Litigator Risk - DNC"
TRACED_TAG = "P1 Enrich - Traced"
TRACERFY_PHONE_TAG = "tracerfy"
MAX_TRACE = 60          # owners per run (~$0.02 each)
MAX_SCORE = 400         # phones per run (~$0.02 each with the litigator check)
TRACE_COST, SCORE_COST = 0.02, 0.02
TRESTLE_URL = "https://api.trestleiq.com/3.0/phone_intel"
TRACERFY_TRACE_URL = "https://tracerfy.com/v1/api/trace/"
TRACERFY_QUEUE_URL = "https://tracerfy.com/v1/api/queue/"
TRACERFY_PHONES = (("primary_phone", None), ("mobile_1", "MOBILE"), ("mobile_2", "MOBILE"), ("mobile_3", "MOBILE"),
                   ("mobile_4", "MOBILE"), ("mobile_5", "MOBILE"), ("landline_1", "LANDLINE"),
                   ("landline_2", "LANDLINE"), ("landline_3", "LANDLINE"))
LINE_TYPES = {"mobile": "MOBILE", "landline": "LANDLINE"}
UA = "SiftStack p1_enrich/1.0"


def _title(t) -> str:
    return t if isinstance(t, str) else (t.get("title") or "")


def tier_for(score) -> str | None:
    if score is None:
        return None
    return next(t for t, low in TIERS if score >= low)


def scored(p: dict) -> bool:
    return bool({*TIER_TAGS, LITIGATOR_TAG} & set(p.get("tags") or []))


# ----------------------------------------------------------------- plan
def classify(d: dict) -> tuple[str, list[str]]:
    """('ready'|'score'|'trace'|'traced'|'entity'|'skip:<why>', unscored numbers) for a property detail."""
    why = skip_reason(d)
    if why in ("not person", "no first name"):
        return "entity", []
    if why:
        return "skip:" + why, []
    phones = {p["number"]: p for p in ((d.get("owner") or {}).get("phones") or [])}
    if _best_primary(phones):
        return "ready", []
    unscored = [n for n, p in phones.items() if not scored(p) and len(digits(n)) == 10]
    # Score what's there first; trace only when scoring can't produce a usable mobile.
    if any(phones[n].get("type") in ("MOBILE", "UNKNOWN", None, "") for n in unscored):
        return "score", unscored
    if TRACED_TAG in {_title(t) for t in d.get("tags") or []}:
        return "traced", []
    return "trace", unscored


def plan(api: Sift) -> dict:
    lists = {(l.get("title") or ""): l["uuid"] for l in _rows(api.call("/api/internal/list/?limit=999"))}
    tags = _rows(api.call("/api/internal/tag/?offset=0&limit=10000&ordering=title"))
    p1 = next((t["uuid"] for t in tags if t.get("title") == P1_TAG), None)
    if not p1:
        sys.exit(f"Tag '{P1_TAG}' not found")
    owners, entities, counts, seen = {}, [], Counter(), set()
    for r in RUNGS:
        lu = lists.get(LIST_PREFIX + r)
        if not lu:
            continue
        for row in api.search({"any_lists": [lu], "any_tags": [p1]}):
            if row["uuid"] in seen or row.get("status"):
                continue
            seen.add(row["uuid"])
            d = api.call(f"/api/internal/property/{row['uuid']}/")
            kind, unscored = classify(d)
            counts[kind.split(":")[0] if kind.startswith("skip:") else kind] += 1
            o = d.get("owner") or {}
            if kind == "entity":
                entities.append({"uuid": row["uuid"], "rung": r,
                                 "owner": o.get("company") or f"{o.get('first_name') or ''} {o.get('last_name') or ''}".strip()})
            if kind not in ("score", "trace") or not o.get("uuid"):
                continue
            addr, mail = d.get("address") or {}, o.get("address") or {}
            e = owners.setdefault(o["uuid"], {"owner_uuid": o["uuid"], "first": (o.get("first_name") or "").strip(),
                                              "last": (o.get("last_name") or "").strip(), "props": [], "rungs": [],
                                              "unscored": [], "trace": False,
                                              "address": addr if isinstance(addr, dict) else {"street": addr},
                                              "mail": mail if isinstance(mail, dict) else {}})
            e["props"].append(row["uuid"])
            e["rungs"].append(r)
            e["unscored"] = sorted(set(e["unscored"]) | set(unscored))
            e["trace"] = e["trace"] or kind == "trace"
    return {"owners": list(owners.values()), "entities": entities, "counts": dict(counts)}


# ----------------------------------------------------------------- vendors
def _http(url: str, method="GET", data: bytes | None = None, headers: dict | None = None, timeout=60):
    req = urllib.request.Request(url, data=data, method=method, headers={"user-agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "null")


def trestle(number: str, key: str) -> dict:
    """{'score', 'line_type', 'litigator'} or {'error'}; mirrors phone_validator.call_trestle."""
    # requests, not urllib: urllib sends the header as "X-api-key" and Trestle's
    # gateway 403s that casing (seen 2026-11-06/07; curl and requests get 200).
    params = {"phone": number, "add_ons": "litigator_checks"}
    for attempt in range(3):
        try:
            r = requests.get(TRESTLE_URL, params=params, timeout=15,
                             headers={"x-api-key": key, "accept": "application/json", "user-agent": UA})
        except requests.RequestException:
            r = None
        if r is not None:
            if r.status_code == 403:
                raise RuntimeError("Trestle rejected the API key (403)")
            if r.ok:
                data = r.json()
                lit = ((data.get("add_ons") or {}).get("litigator_checks") or {}).get("phone.is_litigator_risk")
                return {"score": data.get("activity_score"), "line_type": data.get("line_type"), "litigator": bool(lit)}
            if r.status_code != 429 and r.status_code < 500:
                return {"error": f"HTTP {r.status_code}"}
        time.sleep(1.5 * 2 ** attempt)
    return {"error": "retries exhausted"}


def score_all(numbers: list[str], key: str) -> dict[str, dict]:
    with ThreadPoolExecutor(max_workers=5) as ex:
        return dict(zip(numbers, ex.map(lambda n: trestle(n, key), numbers)))


def tracerfy_csv(owners: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["first_name", "last_name", "address", "city", "state", "zip", "mail_address", "mail_city", "mail_state", "mail_zip"])
    for o in owners:
        a, m = o["address"], o["mail"] or o["address"]
        w.writerow([o["first"], o["last"], a.get("street") or "", a.get("city") or "", a.get("state") or "",
                    a.get("zip5") or a.get("postal_code") or "", m.get("street") or "", m.get("city") or "",
                    m.get("state") or "", m.get("zip5") or m.get("postal_code") or ""])
    return buf.getvalue()


def tracerfy(owners: list[dict], key: str, interval=5, max_wait=300, stable_polls=3, queue_id=None) -> list[dict]:
    """Submit one batch and poll until the result count is stable (see
    tracerfy_skip_tracer.poll_queue_until_stable: the first non-empty poll is partial).
    With queue_id, re-download that finished (already paid) queue instead of submitting."""
    auth = {"authorization": f"Bearer {key}"}
    if queue_id:
        return _tracerfy_poll(queue_id, auth, 0, interval, max_wait, stable_polls)
    fields = {"first_name_column": "first_name", "last_name_column": "last_name", "address_column": "address",
              "city_column": "city", "state_column": "state", "zip_column": "zip", "mail_address_column": "mail_address",
              "mail_city_column": "mail_city", "mail_state_column": "mail_state", "mailing_zip_column": "mail_zip"}
    b = "----siftstack" + uuidlib.uuid4().hex
    body = "".join(f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n' for k, v in fields.items())
    body += (f'--{b}\r\nContent-Disposition: form-data; name="csv_file"; filename="p1_enrich.csv"\r\n'
             f"Content-Type: text/csv\r\n\r\n{tracerfy_csv(owners)}\r\n--{b}--\r\n")
    q = _http(TRACERFY_TRACE_URL, "POST", body.encode(), {**auth, "content-type": f"multipart/form-data; boundary={b}"})
    qid = (q or {}).get("queue_id")
    if not qid:
        raise RuntimeError(f"Tracerfy gave no queue_id: {str(q)[:200]}")
    print(f"Tracerfy queue {qid} submitted ({len(owners)} owners); rerun with --tracerfy-queue {qid} to reuse it")
    try:
        min_wait = min(float(q.get("estimated_wait_seconds") or 0), max_wait)
    except (TypeError, ValueError):
        min_wait = 0.0
    return _tracerfy_poll(qid, auth, min_wait, interval, max_wait, stable_polls)


def _tracerfy_poll(qid, auth: dict, min_wait: float, interval, max_wait, stable_polls) -> list[dict]:
    last, unchanged, waited = None, 0, 0
    while waited < max_wait:
        time.sleep(interval)
        waited += interval
        data = _http(f"{TRACERFY_QUEUE_URL}{qid}", headers=auth, timeout=30)
        if isinstance(data, dict):
            if data.get("status") == "failed":
                raise RuntimeError(f"Tracerfy job {qid} failed")
            records = data.get("records") or [] if data.get("status") == "completed" else []
        else:
            records = data or []
        if not records:
            continue
        unchanged = unchanged + 1 if last is not None and len(records) == len(last) else 0
        last = records
        if unchanged >= stable_polls - 1 and waited >= min_wait:
            return records
    return last or []


def _norm(s) -> str:
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def match_tracerfy(owners: list[dict], records: list[dict]) -> dict[str, list[tuple[str, str | None]]]:
    """owner_uuid -> [(10-digit number, type or None)] from Tracerfy hits, matched by name, then address."""
    by_name = {}
    for o in owners:
        by_name.setdefault((_norm(o["first"]), _norm(o["last"])), []).append(o)
    out = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        cands = by_name.get((_norm(rec.get("first_name")), _norm(rec.get("last_name"))), [])
        if len(cands) > 1:
            ra = _norm(rec.get("address") or rec.get("mail_address"))
            cands = [o for o in cands if ra and _norm(o["address"].get("street")) in ra] or cands
        nums = []
        for f, typ in TRACERFY_PHONES:
            n = digits(rec.get(f))
            if len(n) == 10 and n not in [x for x, _ in nums]:
                nums.append((n, typ))
        for o in cands:
            if len(nums) > len(out.get(o["owner_uuid"], [])):
                out[o["owner_uuid"]] = nums
    return out


# ----------------------------------------------------------------- write
def build_phones(before: list[dict], scores: dict[str, dict], adds: list[tuple[str, str | None]]) -> list[dict]:
    """The owner's full new phone list: tier tags on scored numbers, new Tracerfy numbers appended."""
    def tagged(tags, sc):
        keep = [t for t in tags if t not in TIER_TAGS and t != LITIGATOR_TAG]
        if not sc or sc.get("error"):
            return keep
        if sc["litigator"]:
            return keep + [LITIGATOR_TAG]
        return keep + [tier_for(sc["score"])] if tier_for(sc["score"]) else keep

    out, have = [], set()
    for p in before:
        q = dict(p)
        sc = scores.get(digits(p["number"]))
        if sc and not scored(p):
            q["tags"] = tagged(list(p.get("tags") or []), sc)
            if q.get("type") in (None, "", "UNKNOWN") and LINE_TYPES.get(str(sc.get("line_type") or "").lower()):
                q["type"] = LINE_TYPES[sc["line_type"].lower()]
        out.append(q)
        have.add(digits(p["number"]))
    for n, typ in adds:
        if n in have:
            continue
        sc = scores.get(n) or {}
        out.append({"number": n, "type": LINE_TYPES.get(str(sc.get("line_type") or "").lower()) or typ or "UNKNOWN",
                    "status": "UNKNOWN", "is_connected": True, "tags": tagged([TRACERFY_PHONE_TAG], sc)})
        have.add(n)
    return out


def _sig(phones: list[dict]) -> set:
    return {(digits(p["number"]), tuple(sorted(p.get("tags") or []))) for p in phones}


def write_owner(api: Sift, o: dict, scores: dict, adds: list, log: dict) -> bool:
    url = f"/api/internal/owner/{o['owner_uuid']}/"
    entry = {"owner": f"{o['first']} {o['last']}", "props": o["props"]}
    log[o["owner_uuid"]] = entry
    before = api.call(url)
    entry["before"] = before.get("phones") or []
    phones = build_phones(entry["before"], scores, adds)
    if _sig(phones) == _sig(entry["before"]):
        entry["result"] = "nothing to write"
        return True
    entry["payload"] = phones
    after = api.call(url, "PATCH", {"phones": phones})
    ok = isinstance(after, dict) and _sig(after.get("phones") or []) >= _sig(phones)
    entry["result"] = "ok" if ok else f"readback mismatch: {str(after)[:200]}"
    for p in phones:
        if LITIGATOR_TAG in (p.get("tags") or []) and p.get("status") not in ("DNC", "CORRECT_DNC", "WRONG_DNC"):
            body = {"phones": [{"number": p["number"], "type": p.get("type") or "UNKNOWN", "tags": p["tags"],
                                "status": "DNC", "is_connected": p.get("is_connected", True), "verified": False}]}
            try:
                api.call(f"/api/internal/owner/{o['owner_uuid']}/upsert-phones/", "POST", body)
                entry.setdefault("dnc", []).append(p["number"])
            except RuntimeError as e:
                entry.setdefault("dnc_failed", []).append(f"{p['number']}: {e}")
    return ok


# ----------------------------------------------------------------- run
def report(p: dict, res: dict | None, live: bool, max_trace: int, max_score: int) -> str:
    c, owners = p["counts"], p["owners"]
    trace = [o for o in owners if o["trace"]]
    phones = sorted({n for o in owners for n in o["unscored"]})
    head = "P1 enrich (auto-added records)" + ("" if live else " - DRY RUN, nothing paid or written")
    lines = [f"*{head}*",
             f"Rung records with no status: ready {c.get('ready', 0)}, need scoring {c.get('score', 0)}, "
             f"need a trace {c.get('trace', 0)}, already traced with no usable phone {c.get('traced', 0)}, "
             f"LLC/trust/no first name {c.get('entity', 0)}, other skips {c.get('skip', 0)}",
             f"Owners: {len(owners)} ({len(trace)} to trace, {len(phones)} phones to score)"]
    if not res:
        n_t, n_s = min(len(trace), max_trace), min(len(phones), max_score)
        lines.append(f"Est. cost: Tracerfy {n_t} x ${TRACE_COST:.2f} + Trestle {n_s}+new x ${SCORE_COST:.2f} "
                     f"= ~${n_t * TRACE_COST + n_s * SCORE_COST:.2f} plus ~3 numbers per trace hit")
    else:
        lines += [f"Tracerfy: {res['traced']} owners sent, {res['trace_hits']} hits, {res['trace_numbers']} new numbers"
                  + (f" ({res['trace_error']})" if res.get("trace_error") else ""),
                  f"Trestle: {res['scored']} scored ({dict(res['tiers'])}), {res['score_errors']} errors, "
                  f"{res['litigator']} litigator -> DNC",
                  f"Owners written: {res['written']} ok, {len(res['failed'])} failed; "
                  f"now have a Dial First/Second mobile: {res['now_ready']}",
                  f"Spent: ~${res['traced'] * TRACE_COST + res['scored'] * SCORE_COST:.2f}"]
        if res["failed"]:
            lines.append("Failed: " + ", ".join(res["failed"][:10]))
        if res.get("capped"):
            lines.append("Capped this run: " + res["capped"])
    if p["entities"]:
        sample = ", ".join(sorted({e["owner"] for e in p["entities"]})[:8])
        lines.append(f"Not traced (needs a person / decision-maker): {len(p['entities'])}, e.g. {sample}")
    return "\n".join(lines)


def _save_trace(log_path, adds: dict) -> None:
    """Keep paid Tracerfy matches on disk if scoring dies before the normal write log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps({"scores": {}, "tracerfy": adds, "owners": {}}, indent=1), encoding="utf-8")


def run(api: Sift, p: dict, trestle_key: str, tracerfy_key: str, max_trace: int, max_score: int, log_path,
        tracerfy_queue=None) -> dict:
    owners = p["owners"]
    res = {"traced": 0, "trace_hits": 0, "trace_numbers": 0, "scored": 0, "score_errors": 0, "litigator": 0,
           "tiers": Counter(), "written": 0, "failed": [], "now_ready": 0, "capped": ""}
    trace = [o for o in owners if o["trace"] and o["first"] and o["last"]]
    if len(trace) > max_trace:
        res["capped"] += f"{len(trace) - max_trace} owners left to trace next week. "
        trace = trace[:max_trace]
    # score the phones already on file first: a rejected Trestle key raises here, before any Tracerfy spend
    numbers = list(dict.fromkeys(digits(n) for o in owners for n in o["unscored"]))
    if len(numbers) > max_score:
        res["capped"] += f"{len(numbers) - max_score} phones left to score next week."
        numbers = numbers[:max_score]
    scores = score_all(numbers, trestle_key) if numbers else {}

    adds = {}
    if trace and not tracerfy_key:
        res["trace_error"] = "TRACERFY_API_KEY not set, trace skipped"
    elif trace:
        try:
            adds = match_tracerfy(trace, tracerfy(trace, tracerfy_key, queue_id=tracerfy_queue))
            res["traced"] = len(trace)
        except (RuntimeError, urllib.error.URLError, TimeoutError) as e:
            res["trace_error"] = f"trace failed: {e}"
    res["trace_hits"] = sum(1 for v in adds.values() if v)
    res["trace_numbers"] = sum(len(v) for v in adds.values())

    new = [n for n in dict.fromkeys(n for v in adds.values() for n, _ in v) if n not in scores]
    room = max(max_score - len(scores), 0)
    if len(new) > room:
        res["capped"] += f"{len(new) - room} traced phones left to score next week."
        new = new[:room]
    if new:
        try:
            scores.update(score_all(new, trestle_key))
        except RuntimeError:
            _save_trace(log_path, adds)
            raise
    for sc in scores.values():
        if sc.get("error"):
            res["score_errors"] += 1
        else:
            res["scored"] += 1
            res["litigator"] += sc["litigator"]
            res["tiers"][LITIGATOR_TAG if sc["litigator"] else tier_for(sc["score"]) or "no score"] += 1

    log = {}
    traced_props = [u for o in trace for u in o["props"]]
    try:
        for o in owners:
            if not any(digits(n) in scores for n in o["unscored"]) and not adds.get(o["owner_uuid"]):
                continue
            try:
                ok = write_owner(api, o, scores, adds.get(o["owner_uuid"], []), log)
            except RuntimeError as e:
                log.setdefault(o["owner_uuid"], {})["result"] = f"error: {e}"
                ok = False
            if ok:
                res["written"] += 1
                entry = log[o["owner_uuid"]]
                after = {x["number"]: x for x in entry.get("payload") or entry.get("before") or []}
                res["now_ready"] += bool(_best_primary(after))
            else:
                res["failed"].append(f"{o['first']} {o['last']}")
        if traced_props and res["traced"]:
            ensure_tag(api, TRACED_TAG)
            add_tags(api, traced_props, [TRACED_TAG])
    finally:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(json.dumps({"scores": scores, "tracerfy": adds, "owners": log}, indent=1, default=str),
                            encoding="utf-8")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--live", action="store_true", help="pay for Tracerfy/Trestle and write owner phones")
    ap.add_argument("--slack", action="store_true")
    ap.add_argument("--max-trace", type=int, default=MAX_TRACE)
    ap.add_argument("--max-score", type=int, default=MAX_SCORE)
    ap.add_argument("--tracerfy-queue", help="reuse a finished Tracerfy queue id (no new charge) instead of submitting")
    args = ap.parse_args()

    api = Sift(_env("REISIFT_API_KEY"))
    today = ct_now().date().isoformat()
    p = plan(api)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / f"p1_enrich_plan_{today}.json").write_text(json.dumps(p, indent=1, default=str), encoding="utf-8")
    res = None
    if args.live:
        key = _env("TRESTLE_API_KEY")
        if not key:
            msg = "P1 enrich: TRESTLE_API_KEY not set, nothing done"
            print(msg)
            if args.slack:
                _slack(msg)
            return 1
        try:
            res = run(api, p, key, _env("TRACERFY_API_KEY"), args.max_trace, args.max_score,
                      BACKUP_DIR / f"p1_enrich_write_{today}.json", args.tracerfy_queue)
        except RuntimeError as e:
            msg = f"P1 enrich stopped, nothing written to DataSift: {e}"
            print(msg)
            if args.slack:
                _slack(msg)
            return 1
    text = report(p, res, args.live, args.max_trace, args.max_score)
    print(text)
    if args.slack:
        _slack(text)
    return 1 if res and res["failed"] else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # the workflow step is continue-on-error, so say it out loud
        if "--slack" in sys.argv:
            _slack(f"P1 enrich FAILED ({type(e).__name__}: {e}); Friday top-up continues without it")
        raise
