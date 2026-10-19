"""Local-calendar multiweek averages, independent of Home Assistant I/O."""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from math import isfinite
from statistics import fmean
from typing import Any

MIN_SAMPLES = 3
MIN_HISTORY_WEEKS = 2
MAX_HISTORY_WEEKS = 12
MAX_FORECAST_DAYS = 14
WEEKEND_START = 5
GROUPINGS = frozenset({"weekday_weekend", "weekday"})
HOUR = timedelta(hours=1)


def _timestamp(value: Any, now: datetime) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(now.tzinfo) if value.tzinfo is not None else None
    if isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=now.tzinfo)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def average_history_to_forecast(
    statistics: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
    history_weeks: int = 4,
    forecast_days: int = 7,
    day_grouping: str = "weekday_weekend",
) -> list[tuple[datetime, float]]:
    """Average comparable local hours using completed, strictly past observations.

    At least three different local dates are required for an average. Otherwise
    use the exact hour one week earlier, if known. Missing buckets remain absent.
    A one-hour publication allowance excludes recent incomplete recorder data.
    Iterate output in UTC so DST gaps/repeated hours have real elapsed durations.
    """
    if now.tzinfo is None:
        msg = "Forecast time must be timezone-aware"
        raise ValueError(msg)
    if (
        type(history_weeks) is not int
        or not MIN_HISTORY_WEEKS <= history_weeks <= MAX_HISTORY_WEEKS
        or type(forecast_days) is not int
        or not 1 <= forecast_days <= MAX_FORECAST_DAYS
        or day_grouping not in GROUPINGS
        or (day_grouping == "weekday" and history_weeks < MIN_SAMPLES)
    ):
        msg = "Invalid multiweek forecast settings"
        raise ValueError(msg)

    cutoff = now.astimezone(UTC) - HOUR
    earliest = (now - timedelta(weeks=history_weeks)).astimezone(UTC)
    readings: dict[datetime, float | None] = {}
    for row in statistics:
        start = _timestamp(row.get("start"), now)
        raw = row.get("mean")
        if start is None or raw is None or isinstance(raw, bool):
            continue
        stamp = start.astimezone(UTC)
        if stamp < earliest or stamp + HOUR > cutoff:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError, OverflowError):
            continue
        if not isfinite(value):
            continue
        if stamp in readings and readings[stamp] != value:
            readings[stamp] = None  # Conflicting duplicate measurements are unknown.
        else:
            readings[stamp] = value

    # A repeated DST hour contributes one daily sample instead of extra weight.
    daily: dict[tuple[str, int], list[float]] = defaultdict(list)
    for stamp, value in readings.items():
        if value is not None:
            local = stamp.astimezone(now.tzinfo)
            daily[(local.date().isoformat(), local.hour)].append(value)

    buckets: dict[tuple[int, int], list[float]] = defaultdict(list)
    for (day, hour), values in daily.items():
        weekday = datetime.fromisoformat(day).weekday()
        group = weekday if day_grouping == "weekday" else int(weekday >= WEEKEND_START)
        buckets[(group, hour)].append(fmean(values))

    result: list[tuple[datetime, float]] = []
    current = now.astimezone(UTC).replace(minute=0, second=0, microsecond=0)
    end = (now + timedelta(days=forecast_days)).astimezone(UTC)
    while current < end:
        local = current.astimezone(now.tzinfo)
        group = local.weekday() if day_grouping == "weekday" else int(local.weekday() >= WEEKEND_START)
        samples = buckets.get((group, local.hour), [])
        previous_local = local - timedelta(weeks=1)
        previous = previous_local.astimezone(UTC)
        valid_previous = previous.astimezone(now.tzinfo).replace(tzinfo=None) == previous_local.replace(tzinfo=None)
        fallback = readings.get(previous) if valid_previous else None
        value = fmean(samples) if len(samples) >= MIN_SAMPLES else fallback
        if value is not None:
            result.append((local, value))
        current += HOUR
    return result
