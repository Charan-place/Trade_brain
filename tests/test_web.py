"""Hosted web app: Google sign-in allow-list, sessions, Upstox connect (encrypted token), live state."""
from datetime import datetime, timedelta

import pytest

pytest.importorskip("fastapi")
mongomock = pytest.importorskip("mongomock")
from cryptography.fernet import Fernet  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from tbrain.config import Settings  # noqa: E402
from tbrain.timeutil import IST  # noqa: E402
from tbrain.web import state as S  # noqa: E402
from tbrain.web.app import create_app, email_allowed  # noqa: E402
from tbrain.web.tokens import TokenStore, session_start  # noqa: E402

OWNER = "owner@gmail.com"


class FakeGoogle:
    userinfo = {"email": OWNER, "email_verified": True, "name": "Owner", "sub": "1"}
    fail = False

    async def authorize_redirect(self, request, redirect_uri):
        request.session["_state_google"] = "x"
        return RedirectResponse("/auth/callback?code=c&state=x", status_code=302)

    async def authorize_access_token(self, request):
        if self.fail:
            raise RuntimeError("mismatching_state")
        return {"userinfo": dict(self.userinfo)}


class FakeOAuth:
    def __init__(self):
        self.google = FakeGoogle()


class MemState:
    def __init__(self):
        self.st = {"mode": "live", "underlyings": {"NIFTY": {"spot": 25000.0}}, "signals": {"calls": []}}

    def latest(self):
        return self.st


def _settings(allowed=(OWNER,)):
    return Settings(session_secret="s" * 48, allowed_emails=tuple(allowed), owner_email=OWNER,
                    api_key="APIKEY", api_secret="SECRET", redirect_uri="https://tb.example/upstox/callback",
                    public_url="", encryption_key=Fernet.generate_key().decode())


STARTED = []


def _client(allowed=(OWNER,), userinfo=None):
    s = _settings(allowed)
    db = mongomock.MongoClient()["t"]
    oauth = FakeOAuth()
    if userinfo:
        oauth.google.userinfo = userinfo
    tokens = TokenStore(db, s.encryption_key)
    from tbrain.runtime_settings import DictSettings
    app = create_app(s, state_source=MemState(), db=db, tokens=tokens, oauth=oauth, on_upstox_connected=STARTED.append,
                     settings_store=DictSettings({}))
    return TestClient(app), s, db, tokens, oauth


def test_signed_out_users_see_only_the_sign_in_page():
    c, *_ = _client()
    r = c.get("/")
    assert r.status_code == 200 and "Sign in with Google" in r.text and "LIVE CHART" not in r.text.upper()
    assert c.get("/api/state").status_code == 401 and c.get("/api/stream").status_code == 401
    assert c.get("/api/me").status_code == 401
    assert r.headers["x-frame-options"] == "DENY"


def test_allowed_verified_google_account_gets_the_dashboard():
    c, s, db, *_ = _client()
    r = c.get("/login")
    assert r.status_code == 200 and "tb-acct" in r.text           # followed redirects to the dashboard
    assert c.get("/api/state").json()["underlyings"]["NIFTY"]["spot"] == 25000.0
    me = c.get("/api/me").json()
    assert me["email"] == OWNER and me["is_owner"] and me["upstox"]["connected_today"] is False
    assert db.users.find_one({"_id": OWNER})["name"] == "Owner"


@pytest.mark.parametrize("info", [{"email": OWNER, "email_verified": False}, {"email_verified": True}])
def test_unverified_or_missing_email_accounts_are_refused(info):
    c, *_ = _client(userinfo=info)
    r = c.get("/login")
    assert "sign-in failed" in r.text and c.get("/api/state").status_code == 401


def test_unlisted_accounts_land_on_request_access_and_can_be_approved():
    c, s, db, *_ = _client(userinfo={"email": "stranger@gmail.com", "email_verified": True, "name": "Stranger"})
    r = c.get("/login")
    assert "Request sent" in r.text and "stranger@gmail.com" in r.text
    assert c.get("/api/state").status_code == 401
    assert db.access.find_one({"_id": "stranger@gmail.com"})["status"] == "pending"
    db.access.update_one({"_id": "stranger@gmail.com"}, {"$set": {"status": "approved"}})
    c.get("/login")                                                 # sign in again after approval
    assert c.get("/api/state").status_code == 200


def test_admin_access_page_is_owner_only_and_lists_requests():
    c, s, db, *_ = _client()
    c.get("/login")                                                 # owner signs in
    db.access.insert_one({"_id": "x@gmail.com", "name": "X", "status": "pending"})
    r = c.get("/admin/access")
    assert r.status_code == 200 and "x@gmail.com" in r.text

    c2, *_ = _client(allowed=("*",), userinfo={"email": "notowner@gmail.com", "email_verified": True})
    c2.get("/login")
    assert c2.get("/admin/access", follow_redirects=False).status_code == 303   # not the owner


def test_admin_can_approve_and_deny_via_query_param_post():
    c, s, db, *_ = _client()
    c.get("/login")
    db.access.insert_one({"_id": "y@gmail.com", "name": "Y", "status": "pending"})
    r = c.post("/admin/access/approve?email=y@gmail.com", follow_redirects=False)
    assert r.status_code == 303
    assert db.access.find_one({"_id": "y@gmail.com"})["status"] == "approved"
    c.post("/admin/access/deny?email=y@gmail.com")
    assert db.access.find_one({"_id": "y@gmail.com"})["status"] == "denied"


def test_star_allows_any_verified_google_account_but_not_upstox_connect():
    c, *_ = _client(allowed=("*",), userinfo={"email": "anyone@gmail.com", "email_verified": True})
    assert "tb-acct" in c.get("/login").text
    assert c.get("/api/me").json()["is_owner"] is False
    assert c.get("/upstox/connect", follow_redirects=False).status_code == 403


def test_failed_google_callback_shows_error():
    c, _, _, _, oauth = _client()
    oauth.google.fail = True
    assert "sign-in failed" in c.get("/login").text


def test_removing_an_email_from_the_allow_list_revokes_access_immediately():
    c, s, *_ = _client()
    c.get("/login")
    assert c.get("/api/state").status_code == 200
    s.allowed_emails = ()
    assert c.get("/api/state").status_code == 401


def test_upstox_connect_state_check_and_encrypted_token(monkeypatch):
    c, s, db, tokens, _ = _client()
    c.get("/login")
    r = c.get("/upstox/connect", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("https://api.upstox.com/v2/login/authorization/dialog")
    from urllib.parse import parse_qs, urlparse
    q = parse_qs(urlparse(r.headers["location"]).query)
    assert q["client_id"] == ["APIKEY"] and q["redirect_uri"] == ["https://tb.example/upstox/callback"]
    bad = c.get("/upstox/callback?code=abc&state=forged", follow_redirects=False)
    assert bad.status_code == 400                                             # forged state rejected
    r = c.get("/upstox/connect", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    import tbrain.brokers.upstox.auth as A
    monkeypatch.setattr(A, "exchange_code", lambda s_, code: {"access_token": f"TOKEN-{code}"})
    ok = c.get(f"/upstox/callback?code=abc&state={state}", follow_redirects=False)
    assert ok.status_code == 303
    raw = db.upstox_tokens.find_one({"_id": OWNER})
    assert "TOKEN-abc" not in str(raw)                                        # encrypted at rest
    assert tokens.load(OWNER) == "TOKEN-abc"
    assert STARTED[-1] == OWNER                                               # today's brain is started
    assert c.get("/api/me").json()["upstox"]["connected_today"] is True
    # replaying the same callback (state already consumed) is rejected
    assert c.get(f"/upstox/callback?code=abc&state={state}", follow_redirects=False).status_code == 400


def test_token_expires_at_the_next_0330_ist_and_wrong_key_fails_closed():
    db = mongomock.MongoClient()["t"]
    ts = TokenStore(db, Fernet.generate_key().decode())
    t0 = datetime(2026, 10, 23, 9, 0, tzinfo=IST)
    ts.save("A@x.com", "tok", now=t0)
    assert ts.load("a@x.com", now=t0 + timedelta(hours=12)) == "tok"                  # 21:00 same day
    assert ts.load("a@x.com", now=datetime(2026, 10, 24, 3, 0, tzinfo=IST)) == "tok"  # before 03:30
    assert ts.load("a@x.com", now=datetime(2026, 10, 24, 3, 31, tzinfo=IST)) is None
    assert session_start(datetime(2026, 10, 24, 2, 0, tzinfo=IST)).day == 23
    other = TokenStore(db, Fernet.generate_key().decode())
    assert other.load("a@x.com", now=t0) is None                                    # rotated key -> not connected


def test_dict_state_roundtrip_staleness_and_throttled_publisher(monkeypatch):
    d = {}
    pub = S.make_dict_publisher(d, every_s=10, log=lambda *a: None)
    pub({"mode": "live", "n": 1})
    pub({"mode": "live", "n": 2})                       # throttled
    src = S.DictStateSource(d)
    assert src.latest()["n"] == 1 and src.latest()["mode"] == "live"
    d["state"]["ts"] -= 120
    assert src.latest()["mode"] == "stale"
    assert S.DictStateSource({}).latest()["mode"] == "empty"


def test_stream_sends_the_current_state_first():
    import asyncio
    import json

    from tbrain.web import app as W
    c, *_ = _client()
    c.get("/login")
    route = next(r for r in c.app.routes if getattr(r, "path", "") == "/api/stream")

    class Req:
        session = {"user": {"email": OWNER}}

        async def is_disconnected(self):
            return False

    async def first_two():
        resp = await route.endpoint(Req())
        out = []
        async for chunk in resp.body_iterator:
            out.append(chunk)
            if len(out) == 2:
                return out

    retry, chunk = asyncio.run(first_two())
    assert retry.startswith("retry:") and chunk.startswith("data: ") and json.loads(chunk[6:])["underlyings"]["NIFTY"]["spot"] == 25000.0
    assert W.SSE_EVERY_S <= 1.0 and W.STREAM_MAX_S < 150


def test_email_allowed_rules():
    assert email_allowed("a@b.com", True, ("a@b.com",))
    assert not email_allowed("a@b.com", None, ("a@b.com",))
    assert not email_allowed(None, True, ("*",))


def test_hosted_day_guards(monkeypatch):
    from tbrain.web.hosted import run_hosted_day
    s = _settings()
    db = mongomock.MongoClient()["t"]
    ts = TokenStore(db, s.encryption_key)
    ran = []

    class FakeRecorder:
        def __init__(self, s_, token_provider, **kw):
            self.tp, self.kw = token_provider, kw

        def run(self):
            self.kw["publish"]({"mode": "live"})
            ran.append(self.tp())

    log = lambda *a: None  # noqa: E731
    mon = datetime(2026, 10, 26, 8, 55, tzinfo=IST)
    assert run_hosted_day(s, {}, log, FakeRecorder, ts, now=datetime(2026, 10, 25, 9, 0, tzinfo=IST)) == "weekend"
    assert run_hosted_day(s, {}, log, FakeRecorder, ts, now=datetime(2026, 10, 26, 15, 31, tzinfo=IST)) == "market-closed"
    assert run_hosted_day(s, {}, log, FakeRecorder, ts, now=mon) == "no-token"
    ts.save(OWNER, "TOK", now=mon)
    d = {}
    assert run_hosted_day(s, d, log, FakeRecorder, ts, now=mon) == "done" and ran == ["TOK"] and "state" in d
    assert run_hosted_day(s, d, log, FakeRecorder, ts, now=mon) == "already-running"   # state is fresh


def test_rule_switches_owner_only_json_only():
    c, *_ = _client()
    assert c.get("/api/settings").status_code == 401
    c.get("/login")
    assert c.get("/api/settings").json() == {"settings": {}, "can_edit": True}
    assert c.post("/api/settings", data="allow_afternoon=true",
                  headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 415
    r = c.post("/api/settings", json={"allow_afternoon": True, "min_score": 1})
    assert r.status_code == 200 and r.json()["settings"] == {"allow_afternoon": True}
    v, *_ = _client(allowed=("*",), userinfo={"email": "viewer@gmail.com", "email_verified": True})
    v.get("/login")
    assert v.get("/api/settings").json()["can_edit"] is False
    assert v.post("/api/settings", json={"allow_afternoon": True}).status_code == 403
