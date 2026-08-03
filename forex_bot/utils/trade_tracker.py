"""
Trade Tracker — persistent ledger backed by a JSON file.

Fixes vs previous version:
  - Persisted to disk (bot restarts no longer lose trade history)
  - record_open called BEFORE email so ledger is always populated
  - load_from_oanda() seeds the ledger from live OANDA open trades on startup
    so trades opened in a previous session are still tracked
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing   import Optional

log = logging.getLogger(__name__)

LEDGER_FILE = "logs/trade_ledger.json"

# In-memory mirror of the JSON file
_ledger: dict = {}


def _save():
    os.makedirs("logs", exist_ok=True)
    try:
        with open(LEDGER_FILE, "w") as f:
            json.dump(_ledger, f, indent=2)
    except Exception as e:
        log.error(f"Ledger save failed: {e}")


def _load():
    global _ledger
    if os.path.exists(LEDGER_FILE):
        try:
            with open(LEDGER_FILE) as f:
                _ledger = json.load(f)
            log.info(f"Ledger loaded: {len(_ledger)} trades from {LEDGER_FILE}")
        except Exception as e:
            log.warning(f"Ledger load failed (starting fresh): {e}")
            _ledger = {}


# Load on import
_load()


def record_open(trade_id: str, instrument: str, direction: str,
                entry: float, units: int, sl: float, tp: float, strategy: str):
    _ledger[str(trade_id)] = {
        "instrument": instrument,
        "direction":  direction,
        "entry":      entry,
        "units":      abs(units),
        "sl":         sl,
        "tp":         tp,
        "strategy":   strategy,
        "opened_at":  datetime.now(timezone.utc).isoformat(),
    }
    _save()
    log.debug(f"Ledger: recorded {trade_id} {direction} {instrument} @ {entry}")


def seed_from_oanda(open_trades: list):
    """
    Called at bot startup. For each OANDA open trade not already in our ledger,
    add a minimal entry so win/loss emails still fire even after a restart.
    """
    added = 0
    for t in open_trades:
        tid = str(t["id"])
        if tid not in _ledger:
            _ledger[tid] = {
                "instrument": t.get("instrument", "?"),
                "direction":  "BUY" if int(t.get("currentUnits", 1)) > 0 else "SELL",
                "entry":      float(t.get("price", 0)),
                "units":      abs(int(t.get("currentUnits", 0))),
                "sl":         0,
                "tp":         0,
                "strategy":   "pre-existing",
                "opened_at":  t.get("openTime", ""),
            }
            added += 1
    if added:
        _save()
        log.info(f"Ledger: seeded {added} pre-existing trades from OANDA")


def get_open(trade_id: str) -> dict:
    return _ledger.get(str(trade_id), {})


def remove(trade_id: str):
    _ledger.pop(str(trade_id), None)
    _save()


def all_open_ids() -> set:
    return set(_ledger.keys())
