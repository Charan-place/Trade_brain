"""Trade history: every real call (not shadows), grouped by day, with totals. Signal-only: these are the brain's
calls tracked to their outcome at the option bid, not orders placed.

Sources: MongoDB `calls` (hosted / when MONGODB_URI is set) or data/signals/calls.jsonl (local).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from tbrain.timeutil import IST

FIELDS = ("id", "und", "symbol", "direction", "dir", "issued_ms", "exit_ms", "entry", "exit", "p_sl", "p_t1", "p_t2",
          "outcome", "status", "r_now", "pnl_per_lot", "score", "max_score", "lot", "expiry", "strike", "kind", "t1_hit")


def _day(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, IST).date().isoformat() if ms else None


def _hhmm(ms) -> str | None:
    return datetime.fromtimestamp(ms / 1000, IST).strftime("%H:%M") if ms else None


def compact(c: dict) -> dict:
    t = {k: c.get(k) for k in FIELDS}
    t["regime"] = (c.get("regime") or {}).get("label") if isinstance(c.get("regime"), dict) else c.get("regime")
    t["time"], t["exit_time"] = _hhmm(c.get("issued_ms")), _hhmm(c.get("exit_ms"))
    t["day"] = c.get("day") or _day(c.get("issued_ms"))
    return t


def summarize(calls: list[dict], days: int = 60) -> dict:
    by: dict[str, list[dict]] = {}
    for c in calls:
        t = compact(c)
        if t["day"]:
            by.setdefault(t["day"], []).append(t)
    out = []
    for d in sorted(by, reverse=True)[:days]:
        ts = sorted(by[d], key=lambda t: t["issued_ms"] or 0)
        closed = [t for t in ts if t["status"] == "closed" and t["outcome"] != "NO_DATA"]
        wins = sum((t["pnl_per_lot"] or 0) > 0 for t in closed)
        out.append({"day": d, "calls": len(ts), "closed": len(closed), "open": len(ts) - len(closed),
                    "wins": wins, "losses": len(closed) - wins,
                    "win_rate": wins / len(closed) if closed else None,
                    "sum_r": round(sum(t["r_now"] or 0 for t in closed), 2),
                    "pnl_per_lot": round(sum(t["pnl_per_lot"] or 0 for t in closed), 2),
                    "pnl_open_per_lot": round(sum(t["pnl_per_lot"] or 0 for t in ts if t["status"] != "closed"), 2),
                    "trades": ts})
    tot_closed = sum(x["closed"] for x in out)
    tot_wins = sum(x["wins"] for x in out)
    return {"days": out,
            "total": {"days": len(out), "calls": sum(x["calls"] for x in out), "closed": tot_closed, "wins": tot_wins,
                      "losses": tot_closed - tot_wins, "win_rate": tot_wins / tot_closed if tot_closed else None,
                      "sum_r": round(sum(x["sum_r"] for x in out), 2),
                      "pnl_per_lot": round(sum(x["pnl_per_lot"] for x in out), 2),
                      "green_days": sum(x["pnl_per_lot"] > 0 for x in out)}}


def from_files(data_dir: Path, days: int = 60) -> dict:
    p = Path(data_dir) / "signals" / "calls.jsonl"
    last: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                c = json.loads(line)["call"]
                last[c["id"]] = c
            except (json.JSONDecodeError, KeyError):
                continue
    return summarize(list(last.values()), days)


def from_mongo(db, owner: str | None, days: int = 60) -> dict:
    q = {"owner": owner} if owner else {}
    proj = {k: 1 for k in FIELDS + ("regime", "day")}
    calls = list(db.calls.find(q, proj).sort("issued_ms", -1).limit(5000))
    return summarize(calls, days)


# ---------------------------------------------------------------- paper / real books (owner only)
def exec_compact(t: dict) -> dict | None:
    """A paper/real trade in the same shape as a call, so the dashboard table can show it."""
    if not t.get("filled_qty"):
        return None                                   # skipped / missed: never held a position
    entry, exit_ = t.get("entry_price"), t.get("exit_price")
    lots = t.get("lots") or 1
    risk = (entry - t["p_sl"]) if entry and t.get("p_sl") else None
    r = ((exit_ - entry) / risk) if (risk and exit_ is not None and risk > 0) else None
    closed = t.get("state") == "closed"
    return {"id": t.get("call_id"), "und": t.get("und"), "symbol": t.get("symbol"), "dir": t.get("dir"),
            "issued_ms": t.get("created_ms"), "exit_ms": t.get("closed_ms"), "entry": entry, "exit": exit_,
            "status": "closed" if closed else "open", "outcome": (t.get("exit_reason") or t.get("state") or "")[:18],
            "r_now": round(r, 2) if r is not None else None,
            "pnl_per_lot": round(t["pnl"] / lots, 2) if t.get("pnl") is not None else None,
            "lot": t.get("lot_size"), "regime": None}


def summarize_exec(trades: list[dict], days: int = 60) -> dict:
    rows = [x for x in (exec_compact(t) for t in trades) if x]
    out = summarize(rows, days)
    out["skipped"] = sum(1 for t in trades if not t.get("filled_qty"))
    return out


def from_exec_files(data_dir: Path, book: str, days: int = 60) -> dict:
    p = Path(data_dir) / "exec" / f"{book}.jsonl"
    last: dict[str, dict] = {}
    if p.exists():
        for line in p.read_text().splitlines():
            try:
                tr = json.loads(line)["trade"]
                last[tr["id"]] = tr
            except (json.JSONDecodeError, KeyError):
                continue
    return summarize_exec(list(last.values()), days)


def from_mongo_exec(db, owner: str | None, book: str, days: int = 60) -> dict:
    q = {"book": book, **({"owner": owner} if owner else {})}
    return summarize_exec(list(db.exec_trades.find(q).sort("created_ms", -1).limit(5000)), days)
