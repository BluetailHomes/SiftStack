"""Tests for src/daily_kpi_report.py: snapshot, markers, follow-up completeness, delivery."""
import datetime as dt
import io
import json
import os
import re
import sys
import urllib.error
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import daily_kpi_report as r  # noqa: E402
import pm_text_sender as p  # noqa: E402

R = "rahaf@bluetailhomes.com"
CT = dt.timezone(dt.timedelta(hours=-5))
FRI, MON = "2026-10-30", dt.date(2026, 11, 2)


def call(h, mi, to, dur=95, result="answered", cid=None):
    return {"ct": dt.datetime(2026, 10, 30, h, mi, tzinfo=CT), "from": "8160000000", "to": to, "dir": "outbound",
            "by": R, "dur": dur, "result": result, "rec": "https://recordings.example/secret.mp3", "id": cid}


def model(uuid="u1", street="123 Main St", calls=(), marks=(), phones=None, status_changes=()):
    return {"uuid": uuid, "street": street, "calls": list(calls), "marks": list(marks), "phones": phones or {},
            "status_changes": list(status_changes), "tags_added": [], "open_tasks": None}


def mark(to, number):
    return {"ct": dt.datetime(2026, 10, 30, 12, 0, tzinfo=CT), "phone": number, "frm": "UNKNOWN", "to": to, "by": R}


def models():
    return [model(calls=[call(10, 20, "8165551234", cid="smrt-42"), call(14, 5, "8165550000", dur=20, result="noanswer"),
                         call(16, 40, "8165557777", dur=130)],
                  marks=[mark("CORRECT", "8165551234"), mark("NO_ANSWER", "8165550000")],
                  status_changes=[{"to": "warm lead"}])]


def snap(live=True, stats=None, generated="2026-10-30T19:16:00-05:00"):
    s = r.build_snapshot(models(), R, FRI, stats or {"selected": 40, "skipped": 0, "errors": 0},
                         now=dt.datetime.fromisoformat(generated))
    s["live"] = live if live is not None else s["live"]
    return s


def save(folder, s, name=None):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / (name or f"kpi_snapshot_{s['day']}.json")).write_text(json.dumps(s), encoding="utf-8")


FU_OK = {"ok": True, "today": 1, "overdue": 2, "oldest": dt.date(2026, 10, 1), "tomorrow": 0, "no_due": 0,
         "show": [{"due": dt.datetime(2026, 11, 2, 14, 0, tzinfo=CT), "all_day": False, "uuid": "p1",
                   "title": "HOT Follow-Up", "street": "9 Elm St"}]}


def args(tmp_path, **kw):
    base = dict(day=None, today=MON.isoformat(), snapshots=str(tmp_path / "snaps"), markers=str(tmp_path / "markers"),
                out=str(tmp_path / "out"), send=False, force=False, alert=False, only_between=None)
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def no_live_offer_reads(monkeypatch):
    """Tests never read the real account; tests that need offers inject them."""
    monkeypatch.setattr(r, "load_offers", lambda *a, **kw: {"ok": False, "why": "test: no network"})


class Poster:
    def __init__(self, ok=True):
        self.ok, self.sent = ok, []

    def __call__(self, text, url):
        self.sent.append(text)
        return self.ok


# ------------------------------------------------------------ snapshot
def test_snapshot_has_no_full_phone_or_recording(tmp_path):
    path = r.write_snapshot(models(), R, FRI, {"selected": 1, "skipped": 0, "errors": 0}, folder=tmp_path)
    raw = path.read_text(encoding="utf-8")
    assert path.name == f"kpi_snapshot_{FRI}.json"
    assert not re.search(r"\d{7,}", raw.replace("2026", "")), "no phone number beyond the last 4"
    assert "recording" not in raw and "http" not in raw
    s = json.loads(raw)
    assert s["kpis"]["dials"] == 3 and s["kpis"]["qualified"] == 1
    assert [(c["last4"], c["id"]) for c in s["calls60"]] == [("1234", "smrt-42"), ("7777", None)]


def test_find_snapshot_prefers_live_then_newest_and_skips_bad_files(tmp_path):
    save(tmp_path / "run1", snap(live=True, generated="2026-10-30T19:16:00-05:00"))
    retro = snap(live=False, generated="2026-10-31T09:00:00-05:00")
    retro["kpis"]["dials"] = 1
    save(tmp_path / "run2", retro)
    (tmp_path / "run3").mkdir()
    (tmp_path / "run3" / f"kpi_snapshot_{FRI}.json").write_text("{not json", encoding="utf-8")
    old = snap()
    old["version"] = 0
    save(tmp_path / "run4", old)
    assert r.find_snapshot(tmp_path, FRI)["kpis"]["dials"] == 3          # live beats a later re-run
    assert r.find_snapshot(tmp_path, "2026-10-29") is None
    assert r.find_snapshot(tmp_path / "missing", FRI) is None


# ------------------------------------------------------------ report content
def test_report_labels_and_privacy():
    text = r.format_report(snap(), FU_OK, MON)
    assert "Your numbers for Fri Oct 30" in text
    assert "Dials: 3 visible in DataSift (provisional" in text and "target 150" in text
    assert "60+ second calls with a correct-number contact: 1 (2+ minutes: 0)" in text
    assert "All answered 60+ second calls* (2; length only, content not reviewed)" in text
    assert "meaningful" not in text.lower()
    assert "Photos received: Not tracked" in text and "Offers presented" not in text
    assert text.index("*Offers Made*") < text.index("*Activity*")
    assert "*Offers Made* (team · approved prices presented to sellers)\n• Unavailable this morning (a data issue, not a zero)." in text
    assert "10:20 AM · 123 Main St · …1234 · 95s · CORRECT · call smrt-42" in text
    assert "UNMARKED - please mark it" in text and "app.reisift.io/records/properties/u1|open record" in text
    assert not re.search(r"\d{7,}", text)
    assert "recordings.example" not in text
    assert "HOT Follow-Up · <https://app.reisift.io/records/properties/p1|9 Elm St> · due today 2:00 PM" in text
    assert "Due today: 1 · Overdue: 2 (oldest Thu Oct 1) · Due tomorrow: 0" in text
    assert "Some of yesterday's activity may be missing" not in text


def test_followups_unavailable_is_not_shown_as_zero():
    text = r.format_report(snap(), {"ok": False, "why": "task API error"}, MON)
    assert "Follow-ups are unavailable this morning. Please check your tasks in DataSift." in text
    assert "Due today: 0" not in text and "task API error" not in text


def test_incomplete_snapshot_gets_generic_note_only():
    text = r.format_report(snap(stats={"selected": 40, "skipped": 3, "errors": 1}), FU_OK, MON)
    assert "Some of yesterday's activity may be missing from these counts (a data issue, not your work)." in text
    assert "skipped" not in text and "error" not in text.lower()


def test_focus_is_objective():
    s = snap()                                   # 14:00 block hit, 10:15 and 16:45 hit too
    s["kpis"]["blocks"] = {"10:15": True, "14:00": False, "16:45": True}
    assert "missed 14:00" in r._focus(s, FU_OK)
    s["kpis"]["blocks"] = {"10:15": True, "14:00": True, "16:45": True}
    assert "60+ second call (1 left unmarked" in r._focus(s, FU_OK)


def test_long_report_fits_google_chat_limit():
    s = snap()
    s["calls60"] = [dict(s["calls60"][0], street="X" * 200) for _ in range(10)]
    fu = dict(FU_OK, show=[dict(FU_OK["show"][0], street="Y" * 200) for _ in range(10)])
    text = r.format_report(s, fu, MON)
    assert len(text) <= r.CHAT_MAX_CHARS and "Today's focus" in text


# ------------------------------------------------------------ follow-up source
class FakeApi:
    def __init__(self, tasks, count=None, page=100, fail=False):
        self.tasks, self.count, self.page, self.fail = tasks, count, page, fail

    def call(self, path):
        if self.fail:
            raise RuntimeError("HTTP 500")
        if path.startswith("/api/internal/task-preset/"):
            return {"results": [{"title": "HOT Follow-Up"}]}
        if path.startswith("/api/internal/property/"):
            return {"address": {"street": "9 Elm St"}, "owner": {"first_name": "Secret"}}
        off = int(re.search(r"offset=(\d+)", path).group(1))
        rows = self.tasks[off:off + self.page]
        return {"count": self.count if self.count is not None else len(self.tasks), "results": rows,
                "next": "more" if off + self.page < len(self.tasks) else None}


def task(i, due, who=R, title="HOT Follow-Up", completed=None):
    return {"uuid": f"t{i}", "due_date": due, "all_day": False, "completed": completed, "title": title,
            "assigned_to_user": {"email": who}, "assigned_to_property": f"p{i}"}


def test_followups_counts_and_hides_custom_titles():
    tasks = [task(1, "2026-11-02T19:00:00Z"), task(2, "2026-10-20T15:00:00Z", title="Call Jane Doe back"),
             task(3, "2026-11-03T15:00:00Z"), task(4, "2026-11-02T19:00:00Z", who="kaycie@x.com")]
    tasks += [task(i, "2026-10-29T15:00:00Z") for i in range(5, 160)]          # 2 pages
    fu = r.load_followups(FakeApi(tasks), R, MON)
    assert fu["ok"] and (fu["today"], fu["overdue"], fu["tomorrow"]) == (1, 156, 1)
    assert fu["oldest"] == dt.date(2026, 10, 20)
    assert len(fu["show"]) == r.FOLLOWUP_LIST_MAX and fu["show"][0]["street"] == "9 Elm St"
    assert all("Jane" not in t["title"] for t in fu["show"])
    assert "Follow-up" in {t["title"] for t in r.load_followups(FakeApi(tasks[:3]), R, MON)["show"]}


def test_followups_incomplete_or_failed_read_is_unavailable():
    tasks = [task(i, "2026-11-02T19:00:00Z") for i in range(3)]
    assert not r.load_followups(FakeApi(tasks, count=5), R, MON)["ok"]       # count says more exist
    assert not r.load_followups(FakeApi(tasks + [tasks[0]]), R, MON)["ok"]   # duplicate rows
    assert not r.load_followups(FakeApi(tasks, fail=True), R, MON)["ok"]


# ------------------------------------------------------------ delivery
def test_post_chat_retries_5xx_then_succeeds_and_hides_url(capsys):
    calls = []

    def opener(req, timeout):
        calls.append(req)
        if len(calls) < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "busy", {}, io.BytesIO(b""))
        return io.BytesIO(b"{}")

    assert r.post_chat("hi", "https://chat.example/hook?key=SECRET", sleep=lambda s: None, opener=opener) == "posted"
    assert len(calls) == 3 and json.loads(calls[0].data) == {"text": "hi"}
    assert "SECRET" not in capsys.readouterr().out


def test_post_chat_gives_up_and_does_not_retry_4xx():
    n = []

    def bad(req, timeout):
        n.append(1)
        raise urllib.error.HTTPError(req.full_url, 400, "bad", {}, io.BytesIO(b""))
    assert r.post_chat("hi", "https://x", sleep=lambda s: None, opener=bad) == "failed" and len(n) == 1
    n.clear()

    def down(req, timeout):                                   # never connected: safe to retry
        n.append(1)
        raise urllib.error.URLError(ConnectionRefusedError())
    assert r.post_chat("hi", "https://x", sleep=lambda s: None, opener=down) == "failed" and len(n) == r.POST_TRIES


def test_post_chat_timeout_is_uncertain_and_not_retried():
    for exc in (TimeoutError(), urllib.error.URLError(TimeoutError()), ConnectionResetError()):
        n = []

        def slow(req, timeout):
            n.append(1)
            raise exc
        assert r.post_chat("hi", "https://x", sleep=lambda s: None, opener=slow) == "uncertain" and len(n) == 1


# ------------------------------------------------------------ run(): dedup, notices, dry run
def test_dry_run_never_posts_and_monday_covers_friday(tmp_path, capsys):
    save(tmp_path / "snaps" / "123", snap())
    post, alerts = Poster(), []
    assert r.run(args(tmp_path), post=post, alert=alerts.append, followups=FU_OK) == 0
    assert post.sent == [] and "Your numbers for Fri Oct 30" in capsys.readouterr().out
    assert marker(tmp_path / "out", r.report_marker(FRI))["status"] == "dry_run"
    # A dry-run marker never blocks a later live post.
    assert r._marker_block([tmp_path / "out"], r.report_marker(FRI)) is None


def marker(folder, name):
    return json.loads((folder / name).read_text(encoding="utf-8"))


def test_send_posts_once_then_dedups_and_force_reposts(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    save(tmp_path / "snaps" / "123", snap())
    post = Poster()
    a = args(tmp_path, send=True)
    assert r.run(a, post=post, alert=print, followups=FU_OK) == 0
    name = r.report_marker(FRI)
    assert len(post.sent) == 1 and marker(tmp_path / "out", name)["status"] == "posted"
    assert r.run(a, post=post, alert=print, followups=FU_OK) == 0 and len(post.sent) == 1
    assert marker(tmp_path / "out", name)["status"] == "posted"            # the skip didn't overwrite it
    # A marker downloaded from an earlier run's artifact also blocks a repeat.
    (tmp_path / "markers" / "999").mkdir(parents=True)
    (tmp_path / "out" / name).rename(tmp_path / "markers" / "999" / name)
    assert r.run(a, post=post, alert=print, followups=FU_OK) == 0 and len(post.sent) == 1
    assert marker(tmp_path / "out", name)["status"] == "skipped"
    assert r.run(args(tmp_path, send=True, force=True), post=post, alert=print, followups=FU_OK) == 0
    assert len(post.sent) == 2


def test_missing_snapshot_sends_one_brief_notice_and_alerts_internally(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    post, alerts = Poster(), []
    a = args(tmp_path, send=True, alert=True)
    assert r.run(a, post=post, alert=alerts.append, followups=FU_OK) == 2
    assert post.sent == [r.DELAYED_NOTICE]
    assert "snapshot" not in r.DELAYED_NOTICE.lower() and "audit" not in r.DELAYED_NOTICE.lower()
    assert alerts and "no KPI snapshot" in alerts[0]
    assert r.run(a, post=post, alert=alerts.append, followups=FU_OK) == 2
    assert post.sent == [r.DELAYED_NOTICE]                       # never repeated


def test_delivery_failure_alerts_internally_and_allows_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    save(tmp_path / "snaps" / "1", snap())
    alerts = []
    a = args(tmp_path, send=True, alert=True)
    assert r.run(a, post=Poster(ok=False), alert=alerts.append, followups=FU_OK) == 1
    assert any("failed to post" in x and "Safe to re-run" in x for x in alerts)
    assert marker(tmp_path / "out", r.report_marker(FRI))["status"] == "failed"
    post = Poster()                                                        # retry is not blocked
    assert r.run(a, post=post, alert=alerts.append, followups=FU_OK) == 0 and len(post.sent) == 1
    assert marker(tmp_path / "out", r.report_marker(FRI))["status"] == "posted"


def test_uncertain_post_alerts_and_blocks_retry_until_forced(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    save(tmp_path / "snaps" / "1", snap())
    alerts = []
    a = args(tmp_path, send=True, alert=True)
    assert r.run(a, post=Poster(ok="uncertain"), alert=alerts.append, followups=FU_OK) == 1
    assert marker(tmp_path / "out", r.report_marker(FRI))["status"] == "uncertain"
    assert any("may have arrived" in x and "--force" in x for x in alerts)
    post, alerts2 = Poster(), []
    assert r.run(a, post=post, alert=alerts2.append, followups=FU_OK) == 0 and post.sent == []
    assert any("uncertain" in x and "Check the Google Chat space" in x for x in alerts2)
    assert r.run(args(tmp_path, send=True, alert=True, force=True), post=post, alert=print, followups=FU_OK) == 0
    assert len(post.sent) == 1


def test_crash_mid_post_leaves_attempted_marker_that_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    save(tmp_path / "snaps" / "1", snap())

    def crash(text, url):
        raise KeyboardInterrupt                          # runner killed while the request was in flight
    with pytest.raises(KeyboardInterrupt):
        r.run(args(tmp_path, send=True), post=crash, alert=print, followups=FU_OK)
    assert marker(tmp_path / "out", r.report_marker(FRI))["status"] == "attempted"
    post = Poster()
    assert r.run(args(tmp_path, send=True), post=post, alert=print, followups=FU_OK) == 0 and post.sent == []


def test_no_activity_day_is_neutral_and_alerts_internally(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    save(tmp_path / "snaps" / "1", r.build_snapshot([], R, FRI, {"selected": 0, "skipped": 0, "errors": 0}))
    post, alerts = Poster(), []
    assert r.run(args(tmp_path, send=True, alert=True), post=post, alert=alerts.append, followups=FU_OK) == 0
    sent = post.sent[0]
    assert "No dialing activity was recorded in DataSift for Fri Oct 30" in sent and "isn't counted against your goals" in sent
    assert "target" not in sent and "✗" not in sent and "missed" not in sent and "floor" not in sent
    assert "Start with today's 1 follow-up" in sent
    assert any("no dials, contacts or calls" in x for x in alerts)


def test_send_without_webhook_secret_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "")
    save(tmp_path / "snaps" / "1", snap())
    post, alerts = Poster(), []
    assert r.run(args(tmp_path, send=True, alert=True), post=post, alert=alerts.append, followups=FU_OK) == 1
    assert post.sent == [] and r.CHAT_URL_ENV in alerts[0]


def test_technical_details_go_internal_not_to_rahaf(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    save(tmp_path / "snaps" / "1", snap(stats={"selected": 40, "skipped": 2, "errors": 0}))
    post, alerts = Poster(), []
    r.run(args(tmp_path, send=True, alert=True), post=post, alert=alerts.append,
          followups={"ok": False, "why": "task list incomplete (90 read, API count 139)"})
    assert any("skipped=2" in x for x in alerts) and any("task list incomplete" in x for x in alerts)
    assert "skipped" not in post.sent[0] and "API count" not in post.sent[0]


def test_outside_window_skips(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_in_window", lambda spec: False)
    post = Poster()
    assert r.run(args(tmp_path, send=True, only_between="09:45-10:15"), post=post, alert=print) == 0
    assert post.sent == []


# ------------------------------------------------------------ offers made
K = r.APPROVER_EMAIL
load_offers = r.load_offers          # the real loader; the autouse fixture stubs r.load_offers


def note_row(text, ts, author=R, prop="pA", street="1 Oak St", etype="property.message.added"):
    """A property-log row as DataSift returns it (timestamps UTC)."""
    return {"id": f"n-{prop}-{ts}", "event_type": etype, "resource": prop, "resource_type": "property",
            "author": author, "author_extra_info": {"email": author}, "timestamp": ts, "source": "internal-api",
            "payload": {"message": text, "extra_info": {"property": {"address": {"street": street}}, "user": []}}}


def ev(text, ts, **kw):
    return r._note(note_row(text, ts, **kw))


def txt(ts, *amounts, failed=False):
    """A logged outgoing text with dollar amounts, as load_offers keeps it."""
    return {"ts": p.to_ct(ts), "amounts": list(amounts), "uuid": f"s-{ts}", "failed": failed}


def offers_of(notes, texts=()):
    events, flags = r.classify_offers(notes, list(texts))
    return {"ok": True, "events": events, "flags": flags, "texts": list(texts)}


def test_parse_offer_note_formats():
    a = r.parse_offer_note("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie")
    assert a == {"kind": "INITIAL", "low": 75000, "high": 75000, "method": "VERBAL", "approved": "Kaycie"}
    b = r.parse_offer_note("offer|revised|$80k – $85k|text|approved: kaycie\nseller wants to think")
    assert (b["kind"], b["low"], b["high"], b["method"]) == ("REVISED", 80000, 85000, "TEXT")
    assert r.parse_offer_note("OFFER | WRITTEN FOLLOWUP | 75000 | WRITTEN | APPROVED: Kaycie")["kind"] == "WRITTEN FOLLOW-UP"
    assert r.parse_offer_note("PRICE APPROVED | $70,000 - $78,000") == {"kind": "PRICE APPROVED", "low": 70000, "high": 78000}
    assert r.parse_offer_note("PRICE APPROVED | $75,000 | Kaycie") == {"kind": "PRICE APPROVED", "low": 75000, "high": 75000}
    assert r.parse_offer_note("OFFER | INITIAL | $75,000 | VOICEMAIL | APPROVED: Kaycie")["method"] == "VOICEMAIL"
    c = r.parse_offer_note("OFFER | INITIAL | $75,000 | WRITTEN | APPROVED: Kaycie\nCORRECTION: price typo\nSENT: email 11/2 3pm")
    assert c["correction"] and c["sent_line"]
    assert "sent_line" not in r.parse_offer_note("OFFER | INITIAL | $75,000 | WRITTEN | APPROVED: Kaycie\nSENT:")
    for other in ("Seller wants an offer by Friday", "Offer letter mailed", ""):
        assert r.parse_offer_note(other) is None
    for bad in ("OFFER | INITIAL | TBD | VERBAL | APPROVED: Kaycie", "OFFER | INITIAL | $75,000 | EMAIL | APPROVED: Kaycie",
                "OFFER | INITIAL | $90,000 - $80,000 | VERBAL | APPROVED: Kaycie", "OFFER | INITIAL | $75,000 | VERBAL",
                "OFFER | INITIAL | $75 | VERBAL | APPROVED: Kaycie", "PRICE APPROVED | soon"):
        assert r.parse_offer_note(bad) == {"kind": "malformed"}, bad


def test_classify_counting_rules():
    notes = [
        ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-29T15:00:00Z", author=K),
        ev("PRICE APPROVED | $82,000", "2026-10-30T18:30:00Z", author=K),
        ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z"),                # counted initial
        ev("OFFER | INITIAL | $75,000 | TEXT | APPROVED: Kaycie", "2026-10-30T16:00:00Z"),                  # repeat
        ev("OFFER | WRITTEN FOLLOW-UP | $75,000 | WRITTEN | APPROVED: Kaycie", "2026-10-30T17:00:00Z"),     # follow-up
        ev("OFFER | REVISED | $85,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T18:00:00Z"),                # no approval at $85k
        ev("OFFER | REVISED | $82,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T19:00:00Z"),                # approved first
        ev("OFFER | INITIAL | $60,000 | VERBAL | APPROVED: Ty", "2026-10-30T15:00:00Z", prop="pB"),         # not Kaycie
        ev("PRICE APPROVED | $50,000", "2026-10-28T15:00:00Z", prop="pC"),                                   # Rahaf can't approve
        ev("OFFER | REVISED | $50,000 | WRITTEN | APPROVED: Kaycie", "2026-10-30T15:00:00Z", prop="pC"),    # unverified + label
        ev("OFFER | WRITTEN FOLLOW-UP | $40,000 | WRITTEN | APPROVED: Kaycie", "2026-10-30T15:00:00Z", prop="pD"),
        ev("OFFER | INITIAL | about 70 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z", prop="pE"),
    ]
    events, flags = r.classify_offers(notes)
    assert [(e["property"], e["status"], e["category"]) for e in events] == [
        ("pA", "counted", "initial"), ("pA", "repeat", ""), ("pA", "followup", ""), ("pA", "unverified", "revised"),
        ("pA", "counted", "revised"), ("pB", "not_kaycie", ""), ("pC", "unverified", "initial"),
        ("pD", "followup_new_price", ""), ("pE", "malformed", "")]
    text = "\n".join(m for _, m in flags)
    assert "only Kaycie approves prices" in text and "not Kaycie; ignored" in text
    assert "says REVISED but is the property's initial offer" in text
    assert "at a price not presented before" in text and "does not match the format" in text
    assert text.count("no Kaycie PRICE APPROVED note on that property at exactly this price/range") == 2
    assert all(isinstance(ts, dt.datetime) for ts, _ in flags)


def test_approval_must_match_exactly_and_come_first():
    def status(*notes):
        events, flags = r.classify_offers(list(notes))
        return events[-1]["status"], "\n".join(m for _, m in flags)
    offer = ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z")
    assert status(ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-30T14:00:00Z", author=K), offer)[0] == "counted"
    late, why = status(ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-30T16:00:00Z", author=K), offer)
    assert late == "unverified" and "recorded after it" in why and "approval must come first" in why
    assert status(ev("PRICE APPROVED | $70,000 - $80,000", "2026-10-29T14:00:00Z", author=K), offer)[0] == "unverified"
    assert status(ev("PRICE APPROVED | $75,000", "2026-10-29T14:00:00Z", author=K, prop="pZ"), offer)[0] == "unverified"
    assert status(ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z", author=K))[0] == "unverified"
    rng = ev("OFFER | INITIAL | $70,000-$80,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z")
    assert status(ev("PRICE APPROVED | $70,000 - $80,000 | Kaycie", "2026-10-29T14:00:00Z", author=K), rng)[0] == "counted"
    revised = ev("OFFER | REVISED | $80,000 | VERBAL | APPROVED: Kaycie", "2026-10-31T15:00:00Z")
    assert status(ev("PRICE APPROVED | $75,000", "2026-10-29T14:00:00Z", author=K), offer, revised)[0] == "unverified"


def test_price_presented_weeks_ago_is_not_new():
    events, _ = r.classify_offers([ev("PRICE APPROVED | $75,000 | Kaycie", "2026-09-30T15:00:00Z", author=K),
                                   ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-01T15:00:00Z", author=K),
                                   ev("OFFER | REVISED | $75,000 | TEXT | APPROVED: Kaycie", "2026-10-30T15:00:00Z", author=K)])
    assert [e["status"] for e in events] == ["counted", "repeat"]


def test_edited_note_payload_list_uses_new_text():
    row = note_row("", "2026-10-30T16:00:00Z", author=K, etype="property.message.updated")
    row["payload"] = {"message_obj": {"message": ["old text", "OFFER | INITIAL | $79,000 | VERBAL | APPROVED: Kaycie"], "updated": []}}
    n = r._note(row)
    assert n["edited"] and n["low"] == 79000
    assert r._note(dict(row, payload={"message_obj": {"message": ["OFFER | x", "plain note now"]}})) is None


def test_edited_offer_note_is_flagged_not_recounted():
    events, flags = r.classify_offers([ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-29T15:00:00Z", author=K),
                                       ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z", author=K),
                                       ev("OFFER | INITIAL | $79,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T16:00:00Z",
                                          author=K, etype="property.message.updated")])
    assert [e["status"] for e in events] == ["counted"] and "was edited" in flags[0][1]


def test_summary_states_zero_unavailable_not_tracked():
    start = dt.date(2026, 10, 30)
    assert r.summarize_offers({"ok": False, "why": "x"}, start)["state"] == "unavailable"
    assert "Unavailable" in "\n".join(r._offer_lines({"state": "unavailable"}, start))
    nt = r.summarize_offers(offers_of([]), dt.date(2026, 10, 29), tracked_from=start)
    text = "\n".join(r._offer_lines(nt, dt.date(2026, 10, 29)))
    assert nt["state"] == "not_tracked" and "Not tracked yet" in text and "0 /" not in text
    z = r.summarize_offers(offers_of([]), dt.date(2026, 11, 3), tracked_from=start)
    assert z["state"] == "ok" and not z["partial_week"]
    assert "Tue Nov 3: *0 / 10* · Week to date: *0 / 50*" in "\n".join(r._offer_lines(z, dt.date(2026, 11, 3)))


def test_summary_counts_and_monday_shows_last_week_final():
    notes = [ev("PRICE APPROVED | $75,000 | Kaycie", "2026-11-02T14:00:00Z", author=K),
             ev("PRICE APPROVED | $80,000 | Kaycie", "2026-11-05T14:00:00Z", author=K),
             ev("PRICE APPROVED | $90,000 | Kaycie", "2026-11-05T14:00:00Z", author=K, prop="pC"),
             ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-11-02T15:00:00Z", author=K),
             ev("OFFER | REVISED | $80,000 | WRITTEN | APPROVED: Kaycie\nSENT: email, Fri 10 AM", "2026-11-06T15:00:00Z", author=K),
             ev("OFFER | WRITTEN FOLLOW-UP | $80,000 | WRITTEN | APPROVED: Kaycie", "2026-11-06T16:00:00Z", author=K),
             ev("OFFER | INITIAL | $60,000 | TEXT | APPROVED: Kaycie", "2026-11-06T17:00:00Z", prop="pB"),
             ev("OFFER | INITIAL | $90,000 | TEXT | APPROVED: Kaycie", "2026-11-06T18:00:00Z", prop="pC", author=K)]
    o = r.summarize_offers(offers_of(notes, [txt("2026-11-06T17:55:00Z", 90000)]), dt.date(2026, 11, 6),
                           tracked_from=dt.date(2026, 10, 30))
    assert o["day"] == {"offers": 2, "initial": 1, "revised": 1, "live": 0, "texts": 1, "written": 1, "properties": 2,
                        "followups": 1, "voicemails": 0, "review": 1, "presenters": {K: 2}}
    assert o["week"]["offers"] == 3 and o["week"]["properties"] == 2
    lines = "\n".join(r._offer_lines(o, dt.date(2026, 11, 6)))
    assert "Fri Nov 6: *2 / 10* · Week final: *3 / 50*" in lines          # Monday's report covers Friday
    assert "Initial 1 · Revised 1 · Live call 0 · Text 1 · Written 1 · Properties 2 (week: 2)" in lines
    assert "unchanged price this week: 1" in lines and "Waiting for review or a correction this week: 1" in lines
    assert "$" not in lines


def test_first_tracked_week_is_labelled_partial():
    o = r.summarize_offers(offers_of([]), dt.date(2026, 10, 30), tracked_from=dt.date(2026, 10, 30))
    text = "\n".join(r._offer_lines(o, dt.date(2026, 10, 30)))
    assert "Week final: *0* (partial week: tracking began Fri Oct 30, so not compared to 50)" in text
    assert "/ 50" not in text


def test_tracking_start_day_says_today():
    start = dt.date(2026, 10, 30)
    nt = r.summarize_offers(offers_of([]), dt.date(2026, 10, 29), tracked_from=start)
    assert "Tracking starts today" in "\n".join(r._offer_lines(nt, dt.date(2026, 10, 29), today=start))
    assert "Tracking starts Fri Oct 30" in "\n".join(r._offer_lines(nt, dt.date(2026, 10, 28), today=dt.date(2026, 10, 29)))


class LogApi:
    """Account feed paged by cursor (newest first); per-property feed paged by offset."""
    def __init__(self, feed, per_prop=None, page=2, fail_on=None):
        self.feed, self.per_prop, self.page, self.fail_on, self.paths = feed, per_prop or {}, page, fail_on, []

    def call(self, path):
        self.paths.append(path)
        if self.fail_on and self.fail_on in path:
            raise RuntimeError("HTTP 502")
        if path.startswith("/api/internal/property/logs/"):
            m = re.search(r"cursor=(\d+)", path)
            i = int(m.group(1)) if m else 0
            return {"results": self.feed[i:i + self.page], "next_cursor": str(i + self.page) if i + self.page < len(self.feed) else None}
        rows = self.per_prop.get(path.split("/")[4], [])
        off = int(re.search(r"offset=(\d+)", path).group(1))
        return {"results": rows[off:off + 250], "next": "x" if off + 250 < len(rows) else None}


def sms_row(msg, ts, uuid=None, etype="owner.sms.sent", status=None):
    return {"event_type": etype, "timestamp": ts, "resource": "owner-1", "resource_type": "owner",
            "payload": {"sms": {"message": msg, "destination_number": "8165551234", "uuid": uuid or f"u-{ts}", "status": status},
                        "extra_info": {"owner": {"first_name": "Secret", "last_name": "Seller"}}}}


def test_load_offers_reads_window_and_full_property_history():
    old = note_row("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-20T15:00:00Z", author=K)
    new = note_row("OFFER | REVISED | $75,000 | TEXT | APPROVED: Kaycie", "2026-11-02T15:00:00Z", author=K)
    approval = note_row("PRICE APPROVED | $75,000 | Kaycie", "2026-10-19T15:00:00Z", author=K)
    feed = [sms_row("We could do $72,500 cash", "2026-11-02T20:00:00Z"), sms_row("Call me at 3", "2026-11-02T19:00:00Z"),
            sms_row("Around $70k works?", "2026-11-02T18:00:00Z"), new,
            note_row("a regular note", "2026-11-02T14:00:00Z"), note_row("older", "2026-10-31T14:00:00Z"),
            note_row("never read", "2026-10-30T14:00:00Z")]
    api = LogApi(feed, {"pA": [new, old, approval]})
    of = load_offers(api, dt.date(2026, 11, 2))
    assert of["ok"] and [e["status"] for e in of["events"]] == ["counted", "repeat"]
    assert not any("cursor=6" in x for x in api.paths)                   # stopped once past the window
    assert [t["amounts"] for t in of["texts"]] == [[72500], [70000]]
    assert "2 sent text(s) mentioned a dollar amount but only 1 offer note" in r.unlogged_text_check(of, dt.date(2026, 11, 2))
    assert r.unlogged_text_check(of, dt.date(2026, 11, 3)) is None
    dump = json.dumps(of, default=str)
    assert "8165551234" not in dump and "72,500" not in dump and "Secret" not in dump


def test_load_offers_partial_or_failed_read_is_unavailable():
    feed = [note_row("x", f"2026-11-02T1{i}:00:00Z") for i in range(6)]
    assert load_offers(LogApi(feed), dt.date(2026, 11, 1), max_pages=2)["ok"] is False      # page cap hit
    rows = [note_row("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-11-02T15:00:00Z", author=K)]
    assert load_offers(LogApi(rows, fail_on="/property/pA/"), dt.date(2026, 11, 1))["ok"] is False


def test_run_offer_flags_go_internal_only(tmp_path, monkeypatch):
    monkeypatch.setattr(p, "_env", lambda n: "https://chat.example/hook" if n == r.CHAT_URL_ENV else "")
    monkeypatch.setattr(r, "OFFERS_TRACKED_FROM", dt.date(2026, 10, 26))
    save(tmp_path / "snaps" / "1", snap())
    of = offers_of([ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-30T14:00:00Z", author=K),
                    ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z", author=K),
                    ev("OFFER | INITIAL | $60,000 | TEXT | APPROVED: Ty", "2026-10-30T16:00:00Z", prop="pB"),
                    ev("OFFER | INITIAL | $50,000 | TEXT | APPROVED: Kaycie", "2026-10-29T16:00:00Z", prop="pC"),
                    ev("OFFER | INITIAL | junk", "2026-10-27T16:00:00Z", prop="pD")],
                   texts=[txt(f"2026-10-30T2{i}:00:00Z", 70000) for i in range(3)])
    post, alerts = Poster(), []
    assert r.run(args(tmp_path, send=True, alert=True), post=post, alert=alerts.append, followups=FU_OK, offers=of) == 0
    review = next(a for a in alerts if "Offers Made review" in a)
    assert "only Kaycie approves" in review and "3 sent text(s) mentioned a dollar amount but only 2" in review
    assert "3 offer note(s) this week are waiting for review" in review    # pB, pC, pD
    assert "does not match the format" not in review                       # Tuesday's flag is not re-sent Monday
    assert "Still open from earlier days (2;" in review and "Tue Oct 27 (malformed)" in review   # ...but stays listed
    sent = post.sent[0]
    assert "Fri Oct 30: *1 / 10* · Week final: *1 / 50*" in sent and "Waiting for review or a correction this week: 3" in sent
    assert "Ty" not in sent and "$" not in sent and "dollar amount" not in sent


def test_run_offers_unavailable(tmp_path):
    save(tmp_path / "snaps" / "1", snap())
    alerts = []
    r.run(args(tmp_path, alert=True), post=Poster(), alert=alerts.append, followups=FU_OK,
          offers={"ok": False, "why": "offer note read failed: RuntimeError: HTTP 502"})
    assert any("Offers Made shown as unavailable" in a and "HTTP 502" in a for a in alerts)


def statuses(notes, texts=None, since=None):
    events, flags = r.classify_offers(notes, texts, since)
    return [e["status"] for e in events], flags


def test_voicemail_is_outreach_not_an_offer():
    appr = ev("PRICE APPROVED | $75,000 | Kaycie", "2026-11-02T14:00:00Z", author=K)
    vm = ev("OFFER | INITIAL | $75,000 | VOICEMAIL | APPROVED: Kaycie", "2026-11-02T15:00:00Z")
    live = ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-11-03T15:00:00Z")
    events, flags = r.classify_offers([appr, vm, live])
    assert [(e["status"], e["category"]) for e in events] == [("voicemail", ""), ("counted", "initial")]   # live call is still the initial
    assert flags == []
    o = r.summarize_offers(offers_of([appr, vm, live]), dt.date(2026, 11, 3), tracked_from=dt.date(2026, 10, 30))
    assert o["week"]["offers"] == 1 and o["week"]["voicemails"] == 1
    assert ("Voicemails with an approved price this week: 1 (outreach, not counted as offers)"
            in "\n".join(r._offer_lines(o, dt.date(2026, 11, 3))))
    st, flags = statuses([ev("OFFER | INITIAL | $80,000 | VOICEMAIL | APPROVED: Kaycie", "2026-11-02T15:00:00Z")])
    assert st == ["voicemail_unverified"] and "voicemail with a price" in flags[0][1]


def test_text_offer_needs_a_logged_delivered_text():
    appr = ev("PRICE APPROVED | $70,000 - $75,000 | Kaycie", "2026-11-02T14:00:00Z", author=K)
    offer = ev("OFFER | INITIAL | $70,000-$75,000 | TEXT | APPROVED: Kaycie", "2026-11-02T16:00:00Z")
    assert statuses([appr, offer], [txt("2026-11-02T15:50:00Z", 70000, 75000)])[0] == ["counted"]
    assert statuses([appr, offer], [txt("2026-11-02T15:50:00Z", 70000)])[0] == ["send_unconfirmed"]          # one end missing
    assert statuses([appr, offer], [txt("2026-11-02T15:50:00Z", 70000, 75000, failed=True)])[0] == ["send_unconfirmed"]
    assert statuses([appr, offer], [txt("2026-11-01T02:00:00Z", 70000, 75000)])[0] == ["send_unconfirmed"]   # >12h before
    st, flags = statuses([appr, offer], [])
    assert st == ["send_unconfirmed"] and "no logged, delivered text" in flags[0][1]
    assert statuses([appr, offer], [], since=dt.date(2026, 11, 3))[0] == ["counted"]      # older than the text log read: not checked


def test_load_offers_text_evidence_and_failures():
    appr = note_row("PRICE APPROVED | $75,000 | Kaycie", "2026-11-02T14:00:00Z", author=K)
    a = note_row("OFFER | INITIAL | $75,000 | TEXT | APPROVED: Kaycie", "2026-11-02T16:00:00Z")
    feed = [sms_row("", "2026-11-02T16:01:00Z", uuid="t1", etype="owner.sms.updated", status="undelivered"),
            a, sms_row("We can do $75,000", "2026-11-02T15:58:00Z", uuid="t1"), appr]
    of = load_offers(LogApi(feed, {"pA": [a, appr]}), dt.date(2026, 11, 2))
    assert [e["status"] for e in of["events"]] == ["send_unconfirmed"] and of["texts"][0]["failed"]
    feed[0] = sms_row("", "2026-11-02T16:01:00Z", uuid="t1", etype="owner.sms.updated", status="delivered")
    of = load_offers(LogApi(feed, {"pA": [a, appr]}), dt.date(2026, 11, 2))
    assert [e["status"] for e in of["events"]] == ["counted"]


def test_written_offer_needs_a_sent_line():
    appr = ev("PRICE APPROVED | $75,000 | Kaycie", "2026-11-02T14:00:00Z", author=K)
    st, flags = statuses([appr, ev("OFFER | INITIAL | $75,000 | WRITTEN | APPROVED: Kaycie\nmailed it", "2026-11-02T16:00:00Z")])
    assert st == ["send_unconfirmed"] and "no 'SENT:' line" in flags[0][1]
    assert statuses([appr, ev("OFFER | INITIAL | $75,000 | WRITTEN | APPROVED: Kaycie\nSENT: email Mon 11 AM",
                              "2026-11-02T16:00:00Z")])[0] == ["counted"]


def test_correction_replaces_and_keeps_the_original():
    appr = ev("PRICE APPROVED | $75,000 | Kaycie", "2026-11-02T14:00:00Z", author=K)
    bad = ev("OFFER | INITIAL | 75 grand | VERBAL | APPROVED: Kaycie", "2026-11-02T15:00:00Z")
    fix = ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie\nCORRECTION: fixes the 11/2 note", "2026-11-04T15:00:00Z")
    events, flags = r.classify_offers([appr, bad, fix])
    assert [(e["status"], e["ts"].date().isoformat()) for e in events] == [("corrected", "2026-11-02"), ("counted", "2026-11-02")]
    assert events[1]["logged_ts"].date() == dt.date(2026, 11, 4)
    assert len(flags) == 1 and "was corrected by" in flags[0][1] and flags[0][0].date() == dt.date(2026, 11, 4)
    o = r.summarize_offers({"ok": True, "events": events, "flags": flags}, dt.date(2026, 11, 2), tracked_from=dt.date(2026, 10, 30))
    assert o["day"]["offers"] == 1 and o["day"]["review"] == 0                # counted on the day it was presented


def test_correction_cannot_use_a_later_approval():
    offer = ev("OFFER | INITIAL | $80,000 | VERBAL | APPROVED: Kaycie", "2026-11-02T15:00:00Z")
    late = ev("PRICE APPROVED | $80,000 | Kaycie", "2026-11-02T18:00:00Z", author=K)
    fix = ev("OFFER | INITIAL | $80,000 | VERBAL | APPROVED: Kaycie\nCORRECTION: re-logging", "2026-11-02T19:00:00Z")
    st, flags = statuses([offer, late, fix])
    assert st == ["corrected", "unverified"] and any("approval must come first" in m for _, m in flags)


def test_repeat_of_approved_price_during_vacation_is_quiet():
    st, flags = statuses([ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-29T15:00:00Z", author=K),
                          ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie", "2026-10-30T15:00:00Z"),
                          ev("OFFER | REVISED | $75,000 | VERBAL | APPROVED: Kaycie", "2026-11-02T15:00:00Z")])
    assert st == ["counted", "repeat"] and flags == []


def test_review_queue_lists_open_items_until_corrected(tmp_path, monkeypatch):
    monkeypatch.setattr(r, "OFFERS_TRACKED_FROM", dt.date(2026, 10, 26))
    save(tmp_path / "snaps" / "1", snap())
    notes = [ev("OFFER | INITIAL | junk", "2026-10-21T16:00:00Z", prop="pOld"),          # before tracking: never listed
             ev("OFFER | INITIAL | junk", "2026-10-27T16:00:00Z", prop="pD"),
             ev("OFFER | INITIAL | junk", "2026-10-28T16:00:00Z", prop="pE"),
             ev("PRICE APPROVED | $75,000 | Kaycie", "2026-10-27T15:00:00Z", author=K, prop="pE"),
             ev("OFFER | INITIAL | $75,000 | VERBAL | APPROVED: Kaycie\nCORRECTION", "2026-10-29T16:00:00Z", prop="pE")]
    alerts = []
    r.run(args(tmp_path, alert=True), post=Poster(), alert=alerts.append, followups=FU_OK, offers=offers_of(notes))
    review = next(a for a in alerts if "Offers Made review" in a)
    assert "Still open from earlier days (1;" in review and "pD" in review and "pE" not in review and "pOld" not in review
