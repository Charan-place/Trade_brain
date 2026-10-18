"""Upstox instrument master -> option/future contracts for the configured underlyings (NIFTY, SENSEX).

Field names follow Upstox's Instruments doc: instrument_key, trading_symbol, underlying_symbol,
underlying_key, strike_price, expiry (epoch ms), instrument_type (CE/PE/FUT), segment, lot_size,
tick_size, weekly.
"""
from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Iterable

from tbrain.brokers.upstox.http import USER_AGENT
from tbrain.timeutil import epoch_ms_to_ist_date


@dataclass(frozen=True)
class Contract:
    instrument_key: str
    trading_symbol: str
    underlying: str
    underlying_key: str
    segment: str
    kind: str  # CE / PE / FUT
    strike: float | None
    expiry: date
    lot_size: int | None
    tick_size: float | None
    weekly: bool | None

    def to_json(self) -> dict:
        d = asdict(self)
        d["expiry"] = self.expiry.isoformat()
        return d

    @staticmethod
    def from_json(d: dict) -> "Contract":
        d = dict(d)
        d["expiry"] = date.fromisoformat(d["expiry"])
        return Contract(**d)


def parse_rows(rows: Iterable[dict], underlyings: Iterable[tuple[str, str]]) -> list[Contract]:
    wanted = {(s.upper(), seg.upper()) for s, seg in underlyings}
    out: list[Contract] = []
    for r in rows:
        seg = str(r.get("segment", "")).upper()
        sym = str(r.get("underlying_symbol", "")).upper()
        kind = str(r.get("instrument_type", "")).upper()
        if (sym, seg) not in wanted or kind not in ("CE", "PE", "FUT"):
            continue
        exp = r.get("expiry")
        if exp is None:
            continue
        strike = r.get("strike_price")
        out.append(Contract(
            instrument_key=r["instrument_key"],
            trading_symbol=r.get("trading_symbol", ""),
            underlying=sym,
            underlying_key=r.get("underlying_key", ""),
            segment=seg,
            kind=kind,
            strike=float(strike) if strike not in (None, "") and kind != "FUT" else None,
            expiry=epoch_ms_to_ist_date(float(exp)),
            lot_size=int(r["lot_size"]) if r.get("lot_size") not in (None, "") else None,
            tick_size=float(r["tick_size"]) if r.get("tick_size") not in (None, "") else None,
            weekly=r.get("weekly"),
        ))
    return out


def _download(url: str, timeout: int = 60) -> list[dict]:
    req = urllib.request.Request(url, headers={"Accept": "*/*", "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        blob = resp.read()
    if blob[:2] == b"\x1f\x8b":
        blob = gzip.decompress(blob)
    data = json.loads(blob)
    if not isinstance(data, list):
        raise ValueError(f"unexpected instrument file shape from {url}: {type(data).__name__}")
    return data


def load_contracts(urls: Iterable[str], underlyings: tuple[tuple[str, str], ...], cache_dir: Path,
                   today: date, force: bool = False, log=print) -> list[Contract]:
    """Daily cache of the FILTERED contracts only (the raw master file is large and not needed)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"contracts_{today.isoformat()}.json"
    if cache.exists() and not force:
        return [Contract.from_json(d) for d in json.loads(cache.read_text())]
    contracts: dict[str, Contract] = {}
    errors = []
    need = {s for s, _ in underlyings}
    for url in urls:
        try:
            rows = _download(url)
        except (urllib.error.URLError, ValueError, OSError) as e:
            errors.append(f"{url} -> {e}")
            continue
        for c in parse_rows(rows, underlyings):
            contracts[c.instrument_key] = c
        found = {c.underlying for c in contracts.values()}
        log(f"instruments: {url.rsplit('/', 1)[-1]} ok, have {sorted(found)}")
        if need <= found:
            break
    if not contracts:
        raise RuntimeError("Could not load instruments from any URL:\n  " + "\n  ".join(errors) +
                           "\nCheck the Upstox 'Instruments' docs for the current file URLs and set TBRAIN_INSTRUMENT_URLS.")
    missing = need - {c.underlying for c in contracts.values()}
    if missing:
        log(f"WARNING: no contracts found for {sorted(missing)} (check underlying_symbol/segment)")
    out = sorted(contracts.values(), key=lambda c: (c.underlying, c.expiry, c.kind, c.strike or 0))
    cache.write_text(json.dumps([c.to_json() for c in out]))
    return out


def index_key_for(contracts: list[Contract], underlying: str) -> str | None:
    keys = {c.underlying_key for c in contracts if c.underlying == underlying and c.underlying_key}
    return sorted(keys)[0] if len(keys) >= 1 else None


def nearest_future(contracts: list[Contract], underlying: str, today: date) -> Contract | None:
    futs = [c for c in contracts if c.underlying == underlying and c.kind == "FUT" and c.expiry >= today]
    return min(futs, key=lambda c: c.expiry) if futs else None


def option_expiries(contracts: list[Contract], underlying: str, today: date) -> list[date]:
    return sorted({c.expiry for c in contracts if c.underlying == underlying and c.kind in ("CE", "PE") and c.expiry >= today})


def select_chain(contracts: list[Contract], underlying: str, spot: float, today: date,
                 n_expiries: int, n_strikes: int) -> list[Contract]:
    """ATM +/- n_strikes (by strike index, so the step comes from the data, not a hardcoded 50/100)."""
    chosen: list[Contract] = []
    for exp in option_expiries(contracts, underlying, today)[:n_expiries]:
        legs = [c for c in contracts if c.underlying == underlying and c.expiry == exp and c.kind in ("CE", "PE")]
        strikes = sorted({c.strike for c in legs if c.strike is not None})
        if not strikes:
            continue
        atm = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
        keep = set(strikes[max(0, atm - n_strikes): atm + n_strikes + 1])
        chosen.extend(c for c in legs if c.strike in keep)
    return chosen
