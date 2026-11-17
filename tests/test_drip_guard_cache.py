"""Tests for drip_exit_guard._Cache (the --state cache added 2026-11-17)."""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import drip_exit_guard as g  # noqa: E402

OPEN = "prospecting"


def test_no_path_never_caches():
    c = g._Cache(None)
    r = {"uuid": "u1", "status": "dead"}
    c.put("nodrip", r)
    assert not c.fresh("nodrip", r)
    c.save()   # no file, no error


def test_roundtrip_and_status_change_invalidates(tmp_path):
    p = tmp_path / "guard.json"
    r = {"uuid": "u1", "status": "dead"}
    c = g._Cache(str(p))
    c.put("nodrip", r)
    c.save()
    c2 = g._Cache(str(p))
    assert c2.fresh("nodrip", r) and c2.hits == 1
    assert not c2.fresh("nodrip", {"uuid": "u1", "status": OPEN})   # status changed -> stale
    assert not c2.fresh("phone_ok", r)                               # other kind


def test_ttl_expiry_and_drop(tmp_path):
    p = tmp_path / "guard.json"
    closed = {"uuid": "c", "status": "dead"}
    open_ = {"uuid": "o", "status": OPEN}
    c = g._Cache(str(p))
    c.put("nodrip", closed)
    c.put("nodrip", open_)
    age = g._Cache.OPEN_TTL + 5
    for e in c.d["nodrip"].values():
        e["at"] -= age
    assert c.fresh("nodrip", closed)        # closed TTL is 60 min
    assert not c.fresh("nodrip", open_)     # open TTL is 10 min
    c.drop("c")
    assert not c.fresh("nodrip", closed)


def test_save_prunes_old_and_survives_bad_file(tmp_path):
    p = tmp_path / "guard.json"
    c = g._Cache(str(p))
    c.d["nodrip"]["old"] = {"sig": "dead", "at": int(time.time()) - g._Cache.KEEP - 10}
    c.save()
    assert "old" not in g._Cache(str(p)).d["nodrip"]
    p.write_text("{not json")
    assert g._Cache(str(p)).d == {"nodrip": {}, "phone_ok": {}}
