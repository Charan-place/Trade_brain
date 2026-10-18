"""tbrain CLI:  login | check | instruments | probe | record | report | ui"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter

from tbrain.config import Settings
from tbrain.timeutil import ist_date, now_ist


def _token(s: Settings) -> str:
    from tbrain.brokers.upstox.auth import get_token
    return get_token(s)


def cmd_login(s: Settings, a) -> int:
    from tbrain.brokers.upstox.auth import login
    login(s, manual=a.manual, open_browser=not a.no_browser)
    return cmd_check(s, a)


def cmd_check(s: Settings, a) -> int:
    from tbrain.brokers.upstox.auth import load_token, token_is_live
    t = load_token(s)
    if not t:
        print("No token. Run: uv run tbrain login"); return 1
    live = token_is_live(t["access_token"])
    print(f"token obtained {t.get('obtained_at')} -> " + {True: "VALID", False: "EXPIRED/REJECTED (run login)", None: "could not verify (network?)"}[live])
    return 0 if live else 1


def cmd_instruments(s: Settings, a) -> int:
    from tbrain.brokers.upstox import instruments as ins
    today = ist_date()
    cs = ins.load_contracts(s.instrument_urls, s.underlyings, s.data_dir / "instruments", today, force=a.refresh)
    for und, _seg in s.underlyings:
        mine = [c for c in cs if c.underlying == und]
        exps = ins.option_expiries(cs, und, today)
        fut = ins.nearest_future(cs, und, today)
        lots = Counter(c.lot_size for c in mine if c.kind in ("CE", "PE"))
        print(f"\n{und}: {len(mine)} contracts | index key: {ins.index_key_for(cs, und)}")
        print(f"  nearest future: {fut.trading_symbol if fut else None}")
        print(f"  next option expiries: {[e.isoformat() for e in exps[:4]]}")
        print(f"  lot sizes seen: {dict(lots)}")
    return 0


def cmd_probe(s: Settings, a) -> int:
    from tbrain.collector.recorder import Recorder
    r = Recorder(s, lambda: _token(s), write=False, capture_raw=a.raw, n_strikes=1, n_expiries=1)
    m = r.run(duration_s=a.seconds, respect_hours=False)
    out = s.data_dir / "probe" / f"probe_{now_ist():%Y%m%d_%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    from tbrain.brokers.upstox.normalize import message_type
    types = Counter(message_type(x) for x in r.raw)
    out.write_text(json.dumps({"manifest": m, "raw_types": types, "raw_samples": r.raw}, indent=1, default=str))
    print(f"\nprobe: {m['health']} | raw message types: {dict(types)}\nsaved -> {out}")
    return 0 if m["health"]["rows"] > 0 else 2


def cmd_record(s: Settings, a) -> int:
    from tbrain.collector.recorder import Recorder
    m = Recorder(s, lambda: _token(s), ui_port=None if a.no_ui else a.ui_port,
                 open_browser=not a.no_browser).run(duration_s=a.seconds, respect_hours=not a.now)
    print(json.dumps({k: m[k] for k in ("started", "ended", "subscribed", "health", "rows_written", "chain_rows_written")}, indent=1))
    return 0


def cmd_report(s: Settings, a) -> int:
    import pyarrow.compute as pc
    import pyarrow.dataset as ds
    d = a.date or ist_date().isoformat()
    path = s.data_dir / "ticks" / f"date={d}"
    if not path.exists():
        print(f"no data for {d}"); return 1
    t = ds.dataset(str(path), format="parquet").to_table(columns=["recv_ns", "instrument_key"])
    print(f"{d}: {t.num_rows} rows, {len(pc.unique(t['instrument_key']))} instruments")
    ts = t["recv_ns"].to_numpy()
    ts.sort()
    import numpy as np
    gaps = np.diff(ts) / 1e9
    print(f"span {(ts[-1]-ts[0])/3.6e12:.2f} h | gaps>10s: {(gaps > 10).sum()} | max gap {gaps.max() if len(gaps) else 0:.1f}s")
    vc = pc.value_counts(t["instrument_key"]).to_pylist()
    vc.sort(key=lambda x: -x["counts"])
    for x in vc[: a.top]:
        print(f"  {x['values']:<28} {x['counts']}")
    return 0


def cmd_ui(s: Settings, a) -> int:
    from tbrain.ui.server import serve
    serve(s, port=a.port, force_demo=a.demo, open_browser=not a.no_browser)
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="tbrain", description="Nifty + Sensex options brain (data layer)")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("login", help="daily Upstox login (opens browser)")
    x.add_argument("--manual", action="store_true", help="paste redirected URL instead of local listener")
    x.add_argument("--no-browser", action="store_true")
    sub.add_parser("check", help="is today's token valid?")
    x = sub.add_parser("instruments", help="download/inspect NIFTY + SENSEX contracts")
    x.add_argument("--refresh", action="store_true")
    x = sub.add_parser("probe", help="short live test; saves raw decoded samples for schema check")
    x.add_argument("--seconds", type=float, default=20)
    x.add_argument("--raw", type=int, default=12)
    x = sub.add_parser("record", help="record a session (waits for 09:00 IST, stops 15:35 IST)")
    x.add_argument("--now", action="store_true", help="ignore market hours (start immediately)")
    x.add_argument("--seconds", type=float, default=None, help="stop after N seconds")
    x.add_argument("--ui-port", type=int, default=8765, help="embedded brain UI port (push updates every 250 ms)")
    x.add_argument("--no-ui", action="store_true", help="record without the embedded UI")
    x.add_argument("--no-browser", action="store_true", help="don't auto-open the UI")
    x = sub.add_parser("report", help="coverage/gap report for a recorded day")
    x.add_argument("--date", default=None)
    x.add_argument("--top", type=int, default=15)
    x = sub.add_parser("ui", help="open the brain UI (live while `record` runs; replays the last probe otherwise)")
    x.add_argument("--port", type=int, default=8765)
    x.add_argument("--demo", action="store_true", help="force replay of the latest probe")
    x.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    s = Settings.load()
    try:
        return {"login": cmd_login, "check": cmd_check, "instruments": cmd_instruments, "probe": cmd_probe,
                "record": cmd_record, "report": cmd_report, "ui": cmd_ui}[a.cmd](s, a)
    except Exception as e:  # concise errors; no secrets are ever in messages
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
