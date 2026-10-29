"""Run the order server on the static-IP VM:  tbrain orderserver --cert ... --key ...

Reads its own .env (GROWW_API_KEY, GROWW_API_SECRET, TBRAIN_ORDER_SERVER_TOKEN). Downloads Groww's
instrument list at start (and again each morning when you reconnect) so contracts map by exchange token.
"""
from __future__ import annotations

import csv
import io
import os
import sys

INSTRUMENT_CSV_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"  # GrowwAPI.INSTRUMENT_CSV_URL


def load_groww_rows(url: str = INSTRUMENT_CSV_URL, text: str | None = None) -> list[dict]:
    """Rows of Groww's instrument CSV, NSE/BSE options only (keeps memory small)."""
    if text is None:
        import httpx
        r = httpx.get(url, timeout=60, follow_redirects=True)
        r.raise_for_status()
        text = r.text
    out = []
    for row in csv.DictReader(io.StringIO(text)):
        if str(row.get("exchange", "")).upper() in ("NSE", "BSE") and str(row.get("instrument_type", "")).upper() in ("CE", "PE"):
            out.append(row)
    return out


def build(env: dict | None = None, rows: list[dict] | None = None, log=print):
    """(app, broker) for the order server. Separate from serve() so tests can build it without the network."""
    from tbrain.brokers.groww.broker import GrowwBroker
    from tbrain.orderserver.app import create_order_app, groww_login_from_env
    env = dict(os.environ if env is None else env)
    secret = env.get("TBRAIN_ORDER_SERVER_TOKEN", "")
    if len(secret) < 32:
        raise RuntimeError("TBRAIN_ORDER_SERVER_TOKEN must be set (32+ random characters, same value as on Modal)")

    def api_factory(token):
        from growwapi import GrowwAPI
        return GrowwAPI(token)

    broker = GrowwBroker(api_factory, rows if rows is not None else load_groww_rows(), log=log)
    login = groww_login_from_env(env)
    return create_order_app(broker, secret, login=login, log=log), broker


def serve(host: str, port: int, cert: str, key: str) -> int:
    import uvicorn
    app, broker = build()
    print(f"order server: {len(broker._rows_by_token)} NSE/BSE option contracts loaded; listening on {host}:{port}",
          file=sys.stderr)
    uvicorn.run(app, host=host, port=port, ssl_certfile=cert, ssl_keyfile=key, log_level="warning")
    return 0
