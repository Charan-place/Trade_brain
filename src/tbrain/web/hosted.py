"""One trading day of the brain in the hosted (Modal) deployment. Kept free of Modal imports so it is testable."""
from __future__ import annotations

import time

from tbrain.timeutil import MARKET_CLOSE, at_ist, now_ist

FRESH_S = 60  # a state newer than this means another recorder is already running


def run_hosted_day(s, state_dict, log=print, recorder_cls=None, tokens=None, now=None) -> str:
    """Start today's recorder for the owner, unless it should not run. Returns why it stopped."""
    now = now or now_ist()
    if now.weekday() >= 5:
        return "weekend"
    if now >= at_ist(now.date(), MARKET_CLOSE):
        return "market-closed"
    rec = None
    try:
        rec = state_dict.get("state")
    except Exception:  # noqa: BLE001
        pass
    if rec and time.time() - float(rec.get("ts") or 0) < FRESH_S:
        log("[hosted] another recorder is already running - exiting")
        return "already-running"
    if not s.owner_email:
        raise RuntimeError("TBRAIN_OWNER_EMAIL / TBRAIN_ALLOWED_EMAILS is not set")
    if tokens is None:
        from tbrain.storage.mongo import connect
        from tbrain.web.tokens import TokenStore
        tokens = TokenStore(connect(s.mongodb_uri, s.mongodb_db), s.encryption_key)
    if not tokens.load(s.owner_email):
        log("[hosted] Upstox is not connected today - open the app and click 'Connect Upstox'")
        return "no-token"
    from tbrain.web.state import make_dict_publisher
    if recorder_cls is None:
        from tbrain.collector.recorder import Recorder as recorder_cls
    from tbrain.runtime_settings import DictSettings
    r = recorder_cls(s, lambda: tokens.load(s.owner_email) or "", write=True, ui_port=None,
                     publish=make_dict_publisher(state_dict, log=log), log=log, settings_store=DictSettings(state_dict))
    r.run()
    return "done"
