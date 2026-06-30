"""Sanctioned datetime constructors — the one place datetimes are created.

The ``DTZ`` lint family (flake8-datetimez) is enabled repo-wide; this module is
the only file exempt from it. Build datetimes through these helpers instead of
bare ``datetime.now()`` / ``datetime(...)`` / ``strptime()`` so "local or UTC?"
is an explicit choice at every call site and a naive value can't silently reach
the astronomy/ephemeris path. See ADR-0018.

- Civil / astronomical time (epoch for RA/Dec, ephemerides): utc_now,
  utc_from_timestamp, utc.
- Local-time bookkeeping (filenames, log stamps): local_now.
- Parsing a string whose zone the caller attaches afterwards: parse, naive.
"""

import datetime


def utc_now() -> datetime.datetime:
    """Current instant, timezone-aware in UTC. Use for civil/astronomical time."""
    return datetime.datetime.now(datetime.timezone.utc)


def local_now() -> datetime.datetime:
    """Current instant, naive, in the host's local zone.

    Local bookkeeping only (filenames, log stamps) — never the astronomy path;
    use utc_now() there.
    """
    return datetime.datetime.now()


def utc_from_timestamp(ts: float) -> datetime.datetime:
    """A POSIX timestamp as a timezone-aware UTC datetime (e.g. a file mtime)."""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc)


def parse(value: str, fmt: str) -> datetime.datetime:
    """``strptime`` producing a naive datetime; the caller attaches the zone."""
    return datetime.datetime.strptime(value, fmt)


def naive(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime.datetime:
    """Construct a naive datetime from explicit fields; the caller localizes it."""
    return datetime.datetime(year, month, day, hour, minute, second, microsecond)


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime.datetime:
    """Construct a timezone-aware UTC datetime from explicit fields."""
    return datetime.datetime(
        year,
        month,
        day,
        hour,
        minute,
        second,
        microsecond,
        tzinfo=datetime.timezone.utc,
    )
