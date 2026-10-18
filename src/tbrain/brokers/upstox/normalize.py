"""Decoded Upstox V3 feed (protobuf -> MessageToDict) -> flat rows.

Field names verified against MarketDataFeedV3_pb2 in upstox-python-sdk 2.30.0:
FeedResponse{type, feeds{key: Feed}, currentTs, marketInfo}
Feed = ltpc | fullFeed{marketFF | indexFF} | firstLevelWithGreeks
MarketFullFeed{ltpc{ltp,ltt,ltq,cp}, marketLevel{bidAskQuote[{bidQ,bidP,askQ,askP}]}, optionGreeks{delta,theta,gamma,vega,rho},
               marketOHLC, atp, vtt, oi, iv, tbq, tsq}
Proto3/MessageToDict omits zero-valued fields and renders int64 as strings -> coerced here; missing = None.
IMPORTANT (seen live 2026-10-19): `type` is an enum whose zero value is `initial_feed`, so the snapshot message
arrives with NO `type` key at all. Missing type + `feeds` present => initial_feed.
marketOHLC carries '1d' and 'I1' (1-minute) bars; the I1 bar is kept. `rp` is stored raw (meaning not documented in
the sources read; do not interpret until confirmed).
"""
from __future__ import annotations

from typing import Any

DEPTH = 5
_NUM = ("ltp", "cp", "atp", "oi", "iv", "tbq", "tsq", "delta", "gamma", "theta", "vega", "rho")
_INT = ("ltt_ms", "ltq", "vtt", "cur_ts_ms")


def _f(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        f = _f(v)
        return int(f) if f is not None else None


def empty_row() -> dict:
    row = {"recv_ns": None, "cur_ts_ms": None, "instrument_key": None, "src": None,
           "ltp": None, "ltt_ms": None, "ltq": None, "cp": None, "atp": None, "vtt": None,
           "oi": None, "iv": None, "tbq": None, "tsq": None,
           "delta": None, "gamma": None, "theta": None, "vega": None, "rho": None, "rp": None,
           "i1_ts_ms": None, "i1_open": None, "i1_high": None, "i1_low": None, "i1_close": None, "i1_vol": None}
    for n in range(1, DEPTH + 1):
        row.update({f"bp{n}": None, f"bq{n}": None, f"ap{n}": None, f"aq{n}": None})
    return row


def _fill_ltpc(row: dict, l: dict | None) -> None:
    if not l:
        return
    row["ltp"], row["cp"] = _f(l.get("ltp")), _f(l.get("cp"))
    row["ltt_ms"], row["ltq"] = _i(l.get("ltt")), _i(l.get("ltq"))


def _fill_greeks(row: dict, g: dict | None) -> None:
    if g:
        for k in ("delta", "gamma", "theta", "vega", "rho"):
            row[k] = _f(g.get(k))


def _fill_depth(row: dict, quotes: list | None) -> None:
    for n, q in enumerate((quotes or [])[:DEPTH], start=1):
        row[f"bp{n}"], row[f"bq{n}"] = _f(q.get("bidP")), _i(q.get("bidQ"))
        row[f"ap{n}"], row[f"aq{n}"] = _f(q.get("askP")), _i(q.get("askQ"))


def _fill_ohlc(row: dict, m: dict) -> None:
    for b in ((m.get("marketOHLC") or {}).get("ohlc") or []):
        if b.get("interval") == "I1":
            row["i1_ts_ms"], row["i1_vol"] = _i(b.get("ts")), _i(b.get("vol"))
            for k in ("open", "high", "low", "close"):
                row[f"i1_{k}"] = _f(b.get(k))


def _fill_extras(row: dict, m: dict) -> None:
    for k in ("atp", "oi", "iv", "tbq", "tsq", "rp"):
        row[k] = _f(m.get(k))
    row["vtt"] = _i(m.get("vtt"))


def normalize(msg: dict, recv_ns: int) -> list[dict]:
    mtype = msg.get("type", "initial_feed" if "feeds" in msg else None)
    if mtype not in ("live_feed", "initial_feed"):
        return []
    cur = _i(msg.get("currentTs"))
    rows = []
    for key, feed in (msg.get("feeds") or {}).items():
        row = empty_row()
        row.update(recv_ns=recv_ns, cur_ts_ms=cur, instrument_key=key)
        full = feed.get("fullFeed") or {}
        if "marketFF" in full:
            m = full["marketFF"]
            row["src"] = "market"
            _fill_ltpc(row, m.get("ltpc"))
            _fill_depth(row, (m.get("marketLevel") or {}).get("bidAskQuote"))
            _fill_greeks(row, m.get("optionGreeks"))
            _fill_extras(row, m)
            _fill_ohlc(row, m)
        elif "indexFF" in full:
            row["src"] = "index"
            _fill_ltpc(row, full["indexFF"].get("ltpc"))
            _fill_ohlc(row, full["indexFF"])
        elif "firstLevelWithGreeks" in feed:
            m = feed["firstLevelWithGreeks"]
            row["src"] = "first_level"
            _fill_ltpc(row, m.get("ltpc"))
            fd = m.get("firstDepth")
            _fill_depth(row, [fd] if fd else [])
            _fill_greeks(row, m.get("optionGreeks"))
            _fill_extras(row, m)
        elif "ltpc" in feed:
            row["src"] = "ltpc"
            _fill_ltpc(row, feed["ltpc"])
        else:
            row["src"] = "unknown"
        rows.append(row)
    return rows


def message_type(msg: dict) -> str | None:
    return msg.get("type", "initial_feed" if "feeds" in msg else None)


def parse_market_info(msg: dict) -> dict[str, str]:
    if msg.get("type") != "market_info":
        return {}
    return dict(((msg.get("marketInfo") or {}).get("segmentStatus") or {}))
