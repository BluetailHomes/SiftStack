"""Thanksgiving 2026: no DataSift drip text on Thu 2026-11-26.

The Monday 11-23 batch is on the 5-day morning-text drips (one text a day, Mon-Fri). Their text #4
would go out on Thanksgiving. Kaycie picked "option 2" (2026-11-22): texts #1-3 Mon-Wed, nothing
Thursday, then one final "last try" text Friday 11-27 at ~9:50 CT, and the drip ends (4 texts total).

DataSift drips cannot be paused, so on Wednesday evening (after text #3) this job:
  1. finds every running morning-text drip whose next text is due Thursday,
  2. removes it from that drip (DELETE the drip history),
  3. adds the line's one-time holiday tag ("Holiday Fri 11-27 Line A"), which a one-time sequence
     turns into enrollment in that line's one-text drip ("delay until Fri 9:50 CT, then the line's
     own last-try text from the same number"),
  4. checks every moved record has exactly one running drip, from its holiday sequence,
  5. switches the holiday sequences off again, notes each record, and reports to Slack.

Modes (dry run unless --live):
  setup   create the 15 holiday tags, one-text drips and INACTIVE sequences (idempotent, by title)
  run     the Wednesday move (the dispatcher fires it Wed 2026-11-25 18:00 CT with --live)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
import time

import pm_text_sender as pts

SKIP_DAY = dt.date(2026, 11, 26)
SEND_AT = (2026, 11, 27, 9, 50)          # Central wall clock for the last-try text
RUN_DAY = dt.date(2026, 11, 25)
DRIP_FOLDER = "8047fe0a-dc6e-41f5-9942-b76d6ccf41db"
SEQ_FOLDER = "b8ff56f6-ae03-43a6-8f9e-f781ba3e32c5"

# line -> (source 5-day drip, source sequence that enrolls it)
LINES = {
    "A": ("e640ac2c-1a96-422b-8490-f2e9773b8cac", "7b4e846a-849b-4d20-8a7a-a90021b09cec"),
    "B": ("7bec5d3d-7824-4c5e-8cd4-cc64fd93b139", "8ae76f8d-c27f-4ced-83b1-92af9a2ba1d6"),
    "C": ("5aa9d261-a8a6-44e6-aa0b-0d27227d35ce", "4fa2a5a0-d691-439d-a409-1c76dfe975c2"),
    "D": ("d2260c14-b6ad-4f8d-a3a1-fcf65ab9c4da", "6f074f72-a0ff-4a81-b945-618697b7d370"),
    "E": ("a9654f3c-bcf4-437b-a108-84e0899182cd", "a55bd1f3-f72c-4288-9ed5-882a2dcf7b1c"),
    "F": ("c621816c-91cd-4b45-9505-79c14faed0a4", "bd8d8a3f-9093-4093-a5f5-128e0ef2e1f7"),
    "G": ("7f0efa71-5618-45ac-8f9b-f9d50d901803", "7273ed86-cffb-4186-a6b1-4c4eae41cff1"),
    "H": ("b940dc55-0c22-40f4-932f-d78675ebbf53", "b5898937-dd40-4860-afec-2bd1734fb397"),
    "I": ("ff140166-d0f9-4651-bde7-75a7a059d599", "5b5f1ba3-5431-404b-8506-42c9ce258806"),
    "J": ("1bbd0d98-6a22-4c27-a4cd-4e7b5860e341", "0434a6c7-d08f-41a9-9286-3dbcb9aa08d6"),
    "K": ("0f9c6556-d0fa-4d27-83bb-0426bfbc5383", "b304606d-0796-42ad-9694-aa11043affd5"),
    "KC1": ("a83f3e6e-0510-4835-92ba-6ffea3a964fa", "bd2f1175-2ea5-45c5-9b6e-0fbe5c283111"),
    "KC2": ("dead41cf-8a64-46fe-be03-16c5fe54decd", "0206ce0e-4362-4b3c-b713-2751480bc7cc"),
    "KC3": ("d1a576ac-f48d-4f05-b602-f08b2a31594a", "91685278-6d46-4529-a530-1b0b54e0baf8"),
    "OK": ("d369936d-5ef9-4e2d-965c-4f48f4be66be", "c195adcc-04d7-4382-a3de-4c919d2531fb"),
}
SRC_SEQ_LINE = {seq: line for line, (_, seq) in LINES.items()}
NOTE = ("Thanksgiving: Thursday 11-26 drip text skipped (no seller texts on the holiday). "
        "Moved to a one-time final text Fri 11-27 ~9:50 AM CT from the same line; the 5-day drip ends there.")


def tag_title(line: str) -> str:
    return f"Holiday Fri 11-27 Line {line}"


def drip_title(line: str) -> str:
    return f"Holiday Fri 11-27 - Last Text - Line {line}"


def seq_title(line: str) -> str:
    # A-K keep "Morning Texts Line X" so pm_text_sender's drip_line() still attributes them to the line.
    if len(line) == 1:
        return f"Holiday Fri 11-27 - Morning Texts Line {line} (one-time)"
    return f"Holiday Fri 11-27 - SMS Line {line} (one-time)"


def _rows(r):
    return (r.get("results") if isinstance(r, dict) else r) or []


def _all(api, path: str) -> list[dict]:
    out, off = [], 0
    while True:
        sep = "&" if "?" in path else "?"
        r = api.call(f"{path}{sep}limit=100&offset={off}")
        rows = _rows(r)
        out.extend(rows)
        off += 100
        if not rows or not (isinstance(r, dict) and r.get("next")) or off > 30000:
            return out


def send_at_utc() -> dt.datetime:
    y, mo, d, h, mi = SEND_AT
    naive = dt.datetime(y, mo, d, h, mi)
    off = pts._central_offset(naive + dt.timedelta(hours=6))
    return (naive - off).replace(tzinfo=dt.timezone.utc)


def minutes_until_send(now_utc: dt.datetime | None = None) -> int:
    now_utc = now_utc or dt.datetime.now(dt.timezone.utc)
    return max(1, round((send_at_utc() - now_utc).total_seconds() / 60))


def last_text(drip: dict) -> tuple[str, str]:
    """(message, origin number) of the drip's final send_sms action."""
    sms = [a for a in sorted(drip["actions"], key=lambda a: a["order"]) if a["type"] == "send_sms"]
    d = sms[-1]["details"]
    return d["message"], d["origin_number"]


def holiday_actions(message: str, origin: str, minutes: int) -> list[dict]:
    return [
        {"type": "delay", "order": 0, "details": {"unit": "minutes", "amount": minutes}},
        {"type": "send_sms", "order": 1, "details": {"message": message, "integration": "smrtphone",
                                                       "send_to_all": False, "origin_number": origin}},
    ]


def find_objects(api) -> dict:
    tags = {t.get("title"): t for t in _all(api, "/api/internal/tag/")}
    drips = {d.get("title"): d for d in _all(api, "/api/internal/drip-campaigns/")}
    seqs = {s.get("title"): s for s in _all(api, "/api/internal/sequence/")}
    out = {}
    for line in LINES:
        out[line] = {"tag": tags.get(tag_title(line)), "drip": drips.get(drip_title(line)),
                     "seq": seqs.get(seq_title(line))}
    return out


# ----------------------------------------------------------------- setup
def setup(api, live: bool) -> int:
    found = find_objects(api)
    problems = 0
    for line, (src_drip, _) in LINES.items():
        f = found[line]
        msg, origin = last_text(api.call(f"/api/internal/drip-campaigns/{src_drip}/"))
        print(f"Line {line}: origin {origin}  last text: {msg}")
        tag = f["tag"]
        if not tag:
            print(f"  + tag {tag_title(line)!r}")
            if live:
                tag = api.call("/api/internal/tag/", "POST", {"title": tag_title(line)})
        drip = f["drip"]
        if not drip:
            print(f"  + drip {drip_title(line)!r}")
            if live:
                drip = api.call("/api/internal/drip-campaigns/", "POST", {
                    "title": drip_title(line), "folder": DRIP_FOLDER, "active": True,
                    "actions": holiday_actions(msg, origin, minutes_until_send())})
        seq = f["seq"]
        if not seq:
            print(f"  + sequence {seq_title(line)!r} (INACTIVE)")
            if live:
                seq = api.call("/api/internal/sequence/", "POST", {
                    "title": seq_title(line), "is_active": False, "folder": SEQ_FOLDER,
                    "trigger": "property.tags.added",
                    "conditions": [{"condition": "has_all", "payload": {
                        "field": "tags_uuid", "label": "property.tags", "values": [tag["uuid"]], "resource": True}}],
                    "actions": [{"action": "run-drip-campaign", "payload": {
                        "field": "lists", "label": "run.drip.campaign", "values": [drip["uuid"]]}}]})
        if live:   # read back
            d = api.call(f"/api/internal/drip-campaigns/{drip['uuid']}/")
            s = api.call(f"/api/internal/sequence/{seq['uuid']}/")
            acts = sorted(d["actions"], key=lambda a: a["order"])
            ok = (len(acts) == 2 and acts[0]["type"] == "delay" and acts[0]["details"].get("unit") == "minutes"
                  and acts[1]["details"].get("origin_number") == origin and acts[1]["details"].get("message") == msg
                  and d.get("active") and not s.get("is_active")
                  and s["conditions"][0]["payload"]["values"] == [tag["uuid"]]
                  and s["actions"][0]["payload"]["values"] == [drip["uuid"]])
            problems += not ok
            print(f"  read-back {'OK' if ok else 'MISMATCH'}: tag {tag['uuid']} drip {drip['uuid']} "
                  f"(delay {acts[0]['details']}) seq {s['uuid']} active={s.get('is_active')}")
    return 1 if problems else 0


# ----------------------------------------------------------------- run
def _sent(h: dict) -> int:
    return sum(1 for x in (h.get("details") or {}).get("log") or []
               if x.get("action") == "send_sms" and x.get("status") == "ok")


def _source(h: dict) -> dict:
    return (((h.get("details") or {}).get("event_extra_info") or {}).get("source") or {})


def plan(histories: list[dict], now_ct: dt.datetime, holiday_seq_uuids: set[str]) -> dict:
    move, early, later_today, already = [], [], [], []
    for h in histories:
        if h.get("status") != "running":
            continue
        src = _source(h).get("uuid")
        if src in holiday_seq_uuids:
            already.append(h)
            continue
        line = SRC_SEQ_LINE.get(src)
        if not line:
            continue
        nxt = pts.to_ct((h.get("details") or {}).get("next_run_at") or "")
        if nxt is None:
            continue
        row = {"history": h.get("uuid"), "property": h.get("property"), "line": line,
               "sent": _sent(h), "next": nxt.strftime("%a %m-%d %H:%M")}
        if nxt.date() == SKIP_DAY:
            (move if row["sent"] >= 2 else early).append(row)
        elif nxt.date() == now_ct.date() and nxt > now_ct:
            later_today.append(row)
    return {"move": move, "early": early, "later_today": later_today, "already": len(already)}


def line_tag_uuids(api) -> list[str]:
    tags = {t.get("title"): t["uuid"] for t in _all(api, "/api/internal/tag/")}
    return [tags[f"SMS Line {l}"] for l in LINES if f"SMS Line {l}" in tags]


def map_histories(api, want: set[str]) -> dict[str, str]:
    """history uuid -> property uuid. Drip histories only carry DataSift's internal property number,
    which no property endpoint accepts, so walk the line-tagged records (Prospecting first) and read
    each one's drips until every wanted history is found."""
    found: dict[str, str] = {}
    if not want:
        return found
    tags = line_tag_uuids(api)
    first = api.search({"any_tags": tags, "any_property_status": ["prospecting"]})
    seen = {r["uuid"] for r in first}
    rest = [r for r in api.search({"any_tags": tags}) if r["uuid"] not in seen]
    for row in first + rest:
        for h in pts.drip_rows(api, row["uuid"]):
            if h.get("history_uuid") in want:
                found[h["history_uuid"]] = row["uuid"]
        if want <= set(found):
            break
    return found


def prop_tags(api, uuid: str) -> list[str]:
    p = api.call(f"/api/internal/property/{uuid}/")
    return [t.get("title") if isinstance(t, dict) else t for t in (p.get("tags") or [])]


def set_seqs(api, seqs: dict, active: bool) -> list[str]:
    bad = []
    for line, s in seqs.items():
        api.call(f"/api/internal/sequence/{s['uuid']}/", "PATCH", {"is_active": active})
        if bool(api.call(f"/api/internal/sequence/{s['uuid']}/").get("is_active")) != active:
            bad.append(line)
    return bad


def run(api, live: bool, allow_any_day: bool) -> int:
    now = pts.ct_now()
    if now.date() >= SKIP_DAY:
        print(f"refusing: today {now.date()} is on/after the skip day {SKIP_DAY}")
        return 2
    if live and now.date() != RUN_DAY and not allow_any_day:
        print(f"refusing live run: today is {now.date()}, the move is for {RUN_DAY} (use --allow-any-day)")
        return 2
    found = find_objects(api)
    missing = [l for l, f in found.items() if not all(f.values())]
    if missing:
        print(f"holiday objects missing for lines {missing}: run `setup --live` first")
        return 2
    seqs = {l: f["seq"] for l, f in found.items()}
    p = plan(_all(api, "/api/internal/drip-campaign-history/"), now, {f["seq"]["uuid"] for f in found.values()})
    report = [f"*Thanksgiving drip move* ({'LIVE' if live else 'dry run'}, {now:%a %m-%d %H:%M} CT)",
              f"Due Thursday: {len(p['move'])} to move to Friday's last text, {len(p['early'])} too early in the drip "
              f"(removed, handle by hand), {p['already']} already on a holiday drip."]
    for r in p["later_today"]:
        report.append(f"WARN still due later today: property {r['property']} Line {r['line']} at {r['next']}")
    state = {"run_at": now.isoformat(), "live": live, **p, "results": []}
    if not live:
        for r in p["move"] + p["early"]:
            report.append(f"  would move property {r['property']} Line {r['line']} (sent {r['sent']}, next {r['next']})")
        print("\n".join(report))
        return 0

    minutes = minutes_until_send()
    for line, f in found.items():   # retime each holiday drip to land Fri 9:50 CT
        d = api.call(f"/api/internal/drip-campaigns/{f['drip']['uuid']}/")
        acts = sorted(d["actions"], key=lambda a: a["order"])
        acts[0]["details"] = {"unit": "minutes", "amount": minutes}
        api.call(f"/api/internal/drip-campaigns/{d['uuid']}/", "PATCH", {"actions": acts})
    report.append(f"Holiday drips set to send in {minutes} min (Fri 11-27 ~9:50 CT).")

    results = []
    bad = set_seqs(api, seqs, True)
    try:
        if bad:
            report.append(f"ERROR could not switch on holiday sequences {bad}; nothing moved")
            raise RuntimeError("activate failed")
        hmap = map_histories(api, {r["history"] for r in p["move"] + p["early"]})
        for r in p["move"] + p["early"]:
            res = {**r}
            try:
                uuid = hmap.get(r["history"])
                if not uuid:
                    res["result"] = "ERROR record not found (still on its 5-day drip) - CHECK"
                    results.append(res)
                    continue
                tags = prop_tags(api, uuid)
                res["uuid"] = uuid
                if tag_title(r["line"]) in tags:   # a past tag would not re-trigger the sequence
                    api.call(f"/api/internal/drip-campaign-history/{r['history']}/", "DELETE")
                    res["result"] = "removed only (already had holiday tag) - CHECK"
                elif r["sent"] < 2:
                    api.call(f"/api/internal/drip-campaign-history/{r['history']}/", "DELETE")
                    res["result"] = "removed only (early in drip) - CHECK"
                else:
                    api.call(f"/api/internal/drip-campaign-history/{r['history']}/", "DELETE")
                    api.call(f"/api/internal/property/{uuid}/", "PATCH", {"tags": [tag_title(r["line"])]})
                    res["result"] = "moved"
            except Exception as e:  # noqa: BLE001
                res["result"] = f"ERROR {str(e)[:150]}"
            results.append(res)

        # verify: exactly one running drip per moved record, from its holiday sequence
        pending = [x for x in results if x["result"] == "moved"]
        for _ in range(8):
            time.sleep(40)
            still = []
            for x in pending:
                running = [h for h in pts.drip_rows(api, x["uuid"]) if h.get("status") == "running"]
                if len(running) == 1 and running[0].get("campaign_uuid") == found[x["line"]]["drip"]["uuid"]:
                    x["result"] = "moved+verified"
                else:
                    x["verify"] = [(h.get("name"), h.get("next_in")) for h in running]
                    still.append(x)
            pending = still
            if not pending:
                break
    finally:
        bad_off = set_seqs(api, seqs, False)
        if bad_off:
            report.append(f"ERROR holiday sequences still ON for {bad_off}: switch them off in DataSift")

    for x in results:
        if x.get("uuid") and x["result"].startswith(("moved", "removed")):
            try:
                api.call(f"/api/internal/property/{x['uuid']}/message/", "POST", {"message": NOTE})
            except Exception as e:  # noqa: BLE001
                x["note"] = f"note failed: {str(e)[:80]}"
    ok = sum(x["result"] == "moved+verified" for x in results)
    report.append(f"Result: {ok}/{len(p['move'])} moved and verified.")
    for x in results:
        if x["result"] != "moved+verified":
            report.append(f"  CHECK property {x['property']} Line {x['line']}: {x['result']} {x.get('verify', '')}")
    state["results"] = results
    pts.STATE_DIR.mkdir(parents=True, exist_ok=True)
    (pts.STATE_DIR / f"holiday_drip_pause_{now:%Y-%m-%d}.json").write_text(json.dumps(state, indent=1, default=str))
    text = "\n".join(report)
    print(text)
    pts._slack(text)
    return 0 if ok == len(p["move"]) and not p["early"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["setup", "run"])
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--allow-any-day", action="store_true")
    a = ap.parse_args(argv)
    api = pts.Sift(pts._env("REISIFT_API_KEY"))
    return setup(api, a.live) if a.mode == "setup" else run(api, a.live, a.allow_any_day)


if __name__ == "__main__":
    sys.exit(main())
