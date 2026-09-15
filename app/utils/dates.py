"""Date boundaries resolved against UTC, so a caller's time zone cannot shift a result."""

from datetime import UTC, date, datetime, time, timedelta


def midnight_utc(day: date) -> datetime:
    """The first instant of `day`, in UTC."""
    return datetime.combine(day, time.min, tzinfo=UTC)


def end_of_day_utc(day: date) -> datetime:
    """The exclusive upper bound for a date range whose end is inclusive.

    Adding a day would overflow on `date.max`, which reaches the client as a 500; the last
    representable instant is the same bound for every date a caller can actually send.
    """
    if day == date.max:
        return datetime.combine(day, time.max, tzinfo=UTC)
    return midnight_utc(day + timedelta(days=1))
