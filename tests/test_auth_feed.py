import json
import socket
import threading
import time
import urllib.request

import pytest
from upstox_client.feeder.proto import MarketDataFeedV3_pb2 as pb

from tbrain.brokers.upstox import auth
from tbrain.brokers.upstox.feed import LiveFeed, build_request


def test_login_url_and_code_extraction():
    u = auth.build_login_url("KEY", "http://localhost:5000/upstox_redirect", "st8")
    assert u.startswith(auth.AUTH_DIALOG) and "response_type=code" in u and "redirect_uri=http%3A%2F%2Flocalhost" in u
    assert auth.extract_code("http://localhost:5000/upstox_redirect?code=abc&state=st8", "st8") == "abc"
    with pytest.raises(auth.AuthError):
        auth.extract_code("http://localhost:5000/upstox_redirect?code=abc&state=EVIL", "st8")
    with pytest.raises(auth.AuthError):
        auth.extract_code("http://localhost:5000/upstox_redirect?error=denied")


def test_local_callback_server_captures_code():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    uri = f"http://localhost:{port}/upstox_redirect"
    out = {}
    th = threading.Thread(target=lambda: out.setdefault("code", auth._capture_via_local_server(uri, "st", 10)))
    th.start(); time.sleep(0.3)
    urllib.request.urlopen(f"http://127.0.0.1:{port}/upstox_redirect?code=XYZ&state=st", timeout=5).read()
    th.join(5)
    assert out["code"] == "XYZ"


def test_token_file_is_private(tmp_path):
    from tbrain.config import Settings
    s = Settings(secrets_dir=tmp_path / ".secrets")
    auth.save_token(s, {"access_token": "t0k"})
    p = auth.token_path(s)
    assert oct(p.stat().st_mode & 0o777) == "0o600" and auth.get_token(s) == "t0k"


def test_request_format_matches_sdk():
    req = json.loads(build_request("sub", ["NSE_INDEX|Nifty 50"], "full"))
    assert req["method"] == "sub" and req["data"] == {"instrumentKeys": ["NSE_INDEX|Nifty 50"], "mode": "full"} and req["guid"]


def test_feed_dispatch_and_auth_failure():
    got, states = [], []
    f = LiveFeed(lambda: "t", lambda m, ns: got.append(m), lambda e, d: states.append(e))
    assert f.subscribe(["A", "A", "B"]) == ["A", "B"] and f.subscribe(["A"]) == []
    fr = pb.FeedResponse(); fr.type = pb.Type.Value("live_feed"); fr.feeds["A"].ltpc.ltp = 1.0
    f._handle_message(None, fr.SerializeToString())
    f._handle_message(None, b"\xff\xfe garbage")
    assert got and got[0]["feeds"]["A"]["ltpc"]["ltp"] == 1.0 and f.decode_errors == 1
    f._handle_error(None, Exception("Handshake status 401 Unauthorized"))
    assert f.fatal == "auth" and "auth_failed" in states


def test_all_requests_send_sdk_user_agent(monkeypatch):
    import urllib.request
    seen = []

    class Boom(Exception):
        pass

    def fake_urlopen(req, timeout=None):
        seen.append(req.get_header("User-agent"))
        raise Boom()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    from tbrain.brokers.upstox import rest
    from tbrain.config import Settings
    for fn in (lambda: auth.exchange_code(Settings(api_key="k", api_secret="s"), "c"),
               lambda: auth.token_is_live("t"),
               lambda: rest.get_json(rest.OPTION_CHAIN_URL, "t")):
        try:
            fn()
        except Exception:
            pass
    assert seen and all(ua == "Swagger-Codegen/1.0.0/python" for ua in seen), seen
