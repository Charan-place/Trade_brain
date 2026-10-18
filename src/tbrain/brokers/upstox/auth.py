"""Upstox OAuth login (daily). Flow per Upstox Authentication docs:
1) open /v2/login/authorization/dialog?response_type=code&client_id=..&redirect_uri=..&state=..
2) user logs in; Upstox redirects to redirect_uri?code=..&state=..
3) POST /v2/login/authorization/token (code, client_id, client_secret, redirect_uri, grant_type=authorization_code)
The code is single-use. Token is stored in .secrets/upstox_token.json (chmod 600) and never printed.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from tbrain.brokers.upstox.http import headers
from tbrain.config import Settings
from tbrain.timeutil import now_ist

AUTH_DIALOG = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
PROFILE_URL = "https://api.upstox.com/v2/user/profile"


class AuthError(RuntimeError):
    pass


def build_login_url(client_id: str, redirect_uri: str, state: str) -> str:
    q = urllib.parse.urlencode({"response_type": "code", "client_id": client_id,
                                "redirect_uri": redirect_uri, "state": state})
    return f"{AUTH_DIALOG}?{q}"


def extract_code(url_or_query: str, expected_state: str | None = None) -> str:
    s = url_or_query.strip()
    query = urllib.parse.urlparse(s).query if "://" in s else s.lstrip("?")
    qs = urllib.parse.parse_qs(query)
    if "code" not in qs:
        raise AuthError("No ?code= in the URL. Copy the FULL address-bar URL after login.")
    if expected_state is not None and qs.get("state", [None])[0] != expected_state:
        raise AuthError("state mismatch - possible stale/forged redirect; run login again.")
    return qs["code"][0]


def _capture_via_local_server(redirect_uri: str, state: str, timeout_s: int) -> str:
    u = urllib.parse.urlparse(redirect_uri)
    if u.scheme != "http" or u.hostname not in ("localhost", "127.0.0.1"):
        raise AuthError("redirect_uri is not a local http URL; use --manual")
    path = u.path or "/"
    result: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            p = urllib.parse.urlparse(self.path)
            if p.path != path:
                self.send_response(404); self.end_headers(); return
            try:
                result["code"] = extract_code("?" + p.query, state)
                body, code = b"<h3>Upstox login captured. You can close this tab.</h3>", 200
            except AuthError as e:
                result["error"] = str(e)
                body, code = f"<h3>Login failed: {e}</h3>".encode(), 400
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # keep the auth code out of the console
            pass

    srv = HTTPServer(("127.0.0.1", u.port or 80), Handler)
    srv.timeout = 1
    deadline = time.time() + timeout_s
    try:
        while time.time() < deadline and not result:
            srv.handle_request()
    finally:
        srv.server_close()
    if "error" in result:
        raise AuthError(result["error"])
    if "code" not in result:
        raise AuthError("Timed out waiting for the redirect.")
    return result["code"]


def exchange_code(s: Settings, code: str) -> dict:
    body = urllib.parse.urlencode({"code": code, "client_id": s.api_key, "client_secret": s.api_secret,
                                   "redirect_uri": s.redirect_uri, "grant_type": "authorization_code"}).encode()
    req = urllib.request.Request(TOKEN_URL, data=body, method="POST",
                                 headers=headers(**{"Content-Type": "application/x-www-form-urlencoded"}))
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise AuthError(f"Token exchange failed: HTTP {e.code}: {detail}") from None
    if "access_token" not in data:
        raise AuthError(f"Token response had no access_token (keys: {sorted(data)})")
    return data


def token_path(s: Settings) -> Path:
    return s.secrets_dir / "upstox_token.json"


def save_token(s: Settings, data: dict) -> None:
    s.secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(s.secrets_dir, 0o700)
    p = token_path(s)
    payload = {"access_token": data["access_token"], "obtained_at": now_ist().isoformat()}
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)


def load_token(s: Settings) -> dict | None:
    p = token_path(s)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def token_is_live(token: str) -> bool | None:
    """True/False from the profile endpoint; None if the network check itself failed."""
    req = urllib.request.Request(PROFILE_URL, headers=headers(token))
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403) else None
    except (urllib.error.URLError, OSError):
        return None


def login(s: Settings, manual: bool = False, timeout_s: int = 300, open_browser: bool = True) -> None:
    s.require_app_credentials()
    state = secrets.token_urlsafe(16)
    url = build_login_url(s.api_key, s.redirect_uri, state)
    print("Open this URL and log in to Upstox:\n" + url)
    if open_browser:
        webbrowser.open(url)
    if manual or not s.redirect_uri.startswith("http://"):
        pasted = input("\nAfter login, paste the FULL URL from the address bar here:\n> ")
        code = extract_code(pasted, state)
    else:
        print(f"\nWaiting for redirect on {s.redirect_uri} (up to {timeout_s}s)...")
        try:
            code = _capture_via_local_server(s.redirect_uri, state, timeout_s)
        except OSError as e:  # e.g. macOS AirPlay Receiver occupies port 5000
            print(f"Could not listen on {s.redirect_uri} ({e}). Falling back to paste mode.")
            pasted = input("After login, paste the FULL URL from the address bar here:\n> ")
            code = extract_code(pasted, state)
    save_token(s, exchange_code(s, code))
    print(f"Token saved to {token_path(s)} (not printed).")


def get_token(s: Settings) -> str:
    t = load_token(s)
    if not t or not t.get("access_token"):
        raise AuthError("No token. Run: uv run tbrain login")
    return t["access_token"]
