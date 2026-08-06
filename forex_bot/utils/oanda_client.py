"""
OANDA v20 REST API Client
- Persistent requests.Session (reuses TCP connections = lower latency)
- Batch candle fetching with ThreadPoolExecutor (parallel, not sequential)
- In-memory candle cache (avoids redundant API calls within same bar)
- Batch price fetch for all instruments in one request
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime           import datetime, timezone
from typing             import Optional

import requests

from config import OANDA_API_URL, OANDA_API_KEY, OANDA_ACCOUNT_ID, INSTRUMENTS

log = logging.getLogger(__name__)

# ── candle cache ──────────────────────────────────────────────────────────────
# Structure: { "EUR_USD:H1": {"candles": [...], "cached_at": <epoch>, "bar_time": "..."} }
_candle_cache: dict = {}
CACHE_TTL_SECONDS = 55 * 60  # 55 min — just under one H1 bar


class OandaClient:
    def __init__(self):
        self.base       = OANDA_API_URL
        self.account_id = OANDA_ACCOUNT_ID

        # Persistent session — reuses the TCP connection across requests
        # This alone cuts ~50-100ms per request vs. creating a new connection each time
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {OANDA_API_KEY}",
            "Content-Type":  "application/json",
            "Accept-Encoding": "gzip",          # compressed responses
            "Connection":    "keep-alive",
        })

    # ── helpers ──────────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict = None) -> dict:
        try:
            r = self.session.get(f"{self.base}{path}", params=params, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            log.error(f"OANDA API GET error ({path}): {e}")
            raise

    def _post(self, path: str, body: dict) -> dict:
        try:
            r = self.session.post(f"{self.base}{path}", json=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            log.error(f"OANDA API POST error ({path}): {e}")
            raise

    def _put(self, path: str, body: dict) -> dict:
        try:
            r = self.session.put(f"{self.base}{path}", json=body, timeout=10)
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            log.error(f"OANDA API PUT error ({path}): {e}")
            raise

    # ── account ──────────────────────────────────────────────────────────────

    def get_account(self) -> dict:
        return self._get(f"/v3/accounts/{self.account_id}")["account"]

    def get_balance(self) -> float:
        # Use account summary (lighter endpoint than full account)
        data = self._get(f"/v3/accounts/{self.account_id}/summary")
        return float(data["account"]["balance"])

    def get_open_trades(self) -> list:
        return self._get(f"/v3/accounts/{self.account_id}/openTrades")["trades"]

    def get_trade(self, trade_id: str) -> dict:
        return self._get(f"/v3/accounts/{self.account_id}/trades/{trade_id}")["trade"]

    def get_transactions(self, count: int = 50) -> list:
        return self._get(
            f"/v3/accounts/{self.account_id}/transactions",
            params={"count": count}
        ).get("transactions", [])

    # ── prices ───────────────────────────────────────────────────────────────

    def get_price(self, instrument: str) -> dict:
        """Single instrument price."""
        data = self._get(
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": instrument}
        )
        p   = data["prices"][0]
        bid = float(p["bids"][0]["price"])
        ask = float(p["asks"][0]["price"])
        return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "time": p["time"]}

    def get_prices(self, instruments: list = None) -> dict:
        """
        Batch price fetch — ALL instruments in a single HTTP request.
        ~10x faster than calling get_price() in a loop.
        """
        instruments = instruments or INSTRUMENTS
        data = self._get(
            f"/v3/accounts/{self.account_id}/pricing",
            params={"instruments": ",".join(instruments)}
        )
        result = {}
        for p in data["prices"]:
            bid = float(p["bids"][0]["price"])
            ask = float(p["asks"][0]["price"])
            result[p["instrument"]] = {
                "bid": bid, "ask": ask, "mid": (bid + ask) / 2
            }
        return result

    # ── candles (cached + parallel) ──────────────────────────────────────────

    def get_candles(self, instrument: str, granularity: str = "H1", count: int = 100) -> list:
        """
        Returns candles. Served from cache if the current bar hasn't changed.
        Cache key = instrument:granularity.
        """
        cache_key = f"{instrument}:{granularity}"
        now       = time.time()
        cached    = _candle_cache.get(cache_key)

        if cached and (now - cached["cached_at"]) < CACHE_TTL_SECONDS:
            log.debug(f"Cache hit: {cache_key}")
            return cached["candles"]

        candles = self._fetch_candles(instrument, granularity, count)
        _candle_cache[cache_key] = {"candles": candles, "cached_at": now}
        return candles

    def _fetch_candles(self, instrument: str, granularity: str, count: int) -> list:
        data = self._get(
            f"/v3/instruments/{instrument}/candles",
            params={"granularity": granularity, "count": count, "price": "M"}
        )
        candles = []
        for c in data["candles"]:
            if not c["complete"]:
                continue
            m = c["mid"]
            candles.append({
                "time":   c["time"],
                "open":   float(m["o"]),
                "high":   float(m["h"]),
                "low":    float(m["l"]),
                "close":  float(m["c"]),
                "volume": int(c["volume"]),
            })
        return candles

    def get_all_candles_parallel(
        self,
        instruments: list = None,
        granularity: str  = "H1",
        count: int        = 100,
        max_workers: int  = 6,
    ) -> dict:
        """
        Fetch candles for all instruments in parallel using a thread pool.
        Returns {instrument: [candles]} dict.
        Instruments already in cache are served instantly without a thread.

        max_workers=6 is a safe ceiling for OANDA's rate limits.
        """
        instruments = instruments or INSTRUMENTS
        results     = {}
        to_fetch    = []

        # Serve cached instruments immediately
        for inst in instruments:
            cached = self.get_candles.__wrapped__(self, inst, granularity, count) \
                     if hasattr(self.get_candles, "__wrapped__") else None
            cache_key = f"{inst}:{granularity}"
            now       = time.time()
            entry     = _candle_cache.get(cache_key)
            if entry and (now - entry["cached_at"]) < CACHE_TTL_SECONDS:
                results[inst] = entry["candles"]
            else:
                to_fetch.append(inst)

        if to_fetch:
            log.info(f"Fetching candles for {len(to_fetch)} instruments in parallel…")
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {
                    pool.submit(self._fetch_candles, inst, granularity, count): inst
                    for inst in to_fetch
                }
                for future in as_completed(futures):
                    inst = futures[future]
                    try:
                        candles = future.result()
                        cache_key = f"{inst}:{granularity}"
                        _candle_cache[cache_key] = {"candles": candles, "cached_at": time.time()}
                        results[inst] = candles
                        log.debug(f"  fetched {inst}: {len(candles)} candles")
                    except Exception as e:
                        log.error(f"  failed {inst}: {e}")
                        results[inst] = []

        return results

    def invalidate_cache(self, instrument: str = None, granularity: str = "H1"):
        """Call at the start of each new bar to force fresh candle data."""
        if instrument:
            _candle_cache.pop(f"{instrument}:{granularity}", None)
        else:
            _candle_cache.clear()
            log.debug("Candle cache cleared for new bar.")

    # ── orders ───────────────────────────────────────────────────────────────

    def place_market_order(
        self,
        instrument:         str,
        units:              int,
        stop_loss_price:    Optional[float] = None,
        take_profit_price:  Optional[float] = None,
        client_comment:     str = "",
    ) -> dict:
        """units > 0 = BUY, units < 0 = SELL."""
        def _fmt_price(price: float, inst: str) -> str:
            if "JPY" in inst: return f"{price:.3f}"
            if "XAU" in inst: return f"{price:.2f}"
            return f"{price:.5f}"

        order = {
            "type":       "MARKET",
            "instrument": instrument,
            "units":      str(units),
        }
        if stop_loss_price:
            order["stopLossOnFill"]   = {"price": _fmt_price(stop_loss_price, instrument)}
        if take_profit_price:
            order["takeProfitOnFill"] = {"price": _fmt_price(take_profit_price, instrument)}
        if client_comment:
            order["clientExtensions"] = {"comment": client_comment[:128]}

        data = self._post(f"/v3/accounts/{self.account_id}/orders", {"order": order})
        log.info(f"Order placed: {instrument} {units:+,} units")
        return data

    def close_trade(self, trade_id: str) -> dict:
        data = self._put(f"/v3/accounts/{self.account_id}/trades/{trade_id}/close", {})
        log.info(f"Trade closed: {trade_id}")
        return data

    def close_all_trades(self):
        trades = self.get_open_trades()
        for t in trades:
            self.close_trade(t["id"])
        log.info(f"Closed {len(trades)} open trades.")

    # ── position sizing ──────────────────────────────────────────────────────

    def calculate_units(self, instrument: str, sl_pips: float, risk_pct: float,
                        balance: float = None) -> int:
        """
        Pass in a pre-fetched balance to avoid an extra API call per instrument.
        Falls back to fetching balance if not provided.
        Uses dynamic pip-value formula to prevent oversized positions on JPY and cross-currency pairs.
        """
        if sl_pips <= 0:
            return 1

        if balance is None:
            balance = self.get_balance()

        if balance <= 0:
            return 1

        risk_amount = balance * (risk_pct / 100)

        def _pip_size(inst):
            if "JPY" in inst: return 0.01
            if "XAU" in inst: return 0.10
            return 0.0001

        pip = _pip_size(instrument)
        base, quote = instrument.split("_", 1) if "_" in instrument else (instrument, "USD")

        try:
            prices = self.get_prices([instrument])
            mid_price = prices.get(instrument, {}).get("mid", 0.0) or 0.0
        except Exception:
            mid_price = 0.0

        if quote == "USD":
            pip_value_per_unit = pip
        elif base == "USD":
            pip_value_per_unit = pip / mid_price if mid_price > 0 else pip / 155.0
        else:
            # Cross pair: convert via reference rate if possible
            if "JPY" in quote:
                jpy_rate = 155.0
                try:
                    p = self.get_prices(["USD_JPY"])
                    jpy_rate = p.get("USD_JPY", {}).get("mid", 155.0) or 155.0
                except Exception: pass
                pip_value_per_unit = pip / jpy_rate
            else:
                pip_value_per_unit = pip

        if pip_value_per_unit <= 0 or sl_pips <= 0:
            return 1

        units = int(risk_amount / (sl_pips * pip_value_per_unit))
        return max(1, min(units, 1_000_000))
