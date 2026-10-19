"""SOC energy-balance parity and native coordinator/sensor contracts."""

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from freezegun import freeze_time
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hafo.const import DOMAIN
from custom_components.hafo.coordinator import create_forecaster
from custom_components.hafo.forecasters.battery_soc import BatterySocForecaster
from custom_components.hafo.forecasters.battery_soc_adapter import build_forecast
from custom_components.hafo.forecasters.battery_soc_model import estimate_soc, sample_soc
from custom_components.hafo.sensor import HafoForecastSensor

MODEL_CASES: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "soc-js-parity.json").read_text(encoding="utf-8")
)
NOW = datetime(2026, 1, 1, tzinfo=UTC)
HOUR = 3_600_000


def soc_settings() -> dict[str, Any]:
    """Use generic mappings and simple, analytically checkable inputs."""
    return {
        "source_entity": "sensor.battery_soc",
        "forecast_type": "battery_soc",
        "solar_entity": "sensor.solar_forecast",
        "load_forecast_entity": "sensor.load_forecast",
        "load_power_entity": "sensor.load",
        "capacity_kwh": 10,
        "max_charge_kw": 5,
        "max_discharge_kw": 5,
        "charge_efficiency": 1,
        "discharge_efficiency": 1,
        "forecast_hours": 2,
        "min_soc": 0,
    }


def soc_states(*, solar_kw: float = 0, load_kw: float = 1) -> dict[str, Any]:
    """Build two hours of complete synthetic solar/load forecast states."""
    return {
        "sensor.battery_soc": {"state": "50", "attributes": {"unit_of_measurement": "%"}},
        "sensor.solar_forecast": {
            "state": "0",
            "attributes": {
                "detailedForecast": [
                    {"period_start": (NOW + timedelta(minutes=30 * index)).isoformat(), "pv_estimate": solar_kw}
                    for index in range(4)
                ]
            },
        },
        "sensor.load_forecast": {
            "state": str(load_kw),
            "attributes": {
                "unit_of_measurement": "kW",
                "forecast": [
                    {"time": (NOW + timedelta(hours=index)).isoformat(), "value": load_kw} for index in range(2)
                ],
            },
        },
        "sensor.load": {"state": str(load_kw), "attributes": {"unit_of_measurement": "kW"}},
    }


@pytest.mark.parametrize("case", MODEL_CASES, ids=[case["name"] for case in MODEL_CASES])
def test_pure_model_preserves_verified_javascript_math(case: dict[str, Any]) -> None:
    """The migrated model retains all sixteen existing cross-language scenarios."""
    actual = estimate_soc(case["input"])
    expected = case["expected"]
    assert actual["complete"] == expected["complete"]
    assert actual["coveredUntil"] == pytest.approx(expected["coveredUntil"], abs=1, rel=0)
    assert len(actual["points"]) == len(expected["points"])
    for point, original in zip(actual["points"], expected["points"], strict=True):
        assert point[0] == pytest.approx(original[0], abs=1, rel=0)
        assert point[1] == pytest.approx(original[1], abs=1e-8, rel=1e-10)
    assert actual["energy"] == pytest.approx(expected["energy"])
    assert bool(actual["issues"]) == bool(expected["issues"])


@pytest.mark.parametrize(("solar_kw", "expected"), [(0, 40), (3, 70)])
def test_adapter_one_hour_scalar_and_complete_curve(solar_kw: float, expected: float) -> None:
    """A 10 kWh battery gains/loses exactly the net energy, with fixed lead."""
    now = NOW.timestamp() * 1000
    result = build_forecast(soc_states(solar_kw=solar_kw), soc_settings(), now, now + 2 * HOUR)
    attrs = result["attributes"]
    assert result["value"] == pytest.approx(expected)
    assert attrs["forecast"][0]["value"] == 50
    assert attrs["forecast_lead_hours"] == 1
    assert datetime.fromisoformat(attrs["prediction_time"]) == NOW + timedelta(hours=1)
    assert attrs["forecast_complete"] is True
    assert attrs["forecast_issues"] == []


def test_native_units_calibration_reserves_and_off_grid() -> None:
    """Native reserve priority and Wh/W conversions survive the generic mapping."""
    settings = {
        **soc_settings(),
        "min_soc": None,
        "min_soc_entity": "sensor.cutoff",
        "backup_soc_entity": "number.backup",
        "grid_status_entity": "sensor.grid",
        "capacity_kwh": None,
        "capacity_entity": "sensor.capacity",
        "solar_forecast_factor": 0.5,
    }
    states = soc_states(solar_kw=4)
    states.update(
        {
            "sensor.cutoff": {"state": "5", "attributes": {"unit_of_measurement": "%"}},
            "number.backup": {"state": "20", "attributes": {"unit_of_measurement": "%"}},
            "sensor.grid": {"state": "On Grid", "attributes": {}},
            "sensor.capacity": {"state": "10000", "attributes": {"unit_of_measurement": "Wh"}},
        }
    )
    now = NOW.timestamp() * 1000
    result = build_forecast(states, settings, now, now + HOUR)
    assert result["value"] == pytest.approx(60)
    assert result["attributes"]["min_soc"] == 20
    assert result["attributes"]["min_soc_kind"] == "backup"
    states["sensor.grid"]["state"] = "Off Grid"
    result = build_forecast(states, settings, now, now + HOUR)
    assert result["attributes"]["min_soc"] == 5
    assert result["attributes"]["min_soc_kind"] == "discharge"


def test_unknown_interval_never_bridges_or_extrapolates() -> None:
    """Missing provider data stops the curve and fixed-lead state becomes unknown."""
    states = soc_states()
    states["sensor.solar_forecast"]["attributes"]["detailedForecast"][1]["pv_estimate"] = None
    now = NOW.timestamp() * 1000
    result = build_forecast(states, soc_settings(), now, now + 2 * HOUR)
    attrs = result["attributes"]
    assert attrs["forecast_complete"] is False
    assert attrs["forecast_issues"]
    assert datetime.fromisoformat(attrs["covered_until"]) == NOW + timedelta(minutes=30)
    assert result["value"] is None
    assert sample_soc([[now, 50]], now + HOUR) is None


async def test_coordinator_fixed_lead_sensor_contract_and_cleanup(hass: HomeAssistant) -> None:
    """SOC records the one-hour prediction rather than the nearest current point."""
    for entity, state in soc_states().items():
        hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, title="Battery SOC", data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = create_forecaster(hass, entry)
    assert isinstance(coordinator, BatterySocForecaster)
    with patch("custom_components.hafo.forecasters.battery_soc.dt_util.utcnow", return_value=NOW):
        coordinator.data = await coordinator._async_update_data()
    sensor = HafoForecastSensor(coordinator)
    assert sensor.native_value == pytest.approx(40)
    assert sensor.native_unit_of_measurement == "%"
    assert sensor.device_class == SensorDeviceClass.BATTERY
    assert sensor.state_class == SensorStateClass.MEASUREMENT
    assert sensor.extra_state_attributes["forecast"][0]["value"] == 50
    assert sensor.extra_state_attributes["forecast_type"] == "battery_soc"
    assert coordinator.update_interval == timedelta(minutes=5)
    coordinator.cleanup()
    coordinator.cleanup()


async def test_core_input_refreshes_but_live_load_does_not(hass: HomeAssistant) -> None:
    """Avoid rapid recorder churn from instantaneous load while reacting to SOC."""
    for entity, state in soc_states().items():
        hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = BatterySocForecaster(hass, entry)
    with patch.object(coordinator, "async_request_refresh", new_callable=AsyncMock) as refresh:
        hass.states.async_set("sensor.load", "2", {"unit_of_measurement": "kW"})
        await hass.async_block_till_done()
        refresh.assert_not_awaited()
        hass.states.async_set("sensor.battery_soc", "49", {"unit_of_measurement": "%"})
        await hass.async_block_till_done()
        refresh.assert_awaited_once()
        coordinator.cleanup()
        hass.states.async_set("sensor.battery_soc", "48", {"unit_of_measurement": "%"})
        await hass.async_block_till_done()
        refresh.assert_awaited_once()


async def test_stale_soc_does_not_create_a_fresh_prediction(hass: HomeAssistant) -> None:
    """A stalled numeric SOC is unknown, even with complete future forecasts."""
    reported = NOW - timedelta(hours=2)
    with freeze_time(reported):
        for entity, state in soc_states().items():
            hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = BatterySocForecaster(hass, entry)
    try:
        with freeze_time(NOW):
            result = await coordinator._async_update_data()
        assert result.predicted_value is None
        assert result.forecast == []
        assert result.attributes["forecast"] == []
        assert result.attributes["source_soc_stale"] is True
        assert result.attributes["source_soc_reported_at"] == reported.isoformat()
        assert result.attributes["source_soc_age_seconds"] == 7200
        assert "last reported 120.0 minutes ago" in result.attributes["forecast_issues"][0]
        assert result.attributes["prediction_time"] == (NOW + timedelta(hours=1)).isoformat()
    finally:
        coordinator.cleanup()


async def test_fresh_unchanged_soc_plateau_remains_usable(hass: HomeAssistant) -> None:
    """Repeated identical reports refresh last_reported, not last_updated."""
    reported = NOW - timedelta(hours=2)
    states = soc_states()
    states["sensor.battery_soc"]["state"] = "100"
    with freeze_time(reported):
        for entity, state in states.items():
            hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = BatterySocForecaster(hass, entry)
    try:
        with freeze_time(NOW):
            hass.states.async_set("sensor.battery_soc", "100", {"unit_of_measurement": "%"})
            source = hass.states.get("sensor.battery_soc")
            assert source is not None
            assert source.last_updated == reported
            assert source.last_reported == NOW
            result = await coordinator._async_update_data()
        assert result.predicted_value == pytest.approx(90)
        assert result.forecast[0].value == 100
        assert result.attributes["source_soc_stale"] is False
        assert result.attributes["source_soc_age_seconds"] == 0
        assert result.attributes["forecast_issues"] == []
    finally:
        coordinator.cleanup()


async def test_stale_soc_recovers_on_the_next_poll_after_an_identical_report(hass: HomeAssistant) -> None:
    """A source recovery does not require a changed SOC to resume predictions."""
    with freeze_time(NOW - timedelta(hours=2)):
        for entity, state in soc_states().items():
            hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = BatterySocForecaster(hass, entry)
    try:
        with freeze_time(NOW):
            stale = await coordinator._async_update_data()
            assert stale.predicted_value is None
            hass.states.async_set("sensor.battery_soc", "50", {"unit_of_measurement": "%"})
            recovered = await coordinator._async_update_data()
        assert recovered.predicted_value == pytest.approx(40)
        assert recovered.forecast[0].value == 50
        assert recovered.attributes["source_soc_stale"] is False
        assert recovered.attributes["source_soc_reported_at"] == NOW.isoformat()
        assert recovered.attributes["forecast_issues"] == []
    finally:
        coordinator.cleanup()


@pytest.mark.parametrize(("age_seconds", "stale"), [(600, False), (600.001, True)])
async def test_soc_freshness_boundary(hass: HomeAssistant, age_seconds: float, *, stale: bool) -> None:
    """Exactly ten minutes is accepted; older numeric input is not restamped."""
    with freeze_time(NOW - timedelta(seconds=age_seconds)):
        for entity, state in soc_states().items():
            hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = BatterySocForecaster(hass, entry)
    try:
        with freeze_time(NOW):
            result = await coordinator._async_update_data()
        assert result.attributes["source_soc_stale"] is stale
        assert (result.predicted_value is None) is stale
    finally:
        coordinator.cleanup()


@pytest.mark.parametrize("source_value", ["unknown", "unavailable", None])
async def test_unavailable_or_missing_soc_keeps_normal_issue_handling(
    hass: HomeAssistant, source_value: str | None
) -> None:
    """Missing/non-numeric readings remain ordinary missing-input failures."""
    with freeze_time(NOW - timedelta(hours=2)):
        for entity, state in soc_states().items():
            if entity == "sensor.battery_soc":
                if source_value is not None:
                    hass.states.async_set(entity, source_value, state["attributes"])
            else:
                hass.states.async_set(entity, state["state"], state["attributes"])
    entry = MockConfigEntry(domain=DOMAIN, data=soc_settings())
    entry.add_to_hass(hass)
    coordinator = BatterySocForecaster(hass, entry)
    try:
        with freeze_time(NOW):
            result = await coordinator._async_update_data()
        assert result.predicted_value is None
        assert result.forecast == []
        assert result.attributes["source_soc_stale"] is False
        assert result.attributes["forecast_issues"] == ["SOC forecast requires a current battery SOC from 0 to 100%."]
    finally:
        coordinator.cleanup()
