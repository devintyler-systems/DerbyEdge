"""Track-local post time to UTC.

DraftKings renders the post as a bare clock ("7:02" / "PM") or as a minutes-to-
post countdown ("9" / "MTP").  Neither carries a date or zone, so the race date
(from the upload filename) and the track's registered timezone are required.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from src.derbyedge.tracks import track_timezone

_CLOCK = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*([AP]M)\s*$", re.I)
_MTP = re.compile(r"^\s*(\d{1,3})\s*MTP\s*$", re.I)


def post_time_to_utc(
    race_date: date | None, display: str | None, track_code: str | None,
    *, captured_at: datetime | None = None,
) -> tuple[str | None, str]:
    """Return ``(iso_utc | None, source)``.

    ``source`` is CLOCK (exact, from the rendered clock), MTP_ESTIMATE (capture
    time plus the countdown, an estimate) or an UNAVAILABLE_* reason.
    """
    text = (display or "").strip()
    clock = _CLOCK.match(text)
    if clock:
        zone = track_timezone(track_code)
        if race_date is None:
            return None, "UNAVAILABLE_NO_DATE"
        if zone is None:
            return None, "UNAVAILABLE_NO_TIMEZONE"
        hour, minute, meridiem = int(clock.group(1)), int(clock.group(2)), clock.group(3).upper()
        if not (1 <= hour <= 12 and 0 <= minute <= 59):
            return None, "UNAVAILABLE_INVALID_CLOCK"
        hour = hour % 12 + (12 if meridiem == "PM" else 0)
        local = datetime(race_date.year, race_date.month, race_date.day, hour, minute, tzinfo=ZoneInfo(zone))
        return local.astimezone(timezone.utc).isoformat(), "CLOCK"
    countdown = _MTP.match(text)
    if countdown:
        if captured_at is None or captured_at.tzinfo is None:
            return None, "UNAVAILABLE_MTP_WITHOUT_CAPTURE_TIME"
        eta = captured_at.astimezone(timezone.utc) + timedelta(minutes=int(countdown.group(1)))
        return eta.isoformat(), "MTP_ESTIMATE"
    return None, "UNAVAILABLE_UNRECOGNIZED"
