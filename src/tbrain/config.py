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


ENV_PREFIXES = ("UPSTOX_", "TBRAIN_", "MONGODB_", "GOOGLE_")


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
        if k.upper().startswith(ENV_PREFIXES):
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
    require_vwap: bool = True
    trend_k: float | None = 1.5
    loss_pause_n: int = 0
    regime_gate: bool = False
    premium_stop: bool = False
    fut_oi_fresh: bool = False
    # v2.2 session rules (defaults; the dashboard switches override allow_afternoon / dd_stop at runtime)
    afternoon_cutoff: str = "13:00"     # "off" = no cutoff
    allow_afternoon: bool = False
    dd_stop: bool = True
    dd_peak_r: float = 2.0
    dd_giveback_r: float = 2.0
    # Storage (optional): MongoDB keeps calls, shadows, minute features and sessions for training. Empty = off.
    mongodb_uri: str = field(default="", repr=False)
    mongodb_db: str = "tbrain"
    # Hosted web app (optional): Google sign-in + per-user Upstox connect.
    public_url: str = ""
    google_client_id: str = field(default="", repr=False)
    google_client_secret: str = field(default="", repr=False)
    allowed_emails: tuple[str, ...] = ()
    owner_email: str = ""
    session_secret: str = field(default="", repr=False)
    encryption_key: str = field(default="", repr=False)

    def cutoff_time(self):
        """afternoon_cutoff as a datetime.time, or None when switched off."""
        from datetime import time as dtime
        v = (self.afternoon_cutoff or "").strip().lower()
        if v in ("", "off", "none"):
            return None
        h, m = v.split(":")
        return dtime(int(h), int(m))

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
            require_vwap=e.get("TBRAIN_REQUIRE_VWAP", "1") not in ("0", "false", "False", "off"),
            trend_k=None if e.get("TBRAIN_TREND_K", "1.5").lower() in ("off", "none", "") else float(e.get("TBRAIN_TREND_K", "1.5")),
            loss_pause_n=_int(e, "TBRAIN_LOSS_PAUSE_N", 0),
            regime_gate=e.get("TBRAIN_REGIME_GATE", "0") in ("1", "true", "True", "on"),
            premium_stop=e.get("TBRAIN_PREMIUM_STOP", "0") in ("1", "true", "True", "on"),
            fut_oi_fresh=e.get("TBRAIN_FUT_OI_FRESH", "0") in ("1", "true", "True", "on"),
            afternoon_cutoff=e.get("TBRAIN_AFTERNOON_CUTOFF", "13:00").strip() or "13:00",
            allow_afternoon=e.get("TBRAIN_ALLOW_AFTERNOON", "0") in ("1", "true", "True", "on"),
            dd_stop=e.get("TBRAIN_DD_STOP", "1") not in ("0", "false", "False", "off"),
            dd_peak_r=float(e.get("TBRAIN_DD_PEAK_R", 2.0)),
            dd_giveback_r=float(e.get("TBRAIN_DD_GIVEBACK_R", 2.0)),
            data_dir=Path(e["TBRAIN_DATA_DIR"]) if e.get("TBRAIN_DATA_DIR") else cls.data_dir,
            secrets_dir=Path(e["TBRAIN_SECRETS_DIR"]) if e.get("TBRAIN_SECRETS_DIR") else cls.secrets_dir,
            mongodb_uri=e.get("MONGODB_URI", ""),
            mongodb_db=e.get("MONGODB_DB", "tbrain") or "tbrain",
            public_url=e.get("TBRAIN_PUBLIC_URL", "").rstrip("/"),
            google_client_id=e.get("GOOGLE_CLIENT_ID", ""),
            google_client_secret=e.get("GOOGLE_CLIENT_SECRET", ""),
            allowed_emails=tuple(x.strip().lower() for x in e.get("TBRAIN_ALLOWED_EMAILS", "").split(",") if x.strip()),
            owner_email=(e.get("TBRAIN_OWNER_EMAIL", "") or e.get("TBRAIN_ALLOWED_EMAILS", "").split(",")[0]).strip().lower(),
            session_secret=e.get("TBRAIN_SESSION_SECRET", ""),
            encryption_key=e.get("TBRAIN_ENCRYPTION_KEY", ""),
        )

    def require_app_credentials(self) -> None:
        missing = [n for n, v in (("UPSTOX_API_KEY", self.api_key), ("UPSTOX_API_SECRET", self.api_secret)) if not v]
        if missing:
            raise RuntimeError(f"Missing in .env: {', '.join(missing)}")
