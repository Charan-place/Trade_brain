"""Settings from .env (keys are matched case-insensitively) + process environment.

Secrets are never printed: Settings.__repr__ masks them.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# NOTE: these two URLs come from memory of Upstox's public docs layout and could NOT be verified from the
# build sandbox (egress blocked). `tbrain instruments` tries them in order and reports clearly if both fail;
# override with TBRAIN_INSTRUMENT_URLS (comma separated) if Upstox's docs list different ones.
DEFAULT_INSTRUMENT_URLS = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz",
    "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz",
    "https://assets.upstox.com/market-quote/instruments/exchange/BSE.json.gz",
)

# (underlying_symbol in the instrument file, derivatives segment). Index keys are read from the
# instrument file's `underlying_key` field, not hardcoded. Override: TBRAIN_UNDERLYINGS=NIFTY:NSE_FO,SENSEX:BSE_FO
DEFAULT_UNDERLYINGS = (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO"))


def _parse_underlyings(raw: str) -> tuple[tuple[str, str], ...]:
    out = []
    for part in raw.split(","):
        if ":" in part:
            sym, seg = part.split(":", 1)
            out.append((sym.strip().upper(), seg.strip().upper()))
    return tuple(out)


def load_env(path: Path | None = None) -> dict[str, str]:
    path = path or (ROOT / ".env")
    env: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip().upper()] = v.strip().strip('"').strip("'")
    for k, v in os.environ.items():
        if k.upper().startswith(("UPSTOX_", "TBRAIN_")):
            env[k.upper()] = v
    return env


def _int(env: dict[str, str], key: str, default: int) -> int:
    try:
        return int(env.get(key, default))
    except ValueError:
        return default


@dataclass
class Settings:
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    redirect_uri: str = "http://localhost:5000/upstox_redirect"
    data_dir: Path = ROOT / "data"
    secrets_dir: Path = ROOT / ".secrets"
    instrument_urls: tuple[str, ...] = DEFAULT_INSTRUMENT_URLS
    underlyings: tuple[tuple[str, str], ...] = DEFAULT_UNDERLYINGS
    vix_key: str = "NSE_INDEX|India VIX"  # key format unverified; `tbrain probe` shows whether it ticks
    n_strikes: int = 10
    n_expiries: int = 2
    chain_poll_s: int = 60
    flush_s: int = 30
    stale_s: int = 30
    max_keys: int = 1000  # well under Upstox 'full' limits (docs: 2,000 individual / 1,500 combined)
    tls_verify: bool = True
    # Risk-free rate for Black-76 discounting. PLACEHOLDER (not sourced) - for short-dated options its effect on IV
    # is tiny because the forward comes from put-call parity. Set TBRAIN_RISK_FREE to a current T-bill yield.
    risk_free: float = 0.065
    # Signal engine (signal-only). Costs are a PLACEHOLDER estimate: set your broker's real round-trip cost per lot.
    signal_min_score: int = 5
    cost_per_lot_inr: float = 40.0
    risk_per_trade_inr: float = 0.0

    @classmethod
    def load(cls) -> "Settings":
        e = load_env()
        urls = tuple(u.strip() for u in e.get("TBRAIN_INSTRUMENT_URLS", "").split(",") if u.strip())
        return cls(
            api_key=e.get("UPSTOX_API_KEY", ""),
            api_secret=e.get("UPSTOX_API_SECRET", ""),
            redirect_uri=e.get("UPSTOX_REDIRECT_URI", cls.redirect_uri),
            instrument_urls=urls or DEFAULT_INSTRUMENT_URLS,
            underlyings=_parse_underlyings(e.get("TBRAIN_UNDERLYINGS", "")) or DEFAULT_UNDERLYINGS,
            n_strikes=_int(e, "TBRAIN_N_STRIKES", 10),
            n_expiries=_int(e, "TBRAIN_N_EXPIRIES", 2),
            chain_poll_s=_int(e, "TBRAIN_CHAIN_POLL_S", 60),
            flush_s=_int(e, "TBRAIN_FLUSH_S", 30),
            stale_s=_int(e, "TBRAIN_STALE_S", 30),
            tls_verify=e.get("TBRAIN_TLS_VERIFY", "1") not in ("0", "false", "False"),
            risk_free=float(e.get("TBRAIN_RISK_FREE", 0.065)),
            signal_min_score=_int(e, "TBRAIN_SIGNAL_MIN_SCORE", 5),
            cost_per_lot_inr=float(e.get("TBRAIN_COST_PER_LOT", 40.0)),
            risk_per_trade_inr=float(e.get("TBRAIN_RISK_PER_TRADE", 0.0)),
        )

    def require_app_credentials(self) -> None:
        missing = [n for n, v in (("UPSTOX_API_KEY", self.api_key), ("UPSTOX_API_SECRET", self.api_secret)) if not v]
        if missing:
            raise RuntimeError(f"Missing in .env: {', '.join(missing)}")
