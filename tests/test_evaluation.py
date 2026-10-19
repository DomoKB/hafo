"""Independent checks of paired forecasting error and energy accounting."""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from scripts.evaluate_forecasters import score


@pytest.mark.parametrize(("day", "hours", "night_hours"), [("2026-09-01", 24, 6), ("2026-10-04", 23, 5)])
def test_energy_error_uses_real_hours(day: str, hours: int, night_hours: int) -> None:
    """One kW of error integrates to one kWh per real elapsed hour, including DST."""
    start = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo("Australia/Sydney"))
    end = start + timedelta(days=1)
    times = [start.astimezone(UTC) + timedelta(hours=h) for h in range(hours)]
    rows = [{"start": time, "mean": 1.0} for time in times]
    forecast = {time.timestamp(): 2.0 for time in times}
    with patch("scripts.evaluate_forecasters.predictions", return_value=forecast):
        metrics = score(rows, start, end, ("weekday_weekend",))["metrics"]
    for result in metrics.values():
        assert result["paired_hours"] == hours
        assert result["hourly_mae_kw"] == 1
        assert result["daily_energy_mae_kwh"] == hours
        assert result["overnight_energy_mae_kwh"] == night_hours


def test_missing_daytime_hour_excludes_daily_energy_without_erasing_complete_night() -> None:
    """Partial coverage cannot masquerade as a complete day's energy error."""
    start = datetime(2026, 9, 1, tzinfo=UTC)
    times = [start + timedelta(hours=h) for h in range(24)]
    rows = [{"start": time, "mean": 1.0} for time in times if time.hour != 12]
    forecast = {time.timestamp(): 2.0 for time in times}
    with patch("scripts.evaluate_forecasters.predictions", return_value=forecast):
        metrics = score(rows, start, start + timedelta(days=1), ("weekday_weekend",))["metrics"]
    assert metrics["historical_shift"]["paired_hours"] == 23
    assert metrics["historical_shift"]["daily_energy_mae_kwh"] is None
    assert metrics["historical_shift"]["complete_days"] == 0
    assert metrics["historical_shift"]["overnight_energy_mae_kwh"] == 6
