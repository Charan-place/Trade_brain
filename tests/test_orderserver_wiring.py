"""Phase 2 wiring: settings -> RemoteBroker, owner-only Connect Groww, order server build, CSV filter."""
import pytest

from tbrain.config import Settings

SECRET = "s" * 40


def test_order_broker_is_none_until_configured(monkeypatch):
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_URL", "")
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_TOKEN", "")
    assert Settings.load().order_broker() is None
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_URL", "https://1.2.3.4:8443/")
    assert Settings.load().order_broker() is None          # url alone is not enough


def test_order_broker_built_from_env_and_cert_newlines_restored(monkeypatch):
    pytest.importorskip("httpx")
    from tbrain.brokers.remote import RemoteBroker
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_URL", "https://1.2.3.4:8443/")
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_TOKEN", SECRET)
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_CERT", "")
    s = Settings.load()
    assert s.order_server_url == "https://1.2.3.4:8443" and "s" * 40 not in repr(s)
    assert isinstance(s.order_broker(), RemoteBroker)
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_CERT", "-----BEGIN CERTIFICATE-----\\nAAA\\n-----END CERTIFICATE-----")
    assert Settings.load().order_server_cert.count("\n") == 2


class _RB:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, 0

    def connect(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.fail)
        return {"connected": True}


def _web(factory, owner=True):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from cryptography.fernet import Fernet
    from tbrain.runtime_settings import DictSettings
    from tbrain.web.app import create_app
    from tests import test_web as W
    s = W._settings(allowed=("*",)); s.encryption_key = Fernet.generate_key().decode()
    oauth = W.FakeOAuth()
    if not owner:
        oauth.google.userinfo = {"email": "viewer@gmail.com", "email_verified": True}
    app = create_app(s, state_source=W.MemState(), db=None, tokens=None, oauth=oauth,
                     settings_store=DictSettings({}), order_broker_factory=factory)
    c = TestClient(app); c.get("/login")
    return c


def test_connect_groww_owner_only_and_reports_errors():
    rb = _RB()
    assert _web(lambda: rb, owner=False).post("/api/real/connect", json={}).status_code == 403 and rb.calls == 0
    c = _web(lambda: rb)
    r = c.post("/api/real/connect", json={})
    assert r.status_code == 200 and r.json() == {"connected": True} and rb.calls == 1
    assert c.post("/api/real/connect", data="x", headers={"Content-Type": "text/plain"}).status_code == 415
    assert _web(lambda: None).post("/api/real/connect", json={}).status_code == 400
    r = _web(lambda: _RB("approve today's access on Groww first")).post("/api/real/connect", json={})
    assert r.status_code == 502 and "approve" in r.json()["error"]


def test_groww_csv_keeps_only_nse_bse_options():
    from tbrain.orderserver.main import load_groww_rows
    text = ("exchange,exchange_token,trading_symbol,instrument_type,strike_price,expiry_date,lot_size,freeze_quantity\n"
            "NSE,35003,NIFTY25OCT25000CE,CE,25000,2026-10-28,75,1800\n"
            "BSE,8123,SENSEX25OCT82000PE,PE,82000,2026-10-30,20,1000\n"
            "NSE,2885,RELIANCE,EQ,0,,1,\n"
            "MCX,1,CRUDE,CE,6000,2026-11-16,100,\n")
    rows = load_groww_rows(text=text)
    assert [r["exchange_token"] for r in rows] == ["35003", "8123"]


def test_order_server_build_needs_a_long_token_and_serves_signed_status():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tbrain.brokers.remote import RemoteBroker
    from tbrain.orderserver.main import build
    with pytest.raises(RuntimeError):
        build(env={"TBRAIN_ORDER_SERVER_TOKEN": "short"}, rows=[])
    app, broker = build(env={"TBRAIN_ORDER_SERVER_TOKEN": SECRET}, rows=[], log=lambda *a: None)
    tc = TestClient(app)
    assert tc.get("/healthz").status_code == 200
    assert tc.get("/status").status_code == 401                    # unsigned is refused
    rb = RemoteBroker("http://testserver", SECRET, client=tc)
    assert rb.connected is False                                   # signed call works, no session yet
    bad = RemoteBroker("http://testserver", "x" * 40, client=tc)
    with pytest.raises(Exception):
        bad.connect()                                              # wrong secret never logs in


def test_owner_gets_offline_real_status_when_brain_is_not_running(monkeypatch):
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_URL", "")
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_TOKEN", "")
    c = _web(lambda: None)
    st = c.get("/api/real").json()["status"]
    assert st["brain_running"] is False and st["configured"] is False and st["connected"] is False
    assert st["confirm_phrase"] == "yes use my real money" and st["loss_cap"] > 0 and "not set up" in st["note"]
    assert _web(lambda: None, owner=False).get("/api/real").status_code == 403   # viewers never get it


def test_offline_status_reports_unreachable_order_server(monkeypatch):
    from tbrain.runtime_settings import DictSettings
    from tbrain.ui.server import offline_real_status
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_URL", "https://127.0.0.1:9")    # nothing listens here
    monkeypatch.setenv("TBRAIN_ORDER_SERVER_TOKEN", SECRET)
    st = offline_real_status(Settings.load(), DictSettings({}))
    assert st["configured"] is True and st["connected"] is False and "not reachable" in (st["note"] or "")
