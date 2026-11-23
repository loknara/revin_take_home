"""Office hours and live-transfer availability, computed at call start.

OFFICE_HOURS uses the form "mon-fri 08:00-18:00" (America/New_York). The transfer
destination itself lives only in the Vapi transfer tool (configured from TRANSFER_NUMBER);
the backend only needs to know whether one exists.
"""

import os
import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo("America/New_York")
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
PATTERN = re.compile(
    r"^(mon|tue|wed|thu|fri|sat|sun)-(mon|tue|wed|thu|fri|sat|sun) (\d{2}):(\d{2})-(\d{2}):(\d{2})$"
)


def parse_hours(spec: str) -> tuple[int, int, time, time]:
    match = PATTERN.match(spec.strip().lower())
    if not match:
        raise ValueError("OFFICE_HOURS must look like 'mon-fri 08:00-18:00'")
    first, last = DAYS.index(match[1]), DAYS.index(match[2])
    opens, closes = (
        time(int(match[3]), int(match[4])),
        time(int(match[5]), int(match[6])),
    )
    if first > last or opens >= closes:
        raise ValueError("OFFICE_HOURS must be a forward day and time range")
    return first, last, opens, closes


def spoken_time(value: time) -> str:
    hour = value.hour % 12 or 12
    suffix = "AM" if value.hour < 12 else "PM"
    return f"{hour}:{value.minute:02d} {suffix}" if value.minute else f"{hour} {suffix}"


def office_status(now: datetime | None = None) -> dict:
    first, last, opens, closes = parse_hours(
        os.getenv("OFFICE_HOURS", "mon-fri 08:00-18:00")
    )
    local = (now or datetime.now(NEW_YORK)).astimezone(NEW_YORK)
    open_now = first <= local.weekday() <= last and opens <= local.time() < closes
    return {
        "office_open": open_now,
        "office_hours": f"{DAY_NAMES[first]} to {DAY_NAMES[last]}, {spoken_time(opens)} to {spoken_time(closes)} Eastern",
        "live_transfer_configured": bool(os.getenv("TRANSFER_NUMBER")),
        "local_time": local.strftime("%A %I:%M %p"),
    }
