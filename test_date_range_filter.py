"""Date-range bounds resolve to unix timestamps, compared against created_at.

`created_at` is a DOUBLE PRECISION unix float, so a range filter has to become
a number — no date casting, no string comparison. These check the conversion
and, more importantly, the two boundary decisions that are easy to get subtly
wrong and hard to notice:

  * the end date is INCLUSIVE of its whole day
  * days are LOCAL days (REPORT_TIMEZONE), not UTC

    python3 test_date_range_filter.py
"""
import datetime as dt
import os
import sys
import pathlib
from zoneinfo import ZoneInfo

sys.path.insert(0, str(pathlib.Path(__file__).parent))

TZ = os.getenv("REPORT_TIMEZONE", "Asia/Kolkata")


def _parse(value, end_of_day):
    """Mirror of main._parse_range_bound, isolated from its FastAPI imports."""
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    try:
        return float(raw)
    except ValueError:
        pass
    try:
        tz = ZoneInfo(TZ)
    except Exception:
        tz = dt.timezone.utc
    try:
        if len(raw) == 10:
            d = dt.datetime.strptime(raw, "%Y-%m-%d")
            if end_of_day:
                d = d.replace(hour=23, minute=59, second=59, microsecond=999999)
            return d.replace(tzinfo=tz).timestamp()
        parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=tz)
        return parsed.timestamp()
    except Exception:
        return None


def main():
    tz = ZoneInfo(TZ)
    failures = []

    def check(label, cond):
        if not cond:
            failures.append(label)

    # --- it must produce a NUMBER, which is what created_at is ---------------
    start = _parse("2026-08-19", end_of_day=False)
    check("start bound is not a float", isinstance(start, float))

    # --- the end date is inclusive of its whole day -------------------------
    end = _parse("2026-08-19", end_of_day=True)
    check("end bound is not a float", isinstance(end, float))
    check("end of day is not after start of day", end > start)
    # A ticket at 23:30 local on the 19th must fall inside "to 2026-08-19".
    late = dt.datetime(2026, 8, 19, 23, 30, tzinfo=tz).timestamp()
    check("a 23:30 ticket falls outside its own end date", start <= late <= end)
    # Just under 24h of span, not zero.
    check("end/start span is not a full day", 86399 <= (end - start) <= 86400)

    # --- days are local days ------------------------------------------------
    expected_start = dt.datetime(2026, 8, 19, 0, 0, 0, tzinfo=tz).timestamp()
    check("start is not local midnight", abs(start - expected_start) < 1)
    if TZ != "UTC":
        utc_midnight = dt.datetime(2026, 8, 19, tzinfo=dt.timezone.utc).timestamp()
        check("boundary was resolved in UTC, not the report timezone",
              abs(start - utc_midnight) > 60)

    # --- a raw timestamp passes straight through ----------------------------
    check("epoch float not passed through", _parse("1787097066.577476", False) == 1787097066.577476)
    check("epoch int not passed through", _parse(1787097066, False) == 1787097066.0)

    # --- full ISO datetimes -------------------------------------------------
    iso = _parse("2026-08-19T06:30:00", end_of_day=False)
    check("naive ISO not interpreted in the report timezone",
          abs(iso - dt.datetime(2026, 8, 19, 6, 30, tzinfo=tz).timestamp()) < 1)
    check("Z-suffixed ISO not handled",
          abs(_parse("2026-08-19T06:30:00Z", False)
              - dt.datetime(2026, 8, 19, 6, 30, tzinfo=dt.timezone.utc).timestamp()) < 1)

    # --- unusable input is ignored, never fatal -----------------------------
    for bad in (None, "", "   ", "not-a-date", "2026-13-45"):
        check(f"{bad!r} did not return None", _parse(bad, False) is None)

    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        print(f"\n{len(failures)} failed")
        return 1
    print("OK: bounds are unix timestamps, end date inclusive, local days")
    return 0


if __name__ == "__main__":
    sys.exit(main())
