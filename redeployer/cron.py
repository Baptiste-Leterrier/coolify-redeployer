"""Minimal 5-field cron expression parser and next-run calculator.

Supports `*`, lists (`1,15`), ranges (`1-5`), steps (`*/10`, `0-30/5`), month and
weekday names (`JAN`, `MON`), weekday 7 as Sunday, and the macros @yearly,
@annually, @monthly, @weekly, @daily, @midnight and @hourly.
Like Vixie cron, when both day-of-month and day-of-week are restricted, a day
matches if either one matches.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"])}
DAYS = {d: i for i, d in enumerate(["SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"])}
FIELDS = (  # name, min, max, names
    ("minute", 0, 59, {}),
    ("hour", 0, 23, {}),
    ("day of month", 1, 31, {}),
    ("month", 1, 12, MONTHS),
    ("day of week", 0, 7, DAYS),
)


class CronError(ValueError):
    pass


def _value(token: str, lo: int, hi: int, names: dict, fname: str) -> int:
    t = token.upper()
    if t in names:
        return names[t]
    if not t.isdigit():
        raise CronError(f"CRON_SCHEDULE: invalid {fname} value {token!r}")
    v = int(t)
    if not lo <= v <= hi:
        raise CronError(f"CRON_SCHEDULE: {fname} value {v} out of range {lo}-{hi}")
    return v


def _field(spec: str, lo: int, hi: int, names: dict, fname: str) -> tuple[set[int], bool]:
    values: set[int] = set()
    for part in spec.split(","):
        if not part:
            raise CronError(f"CRON_SCHEDULE: empty item in {fname} field {spec!r}")
        rng, _, step_s = part.partition("/")
        step = 1
        if step_s:
            if not step_s.isdigit() or int(step_s) == 0:
                raise CronError(f"CRON_SCHEDULE: invalid step {step_s!r} in {fname}")
            step = int(step_s)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            start, end = _value(a, lo, hi, names, fname), _value(b, lo, hi, names, fname)
            if start > end:
                raise CronError(f"CRON_SCHEDULE: reversed range {rng!r} in {fname}")
        else:
            start = _value(rng, lo, hi, names, fname)
            end = hi if step_s else start
        values.update(range(start, end + 1, step))
    # As in Vixie cron, a field that starts with "*" (including "*/n") counts as unrestricted
    # for the day-of-month / day-of-week OR rule.
    return values, not spec.startswith("*")


class CronExpression:
    def __init__(self, expr: str):
        self.expr = expr.strip()
        text = MACROS.get(self.expr.lower(), self.expr)
        parts = text.split()
        if len(parts) != 5:
            raise CronError(
                f"CRON_SCHEDULE must have 5 fields, got {len(parts)} (check it on https://crontab.guru)")
        parsed = [_field(p, lo, hi, names, fname) for p, (fname, lo, hi, names) in zip(parts, FIELDS)]
        self.minutes, self.hours = parsed[0][0], parsed[1][0]
        self.doms, self.dom_restricted = parsed[2]
        self.months = parsed[3][0]
        dows, self.dow_restricted = parsed[4]
        self.dows = {d % 7 for d in dows}

    def _day_matches(self, d: datetime) -> bool:
        dom_ok = d.day in self.doms
        dow_ok = (d.isoweekday() % 7) in self.dows
        if self.dom_restricted and self.dow_restricted:
            return dom_ok or dow_ok
        return dom_ok and dow_ok

    def matches(self, local: datetime) -> bool:
        return (local.minute in self.minutes and local.hour in self.hours
                and local.month in self.months and self._day_matches(local))

    def next_after(self, after: datetime, tz: ZoneInfo) -> datetime:
        """First matching minute strictly after `after` (aware), as an aware UTC datetime.

        Walks UTC minutes and converts each to local time, so DST transitions are
        handled by the timezone database: a local time skipped by a spring-forward
        never fires, and a local time repeated by a fall-back fires once.
        """
        t = after.astimezone(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
        limit = t + timedelta(days=366 * 5)
        while t < limit:
            local = t.astimezone(tz)
            if local.month not in self.months or not self._day_matches(local):
                # Jump to the next local midnight (re-checked in UTC terms).
                nxt = (local + timedelta(days=1)).replace(hour=0, minute=0)
                t = max(t + timedelta(minutes=1), nxt.astimezone(timezone.utc))
                continue
            if local.hour not in self.hours:
                t += timedelta(minutes=60 - local.minute)
                continue
            if local.minute in self.minutes:
                if local.fold == 1 and (local.replace(fold=0).astimezone(timezone.utc) != t):
                    t += timedelta(minutes=1)  # second pass of a repeated hour
                    continue
                return t
            t += timedelta(minutes=1)
        raise CronError(f"CRON_SCHEDULE {self.expr!r} never matches")
