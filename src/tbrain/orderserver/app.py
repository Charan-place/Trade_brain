"""Order server (FastAPI). Runs ONLY on the static-IP VM whitelisted in Groww.

It exposes the Broker interface to the brain over signed HTTPS and holds today's Groww session.
Groww keys stay on this machine (GROWW_API_KEY / GROWW_API_SECRET in its .env) and never leave it.

  GET  /status                 connected? (signed)
  POST /session/connect        log in to Groww for today (needs today's approval on Groww's API Keys page)
  POST /session/disconnect     forget today's session
  POST /broker/<op>            place | cancel | modify | get | position | funds | quote | supports | matches
"""
from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tbrain.execution.broker import BrokerError
from tbrain.orderserver.signing import Verifier

OPS = ("place", "cancel", "modify", "get", "position", "funds", "quote", "supports", "matches")


def create_order_app(broker, secret: str, login=None, log=print) -> FastAPI:
    """broker: GrowwBroker. login(): -> access token string for today (raises with a readable message)."""
    v = Verifier(secret)
    app = FastAPI(title="tbrain order server", docs_url=None, redoc_url=None, openapi_url=None)
    lock = asyncio.Lock()

    async def body_of(request: Request):
        raw = await request.body()
        why = v.check(request.method, request.url.path, raw, request.headers.get("x-tb-ts"),
                      request.headers.get("x-tb-nonce"), request.headers.get("x-tb-sig"))
        if why:
            return None, JSONResponse({"error": f"refused: {why}"}, status_code=401)
        try:
            return (json.loads(raw) if raw else {}), None
        except ValueError:
            return None, JSONResponse({"error": "bad json"}, status_code=400)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/status")
    async def status(request: Request):
        _, err = await body_of(request)
        if err:
            return err
        return {"connected": broker.connected, "broker": broker.name}

    @app.post("/session/connect")
    async def connect(request: Request):
        _, err = await body_of(request)
        if err:
            return err
        if login is None:
            return JSONResponse({"error": "no Groww login configured on the order server"}, status_code=500)
        async with lock:
            try:
                token = await asyncio.to_thread(login)
                broker.connect(token)
            except Exception as e:  # noqa: BLE001
                return JSONResponse({"error": f"Groww login failed: {str(e)[:200]}. Approve today's API access on "
                                              "Groww's API Keys page, then try again."}, status_code=502)
        log("[orders] Groww connected for today")
        return {"connected": True}

    @app.post("/session/disconnect")
    async def disconnect(request: Request):
        _, err = await body_of(request)
        if err:
            return err
        broker.disconnect()
        log("[orders] Groww session deleted")
        return {"connected": False}

    @app.post("/broker/{op}")
    async def op(request: Request, op: str):
        body, err = await body_of(request)
        if err:
            return err
        if op not in OPS:
            return JSONResponse({"error": "unknown op"}, status_code=404)
        try:
            async with lock:
                if op == "place":
                    o = await asyncio.to_thread(broker.place, **{k: body.get(k) for k in
                                                                ("ref", "instrument", "side", "qty", "kind", "price", "trigger")})
                    return {"order": o.to_json()}
                if op in ("cancel", "get"):
                    o = await asyncio.to_thread(getattr(broker, op), body["order_id"])
                    return {"order": o.to_json()}
                if op == "modify":
                    o = await asyncio.to_thread(broker.modify, body["order_id"], price=body.get("price"),
                                                trigger=body.get("trigger"))
                    return {"order": o.to_json()}
                if op == "position":
                    return {"qty": await asyncio.to_thread(broker.position, body["instrument"])}
                if op == "funds":
                    return {"funds": await asyncio.to_thread(broker.funds)}
                if op == "quote":
                    return {"quote": await asyncio.to_thread(broker.quote, body["instrument"])}
                if op == "supports":
                    return {"ok": broker.supports(body["instrument"])}
                ok, why = broker.matches(body["instrument"], body.get("call") or {})
                return {"ok": ok, "why": why}
        except BrokerError as e:
            return JSONResponse({"error": str(e)[:300], "retry": True}, status_code=503)
        except KeyError as e:
            return JSONResponse({"error": f"missing field {e}"}, status_code=400)

    return app


def groww_login_from_env(env: dict):
    """login() for the server: Groww API key + secret (needs the daily approval on Groww's API Keys page)."""
    def login() -> str:
        from growwapi import GrowwAPI
        key, sec = env.get("GROWW_API_KEY"), env.get("GROWW_API_SECRET")
        if not key or not sec:
            raise RuntimeError("GROWW_API_KEY / GROWW_API_SECRET are not set on the order server")
        r = GrowwAPI.get_access_token(api_key=key, secret=sec)
        tok = r if isinstance(r, str) else (r.get("token") or r.get("access_token") or (r.get("payload") or {}).get("token"))
        if not tok:
            raise RuntimeError(f"no token in Groww's reply (fields: {sorted(r) if isinstance(r, dict) else type(r)})")
        return tok
    return login
