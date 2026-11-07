"""Tests for p1_enrich: which auto-added P1 records get scored or traced, how tier tags are written,
the litigator fail-safe, Tracerfy matching, caps, and that a dry run pays for and writes nothing."""
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import p1_enrich as pe  # noqa: E402
import weekly_topup as wt  # noqa: E402

A, B, C = "8165550001", "8165550002", "8165550003"
P1_UUID, LIST_UUID = "tag-p1", "list-1"


def _phone(number, type_="MOBILE", tags=(), status="UNKNOWN"):
    return {"number": number, "type": type_, "status": status, "is_connected": True, "tags": list(tags)}


def _det(uuid, phones=(), owner_uuid=None, first="JANE", last="DOE", type_="person", tags=(), company=None):
    return {"uuid": uuid, "status": None, "assigned_to": None, "tags": [{"title": t} for t in tags],
            "address": {"street": "1 MAIN ST", "city": "KANSAS CITY", "state": "MO", "zip5": "64130"},
            "owner": {"uuid": owner_uuid or "o-" + uuid, "type": type_, "first_name": first, "last_name": last,
                      "company": company, "phones": list(phones),
                      "address": {"street": "9 MAIL RD", "city": "KANSAS CITY", "state": "MO", "zip5": "64131"}}}


def _sc(score, litigator=False, line_type="Mobile"):
    return {"score": score, "line_type": line_type, "litigator": litigator}


class FakeApi:
    """GET-by-path fake of pm_text_sender.Sift; owner PATCH echoes the new list back."""

    def __init__(self, dets, echo=True):
        self.dets = {d["uuid"]: d for d in dets}
        self.owners = {d["owner"]["uuid"]: {"phones": d["owner"]["phones"]} for d in dets}
        self.echo, self.writes = echo, []

    def call(self, path, method="GET", body=None, override=None):
        if path.startswith("/api/internal/list/"):
            return {"results": [{"title": wt.LIST_PREFIX + wt.RUNGS[0], "uuid": LIST_UUID}]}
        if path.startswith("/api/internal/tag/") and method == "GET":
            return {"results": [{"title": wt.P1_TAG, "uuid": P1_UUID}, {"title": pe.TRACED_TAG, "uuid": "t"}]}
        if method != "GET":
            self.writes.append((method, path, body))
            if path.startswith("/api/internal/owner/") and method == "PATCH":
                return {"phones": body["phones"] if self.echo else []}
            return {}
        if path.startswith("/api/internal/owner/"):
            return self.owners[path.split("/")[4]]
        return self.dets[path.split("/")[4]]

    def search(self, must, limit=200):
        assert must == {"any_lists": [LIST_UUID], "any_tags": [P1_UUID]}
        return [{"uuid": u, "status": d["status"]} for u, d in self.dets.items()]


# ------------------------------------------------------------ classify / plan

def test_classify_buckets():
    assert pe.classify(_det("r", [_phone(A, tags=["Dial First"])]))[0] == "ready"
    assert pe.classify(_det("s", [_phone(A), _phone(B, "LANDLINE")])) == ("score", [A, B])
    assert pe.classify(_det("t"))[0] == "trace"
    assert pe.classify(_det("t2", [_phone(A, tags=["Dial Fourth"])]))[0] == "trace"
    assert pe.classify(_det("t3", [_phone(A, "LANDLINE")]))[0] == "trace"  # landline alone can't be primary
    assert pe.classify(_det("x", tags=[pe.TRACED_TAG]))[0] == "traced"
    assert pe.classify(_det("e", type_="company", first="", company="4529 BENTON LLC"))[0] == "entity"
    assert pe.classify(_det("m", tags=["Mail Only"]))[0].startswith("skip:")
    # a litigator-tagged mobile is never re-scored into a Dial tier (newer top-up code suppresses the owner)
    assert pe.classify(_det("l", [_phone(A, tags=[pe.LITIGATOR_TAG])]))[0] in ("trace", "skip:litigator tag on ...0001")


def test_plan_groups_by_owner_and_reports_entities():
    dets = [_det("p1", [_phone(A)], owner_uuid="o1"), _det("p2", owner_uuid="o1"),
            _det("p3", [_phone(B, tags=["Dial Second"])]),
            _det("p4", type_="company", first="", company="4529 BENTON LLC")]
    p = pe.plan(FakeApi(dets))
    assert p["counts"] == {"score": 1, "trace": 1, "ready": 1, "entity": 1}
    [o] = p["owners"]
    assert o["owner_uuid"] == "o1" and o["props"] == ["p1", "p2"] and o["unscored"] == [A] and o["trace"]
    assert p["entities"][0]["owner"] == "4529 BENTON LLC"


# ------------------------------------------------------------ phone list

def test_build_phones_sets_tier_and_keeps_other_tags():
    before = [_phone(A, tags=["smartskip"]), _phone(B, tags=["Dial Third"])]
    out = pe.build_phones(before, {A: _sc(90), B: _sc(95)}, [])
    assert out[0]["tags"] == ["smartskip", "Dial First"]
    assert out[1]["tags"] == ["Dial Third"]  # already scored: left alone


def test_litigator_gets_no_dial_tier():
    out = pe.build_phones([_phone(A)], {A: _sc(99, litigator=True)}, [(B, "MOBILE")])
    assert out[0]["tags"] == [pe.LITIGATOR_TAG]
    assert not pe._best_primary({p["number"]: p for p in out})


def test_tracerfy_numbers_appended_once_with_type_from_trestle():
    out = pe.build_phones([_phone(A)], {B: _sc(70, line_type="Landline"), C: _sc(85)},
                          [(A, "MOBILE"), (B, None), (C, None)])
    assert [p["number"] for p in out] == [A, B, C]
    assert out[1]["type"] == "LANDLINE" and out[1]["tags"] == ["tracerfy", "Dial Second"]
    assert out[2]["type"] == "MOBILE" and out[2]["tags"] == ["tracerfy", "Dial First"]


def test_score_error_writes_no_tier():
    assert pe.build_phones([_phone(A)], {A: {"error": "HTTP 400"}}, [])[0]["tags"] == []


# ------------------------------------------------------------ tracerfy matching

def test_match_by_name_then_address():
    o1 = {"owner_uuid": "o1", "first": "Jane", "last": "Doe", "address": {"street": "1 MAIN ST"}}
    o2 = {"owner_uuid": "o2", "first": "Jane", "last": "Doe", "address": {"street": "7 OAK AVE"}}
    recs = [{"first_name": "JANE", "last_name": "DOE", "address": "7 Oak Ave", "primary_phone": "(816) 555-0001",
             "mobile_1": "8165550001", "landline_1": B},
            {"first_name": "Bob", "last_name": "Roe", "mobile_1": C}]
    m = pe.match_tracerfy([o1, o2], recs)
    assert m == {"o2": [(A, None), (B, "LANDLINE")]}


# ------------------------------------------------------------ run

def _plan(dets):
    return pe.plan(FakeApi(dets))


def test_run_writes_scores_traces_and_tags(tmp_path):
    dets = [_det("p1", [_phone(A)]), _det("p2", first="BOB", last="ROE")]
    api = FakeApi(dets)
    p = pe.plan(api)
    recs = [{"first_name": "Bob", "last_name": "Roe", "mobile_1": B, "mobile_2": C}]
    scores = {A: _sc(30), B: _sc(88), C: _sc(99, litigator=True)}
    with patch.object(pe, "tracerfy", return_value=recs), \
            patch.object(pe, "trestle", side_effect=lambda n, k: scores[n]):
        res = pe.run(api, p, "tk", "fk", 60, 400, tmp_path / "log.json")
    assert res["written"] == 2 and res["now_ready"] == 1 and res["litigator"] == 1 and not res["failed"]
    patches = {path: body for m, path, body in api.writes if m == "PATCH"}
    assert patches["/api/internal/owner/o-p1/"]["phones"][0]["tags"] == ["Dial Fourth"]
    bob = patches["/api/internal/owner/o-p2/"]["phones"]
    assert [p["tags"] for p in bob] == [["tracerfy", "Dial First"], ["tracerfy", pe.LITIGATOR_TAG]]
    dnc = [b for m, path, b in api.writes if path.endswith("upsert-phones/")]
    assert dnc == [{"phones": [{"number": C, "type": "MOBILE", "tags": ["tracerfy", pe.LITIGATOR_TAG],
                                "status": "DNC", "is_connected": True, "verified": False}]}]
    tagged = [b for m, path, b in api.writes if path.endswith("add-tags/")]
    assert tagged == [{"query": {"must": {"properties": ["p2"]}, "ordering": ["-list_count"]}, "tags": [pe.TRACED_TAG]}]
    assert (tmp_path / "log.json").exists()


def test_readback_mismatch_is_a_failure(tmp_path):
    api = FakeApi([_det("p1", [_phone(A)])], echo=False)
    with patch.object(pe, "trestle", return_value=_sc(90)):
        res = pe.run(api, pe.plan(api), "tk", "", 60, 400, tmp_path / "log.json")
    assert res["failed"] == ["JANE DOE"] and res["written"] == 0


def test_caps_limit_paid_calls(tmp_path):
    dets = [_det(f"p{i}", first=f"F{i}") for i in range(5)] + \
           [_det("s", [_phone(f"81655501{i:02d}") for i in range(10)])]
    api = FakeApi(dets)
    sent, scored = [], []
    with patch.object(pe, "tracerfy", side_effect=lambda o, k, **kw: sent.extend(o) or []), \
            patch.object(pe, "trestle", side_effect=lambda n, k: scored.append(n) or _sc(50)):
        res = pe.run(api, pe.plan(api), "tk", "fk", 2, 3, tmp_path / "log.json")
    assert len(sent) == 2 and len(scored) == 3
    assert "3 owners left to trace" in res["capped"] and "7 phones left to score" in res["capped"]


def test_no_tracerfy_key_skips_trace_but_still_scores(tmp_path):
    api = FakeApi([_det("p1"), _det("p2", [_phone(A)])])
    with patch.object(pe, "tracerfy") as tr, patch.object(pe, "trestle", return_value=_sc(90)):
        res = pe.run(api, pe.plan(api), "tk", "", 60, 400, tmp_path / "log.json")
    tr.assert_not_called()
    assert "not set" in res["trace_error"] and res["written"] == 1
    assert not [w for w in api.writes if w[1].endswith("add-tags/")]  # untraced records stay traceable


def test_dry_run_main_pays_and_writes_nothing(tmp_path, capsys):
    api = FakeApi([_det("p1"), _det("p2", [_phone(A)])])
    with patch.object(pe, "Sift", return_value=api), patch.object(pe, "STATE_DIR", tmp_path), \
            patch.object(pe, "trestle") as tl, patch.object(pe, "tracerfy") as tr, \
            patch.object(sys, "argv", ["p1_enrich.py"]):
        assert pe.main() == 0
    tl.assert_not_called()
    tr.assert_not_called()
    assert api.writes == []
    assert "DRY RUN" in capsys.readouterr().out


def test_rejected_trestle_key_stops_before_paying_tracerfy(tmp_path):
    api = FakeApi([_det("p1"), _det("p2", [_phone(A)])])
    with patch.object(pe, "tracerfy") as tr, \
            patch.object(pe, "trestle", side_effect=RuntimeError("Trestle rejected the API key (403)")):
        try:
            pe.run(api, pe.plan(api), "tk", "fk", 60, 400, tmp_path / "log.json")
            assert False, "expected RuntimeError"
        except RuntimeError:
            pass
    tr.assert_not_called()
    assert api.writes == []


def test_reused_tracerfy_queue_is_passed_through(tmp_path):
    api = FakeApi([_det("p1", first="BOB", last="ROE")])
    recs = [{"first_name": "Bob", "last_name": "Roe", "mobile_1": B}]
    with patch.object(pe, "tracerfy", return_value=recs) as tr, patch.object(pe, "trestle", return_value=_sc(90)):
        res = pe.run(api, pe.plan(api), "tk", "fk", 60, 400, tmp_path / "log.json", tracerfy_queue="221705")
    assert tr.call_args.kwargs["queue_id"] == "221705"
    assert res["written"] == 1 and res["now_ready"] == 1
