"""Native forms, mappings and settings safeguards for Battery SOC helpers."""

from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hafo.battery_soc_config import soc_schema, validate_soc_input
from custom_components.hafo.battery_soc_migration import MIGRATION_SOURCE
from custom_components.hafo.config_flow import HafoConfigFlow, HafoOptionsFlow
from custom_components.hafo.const import DOMAIN
from custom_components.hafo.forecasters.battery_soc_settings import soc_settings_errors


def settings() -> dict[str, Any]:
    """Use generic sources and explicit ratings, with no vendor-specific defaults."""
    return {
        "source_entity": "sensor.soc",
        "forecast_type": "battery_soc",
        "solar_entity": "sensor.solar",
        "load_forecast_entity": "sensor.load_forecast",
        "capacity_kwh": 10,
        "max_charge_kw": 5,
        "max_discharge_kw": 5,
    }


def populate_sources(hass: HomeAssistant) -> None:
    """Populate the measured percent and forecast entities needed by the form."""
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%", "friendly_name": "Battery"})
    hass.states.async_set("sensor.solar", "10")
    hass.states.async_set("sensor.load_forecast", "1", {"unit_of_measurement": "kW"})


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"charge_efficiency": 0}, {"charge_efficiency": "invalid_number"}),
        ({"discharge_efficiency": 1.1}, {"discharge_efficiency": "invalid_number"}),
        ({"discharge_overhead_kw": -0.1}, {"discharge_overhead_kw": "invalid_number"}),
        ({"capacity_kwh": 0}, {"capacity_kwh": "invalid_number"}),
        ({"capacity_kwh": float("nan")}, {"capacity_kwh": "invalid_number"}),
        ({"capacity_kwh": True}, {"capacity_kwh": "invalid_number"}),
        ({"max_charge_kw": float("inf")}, {"max_charge_kw": "invalid_number"}),
        ({"forecast_hours": 2.5}, {"forecast_hours": "whole_number"}),
        ({"min_soc": 60, "max_soc": 50}, {"min_soc": "invalid_reserve"}),
        ({"solar_forecast_factor": 2.1}, {"solar_forecast_factor": "invalid_number"}),
        ({"solar_entity": ""}, {"solar_entity": "entity_required"}),
        ({"capacity_kwh": None}, {"capacity_entity": "entity_or_value_required"}),
    ],
)
def test_bad_settings_rejected(change: dict[str, Any], error: dict[str, str]) -> None:
    """Invalid saved settings are rejected before construction or option reload."""
    assert soc_settings_errors({**settings(), **change}) == error


async def test_mapping_validation_requires_measured_percent(hass: HomeAssistant) -> None:
    """A forecast helper or wrong-unit source cannot stand in for measured SOC."""
    populate_sources(hass)
    assert validate_soc_input(hass, settings()) == {}
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "kWh"})
    assert validate_soc_input(hass, settings()) == {"source_entity": "soc_unit_required"}
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%", "forecast_lead_hours": 1})
    assert validate_soc_input(hass, settings()) == {"source_entity": "measured_soc_required"}
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
    assert validate_soc_input(hass, {**settings(), "solar_entity": "sensor.missing"}) == {
        "solar_entity": "entity_not_found"
    }


async def test_creation_is_separate_strategy_with_validated_second_step(hass: HomeAssistant) -> None:
    """New SOC helper uses its own identity and does not replace a load helper."""
    populate_sources(hass)
    flow = HafoConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    with (
        patch.object(flow, "async_set_unique_id", return_value=None) as unique,
        patch.object(flow, "_abort_if_unique_id_configured", return_value=None),
    ):
        first = await flow.async_step_user({"source_entity": "sensor.soc", "forecast_type": "battery_soc"})
    assert first.get("step_id") == "battery_soc"
    unique.assert_awaited_once_with("hafo_sensor.soc_battery_soc")
    invalid = await flow.async_step_battery_soc({**settings(), "charge_efficiency": 0})
    assert invalid.get("type") == FlowResultType.FORM
    assert invalid.get("errors") == {"charge_efficiency": "invalid_number"}
    valid = await flow.async_step_battery_soc(settings())
    assert valid.get("type") == FlowResultType.CREATE_ENTRY
    data = valid.get("data")
    assert data is not None
    assert data["source_entity"] == "sensor.soc"
    assert data["forecast_type"] == "battery_soc"
    assert valid.get("title") == "Battery"


@pytest.mark.parametrize("migrate", [True, False])
async def test_legacy_settings_prefilled_and_migration_explicit(hass: HomeAssistant, *, migrate: bool) -> None:
    """The one matching companion can donate identity/history and calibration."""
    populate_sources(hass)
    legacy = MockConfigEntry(
        domain="sigen_soc_forecast",
        data={
            **settings(),
            "soc_entity": "sensor.soc",
            "solar_forecast_factor": 0.86,
        },
        options={"charge_efficiency": 0.98},
    )
    legacy.add_to_hass(hass)
    flow = HafoConfigFlow()
    flow.hass = hass
    flow.context = {"source": "user"}
    with (
        patch.object(flow, "async_set_unique_id", return_value=None),
        patch.object(flow, "_abort_if_unique_id_configured", return_value=None),
    ):
        first = await flow.async_step_user({"source_entity": "sensor.soc", "forecast_type": "battery_soc"})
    schema = first.get("data_schema")
    assert schema is not None
    prefilled = schema({"migrate_existing": migrate})
    assert prefilled["solar_forecast_factor"] == 0.86
    assert prefilled["charge_efficiency"] == 0.98
    result = await flow.async_step_battery_soc(prefilled)
    data = result.get("data")
    assert data is not None
    assert data.get(MIGRATION_SOURCE) == (legacy.entry_id if migrate else None)
    assert "migrate_existing" not in data


async def test_options_preserve_source_type_and_previous_calibration(hass: HomeAssistant) -> None:
    """A partial valid options edit preserves settings; type/source changes fail."""
    populate_sources(hass)
    entry = MockConfigEntry(domain=DOMAIN, data=settings(), options={"solar_forecast_factor": 0.86})
    entry.add_to_hass(hass)
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    for change, field in [
        ({"forecast_type": "historical_shift"}, "forecast_type"),
        ({"source_entity": "sensor.another"}, "source_entity"),
    ]:
        result = await flow.async_step_init(change)
        assert result.get("type") == FlowResultType.FORM
        errors = result.get("errors")
        assert errors is not None
        assert errors.get(field) == "new_helper_required"
        assert entry.options == {"solar_forecast_factor": 0.86}
    result = await flow.async_step_init({"charge_efficiency": 0.98})
    assert result.get("data") == {"solar_forecast_factor": 0.86, "charge_efficiency": 0.98}


async def test_load_helper_cannot_be_switched_into_soc(hass: HomeAssistant) -> None:
    """Prevent changing the recorded sensor unit from power into percent."""
    entry = MockConfigEntry(domain=DOMAIN, data={"source_entity": "sensor.load", "forecast_type": "historical_shift"})
    entry.add_to_hass(hass)
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init({"forecast_type": "battery_soc"})
    assert result.get("errors") == {"forecast_type": "new_helper_required"}
    assert entry.options == {}


def test_optional_mapping_has_no_invalid_empty_selector_default() -> None:
    """Optional native mappings may be omitted rather than submitting empty IDs."""
    schema = soc_schema(settings())
    data = schema({})
    assert "backup_soc_entity" not in data
    assert data["capacity_kwh"] == 10
    assert data["solar_entity"] == "sensor.solar"
