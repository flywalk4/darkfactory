"""Measure schedules.next_time for a sparse cron: the current code against the earlier day-by-day, uncached form.

Run: .venv/bin/python scripts/bench_cron.py
"""

import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from darkfactory import schedules

EXPRESSION, START, CALLS = "0 0 1 12 *", 1782864000, 3000


def baseline(expression, after, timezone="UTC"):
    """next_time as it was before: parse on every call, test every day, no month skip."""
    minutes, hours, days, months, weekdays, (day_set, weekday_set) = schedules.parse.__wrapped__(expression)
    tz = ZoneInfo(timezone)
    moment = datetime.fromtimestamp(after, tz).replace(second=0, microsecond=0) + timedelta(minutes=1)
    day = moment.replace(hour=0, minute=0)
    for _ in range(366 * 5):
        weekday = (day.weekday() + 1) % 7
        if day_set and weekday_set:
            day_ok = day.day in days or weekday in weekdays
        else:
            day_ok = day.day in days and weekday in weekdays
        if day.month in months and day_ok:
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    stamp = day.replace(hour=hour, minute=minute).timestamp()
                    if stamp >= moment.timestamp():
                        return stamp
        day = (day + timedelta(days=1)).replace(hour=0, minute=0)
    raise ValueError("never")


def timed(function):
    began = time.perf_counter()
    for _ in range(CALLS):
        result = function(EXPRESSION, START)
    return time.perf_counter() - began, result


if __name__ == "__main__":
    before, expected = timed(baseline)
    after, actual = timed(schedules.next_time)
    assert expected == actual
    print(f"before: {before:.3f}s  after: {after:.3f}s  speedup: {before / after:.1f}x  ({CALLS} calls)")
