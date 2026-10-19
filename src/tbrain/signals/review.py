"""`tbrain review`: post-mortem of a day's calls from data/signals/calls.jsonl (what worked, what leaked)."""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tbrain.timeutil import IST


def load_calls(path: Path) -> list[dict]:
    calls: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
                calls[rec["call"]["id"]] = rec["call"]
            except (json.JSONDecodeError, KeyError):
                continue
    return sorted(calls.values(), key=lambda c: c["issued_ms"])


def _day(c: dict) -> str:
    return datetime.fromtimestamp(c["issued_ms"] / 1000, IST).date().isoformat()


def _line(label: str, cs: list[dict]) -> str:
    closed = [c for c in cs if c["status"] == "closed"]
    if not cs:
        return f"  {label:<22} —"
    w = sum(c["pnl_per_lot"] > 0 for c in closed)
    return (f"  {label:<22} n={len(cs):<3} W/L {w}/{len(closed) - w:<3} "
            f"sumR {sum(c['r_now'] for c in cs):+6.2f}  P&L/lot {sum(c['pnl_per_lot'] for c in cs):+9,.0f}")


def review(path: Path, day: str | None = None) -> str:
    calls = load_calls(path)
    shadows = load_calls(path.with_name("shadow.jsonl"))
    if day:
        shadows = [c for c in shadows if _day(c) == day]
    if day:
        calls = [c for c in calls if _day(c) == day]
    if not calls:
        return f"No calls{' on ' + day if day else ''}."
    out = [f"Calls {day or 'all days'}: {len(calls)}", _line("ALL", calls)]
    groups: dict[str, dict[str, list]] = {"by direction": defaultdict(list), "by index": defaultdict(list),
                                          "by hour": defaultdict(list), "by score": defaultdict(list),
                                          "by outcome": defaultdict(list)}
    for c in calls:
        groups["by direction"][c["direction"]].append(c)
        groups["by index"][c["und"]].append(c)
        groups["by hour"][datetime.fromtimestamp(c["issued_ms"] / 1000, IST).strftime("%H:00")].append(c)
        groups["by score"][f"{c['score']}/{c['max_score']}"].append(c)
        groups["by outcome"][c.get("outcome") or c["status"]].append(c)
    for title, g in groups.items():
        out.append(title)
        out += [_line(k, v) for k, v in sorted(g.items())]
    sl = [c for c in calls if c.get("outcome") == "SL" and c.get("exit") is not None]
    if sl:
        slip = [c["exit"] - c["p_sl"] for c in sl]
        worst = min(sl, key=lambda c: c["r_now"])
        out.append(f"stop quality: {len(sl)} SL exits, avg exit vs planned SL premium {sum(slip) / len(slip):+.2f}, "
                   f"worst {worst['r_now']:.2f}R ({worst['symbol']}) — beyond -1.2R means the stop under-modelled the option")
    wins = [c for c in calls if c["pnl_per_lot"] > 0 and c.get("mfe")]
    if wins:
        cap = sum(c["pnl_per_lot"] / c["mfe"] for c in wins) / len(wins)
        out.append(f"exit quality: winners kept {cap:.0%} of their best open profit on average")
    corr = [c for c in calls if c.get("correlated_with")]
    if corr:
        out.append(_line("correlated (2nd index)", corr))
    regimes = defaultdict(list)
    for c in calls:
        regimes[(c.get("regime") or {}).get("label", "n/a")].append(c)
    if any(k != "n/a" for k in regimes):
        out.append("by regime (at issue time)")
        out += [_line(k, v) for k, v in sorted(regimes.items())]
    if shadows:
        from tbrain.signals.engine import SignalEngine
        out.append(f"shadow candidates (blocked, tracked silently): {len(shadows)}")
        by = defaultdict(list)
        for sh in shadows:
            for b in {SignalEngine.blocker_key(x) for x in sh.get("blocked_by", [])} or {"other"}:
                by[b].append(sh)
        for b, v in sorted(by.items()):
            sr = sum(x["r_now"] for x in v)
            out.append(_line(b, v) + ("   <- filter SAVED money" if sr < 0 else "   <- filter COST money" if sr > 0 else ""))
    return "\n".join(out)
