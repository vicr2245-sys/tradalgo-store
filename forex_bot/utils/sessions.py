"""
Session Utilities
Determines if the bot should be trading based on London/NY sessions.
"""

from datetime import datetime, timezone
from typing import Optional
from config import LONDON_OPEN, LONDON_CLOSE, NY_OPEN, NY_CLOSE


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def current_session() -> Optional[str]:
    """Returns 'London', 'New York', 'London/NY Overlap', or None."""
    now = utc_now()
    h, m = now.hour, now.minute

    in_london = (h, m) >= LONDON_OPEN and (h, m) < LONDON_CLOSE
    in_ny     = (h, m) >= NY_OPEN     and (h, m) < NY_CLOSE

    if in_london and in_ny:
        return "London/NY Overlap"
    if in_london:
        return "London"
    if in_ny:
        return "New York"
    return None


def is_trading_session() -> bool:
    return current_session() is not None


def session_info() -> dict:
    now = utc_now()
    session = current_session()
    return {
        "utc_time":       now.strftime("%H:%M:%S"),
        "session":        session or "Off-hours",
        "trading_active": session is not None,
    }


def minutes_until_next_session() -> int:
    """How many minutes until London open if we're in off-hours."""
    now  = utc_now()
    h, m = now.hour, now.minute
    if is_trading_session():
        return 0

    lo_h, lo_m = LONDON_OPEN
    current_mins = h * 60 + m
    london_mins  = lo_h * 60 + lo_m

    if current_mins < london_mins:
        return london_mins - current_mins
    else:
        return (24 * 60 - current_mins) + london_mins
