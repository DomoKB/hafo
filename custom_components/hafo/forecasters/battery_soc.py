"""Battery SOC forecasting from existing HA solar, load and battery entities."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, EventStateChangedData, HomeAssistant
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from custom_components.hafo.const import CONF_SOURCE_ENTITY, DOMAIN, FORECAST_TYPE_BATTERY_SOC

from .battery_soc_adapter import build_forecast
from .battery_soc_settings import SOC_DEFAULTS, soc_settings_errors
from .historical_shift import ForecastPoint, ForecastResult

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BatterySocForecastResult(ForecastResult):
    """A fixed one-hour prediction and the full, recorder-compatible issue."""

    predicted_value: float | None
    attributes: dict[str, Any]


class BatterySocForecaster(DataUpdateCoordinator[ForecastResult | None]):
    """Refresh every five minutes and on changes to the forecast's core inputs."""

    UPDATE_INTERVAL = timedelta(minutes=5)
    MAX_SOURCE_AGE = timedelta(minutes=10)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Read generic mappings and retain the existing SOC model's behaviour."""
        self._entry = entry
        self.settings = {**SOC_DEFAULTS, **entry.data, **entry.options}
        self._source_entity: str = self.settings[CONF_SOURCE_ENTITY]
        errors = soc_settings_errors(self.settings)
        if errors:
            message = f"Invalid battery SOC configuration: {errors}"
            raise ValueError(message)
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.entry_id}",
            config_entry=entry,
            update_interval=self.UPDATE_INTERVAL,
        )
        # Current load changes every few seconds; it is only a bounded lead-in
        # and is not an event trigger. Source forecasts/reserves/SOC trigger a
        # debounced refresh so their changes do not wait for the next poll.
        tracked = {self._source_entity}
        tracked.update(
            value
            for key, value in self.settings.items()
            if key.endswith("_entity") and key != "load_power_entity" and isinstance(value, str) and value
        )
        self._unsubscribe: Callable[[], None] | None = async_track_state_change_event(
            hass, tracked, self._async_source_changed
        )

    @property
    def entry(self) -> ConfigEntry:
        """Return this forecaster's helper entry."""
        return self._entry

    @property
    def source_entity(self) -> str:
        """Return the measured SOC source."""
        return self._source_entity

    @property
    def history_days(self) -> int:
        """SOC uses provided forecasts rather than historical pattern lookup."""
        return 0

    async def _async_source_changed(self, _event: Event[EventStateChangedData]) -> None:
        """Refresh an active helper when its inputs change."""
        if self._unsubscribe is not None:
            await self.async_request_refresh()

    async def _async_update_data(self) -> BatterySocForecastResult:
        """Read HA state without cloud requests, optimisation or device writes."""
        now = dt_util.utcnow()
        start = now.timestamp() * 1000
        entities = {
            value
            for key, value in self.settings.items()
            if key.endswith("_entity") and isinstance(value, str) and value
        }
        entities.add(self.source_entity)
        states = {entity: self.hass.states.get(entity) for entity in entities}
        source = states[self.source_entity]
        # A constant SOC can retain an old last_updated while being reported
        # normally. Only last_reported distinguishes that plateau from a stalled
        # integration which leaves an old numeric value in the state machine.
        source_age = max(0.0, (now - source.last_reported).total_seconds()) if source is not None else None
        try:
            numeric_source = source is not None and math.isfinite(float(source.state))
        except ValueError:
            numeric_source = False
        source_stale = numeric_source and source_age is not None and source_age > self.MAX_SOURCE_AGE.total_seconds()
        if source_stale:
            states[self.source_entity] = None
        model = build_forecast(states, self.settings, start, start + int(self.settings["forecast_hours"]) * 3_600_000)
        attributes = model["attributes"]
        attributes["forecast_type"] = FORECAST_TYPE_BATTERY_SOC
        attributes["source_soc_reported_at"] = source.last_reported.isoformat() if source is not None else None
        attributes["source_soc_age_seconds"] = source_age
        attributes["source_soc_stale"] = source_stale
        if source_stale and source_age is not None:
            attributes["forecast_issues"].insert(
                0,
                f"Measured battery SOC was last reported {source_age / 60:.1f} minutes ago; "
                "forecast paused until a fresh reading is reported.",
            )
        return BatterySocForecastResult(
            forecast=[
                ForecastPoint(time=datetime.fromisoformat(p["time"]), value=p["value"]) for p in attributes["forecast"]
            ],
            source_entity=self.source_entity,
            history_days=0,
            generated_at=now,
            predicted_value=model["value"],
            attributes=attributes,
        )

    def cleanup(self) -> None:
        """Release source subscriptions when the helper is unloaded."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
