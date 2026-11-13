"""Tests for the obituary deep-prospecting job: selection, the date-of-death sanity rule, heir
matching (the obituary confirms, SmartSkip supplies the people), the phone merge, the notes, and
the SmartSkip vertical-export parser."""
import datetime as dt
import sys
from argparse import Namespace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import obituary_dp as od  # noqa: E402
import smartskip  # noqa: E402

TODAY = dt.date(2026, 11, 13)


def _d(**kw):
    d = {"uuid": "p1", "status": None, "assigned_to": None, "tags": ["Obituary", "Obit 6mo+"],
         "address": {"street": "4614 Lawn Ave", "city": "Kansas City", "state": "MO", "county": "Jackson", "postal_code": "64130"},
         "owner": {"uuid": "o1", "type": "person", "first_name": "Nellie", "last_name": "Monroe", "deceased": False, "phones": []},
         "last_obituary_date": "2026-05-01", "secondary_owners": []}
    d.update(kw)
    return d


def _rel(first, last, rel="Child", phones=("8165550101",), deceased=False):
    return {"first": first, "last": last, "name": f"{first} {last}", "rel": rel, "age": "50", "deceased": deceased,
            "phones": [{"number": n, "type": "Mobile"} for n in phones]}


# ----------------------------------------------------------------- select
def test_eligible_keeps_a_clean_jackson_record_and_a_deceased_owner():
    assert od.eligible(_d(), TODAY) is None
    dead = _d(owner={**_d()["owner"], "deceased": True})
    assert od.eligible(dead, TODAY) is None          # the heir case is the point, not a suppression


def test_eligible_close_out_only_when_obituary_is_newer():
    rec = _d(status="close_out", last_obituary_date="2026-05-01")
    assert od.eligible(rec, TODAY, closed_on=dt.date(2026, 3, 1)) is None              # died after we closed it
    assert od.eligible(rec, TODAY, closed_on=dt.date(2026, 8, 15)).startswith("status close_out")  # obit predates close-out
    assert od.eligible(rec, TODAY, closed_on=dt.date(2026, 5, 1)).startswith("status close_out")   # same day: not newer
    assert "date unknown" in od.eligible(rec, TODAY)                                   # no close-out date: skip
    assert od.eligible(_d(status="close_out", last_obituary_date=None), TODAY, closed_on=dt.date(2026, 3, 1)).startswith("status close_out")


def test_closed_out_on_reads_latest_close_out_from_logs():
    class Api:
        def call(self, path):
            assert path.startswith("/api/internal/property/p1/logs/")
            return {"results": [
                {"event_type": "property.status.updated", "timestamp": "2026-06-02T15:00:00Z", "payload": {"property": {"status": [None, "close_out"]}}},
                {"event_type": "property.status.updated", "timestamp": "2026-07-01T15:00:00Z", "payload": {"property": {"status": ["close_out", "prospecting"]}}},
                {"event_type": "property.status.updated", "timestamp": "2026-09-10T15:00:00Z", "payload": {"property": {"status": ["prospecting", "close_out"]}}},
                {"event_type": "owner.call.made", "timestamp": "2026-09-20T15:00:00Z", "payload": {}}]}
    assert od.closed_out_on(Api(), "p1") == dt.date(2026, 9, 10)

    class Broken:
        def call(self, path):
            raise RuntimeError("500")
    assert od.closed_out_on(Broken(), "p1") is None


def test_eligible_refusals():
    assert "DP Review" in od.eligible(_d(tags=["Obit 6mo+", "DP Review"]), TODAY)
    assert "DP Researched" in od.eligible(_d(tags=["Obit 6mo+", "dp researched"]), TODAY)
    assert "Deep Prospecting" in od.eligible(_d(tags=["Obit 6mo+", "Deep Prospecting"]), TODAY)
    assert od.eligible(_d(address={**_d()["address"], "county": "Oklahoma", "state": "OK"}), TODAY) == "other county"
    assert od.eligible(_d(owner={"type": "company", "first_name": ""}), TODAY) == "not a person"
    assert od.eligible(_d(status="Warm Lead"), TODAY).startswith("status")
    assert od.eligible(_d(status="not_interested"), TODAY).startswith("status")
    assert od.eligible(_d(assigned_to="u-rahaf"), TODAY) == "assigned"
    assert "Do Not Market" in od.eligible(_d(tags=["Obit 6mo+", "Do Not Market"]), TODAY)
    assert "under 90 days" in od.eligible(_d(last_obituary_date="2026-10-01"), TODAY)


def test_eligible_stays_inside_the_p1_neighborhoods():
    assert od.eligible(_d(neighborhood="Vineyard - Kansas City"), TODAY) is None
    assert od.eligible(_d(neighborhood="Fairlane - Kansas City, MO"), TODAY) is None
    assert od.eligible(_d(neighborhood="Cler-mont"), TODAY) == "outside P1 neighborhoods"
    assert od.eligible(_d(neighborhood=None), TODAY) is None     # missing data passes


def test_missing_obituary_date_is_kept():
    assert od.eligible(_d(last_obituary_date=None), TODAY) is None


# ----------------------------------------------------------------- screen
def test_parse_json_takes_the_last_object():
    text = 'I found {"a": 1} first.\nFinal:\n{"classification": "owner_deceased", "survivors": [{"name": "X"}]}'
    assert od.parse_json(text)["classification"] == "owner_deceased"


def test_sanity_downgrades_stale_and_future_deaths():
    obit = dt.date(2026, 5, 1)
    ok = od.sanity({"classification": "owner_deceased", "date_of_death": "2026-04-20"}, obit, TODAY)
    assert ok["classification"] == "owner_deceased"
    stale = od.sanity({"classification": "owner_deceased", "date_of_death": "2019-02-01"}, obit, TODAY)
    assert stale["classification"] == "not_found" and "stale match" in stale["notes"]
    future = od.sanity({"classification": "owner_deceased", "date_of_death": "2027-01-01"}, obit, TODAY)
    assert future["classification"] == "not_found"


# ----------------------------------------------------------------- heirs
def test_matching_catches_married_daughters_and_refuses_strangers():
    assert od.matches("Linda (Monroe) Jackson", _rel("Linda", "Jackson"))
    assert od.matches("Linda Monroe Jackson", _rel("Linda", "Monroe"))
    assert not od.matches("Linda Jackson", _rel("Lisa", "Jackson"))
    assert not od.matches("Linda Jackson", _rel("Linda", "Smith"))


def test_heirs_come_from_smartskip_with_obituary_relationships():
    screen = {"survivors": [{"name": "Linda (Monroe) Jackson", "relationship": "daughter"},
                            {"name": "Carl Monroe", "relationship": "son"}]}
    rec = {"relatives": [_rel("Linda", "Jackson", rel="Relative"), _rel("Ray", "Monroe", phones=("8165550199",)),
                         _rel("Carl", "Monroe", deceased=True)],
           "subject_phones": [{"number": "8165550000", "type": "Landline"}]}
    people, missing = od.heirs(screen, rec)
    assert [(p["name"], p["relationship"]) for p in people] == [("Linda Jackson", "Daughter"), ("Decedent's own lines", "owner")]
    assert missing == ["Carl Monroe (son)"]       # SmartSkip's deceased Carl isn't a callable heir; Ray isn't in the obituary


def test_no_survivors_falls_back_to_close_relatives_marked_unconfirmed():
    people, _ = od.heirs({"survivors": []}, {"relatives": [_rel("Ann", "Monroe", rel="Sibling"), _rel("Bo", "Lee", rel="Relative")]})
    assert [p["relationship"] for p in people] == ["Sibling (unconfirmed)"]


def test_numbers_to_score_skips_heir_landlines_and_known_numbers():
    people = [{"phones": [{"number": "1", "type": "Mobile"}, {"number": "2", "type": "Landline"}, {"number": "3", "type": ""}],
               "owner_line": False},
              {"phones": [{"number": "4", "type": "Landline"}, {"number": "1", "type": "Mobile"}], "owner_line": True}]
    assert od.numbers_to_score(people, existing={"3"}) == ["1", "4"]


def test_merge_keeps_existing_and_drops_drop_and_litigators():
    before = [{"number": "8165550000", "type": "LANDLINE", "tags": ["Dial Third"], "status": "WRONG"}]
    people = [{"name": "Linda Jackson", "relationship": "Daughter", "owner_line": False,
               "phones": [{"number": n, "type": "Mobile"} for n in ("8165550000", "8165550101", "8165550102", "8165550103")]}]
    scores = {"8165550101": {"score": 90, "line_type": "Mobile", "litigator": False},
              "8165550102": {"score": 10, "line_type": "Mobile", "litigator": False},
              "8165550103": {"score": 95, "line_type": "Mobile", "litigator": True}}
    phones, added = od.merge_phones(before, people, scores)
    assert phones[0] == before[0] and len(phones) == 2
    assert phones[1] == {"number": "8165550101", "type": "MOBILE", "status": "UNKNOWN", "is_connected": True,
                         "tags": ["Dial First", "DP Heir", "SmartSkip"]}
    assert added == [{"number": "8165550101", "tier": "Dial First", "line_type": "MOBILE", "who": "Linda Jackson", "relationship": "Daughter"}]
    assert not any("Linda" in t for p in phones for t in p.get("tags") or [])   # never a name in a phone tag


# ----------------------------------------------------------------- notes
def test_heir_note_lists_call_order_by_last_four():
    s = {"decedent_name": "Nellie Monroe", "date_of_death": "2026-04-20", "dod_source": "Legacy.com", "confidence": "high",
         "obituary_links": ["https://example.com/obit"]}
    added = [{"number": "8165550101", "tier": "Dial Second", "line_type": "MOBILE", "who": "Linda Jackson", "relationship": "Daughter"},
             {"number": "8165550222", "tier": "Dial First", "line_type": "MOBILE", "who": "Ray Monroe", "relationship": "Son"},
             {"number": "8165550333", "tier": "Dial Third", "line_type": "LANDLINE", "who": "Ray Monroe", "relationship": "Son"}]
    note = od.heir_note(s, added, ["Carl Monroe (son)"], TODAY)
    assert "PENDING REVIEW" in note and "8165550" not in note
    assert note.index("1. Ray Monroe - Son: x0222 (First, mobile)") < note.index("2. Linda Jackson - Daughter: x0101")
    assert "x0333" not in note and "Carl Monroe (son)" in note and "text by hand" in note


def test_living_note_warns_off_asking_for_the_deceased():
    note = od.living_note({"classification": "relative_deceased", "decedent_name": "Ruth Baynham",
                           "relationship_to_owner": "wife", "date_of_death": "2026-03-02", "confidence": "high"}, TODAY)
    assert "OWNER IS LIVING" in note and "Ruth Baynham (wife" in note and "Do not ask for the deceased" in note


def test_plan_also_searches_the_siftmap_pull_tag(monkeypatch):
    seen = []
    api = Namespace(search=lambda must: seen.append(must) or [])
    monkeypatch.setattr(od, "tag_uuids", lambda api: {od.PULL_TAG: "pull-uuid", od.RESEARCHED_TAG: "r-uuid"})
    od.plan(api, "Jackson", TODAY)
    assert seen[0]["any_tags"] == list(od.OBIT_TAGS) + ["pull-uuid"]
    assert seen[0]["must_not"] == {"any_tags": ["r-uuid"]}
    monkeypatch.setattr(od, "tag_uuids", lambda api: {})     # preset not created yet
    od.plan(api, "Jackson", TODAY)
    assert seen[1]["any_tags"] == list(od.OBIT_TAGS)


# ----------------------------------------------------------------- write
class FakeApi:
    def __init__(self, phones):
        self.phones, self.calls = phones, []

    def call(self, path, method="GET", body=None, override=None):
        self.calls.append((method, path, body))
        if path.startswith("/api/internal/owner/"):
            if method == "PATCH":
                self.phones = body["phones"]
            return {"phones": self.phones}
        return {}


def test_write_record_heir_case(monkeypatch):
    tagged = []
    monkeypatch.setattr(od, "add_tags", lambda api, uuids, titles: tagged.append((uuids, titles)))
    api = FakeApi([{"number": "8165550000", "type": "LANDLINE", "tags": []}])
    people = [{"name": "Linda Jackson", "relationship": "Daughter", "owner_line": False,
               "phones": [{"number": "8165550101", "type": "Mobile"}]}]
    log = {}
    ok = od.write_record(api, _d(), {"classification": "owner_deceased", "decedent_name": "Nellie Monroe"}, people, [],
                         {"8165550101": {"score": 70, "line_type": "Mobile"}}, {}, TODAY, log)
    assert ok and [p["number"] for p in api.phones] == ["8165550000", "8165550101"]
    assert any(m == "POST" and p.endswith("/message/") for m, p, _ in api.calls)
    assert tagged == [(["p1"], ["DP Researched", "DP Review"])]
    assert log["p1"]["added"][0]["tier"] == "Dial Second"


def test_write_record_not_found_only_marks_researched(monkeypatch):
    tagged = []
    monkeypatch.setattr(od, "add_tags", lambda api, uuids, titles: tagged.append(titles))
    api = FakeApi([])
    od.write_record(api, _d(), {"classification": "not_found"}, [], [], {}, {}, TODAY, {})
    assert tagged == [["DP Researched"]] and api.calls == []


def test_report_plan_only_estimates_cost():
    p = {"records": [_d()], "counts": {"eligible": 1, "other": 3}}
    text = od.report(p, None, Namespace(live=False, screen=False, max_records=10, max_skip=10, max_score=120))
    assert "plan only" in text and "Nellie Monroe" in text and "Est. cost" in text


# ----------------------------------------------------------------- smartskip
VERTICAL = """Input Name,Relationship,First Name,Last Name,Property Address,Deceased,Possible Type,Age,Phone 1 number,Phone 1 type,Phone 2 number,Phone 2 type
Nellie Monroe,Subject,Nellie,Monroe,4614 Lawn Ave,Yes,,,(816) 555-0000,Landline,,
Nellie Monroe,Relative,Linda,Jackson,,,Child,52,1-816-555-0101,Mobile,816.555.0101,Mobile
Nellie Monroe,Relative,Old,Monroe,,,Parent,104,8165550199,Mobile,,
Nellie Monroe,Associate,Bob,Neighbor,,,,40,8165550188,Mobile,,
"""


def test_smartskip_parse():
    [rec] = smartskip.parse(VERTICAL)
    assert rec["deceased"] and rec["subject_phones"] == [{"number": "8165550000", "type": "Landline"}]
    assert [(r["name"], r["rel"]) for r in rec["relatives"]] == [("Linda Jackson", "Child")]   # age 104 + associate dropped
    assert rec["relatives"][0]["phones"] == [{"number": "8165550101", "type": "Mobile"}]       # deduped


def test_match_record_by_street_then_name():
    recs = smartskip.parse(VERTICAL)
    assert od.match_record(_d(), recs) is recs[0]
    moved = _d(address={**_d()["address"], "street": "1 Elsewhere Rd"})
    assert od.match_record(moved, recs) is recs[0]
    assert od.match_record(_d(address={"street": "9 Nope St"}, owner={"first_name": "Al", "last_name": "Bee"}), recs) is None


def test_build_csv_uses_the_mapped_headers():
    text = smartskip.build_csv([od.skip_row(_d())]).decode()
    assert text.splitlines()[0] == ",".join(smartskip.SCHEMA.values())
    assert "Nellie,Monroe" in text and "4614 Lawn Ave" in text
