from tbrain.brokers.upstox import instruments as ins

UNDS = (("NIFTY", "NSE_FO"), ("SENSEX", "BSE_FO"))


def test_parse_filters_only_nifty_and_sensex(rows):
    cs = ins.parse_rows(rows, UNDS)
    assert {c.underlying for c in cs} == {"NIFTY", "SENSEX"}
    assert all(c.segment == ("NSE_FO" if c.underlying == "NIFTY" else "BSE_FO") for c in cs)
    assert not any(c.instrument_key in ("NSE_FO|999", "NSE_EQ|X") for c in cs)


def test_expiry_epoch_ms_maps_to_ist_date(rows, today):
    cs = ins.parse_rows(rows, UNDS)
    assert ins.option_expiries(cs, "NIFTY", today)[0].isoformat() == "2026-10-20"


def test_select_chain_atm_window_uses_data_step(rows, today):
    cs = ins.parse_rows(rows, UNDS)
    legs = ins.select_chain(cs, "NIFTY", 25012.0, today, n_expiries=1, n_strikes=2)
    assert sorted({c.strike for c in legs}) == [24900, 24950, 25000, 25050, 25100]
    assert len(legs) == 10 and not any(c.instrument_key == "NSE_FO|OLD" for c in legs)
    s = ins.select_chain(cs, "SENSEX", 82049.0, today, n_expiries=2, n_strikes=1)
    assert sorted({c.strike for c in s}) == [81900, 82000, 82100] and len(s) == 12


def test_index_key_and_future(rows, today):
    cs = ins.parse_rows(rows, UNDS)
    assert ins.index_key_for(cs, "NIFTY") == "NSE_INDEX|Nifty 50"
    assert ins.index_key_for(cs, "SENSEX") == "BSE_INDEX|SENSEX"
    assert ins.nearest_future(cs, "SENSEX", today).kind == "FUT"


def test_load_contracts_download_and_cache(tmp_path, gz_url, today):
    logs = []
    cs = ins.load_contracts(["file:///nope.json.gz", gz_url], UNDS, tmp_path / "c", today, log=logs.append)
    assert len(cs) > 100 and (tmp_path / "c" / f"contracts_{today}.json").exists()
    again = ins.load_contracts(["file:///nope.json.gz"], UNDS, tmp_path / "c", today)  # served from cache
    assert again == cs
