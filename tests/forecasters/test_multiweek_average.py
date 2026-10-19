"""Behaviour and chronological boundaries of the multiweek forecaster."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hafo.const import DOMAIN
from custom_components.hafo.coordinator import create_forecaster
from custom_components.hafo.forecasters import multiweek_average as mw
from custom_components.hafo.forecasters.multiweek_average import MultiweekAverageForecaster
from custom_components.hafo.forecasters.multiweek_profile import average_history_to_forecast

TZ = ZoneInfo("Australia/Sydney")
NOW = datetime(2026, 9, 28, tzinfo=TZ)  # Monday.
HOUR = timedelta(hours=1)


def row(day: str, hour: int, mean: float) -> dict[str, Any]:
    """Construct a recorder observation in local time."""
    return {"start": datetime.fromisoformat(day).replace(hour=hour, tzinfo=TZ), "mean": mean}


def full_history(now: datetime, value: float = 2.0) -> list[dict[str, Any]]:
    """Four weeks of known observations with genuine UTC hour durations."""
    current = (now - timedelta(weeks=4)).astimezone(UTC)
    result = []
    while current < now.astimezone(UTC):
        result.append({"start": current, "mean": value})
        current += HOUR
    return result


def test_comparable_days_average_instead_of_copying_one_day() -> None:
    """Three independent days give an arithmetic mean; weekends stay separate."""
    history = [
        row("2026-09-21", 12, 1),
        row("2026-09-22", 12, 2),
        row("2026-09-25", 12, 6),
        row("2026-09-12", 12, 10),
        row("2026-09-19", 12, 20),
        row("2026-09-26", 12, 30),
    ]
    result = dict(average_history_to_forecast(history, now=NOW))
    assert result[NOW.replace(hour=12)] == 3
    assert result[(NOW + timedelta(days=5)).replace(hour=12)] == 20


def test_exact_weekday_option_excludes_other_weekdays() -> None:
    """A Monday pattern can be learned separately when enough Mondays exist."""
    history = [
        row("2026-09-07", 12, 12),
        row("2026-09-14", 12, 14),
        row("2026-09-21", 12, 16),
        row("2026-09-22", 12, 100),
    ]
    result = dict(average_history_to_forecast(history, now=NOW, day_grouping="weekday"))
    assert result[NOW.replace(hour=12)] == 14


def test_future_and_recent_unpublished_rows_cannot_change_a_forecast() -> None:
    """Predictions never incorporate observations unavailable at their issue time."""
    history = full_history(NOW)
    expected = average_history_to_forecast(history, now=NOW)
    contaminated = [
        *history,
        {"start": NOW, "mean": 99999},
        {"start": NOW + HOUR, "mean": 99999},
        {"start": NOW - HOUR, "mean": 99999},
    ]
    assert average_history_to_forecast(contaminated, now=NOW) == expected


def test_sparse_history_uses_known_previous_week_or_abstains() -> None:
    """A missing profile falls back to the genuine weekly reading, never zero."""
    assert average_history_to_forecast([], now=NOW) == []
    result = average_history_to_forecast([row("2026-09-21", 12, 5)], now=NOW, forecast_days=1)
    assert result == [(NOW.replace(hour=12), 5)]


def test_duplicate_and_invalid_rows_cannot_invent_sample_coverage() -> None:
    """Repeated rows are not additional days; invalid values are skipped."""
    single = row("2026-09-21", 12, 5)
    bad = [
        {"start": "invalid", "mean": 9},
        {"start": NOW, "mean": None},
        {"start": NOW - timedelta(days=3), "mean": float("nan")},
        {"start": NOW - timedelta(days=3), "mean": float("inf")},
        {"start": NOW - timedelta(days=3), "mean": True},
    ]
    assert average_history_to_forecast([single] * 4 + bad, now=NOW, forecast_days=1) == [(NOW.replace(hour=12), 5)]
    conflict = {**single, "mean": 7}
    assert average_history_to_forecast([single, conflict, single], now=NOW, forecast_days=1) == []


@pytest.mark.parametrize("value", [0.0, -2.0])
def test_zero_and_signed_measurements_are_valid(value: float) -> None:
    """Generic HAFO sensors retain genuine zero and negative measurements."""
    result = average_history_to_forecast(full_history(NOW, value), now=NOW, forecast_days=1)
    assert len(result) == 24
    assert all(point[1] == value for point in result)


@pytest.mark.parametrize(("date", "hours"), [("2026-10-04", 23), ("2026-04-05", 25)])
def test_dst_days_preserve_actual_hour_coverage(date: str, hours: int) -> None:
    """No invented spring hour and no collapsed autumn repeated hour."""
    now = datetime.fromisoformat(date).replace(tzinfo=TZ)
    result = average_history_to_forecast(full_history(now), now=now, forecast_days=1)
    times = [time.astimezone(UTC) for time, _ in result]
    assert len(times) == hours
    assert len(set(times)) == hours
    assert all(b - a == HOUR for a, b in pairwise(times))


def test_missing_previous_week_dst_hour_is_not_replaced_by_adjacent_hour() -> None:
    """A nonexistent 02:00 in the preceding week cannot become 03:00."""
    now = datetime(2026, 10, 11, tzinfo=TZ)
    assert average_history_to_forecast([row("2026-10-04", 3, 8)], now=now, forecast_days=1) == [
        (now.replace(hour=3), 8)
    ]


def test_partial_hour_forecast_starts_at_current_hour_without_reading_current_load() -> None:
    """Past averages can cover the remaining current hour without look-ahead."""
    now = NOW.replace(hour=10, minute=25)
    result = average_history_to_forecast(full_history(now), now=now, forecast_days=1)
    assert result[0][0] == now.replace(minute=0)
    assert result[0][1] == 2


@pytest.mark.parametrize(
    "kwargs",
    [
        {"history_weeks": 1},
        {"history_weeks": 13},
        {"history_weeks": True},
        {"history_weeks": 2.5},
        {"forecast_days": 0},
        {"day_grouping": "invalid"},
    ],
)
def test_invalid_settings_fail_explicitly(kwargs: dict[str, Any]) -> None:
    """Invalid configuration must not silently return a different algorithm."""
    with pytest.raises(ValueError, match="Invalid multiweek"):
        average_history_to_forecast([], now=NOW, **kwargs)


def test_naive_forecast_time_is_rejected() -> None:
    """Local calendar grouping requires an explicit timezone."""
    with pytest.raises(ValueError, match="timezone-aware"):
        average_history_to_forecast([], now=NOW.replace(tzinfo=None))


async def test_factory_coordinator_and_saved_options(hass: HomeAssistant) -> None:
    """The new factory option obeys saved settings and the existing sensor contract."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Load Average",
        data={
            "source_entity": "sensor.load",
            "forecast_type": "multiweek_average",
            "history_weeks": 4,
        },
        options={"history_weeks": 3, "forecast_days": 1, "day_grouping": "weekday_weekend"},
    )
    entry.add_to_hass(hass)
    forecaster = create_forecaster(hass, entry)
    assert isinstance(forecaster, MultiweekAverageForecaster)
    assert forecaster.history_days == 21
    with (
        patch.object(mw.dt_util, "now", return_value=NOW),
        patch.object(mw, "get_statistics_for_sensor", return_value=full_history(NOW)) as fetch,
    ):
        result = await forecaster._generate_forecast()
    fetch.assert_awaited_once_with(hass, "sensor.load", NOW - timedelta(days=21), NOW)
    assert result.history_days == 21
    assert result.source_entity == "sensor.load"
    assert len(result.forecast) == 24
    assert all(point.value == 2 for point in result.forecast)


async def test_coordinator_abstains_with_no_usable_history(hass: HomeAssistant) -> None:
    """Missing recorder data is unavailable, not an all-zero forecast."""
    entry = MockConfigEntry(domain=DOMAIN, title="Average", data={"source_entity": "sensor.load"})
    with (
        patch.object(mw, "get_statistics_for_sensor", return_value=[]),
        pytest.raises(ValueError, match="No usable historical"),
    ):
        await MultiweekAverageForecaster(hass, entry)._generate_forecast()


@pytest.mark.parametrize("settings", [{"history_weeks": 2.5}, {"forecast_days": True}])
def test_coordinator_does_not_silently_truncate_invalid_settings(hass: HomeAssistant, settings: dict[str, Any]) -> None:
    """Invalid stored options do not become a different valid configuration."""
    entry = MockConfigEntry(domain=DOMAIN, title="Average", data={"source_entity": "sensor.load", **settings})
    with pytest.raises(ValueError, match="whole numbers"):
        MultiweekAverageForecaster(hass, entry)
