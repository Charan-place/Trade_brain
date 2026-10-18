"""Small REST helpers. Put/Call option chain: GET /v2/option/chain?instrument_key=<underlying>&expiry_date=YYYY-MM-DD
(endpoint path per Upstox community/support reply; params + fields per the Put/Call Option Chain doc)."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from datetime import date

from tbrain.brokers.upstox.http import headers

OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"


class RestError(RuntimeError):
    pass


def get_json(url: str, token: str, params: dict | None = None, timeout: int = 20) -> dict:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers=headers(token))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise RestError(f"HTTP {e.code} {url.split('?')[0]}: {e.read().decode(errors='replace')[:300]}") from None


def _md(side: dict | None) -> dict:
    side = side or {}
    m, g = side.get("market_data") or {}, side.get("option_greeks") or {}
    return {"instrument_key": side.get("instrument_key"),
            **{k: m.get(k) for k in ("ltp", "close_price", "volume", "oi", "prev_oi", "bid_price", "bid_qty", "ask_price", "ask_qty")},
            **{k: g.get(k) for k in ("delta", "gamma", "theta", "vega", "iv", "pop")}}


def flatten_chain(payload: dict, underlying: str, recv_ns: int) -> list[dict]:
    rows = []
    for e in payload.get("data") or []:
        base = {"recv_ns": recv_ns, "underlying": underlying, "expiry": str(e.get("expiry")),
                "strike": e.get("strike_price"), "underlying_spot": e.get("underlying_spot_price"), "pcr": e.get("pcr")}
        for side, key in (("CE", "call_options"), ("PE", "put_options")):
            if e.get(key):
                rows.append({**base, "side": side, **_md(e[key])})
    return rows


def fetch_option_chain(token: str, underlying_key: str, expiry: date) -> dict:
    return get_json(OPTION_CHAIN_URL, token, {"instrument_key": underlying_key, "expiry_date": expiry.isoformat()})
