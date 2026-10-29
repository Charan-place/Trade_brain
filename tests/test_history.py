"""Trade history: per-day totals from saved calls (files or MongoDB), served to the dashboard."""
import json
from datetime import datetime

import pytest

from tbrain import history as H
from tbrain.timeutil import IST


def _ms(d, h, m):
    return datetime(2026, 10, d, h, m, tzinfo=IST).timestamp() * 1000


def _call(cid, d, h, m, r, pnl, status="closed", outcome="T2"):
    return {"id": cid, "und": "NIFTY", "symbol": f"NIFTY {cid}", "dir": -1, "direction": "bearish",
            "issued_ms": _ms(d, h, m), "exit_ms": _ms(d, h, m) + 20 * 60_000, "entry": 100.0, "exit": 120.0,
            "status": status, "outcome": outcome if status == "closed" else None, "r_now": r, "pnl_per_lot": pnl,
            "regime": {"label": "trend"}, "score": 5, "max_score": 7}


CALLS = [_call("a", 21, 10, 0, 2.0, 2000), _call("b", 21, 13, 10, -1.0, -900, outcome="SL"),
         _call("c", 22, 9, 40, 1.0, 650), _call("d", 22, 11, 0, 0.3, 120, status="open")]


def test_summarize_groups_by_ist_day_newest_first_with_totals():
    h = H.summarize(CALLS)
    assert [d["day"] for d in h["days"]] == ["2026-10-22", "2026-10-21"]
    d21 = h["days"][1]
    assert (d21["calls"], d21["wins"], d21["losses"], d21["sum_r"], d21["pnl_per_lot"]) == (2, 1, 1, 1.0, 1100)
    d22 = h["days"][0]
    assert d22["open"] == 1 and d22["closed"] == 1 and d22["pnl_per_lot"] == 650 and d22["pnl_open_per_lot"] == 120
    assert [t["time"] for t in d21["trades"]] == ["10:00", "13:10"] and d21["trades"][0]["regime"] == "trend"
    assert h["total"] == {"days": 2, "calls": 4, "closed": 3, "wins": 2, "losses": 1, "win_rate": 2 / 3,
                          "sum_r": 2.0, "pnl_per_lot": 1750, "green_days": 2}


def test_from_files_uses_last_event_per_call(tmp_path):
    (tmp_path / "signals").mkdir()
    first = dict(CALLS[0], status="open", r_now=0.1, pnl_per_lot=50)
    lines = [{"event": "issue", "ts": 1, "call": first}, {"event": "close", "ts": 2, "call": CALLS[0]}]
    (tmp_path / "signals" / "calls.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
    h = H.from_files(tmp_path)
    assert h["total"]["calls"] == 1 and h["days"][0]["pnl_per_lot"] == 2000


def test_from_mongo_filters_owner():
    mongomock = pytest.importorskip("mongomock")
    db = mongomock.MongoClient()["t"]
    for c in CALLS:
        db.calls.insert_one(dict(c, _id=c["id"], owner="me@x.com", day=H._day(c["issued_ms"])))
    db.calls.insert_one(dict(_call("z", 21, 10, 5, 9.0, 9000), _id="z", owner="someone@else.com"))
    assert H.from_mongo(db, "me@x.com")["total"]["calls"] == 4
    assert H.from_mongo(db, None)["total"]["calls"] == 5


def test_web_history_needs_sign_in_and_reads_mongo():
    pytest.importorskip("fastapi")
    from tests.test_web import OWNER, _client
    c, s, db, *_ = _client()
    assert c.get("/api/history").status_code == 401
    db.calls.insert_one(dict(CALLS[0], _id="a", owner=OWNER, day="2026-10-21"))
    c.get("/login")
    h = c.get("/api/history").json()
    assert h["total"]["calls"] == 1 and h["days"][0]["trades"][0]["symbol"] == "NIFTY a"


def test_local_ui_serves_history(tmp_path):
    import urllib.request
    from tbrain.ui.server import Broadcaster, UIServer
    srv = UIServer(Broadcaster(), 8792, history=lambda book="signal": H.summarize(CALLS)).start()
    try:
        h = json.loads(urllib.request.urlopen("http://127.0.0.1:8792/api/history").read())
        assert h["total"]["pnl_per_lot"] == 1750
    finally:
        srv.stop()
