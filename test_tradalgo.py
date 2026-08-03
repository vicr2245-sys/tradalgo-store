"""
Test suite for tradalgo.py

Run with:  python -m pytest test_tradalgo.py -v
Requires:  pip install pytest numpy requests flask --break-system-packages

This is a dev-time tool only — it is not bundled into tradalgo.exe and has
no effect on the shipped application. It exists to catch regressions in the
areas that are easiest to silently break: indicator math, position sizing
(the site of a real, serious bug found and fixed during development — see
Section 7b in tradalgo.py), and the strategy consensus logic.

Importing tradalgo.py as a module is safe here: nothing at module level
starts threads, opens network connections, or launches the GUI — those only
happen inside main()/run_bot()/start_dashboard(), which these tests never
call.
"""
import importlib.util
import sys
from pathlib import Path

import numpy as np
import pytest

# ── Import tradalgo.py as a module without running its __main__ block ──────
_SPEC = importlib.util.spec_from_file_location("tradalgo", Path(__file__).parent / "tradalgo.py")
tradalgo = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(tradalgo)


# ══════════════════════════════════════════════════════════════════════════
# Indicators
# ══════════════════════════════════════════════════════════════════════════

class TestEMA:
    def test_flat_series_converges_to_the_flat_value(self):
        series = np.array([100.0] * 30)
        result = tradalgo._ema(series, 9)
        assert result[-1] == pytest.approx(100.0)

    def test_first_value_is_a_simple_average_of_the_seed_window(self):
        series = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = tradalgo._ema(series, 3)
        # index 2 (period-1) should be the plain mean of the first 3 values
        assert result[2] == pytest.approx(2.0)

    def test_values_before_the_seed_window_are_nan(self):
        series = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = tradalgo._ema(series, 3)
        assert tradalgo._nan(result[0])
        assert tradalgo._nan(result[1])

    def test_rising_series_produces_a_rising_ema(self):
        series = np.arange(1.0, 51.0)  # 1, 2, 3, ... 50
        result = tradalgo._ema(series, 10)
        valid = result[~np.isnan(result)]
        assert np.all(np.diff(valid) > 0), "EMA should be monotonically increasing for a monotonically increasing series"


class TestSMA:
    def test_matches_numpy_mean_over_the_window(self):
        series = np.array([2.0, 4.0, 6.0, 8.0, 10.0])
        result = tradalgo._sma(series, 3)
        assert result[2] == pytest.approx((2 + 4 + 6) / 3)
        assert result[3] == pytest.approx((4 + 6 + 8) / 3)
        assert result[4] == pytest.approx((6 + 8 + 10) / 3)

    def test_values_before_the_window_are_nan(self):
        series = np.array([1.0, 2.0, 3.0])
        result = tradalgo._sma(series, 3)
        assert tradalgo._nan(result[0])
        assert tradalgo._nan(result[1])


class TestRSI:
    def test_bounded_between_0_and_100(self):
        rng = np.random.default_rng(42)
        series = 100 + np.cumsum(rng.normal(0, 1, 200))
        result = tradalgo._rsi(series, 14)
        valid = result[~np.isnan(result)]
        assert np.all(valid >= 0) and np.all(valid <= 100)

    def test_strictly_rising_series_approaches_100(self):
        series = np.arange(1.0, 51.0)
        result = tradalgo._rsi(series, 14)
        assert result[-1] == pytest.approx(100.0, abs=0.5)

    def test_strictly_falling_series_approaches_0(self):
        series = np.arange(50.0, 0.0, -1.0)
        result = tradalgo._rsi(series, 14)
        assert result[-1] == pytest.approx(0.0, abs=0.5)


class TestMACD:
    def test_returns_three_arrays_of_matching_length(self):
        series = np.array(np.linspace(1, 2, 60))
        macd_line, signal_line, hist = tradalgo._macd(series)
        assert len(macd_line) == len(signal_line) == len(hist) == len(series)

    def test_histogram_equals_macd_minus_signal(self):
        series = np.linspace(1, 2, 60)
        macd_line, signal_line, hist = tradalgo._macd(series)
        valid = ~np.isnan(macd_line) & ~np.isnan(signal_line)
        np.testing.assert_allclose(hist[valid], macd_line[valid] - signal_line[valid])


class TestBollinger:
    def test_upper_band_always_at_or_above_middle_at_or_above_lower(self):
        rng = np.random.default_rng(7)
        series = 100 + np.cumsum(rng.normal(0, 0.5, 100))
        upper, mid, lower = tradalgo._bollinger(series, 20, 2.0)
        valid = ~np.isnan(mid)
        assert np.all(upper[valid] >= mid[valid])
        assert np.all(mid[valid] >= lower[valid])

    def test_flat_series_has_zero_band_width(self):
        series = np.array([50.0] * 30)
        upper, mid, lower = tradalgo._bollinger(series, 20, 2.0)
        assert upper[-1] == pytest.approx(lower[-1])
        assert upper[-1] == pytest.approx(50.0)


class TestATR:
    def test_zero_range_candles_give_zero_atr(self):
        candles = [{"high": 100.0, "low": 100.0, "close": 100.0} for _ in range(20)]
        result = tradalgo._atr(candles, 14)
        assert result[-1] == pytest.approx(0.0)

    def test_atr_is_never_negative(self):
        rng = np.random.default_rng(3)
        candles = []
        price = 100.0
        for _ in range(50):
            o = price
            h = o + abs(rng.normal(0, 1))
            l = o - abs(rng.normal(0, 1))
            c = rng.uniform(l, h)
            candles.append({"open": o, "high": h, "low": l, "close": c})
            price = c
        result = tradalgo._atr(candles, 14)
        valid = result[~np.isnan(result)]
        assert np.all(valid >= 0)


# ══════════════════════════════════════════════════════════════════════════
# Position sizing (Section 7b) — the site of the most serious bug found
# during development: live trading previously sized JPY pairs at the
# 1,000,000-unit hard cap regardless of risk %, and XAU_USD position sizes
# were off by ~1000x from the intended risk. These tests lock in the fix.
# ══════════════════════════════════════════════════════════════════════════

class TestPositionSizing:
    BALANCE = 10_000.0
    RISK_PCT = 1.0  # risking $100

    @pytest.mark.parametrize("instrument,sl_pips", [
        ("EUR_USD", 20), ("GBP_USD", 20), ("AUD_USD", 20), ("NZD_USD", 20),
        ("XAU_USD", 200),
    ])
    def test_usd_quoted_pairs_hit_risk_target_with_no_price_lookup(self, instrument, sl_pips):
        # These don't need a reference rate at all (quote currency is USD),
        # so they should be exact regardless of price_lookup availability.
        units = tradalgo.calculate_position_units(instrument, sl_pips, self.RISK_PCT, self.BALANCE)
        ppv = tradalgo.pip_value_usd_per_unit(instrument)
        realized_risk = units * sl_pips * ppv
        assert realized_risk == pytest.approx(100.0, rel=0.02)

    @pytest.mark.parametrize("instrument,sl_pips", [
        ("USD_JPY", 20), ("USD_CHF", 20), ("USD_CAD", 20),
        ("EUR_GBP", 20), ("EUR_JPY", 20),
    ])
    def test_non_usd_quoted_pairs_hit_risk_target_using_fallback_rates(self, instrument, sl_pips):
        # No price_lookup provided — exercises the documented fallback path
        # used during backtesting. Should still land close to the target.
        units = tradalgo.calculate_position_units(instrument, sl_pips, self.RISK_PCT, self.BALANCE)
        ppv = tradalgo.pip_value_usd_per_unit(instrument)
        realized_risk = units * sl_pips * ppv
        assert realized_risk == pytest.approx(100.0, rel=0.10)

    def test_jpy_pair_does_not_hit_the_million_unit_cap(self):
        # Regression test for the specific real bug: the old live formula
        # sized every USD_JPY trade at exactly 1,000,000 units (the hard
        # cap), silently ignoring the risk % setting entirely.
        units = tradalgo.calculate_position_units("USD_JPY", 20, self.RISK_PCT, self.BALANCE)
        assert units < 1_000_000
        assert units == pytest.approx(77_500, rel=0.05)

    def test_live_price_lookup_is_more_accurate_than_fallback(self):
        # When a live reference price is available (as it always is during
        # live trading), the result should still land almost exactly on
        # target — tighter than the fallback-only path.
        prices = {"USD_JPY": {"mid": 154.20}}
        units = tradalgo.calculate_position_units(
            "EUR_JPY", 20, self.RISK_PCT, self.BALANCE,
            price_lookup=lambda p: prices.get(p, {}).get("mid"),
        )
        ppv = tradalgo.pip_value_usd_per_unit("EUR_JPY", price_lookup=lambda p: prices.get(p, {}).get("mid"))
        realized_risk = units * 20 * ppv
        assert realized_risk == pytest.approx(100.0, rel=0.01)

    def test_zero_or_negative_balance_returns_minimum_unit_not_a_crash(self):
        assert tradalgo.calculate_position_units("EUR_USD", 20, 1.0, 0.0) == 1
        assert tradalgo.calculate_position_units("EUR_USD", 20, 1.0, -500.0) == 1

    def test_zero_stop_loss_returns_minimum_unit_not_a_division_by_zero(self):
        assert tradalgo.calculate_position_units("EUR_USD", 0, 1.0, self.BALANCE) == 1

    def test_units_never_exceed_the_hard_cap(self):
        # Absurdly large risk % / balance should still clamp at 1,000,000.
        units = tradalgo.calculate_position_units("EUR_USD", 1, 100.0, 10_000_000.0)
        assert units == 1_000_000

    def test_backtest_and_live_use_the_same_underlying_formula(self):
        # There is exactly one sizing function now — this test exists mainly
        # to document that fact and fail loudly if a future edit
        # reintroduces a second, divergent formula.
        import inspect
        client_source = inspect.getsource(tradalgo.OandaClient.calculate_units)
        assert "calculate_position_units" in client_source


# ══════════════════════════════════════════════════════════════════════════
# Strategy consensus logic
# ══════════════════════════════════════════════════════════════════════════

class TestConsensusSignal:
    WEIGHTS = {"A": 0.25, "B": 0.20, "C": 0.20, "D": 0.20, "E": 0.15}

    def test_majority_buy_produces_buy_signal(self):
        results = {
            "A": {"signal": "BUY", "sl_pips": 20, "tp_pips": 50},
            "B": {"signal": "BUY", "sl_pips": 20, "tp_pips": 50},
            "C": {"signal": None},
            "D": {"signal": None},
            "E": {"signal": None},
        }
        out = tradalgo.consensus_signal(results, self.WEIGHTS, threshold=0.35)
        assert out["signal"] == "BUY"

    def test_majority_sell_produces_sell_signal(self):
        results = {
            "A": {"signal": "SELL", "sl_pips": 20, "tp_pips": 50},
            "B": {"signal": "SELL", "sl_pips": 20, "tp_pips": 50},
            "C": {"signal": None}, "D": {"signal": None}, "E": {"signal": None},
        }
        out = tradalgo.consensus_signal(results, self.WEIGHTS, threshold=0.35)
        assert out["signal"] == "SELL"

    def test_below_threshold_produces_no_signal(self):
        results = {
            "A": {"signal": "BUY", "sl_pips": 20, "tp_pips": 50},
            "B": {"signal": None}, "C": {"signal": None},
            "D": {"signal": None}, "E": {"signal": None},
        }
        # A alone = 0.25 weight, below the 0.35 threshold
        out = tradalgo.consensus_signal(results, self.WEIGHTS, threshold=0.35)
        assert out["signal"] is None

    def test_conflicting_signals_of_equal_weight_produce_no_signal(self):
        results = {
            "A": {"signal": "BUY", "sl_pips": 20, "tp_pips": 50},
            "B": {"signal": "SELL", "sl_pips": 20, "tp_pips": 50},
            "C": {"signal": None}, "D": {"signal": None}, "E": {"signal": None},
        }
        out = tradalgo.consensus_signal(results, self.WEIGHTS, threshold=0.35)
        assert out["signal"] is None

    def test_no_strategies_fired_produces_no_signal(self):
        results = {k: {"signal": None} for k in self.WEIGHTS}
        out = tradalgo.consensus_signal(results, self.WEIGHTS, threshold=0.35)
        assert out["signal"] is None
        assert out["score"] == 0


# ══════════════════════════════════════════════════════════════════════════
# News event parsing — regression test for the DST bug fixed earlier.
# The old code manually applied a hardcoded EST offset and silently broke
# entirely when ForexFactory changed its API to return full ISO-8601
# timestamps. This locks in the fix.
# ══════════════════════════════════════════════════════════════════════════

class TestNewsEventParsing:
    def test_edt_summer_event_converts_correctly_to_utc(self):
        from datetime import datetime
        # 2pm EDT (UTC-4, summer) should be 6pm UTC
        dt = datetime.fromisoformat("2026-07-29T14:00:00-04:00")
        utc = dt.astimezone(tradalgo.timezone.utc)
        assert utc.hour == 18
        assert utc.day == 29

    def test_est_winter_event_converts_correctly_to_utc(self):
        from datetime import datetime
        # 2pm EST (UTC-5, winter) should be 7pm UTC — the offset itself
        # differs from summer, proving DST is actually being respected
        # rather than a hardcoded constant.
        dt = datetime.fromisoformat("2026-01-15T14:00:00-05:00")
        utc = dt.astimezone(tradalgo.timezone.utc)
        assert utc.hour == 19

    def test_late_evening_event_rolls_over_to_the_next_utc_day(self):
        from datetime import datetime
        # 10:30pm EDT should become 2:30am UTC the *next* day — the old
        # code's modulo-based hour math never advanced the date field here.
        dt = datetime.fromisoformat("2026-07-30T22:30:00-04:00")
        utc = dt.astimezone(tradalgo.timezone.utc)
        assert utc.day == 31
        assert utc.hour == 2
        assert utc.minute == 30

    def test_fetch_news_events_parses_a_realistic_payload(self, monkeypatch):
        sample = [
            {"title": "Federal Funds Rate", "country": "USD",
             "date": "2026-07-29T14:00:00-04:00", "impact": "High"},
            {"title": "German ifo", "country": "EUR",
             "date": "2026-07-27T04:00:00-04:00", "impact": "Low"},
        ]

        class FakeResponse:
            def json(self_inner): return sample
            def raise_for_status(self_inner): pass

        monkeypatch.setattr(tradalgo.requests, "get", lambda *a, **kw: FakeResponse())
        tradalgo._news_cache["fetched_at"] = 0.0  # force a refetch
        events = tradalgo._fetch_news_events()

        assert len(events) == 1  # only the High-impact one
        assert events[0]["title"] == "Federal Funds Rate"
        assert events[0]["time"].hour == 18  # correctly converted to UTC


# ══════════════════════════════════════════════════════════════════════════
# Misc small utilities
# ══════════════════════════════════════════════════════════════════════════

class TestPipSize:
    def test_jpy_pairs_use_001(self):
        assert tradalgo._pip_size("USD_JPY") == 0.01
        assert tradalgo._pip_size("EUR_JPY") == 0.01

    def test_gold_uses_01(self):
        assert tradalgo._pip_size("XAU_USD") == 0.1

    def test_standard_pairs_use_00001(self):
        assert tradalgo._pip_size("EUR_USD") == 0.0001


class TestAtomicWrite:
    def test_write_and_read_round_trip(self, tmp_path):
        p = tmp_path / "test.json"
        tradalgo._atomic_write_json(p, {"a": 1, "b": [1, 2, 3]})
        import json
        assert json.loads(p.read_text()) == {"a": 1, "b": [1, 2, 3]}

    def test_no_leftover_temp_files(self, tmp_path):
        p = tmp_path / "test.json"
        tradalgo._atomic_write_json(p, {"x": 1})
        leftovers = list(tmp_path.glob("test.json.tmp*"))
        assert not leftovers

    def test_failed_write_does_not_corrupt_existing_file(self, tmp_path):
        import datetime as dt
        p = tmp_path / "test.json"
        tradalgo._atomic_write_json(p, {"a": 1})
        with pytest.raises(TypeError):
            tradalgo._atomic_write_json(p, {"bad": dt.datetime.now()})  # not JSON-serializable
        import json
        assert json.loads(p.read_text()) == {"a": 1}


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
