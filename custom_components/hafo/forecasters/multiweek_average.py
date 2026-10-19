"""Multiweek average forecaster using local weekday/hour patterns."""

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.hafo.const import (
    CONF_DAY_GROUPING,
    CONF_FORECAST_DAYS,
    CONF_HISTORY_WEEKS,
    DEFAULT_DAY_GROUPING,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_HISTORY_WEEKS,
)

from .historical_shift import ForecastPoint, ForecastResult, HistoricalShiftForecaster, get_statistics_for_sensor
from .multiweek_profile import average_history_to_forecast


class MultiweekAverageForecaster(HistoricalShiftForecaster):
    """Reuse HAFO's lifecycle/output contract with a multiweek mean algorithm."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Read method-specific settings, preferring saved options."""
        super().__init__(hass, entry)
        settings = {**entry.data, **entry.options}
        raw_weeks = settings.get(CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS)
        raw_days = settings.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS)
        self._history_weeks = int(raw_weeks)
        self._forecast_days = int(raw_days)
        if (
            isinstance(raw_weeks, bool)
            or isinstance(raw_days, bool)
            or float(raw_weeks) != self._history_weeks
            or float(raw_days) != self._forecast_days
        ):
            msg = "Multiweek settings require whole numbers"
            raise ValueError(msg)
        self._day_grouping = str(settings.get(CONF_DAY_GROUPING, DEFAULT_DAY_GROUPING))
        self._history_days = self._history_weeks * 7

    async def _generate_forecast(self) -> ForecastResult:
        """Fetch prior hourly statistics and average comparable local hours."""
        now = dt_util.now()
        statistics = await get_statistics_for_sensor(
            self.hass, self.source_entity, now - timedelta(days=self.history_days), now
        )
        points = average_history_to_forecast(
            statistics,
            now=now,
            history_weeks=self._history_weeks,
            forecast_days=self._forecast_days,
            day_grouping=self._day_grouping,
        )
        if not points:
            msg = f"No usable historical hours for {self.source_entity}"
            raise ValueError(msg)
        return ForecastResult(
            forecast=[ForecastPoint(time=time, value=value) for time, value in points],
            source_entity=self.source_entity,
            history_days=self.history_days,
            generated_at=now,
        )
