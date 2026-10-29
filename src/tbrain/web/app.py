"""Hosted web app (FastAPI).

Routes
  /                 sign-in page, or the live dashboard once signed in
  /login            -> Google sign-in            /auth/callback   Google redirect target
  /logout
  /request-access   shown after sign-in if the account isn't approved yet
  /admin/access     owner-only: approve/deny pending Google accounts
  /upstox/connect   -> Upstox login (daily)      /upstox/callback Upstox redirect target (token stored encrypted)
  /api/me           who am I + Upstox status     /api/state, /api/stream   live brain state (JSON / SSE)
  /privacy, /terms  static legal pages
  /healthz

Access: a Google account is let in if its verified email is in TBRAIN_ALLOWED_EMAILS ("*" = any verified Google
account) OR has status "approved" in the `access` Mongo collection. Anyone else who signs in lands on
/request-access and a pending row is created in `access` for the owner to approve from /admin/access.
Signal-only: nothing here can place an order.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from starlette.middleware.sessions import SessionMiddleware

STATIC = Path(__file__).resolve().parents[1] / "ui" / "static"
GOOGLE_META = "https://accounts.google.com/.well-known/openid-configuration"
SSE_EVERY_S = 1.0
STREAM_MAX_S = 110

SIGNIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Trade Brain</title>
<style>
:root{--bg:#f6f7f9;--bg2:#eef1f6;--card:#fff;--ink:#14171c;--ink2:#5b6470;--ring:#dde1e7;--acc:#1f6feb;--acc2:#2f80ff}
*{box-sizing:border-box}
@media (prefers-color-scheme:dark){:root{--bg:#0b0d11;--bg2:#12151a;--card:#171a20;--ink:#e8eaee;--ink2:#9aa3ae;--ring:#2a2f37;--acc:#4c8dff;--acc2:#5e9bff}}
body{margin:0;min-height:100vh;display:grid;place-items:center;
  background:radial-gradient(circle at 20% 15%,var(--bg2),var(--bg) 60%);
  color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}
.card{background:var(--card);box-shadow:0 0 0 1px var(--ring),0 20px 48px -20px rgba(20,23,28,.25);
  border-radius:18px;padding:32px;max-width:380px;width:100%;animation:rise .4s ease-out}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:translateY(0)}}
.mark{width:40px;height:40px;border-radius:11px;display:grid;place-items:center;margin-bottom:16px;
  background:linear-gradient(135deg,var(--acc),var(--acc2));color:#fff;font-weight:700;font-size:17px}
h1{font-size:21px;margin:0 0 6px;letter-spacing:-.01em} p{color:var(--ink2);margin:0 0 22px;font-size:13.5px}
a.btn{display:flex;align-items:center;justify-content:center;gap:10px;text-align:center;
  background:#fff;color:#1f1f1f;text-decoration:none;padding:11px 14px;border-radius:10px;font-weight:600;
  box-shadow:0 0 0 1px var(--ring);transition:box-shadow .15s,transform .15s}
a.btn:hover{box-shadow:0 0 0 1px var(--acc2),0 4px 14px -6px rgba(31,111,235,.35);transform:translateY(-1px)}
a.btn svg{flex:none}
.err{color:#d1453b;background:color-mix(in srgb,#d1453b 10%,transparent);border-radius:8px;
  padding:9px 11px;margin-top:14px;font-size:13.5px}
small{display:block;margin-top:20px;color:var(--ink2);font-size:12px;text-align:center}
</style></head><body><div class="card">
<div class="mark">TB</div>
<h1>Trade Brain</h1>
<p>NIFTY + SENSEX options brain. Signal-only &mdash; it never places orders.</p>
<a class="btn" href="/login">
<svg width="18" height="18" viewBox="0 0 18 18"><path fill="#4285F4" d="M17.64 9.2c0-.64-.06-1.25-.16-1.84H9v3.48h4.84a4.14 4.14 0 0 1-1.8 2.72v2.26h2.92c1.7-1.57 2.68-3.88 2.68-6.62Z"/><path fill="#34A853" d="M9 18c2.43 0 4.47-.8 5.96-2.18l-2.92-2.26c-.81.54-1.84.86-3.04.86-2.34 0-4.32-1.58-5.03-3.7H.96v2.33A9 9 0 0 0 9 18Z"/><path fill="#FBBC05" d="M3.97 10.72A5.4 5.4 0 0 1 3.68 9c0-.6.1-1.18.29-1.72V4.95H.96A9 9 0 0 0 0 9c0 1.45.35 2.83.96 4.05l3.01-2.33Z"/><path fill="#EA4335" d="M9 3.58c1.32 0 2.51.46 3.44 1.35l2.59-2.59C13.46.89 11.43 0 9 0A9 9 0 0 0 .96 4.95l3.01 2.33C4.68 5.16 6.66 3.58 9 3.58Z"/></svg>
Sign in with Google</a>__ERR__
<small>Personal research tool &middot; Not investment advice<br>
<a href="/privacy" style="color:inherit">Privacy</a> &middot; <a href="/terms" style="color:inherit">Terms</a></small>
</div></body></html>"""

LEGAL_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} &middot; Trade Brain</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--ink2:#5b6470;--ring:#dde1e7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b0d11;--card:#171a20;--ink:#e8eaee;--ink2:#9aa3ae;--ring:#2a2f37}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:15px/1.65 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}}
.wrap{{max-width:680px;margin:0 auto;padding:40px 20px 60px}}
.card{{background:var(--card);box-shadow:0 0 0 1px var(--ring);border-radius:14px;padding:32px}}
a{{color:#1f6feb}} h1{{font-size:24px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 8px}}
.meta{{color:var(--ink2);font-size:13px;margin:0 0 24px}}
p,li{{color:var(--ink2)}} ul{{padding-left:20px}}
.back{{display:inline-block;margin-bottom:18px;color:var(--ink2);text-decoration:none;font-size:13.5px}}
</style></head><body><div class="wrap">
<a class="back" href="/">&larr; Trade Brain</a>
<div class="card"><h1>{title}</h1><p class="meta">Last updated: 2026-10-23</p>
{body}
</div></div></body></html>"""

PRIVACY_BODY = """
<p>Trade Brain is a personal, invite-only options research dashboard. It is not a public product and does not
sell or share your data with third parties.</p>
<h2>What we collect</h2>
<ul>
<li><strong>Google account:</strong> your email, name and profile info via Google Sign-In, used only to identify
you and check you're on the allowed list.</li>
<li><strong>Upstox access token:</strong> if you connect a broker account, your Upstox access token is stored
encrypted and used only to read market data (quotes, option chains). The app never places, modifies or
cancels orders.</li>
<li><strong>Usage data:</strong> session cookies to keep you signed in. No analytics or ad trackers.</li>
</ul>
<h2>How it's used</h2>
<p>Data is used solely to run the dashboard you see: computing and displaying trading signals from market data.
Market/options data processed by the app is not personal data.</p>
<h2>Storage &amp; security</h2>
<p>Data is stored in a managed MongoDB database. Upstox tokens are encrypted at rest. Access to the app is
restricted to an explicit allow-list of Google accounts.</p>
<h2>Data deletion</h2>
<p>Contact the app owner to request deletion of your account data or disconnection of your Upstox token.</p>
<h2>Contact</h2>
<p>Questions: reach the app owner directly.</p>
"""

TERMS_BODY = """
<p>By signing in to Trade Brain you agree to the following.</p>
<h2>Nature of the service</h2>
<p>Trade Brain is a personal research tool that displays market signals derived from options data. It is
<strong>signal-only</strong>: it never places, modifies or cancels any order on your behalf.</p>
<h2>Not investment advice</h2>
<p>Nothing shown in this app is investment, legal or tax advice. All trading decisions and their outcomes are
entirely your own responsibility.</p>
<h2>Access</h2>
<p>Access is limited to accounts explicitly allow-listed by the app owner and may be revoked at any time
without notice.</p>
<h2>No warranty</h2>
<p>The app is provided "as is", without warranty of any kind, including accuracy, uptime or fitness for a
particular purpose.</p>
<h2>Changes</h2>
<p>These terms may be updated at any time; continued use after a change means you accept the update.</p>
"""

ACCOUNT_BAR = """
<div id="tb-acct" style="position:fixed;right:12px;bottom:12px;z-index:30;background:var(--surface,#fff);color:var(--ink,#111);
box-shadow:0 0 0 1px var(--ring,#ccc),0 6px 20px rgba(0,0,0,.18);border-radius:10px;padding:8px 10px;font:12.5px system-ui;display:flex;gap:10px;align-items:center;max-width:calc(100vw - 24px);flex-wrap:wrap">
<style>@media (max-width:640px){#tb-acct{position:static!important;margin:12px 16px 20px}}</style>
<span id="tb-who">...</span><span id="tb-up"></span>
<a href="/upstox/connect" id="tb-con" style="display:none">Connect Upstox</a>
<a href="/admin/access" id="tb-admin" style="display:none">Access</a>
<a href="/logout">Sign out</a></div>
<script>
(async()=>{try{const r=await fetch('/api/me',{cache:'no-store'});if(!r.ok)return;const m=await r.json();
document.getElementById('tb-who').textContent=m.email;
const up=document.getElementById('tb-up'),con=document.getElementById('tb-con'),adm=document.getElementById('tb-admin');
if(m.upstox&&m.upstox.connected_today){up.textContent='Upstox: connected today';}
else{up.textContent='Upstox: not connected today';con.style.display='inline';}
if(!m.is_owner){con.style.display='none';up.textContent='Viewer';}
else{adm.style.display='inline';}
}catch(e){}})();
</script>"""


def email_allowed(email: str | None, verified, allowed: tuple[str, ...]) -> bool:
    if not email or verified is not True:
        return False
    return "*" in allowed or email.lower() in allowed


REQUEST_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Request access &middot; Trade Brain</title>
<style>
:root{{--bg:#f6f7f9;--bg2:#eef1f6;--card:#fff;--ink:#14171c;--ink2:#5b6470;--ring:#dde1e7;--acc:#1f6feb}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b0d11;--bg2:#12151a;--card:#171a20;--ink:#e8eaee;--ink2:#9aa3ae;--ring:#2a2f37;--acc:#4c8dff}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;background:radial-gradient(circle at 20% 15%,var(--bg2),var(--bg) 60%);
  color:var(--ink);font:15px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}}
.card{{background:var(--card);box-shadow:0 0 0 1px var(--ring);border-radius:18px;padding:32px;max-width:380px;width:100%}}
h1{{font-size:20px;margin:0 0 6px}} p{{color:var(--ink2);margin:0 0 16px;font-size:13.5px}}
.btn{{display:block;text-align:center;background:var(--acc);color:#fff;text-decoration:none;padding:11px 14px;border-radius:10px;font-weight:600;border:0;width:100%;font-size:14.5px;cursor:pointer}}
.ok{{color:#1a8754;background:color-mix(in srgb,#1a8754 10%,transparent);border-radius:8px;padding:9px 11px;font-size:13.5px}}
a{{color:var(--ink2)}}
</style></head><body><div class="card">
<h1>Request access</h1>
{body}
</div></body></html>"""


def access_doc(email: str, name: str | None) -> dict:
    from datetime import datetime, timezone
    return {"_id": email, "name": name, "status": "pending", "requested_at": datetime.now(timezone.utc)}


ADMIN_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Access &middot; Trade Brain</title>
<style>
:root{{--bg:#f6f7f9;--card:#fff;--ink:#14171c;--ink2:#5b6470;--ring:#dde1e7}}
@media (prefers-color-scheme:dark){{:root{{--bg:#0b0d11;--card:#171a20;--ink:#e8eaee;--ink2:#9aa3ae;--ring:#2a2f37}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:24px}}
.wrap{{max-width:760px;margin:0 auto}}
h1{{font-size:19px;margin:0 0 18px}}
table{{width:100%;border-collapse:collapse;background:var(--card);box-shadow:0 0 0 1px var(--ring);border-radius:10px;overflow:hidden}}
th,td{{text-align:left;padding:10px 12px;border-bottom:1px solid var(--ring);font-size:13.5px}}
th{{color:var(--ink2);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.02em}}
tr:last-child td{{border-bottom:0}}
.st{{padding:2px 8px;border-radius:99px;font-size:12px}}
.st-approved{{background:color-mix(in srgb,#1a8754 15%,transparent);color:#1a8754}}
.st-pending{{background:color-mix(in srgb,#b58900 15%,transparent);color:#b58900}}
.st-denied{{background:color-mix(in srgb,#d1453b 15%,transparent);color:#d1453b}}
button{{border:0;border-radius:6px;padding:5px 10px;font-size:12.5px;cursor:pointer;margin-right:6px}}
.a-approve{{background:#1a8754;color:#fff}} .a-deny{{background:#d1453b;color:#fff}}
a.back{{color:var(--ink2);text-decoration:none;font-size:13px;display:inline-block;margin-bottom:14px}}
</style></head><body><div class="wrap">
<a class="back" href="/">&larr; Trade Brain</a>
<h1>Access requests</h1>
<table><thead><tr><th>Email</th><th>Name</th><th>Status</th><th>Actions</th></tr></thead>
<tbody>{rows}</tbody></table>
</div></body></html>"""


def create_app(s, *, state_source=None, db=None, tokens=None, oauth=None, on_upstox_connected=None,
               settings_store=None, order_broker_factory=None) -> FastAPI:
    """Build the app. Dependencies are injectable so tests can run without Google, Mongo or Modal."""
    if not s.session_secret:
        raise RuntimeError("TBRAIN_SESSION_SECRET is not set")
    if order_broker_factory is None:
        order_broker_factory = s.order_broker
    if settings_store is None:
        from tbrain.runtime_settings import FileSettings
        settings_store = FileSettings(s.data_dir / "live" / "settings.json")
    if state_source is None:
        from tbrain.web.state import FileStateSource
        state_source = FileStateSource(s)
    if db is None and s.mongodb_uri:
        from tbrain.storage.mongo import connect
        db = connect(s.mongodb_uri, s.mongodb_db)
    if tokens is None and db is not None and s.encryption_key:
        from tbrain.web.tokens import TokenStore
        tokens = TokenStore(db, s.encryption_key)
    if oauth is None:
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register(name="google", client_id=s.google_client_id, client_secret=s.google_client_secret,
                       server_metadata_url=GOOGLE_META, client_kwargs={"scope": "openid email profile"})

    app = FastAPI(title="Trade Brain", docs_url=None, redoc_url=None, openapi_url=None)
    secure = s.public_url.startswith("https://")
    app.add_middleware(SessionMiddleware, secret_key=s.session_secret, session_cookie="tb_session",
                       max_age=7 * 24 * 3600, same_site="lax", https_only=secure)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if secure:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return resp

    def is_approved(email: str) -> bool:
        if email_allowed(email, True, s.allowed_emails):
            return True
        if db is None:
            return False
        try:
            doc = db.access.find_one({"_id": email})
        except Exception:  # noqa: BLE001 - a Mongo hiccup should deny, not 500
            return False
        return bool(doc and doc.get("status") == "approved")

    def user(request: Request) -> dict | None:
        u = request.session.get("user")
        # re-check the allow-list/Mongo status on every request so revoking access takes effect immediately
        if u and is_approved(u.get("email", "")):
            return u
        return None

    def is_owner(u: dict) -> bool:
        return bool(u) and bool(s.owner_email) and u.get("email") == s.owner_email

    def base(request: Request) -> str:
        return s.public_url or str(request.base_url).rstrip("/")

    @app.get("/favicon.ico")
    async def favicon():
        from fastapi import Response
        return Response(status_code=204)

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy():
        return HTMLResponse(LEGAL_SHELL.format(title="Privacy Policy", body=PRIVACY_BODY))

    @app.get("/terms", response_class=HTMLResponse)
    async def terms():
        return HTMLResponse(LEGAL_SHELL.format(title="Terms of Service", body=TERMS_BODY))

    @app.get("/healthz")
    async def healthz():
        return PlainTextResponse("ok")

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if not user(request):
            err = request.session.pop("flash", "")
            return HTMLResponse(SIGNIN_HTML.replace("__ERR__", f'<div class="err">{err}</div>' if err else ""))
        html = (STATIC / "index.html").read_text()
        return HTMLResponse(html.replace("</body>", ACCOUNT_BAR + "</body>"), headers={"Cache-Control": "no-store"})

    @app.get("/login")
    async def login(request: Request):
        return await oauth.google.authorize_redirect(request, base(request) + "/auth/callback")

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        try:
            tok = await oauth.google.authorize_access_token(request)
        except Exception:  # noqa: BLE001 - bad/expired state, user cancelled, etc.
            request.session["flash"] = "Google sign-in failed. Please try again."
            return RedirectResponse("/", status_code=303)
        info = tok.get("userinfo") or {}
        email = (info.get("email") or "").lower()
        name = info.get("name")
        if not email or info.get("email_verified") is not True:
            request.session.clear()
            request.session["flash"] = "Google sign-in failed. Please try again."
            return RedirectResponse("/", status_code=303)
        if db is not None:
            from datetime import datetime, timezone
            try:
                db.users.update_one({"_id": email}, {"$set": {"name": name, "google_sub": info.get("sub"),
                                                              "last_login": datetime.now(timezone.utc)},
                                                     "$setOnInsert": {"created_at": datetime.now(timezone.utc)}},
                                    upsert=True)
            except Exception:  # noqa: BLE001 - sign-in still works if Mongo hiccups
                pass
        if not is_approved(email):
            request.session.clear()
            if db is not None:
                try:
                    db.access.update_one({"_id": email}, {"$setOnInsert": access_doc(email, name)}, upsert=True)
                except Exception:  # noqa: BLE001
                    pass
            request.session["pending_email"] = email
            return RedirectResponse("/request-access", status_code=303)
        request.session.clear()
        request.session["user"] = {"email": email, "name": name, "sub": info.get("sub")}
        return RedirectResponse("/", status_code=303)

    @app.get("/request-access", response_class=HTMLResponse)
    async def request_access(request: Request):
        email = request.session.get("pending_email")
        if not email:
            return RedirectResponse("/", status_code=303)
        status = None
        if db is not None:
            try:
                doc = db.access.find_one({"_id": email})
                status = doc.get("status") if doc else None
            except Exception:  # noqa: BLE001
                pass
        if status == "denied":
            body = f'<p class="ok" style="color:#d1453b;background:color-mix(in srgb,#d1453b 10%,transparent)">Access for <strong>{email}</strong> was declined.</p><p><a href="/logout">Sign out</a></p>'
        else:
            body = (f'<div class="ok">Request sent for <strong>{email}</strong>. You\'ll get access once the owner '
                    f'approves it &mdash; just sign in again after that.</div><p style="margin-top:14px"><a href="/logout">Sign out</a></p>')
        return HTMLResponse(REQUEST_HTML.format(body=body))

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/", status_code=303)

    @app.get("/api/me")
    async def me(request: Request):
        u = user(request)
        if not u:
            return JSONResponse({"error": "sign in"}, status_code=401)
        up = tokens.status(u["email"]) if tokens is not None else None
        return {"email": u["email"], "name": u.get("name"), "is_owner": is_owner(u), "upstox": up}

    @app.get("/upstox/connect")
    async def upstox_connect(request: Request):
        u = user(request)
        if not u:
            return RedirectResponse("/", status_code=303)
        if not is_owner(u):
            return PlainTextResponse("Only the owner account can connect Upstox for now.", status_code=403)
        if tokens is None:
            return PlainTextResponse("Server is missing MONGODB_URI / TBRAIN_ENCRYPTION_KEY.", status_code=500)
        from tbrain.brokers.upstox.auth import build_login_url
        state = secrets.token_urlsafe(24)
        request.session["upstox_state"] = state
        return RedirectResponse(build_login_url(s.api_key, s.redirect_uri, state), status_code=303)

    @app.get("/upstox/callback")
    async def upstox_callback(request: Request):
        u = user(request)
        if not u or not is_owner(u):
            return RedirectResponse("/", status_code=303)
        expected = request.session.pop("upstox_state", None)
        code, state = request.query_params.get("code"), request.query_params.get("state")
        if not code or not expected or not secrets.compare_digest(str(state), expected):
            return PlainTextResponse("Upstox login failed (state mismatch). Start again from Connect Upstox.", status_code=400)
        from tbrain.brokers.upstox.auth import exchange_code
        try:
            data = await asyncio.to_thread(exchange_code, s, code)
        except Exception as e:  # noqa: BLE001
            return PlainTextResponse(f"Upstox token exchange failed: {str(e)[:200]}", status_code=502)
        tokens.save(u["email"], data["access_token"])
        if on_upstox_connected is not None:  # hosted: start today's brain right away
            try:
                await asyncio.to_thread(on_upstox_connected, u["email"])
            except Exception:  # noqa: BLE001 - the 09:00 schedule is the fallback
                pass
        return RedirectResponse("/", status_code=303)

    def csrf_token(request: Request) -> str:
        tok = request.session.get("csrf")
        if not tok:
            tok = secrets.token_urlsafe(24)
            request.session["csrf"] = tok
        return tok

    def csrf_ok(request: Request, token: str) -> bool:
        expected = request.session.get("csrf")
        return bool(expected) and secrets.compare_digest(token or "", expected)

    @app.get("/admin/access", response_class=HTMLResponse)
    async def admin_access(request: Request):
        u = user(request)
        if not u or not is_owner(u):
            return RedirectResponse("/", status_code=303)
        if db is None:
            return PlainTextResponse("Server is missing MONGODB_URI.", status_code=500)
        tok = csrf_token(request)
        rows = []
        for doc in db.access.find().sort("requested_at", -1):
            email, status = doc["_id"], doc.get("status", "pending")
            name = doc.get("name") or ""
            from urllib.parse import quote

            def form(action: str, cls: str) -> str:
                return (f'<form method="post" action="/admin/access/{action}?email={quote(email)}" style="display:inline">'
                        f'<input type="hidden" name="csrf" value="{tok}">'
                        f'<button class="a-{cls}">{action.title()}</button></form>')

            actions = "".join(form(a, a) for a in (("approve", "deny") if status != "approved" else ("deny",)))
            if status == "denied":
                actions += form("approve", "approve")
            rows.append(f'<tr><td>{email}</td><td>{name}</td><td><span class="st st-{status}">{status}</span></td><td>{actions}</td></tr>')
        table = "\n".join(rows) or '<tr><td colspan="4" style="color:var(--ink2)">No access requests yet.</td></tr>'
        return HTMLResponse(ADMIN_HTML.format(rows=table))

    @app.post("/admin/access/approve")
    async def admin_approve(request: Request, email: str = "", csrf: str = ""):
        u = user(request)
        if not u or not is_owner(u) or db is None or not csrf_ok(request, csrf):
            return RedirectResponse("/", status_code=303)
        email = email.lower()
        if email:
            db.access.update_one({"_id": email}, {"$set": {"status": "approved"}}, upsert=True)
        return RedirectResponse("/admin/access", status_code=303)

    @app.post("/admin/access/deny")
    async def admin_deny(request: Request, email: str = "", csrf: str = ""):
        u = user(request)
        if not u or not is_owner(u) or db is None or not csrf_ok(request, csrf):
            return RedirectResponse("/", status_code=303)
        email = email.lower()
        if email:
            db.access.update_one({"_id": email}, {"$set": {"status": "denied"}}, upsert=True)
        return RedirectResponse("/admin/access", status_code=303)

    @app.get("/api/history")
    async def api_history(request: Request, days: int = 60, book: str = "signal"):
        u = user(request)
        if not u:
            return JSONResponse({"error": "sign in"}, status_code=401)
        from tbrain import history as H
        days = max(1, min(int(days), 365))
        if book not in ("signal", "paper", "real"):
            return JSONResponse({"error": "book must be signal, paper or real"}, status_code=400)
        if book != "signal" and not is_owner(u):
            return JSONResponse({"error": "owner only"}, status_code=403)
        try:
            if book != "signal":
                h = await asyncio.to_thread(H.from_mongo_exec, db, s.owner_email or None, book, days) if db is not None \
                    else await asyncio.to_thread(H.from_exec_files, s.data_dir, book, days)
            elif db is not None:
                h = await asyncio.to_thread(H.from_mongo, db, s.owner_email or None, days)
            else:
                h = await asyncio.to_thread(H.from_files, s.data_dir, days)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": f"history unavailable: {type(e).__name__}"}, status_code=503)
        return JSONResponse(h, headers={"Cache-Control": "no-store"})

    # ---------------- real money (OWNER ONLY). Every other account sees signals only.
    def _owner_json(request: Request):
        u = user(request)
        if not u:
            return None, JSONResponse({"error": "sign in"}, status_code=401)
        if not is_owner(u):
            return None, JSONResponse({"error": "owner only"}, status_code=403)
        if "application/json" not in (request.headers.get("content-type") or "") and request.method == "POST":
            return None, JSONResponse({"error": "json only"}, status_code=415)
        return u, None

    @app.get("/api/real")
    async def real_get(request: Request):
        u, err = _owner_json(request)
        if err:
            return err
        from tbrain.ui.server import offline_real_status
        return {"real": await asyncio.to_thread(settings_store.get_real),
                "defaults": {"loss_cap": s.real_daily_loss_cap, "loss_cap_max": s.real_loss_cap_max},
                "status": await asyncio.to_thread(offline_real_status, s, settings_store)}

    @app.post("/api/real")
    async def real_set(request: Request):
        u, err = _owner_json(request)
        if err:
            return err
        from tbrain.execution.hub import CONFIRM_PHRASE
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "bad json"}, status_code=400)
        if "loss_cap" in body:
            try:
                cap = float(body["loss_cap"])
            except (TypeError, ValueError):
                return JSONResponse({"error": "loss cap must be a number"}, status_code=400)
            if not (0 < cap <= s.real_loss_cap_max):
                return JSONResponse({"error": f"loss cap must be between 1 and {s.real_loss_cap_max:,.0f}"}, status_code=400)
            await asyncio.to_thread(settings_store.set_real, loss_cap=cap)
        if "on" in body:
            if body["on"] is True:
                if str(body.get("confirm", "")).strip().lower() != CONFIRM_PHRASE:
                    return JSONResponse({"error": f'type exactly: {CONFIRM_PHRASE}'}, status_code=400)
                await asyncio.to_thread(settings_store.set_real, on=True, why_off=None)
            elif body["on"] is False:
                await asyncio.to_thread(settings_store.set_real, on=False, why_off="switched off by you")
            else:
                return JSONResponse({"error": "on must be true or false"}, status_code=400)
        return {"real": await asyncio.to_thread(settings_store.get_real)}

    @app.post("/api/real/connect")
    async def real_connect(request: Request):
        """Owner: log in to Groww for today through the static-IP order server."""
        u, err = _owner_json(request)
        if err:
            return err
        rb = order_broker_factory() if order_broker_factory else None
        if rb is None:
            return JSONResponse({"error": "order server is not set up yet (TBRAIN_ORDER_SERVER_URL / _TOKEN)"}, status_code=400)
        try:
            r = await asyncio.to_thread(rb.connect)
        except Exception as e:  # noqa: BLE001
            return JSONResponse({"error": str(e)[:300]}, status_code=502)
        return {"connected": bool(r.get("connected"))}

    @app.post("/api/real/exit")
    async def real_exit(request: Request):
        u, err = _owner_json(request)
        if err:
            return err
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if isinstance(body, dict) and body.get("all") is True:
            c = await asyncio.to_thread(settings_store.push_command, "exit_all")
        elif isinstance(body, dict) and isinstance(body.get("call_id"), str) and body["call_id"]:
            c = await asyncio.to_thread(settings_store.push_command, "exit", body["call_id"])
        else:
            return JSONResponse({"error": "send {call_id} or {all: true}"}, status_code=400)
        return {"queued": c}

    @app.post("/api/real/approve")
    async def real_approve(request: Request):
        u, err = _owner_json(request)
        if err:
            return err
        try:
            body = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        if not (isinstance(body, dict) and isinstance(body.get("call_id"), str) and body["call_id"]):
            return JSONResponse({"error": "send {call_id}"}, status_code=400)
        return {"queued": await asyncio.to_thread(settings_store.push_command, "approve", body["call_id"])}

    @app.get("/api/settings")
    async def get_settings(request: Request):
        u = user(request)
        if not u:
            return JSONResponse({"error": "sign in"}, status_code=401)
        return {"settings": await asyncio.to_thread(settings_store.get), "can_edit": is_owner(u)}

    @app.post("/api/settings")
    async def post_settings(request: Request):
        u = user(request)
        if not u:
            return JSONResponse({"error": "sign in"}, status_code=401)
        if not is_owner(u):
            return JSONResponse({"error": "only the owner can change the brain's rules"}, status_code=403)
        if "application/json" not in (request.headers.get("content-type") or ""):
            return JSONResponse({"error": "json only"}, status_code=415)   # CSRF guard (forms can't send JSON)
        try:
            changes = await request.json()
        except ValueError:
            return JSONResponse({"error": "bad json"}, status_code=400)
        cur = await asyncio.to_thread(settings_store.update, changes if isinstance(changes, dict) else {})
        return {"settings": cur, "can_edit": True}

    @app.get("/api/state")
    async def api_state(request: Request):
        if not user(request):
            return JSONResponse({"error": "sign in"}, status_code=401)
        st = await asyncio.to_thread(state_source.latest)
        if not is_owner(user(request)):
            st = {k: v for k, v in st.items() if k != "execution"}     # paper/real are owner-only
        return JSONResponse(st, headers={"Cache-Control": "no-store"})

    @app.get("/api/stream")
    async def api_stream(request: Request):
        if not user(request):
            return JSONResponse({"error": "sign in"}, status_code=401)

        owner = is_owner(user(request))

        async def gen():
            # Hosting platforms cap request time (Modal: 150 s), so each stream ends after STREAM_MAX_S and the
            # browser's EventSource reconnects by itself after `retry` ms (the page polls /api/state meanwhile).
            last = None
            beats = 0
            started = asyncio.get_running_loop().time()
            yield "retry: 1500\n\n"
            while not await request.is_disconnected():
                if asyncio.get_running_loop().time() - started > STREAM_MAX_S:
                    return
                st = await asyncio.to_thread(state_source.latest)
                if not owner:
                    st = {k: v for k, v in st.items() if k != "execution"}   # paper/real are owner-only
                payload = json.dumps(st, default=str, separators=(",", ":"))
                if payload != last:
                    last = payload
                    beats = 0
                    yield f"data: {payload}\n\n"
                else:
                    beats += 1
                    if beats * SSE_EVERY_S >= 15:
                        beats = 0
                        yield ": keep-alive\n\n"
                await asyncio.sleep(SSE_EVERY_S)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    return app
