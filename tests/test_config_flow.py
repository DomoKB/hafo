"""Tests for the HAFO config flow."""

from typing import Any
from unittest.mock import patch

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hafo.config_flow import HafoConfigFlow, HafoOptionsFlow
from custom_components.hafo.const import (
    CONF_FORECAST_TYPE,
    CONF_HISTORY_DAYS,
    CONF_SOURCE_ENTITY,
    DEFAULT_FORECAST_TYPE,
    DEFAULT_HISTORY_DAYS,
    DOMAIN,
    FORECAST_TYPE_HISTORICAL_SHIFT,
)
from custom_components.hafo.coordinator import create_forecaster


@pytest.fixture
def config_flow(hass: HomeAssistant) -> HafoConfigFlow:
    """Create a config flow for testing."""
    flow = HafoConfigFlow()
    flow.hass = hass
    # Initialize the context dict that HA expects
    flow.context = {"source": "user"}
    return flow


async def test_user_flow_shows_form(hass: HomeAssistant, config_flow: HafoConfigFlow) -> None:
    """Test that the user flow shows the form initially."""
    result = await config_flow.async_step_user(user_input=None)

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "user"


async def test_user_flow_creates_entry(hass: HomeAssistant, config_flow: HafoConfigFlow) -> None:
    """Test that the user flow creates a config entry."""
    # Set up a test entity
    hass.states.async_set("sensor.test_power", "100.0", {"friendly_name": "Test Power"})

    # Mock async_set_unique_id since it requires more context
    with (
        patch.object(config_flow, "async_set_unique_id", return_value=None),
        patch.object(config_flow, "_abort_if_unique_id_configured", return_value=None),
    ):
        result = await config_flow.async_step_user(
            user_input={
                CONF_SOURCE_ENTITY: "sensor.test_power",
                CONF_HISTORY_DAYS: 7,
                CONF_FORECAST_TYPE: DEFAULT_FORECAST_TYPE,
            },
        )

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert result.get("title") == "Test Power"
    data = dict(result.get("data", {}))
    assert data[CONF_SOURCE_ENTITY] == "sensor.test_power"
    assert data[CONF_HISTORY_DAYS] == 7


async def test_user_flow_entity_not_found(hass: HomeAssistant, config_flow: HafoConfigFlow) -> None:
    """Test that the user flow shows error for missing entity."""
    # Submit with a non-existent entity
    result = await config_flow.async_step_user(
        user_input={
            CONF_SOURCE_ENTITY: "sensor.nonexistent",
            CONF_HISTORY_DAYS: DEFAULT_HISTORY_DAYS,
            CONF_FORECAST_TYPE: DEFAULT_FORECAST_TYPE,
        },
    )

    assert result.get("type") == FlowResultType.FORM
    assert result.get("errors") == {CONF_SOURCE_ENTITY: "entity_not_found"}


def test_async_get_options_flow_returns_options_flow() -> None:
    """async_get_options_flow returns HafoOptionsFlow instance."""
    entry = MockConfigEntry(domain=DOMAIN, entry_id="test_id")
    flow = HafoConfigFlow.async_get_options_flow(entry)
    assert isinstance(flow, HafoOptionsFlow)


async def test_options_flow_show_form(hass: HomeAssistant) -> None:
    """Options flow shows form with current values."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Forecast",
        data={
            CONF_SOURCE_ENTITY: "sensor.test_power",
            CONF_HISTORY_DAYS: 7,
            CONF_FORECAST_TYPE: FORECAST_TYPE_HISTORICAL_SHIFT,
        },
        entry_id="options_test_id",
    )
    entry.add_to_hass(hass)

    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id

    result = await flow.async_step_init(user_input=None)

    assert result.get("type") == FlowResultType.FORM
    assert result.get("step_id") == "init"
    assert "data_schema" in result


async def test_options_flow_submit_updates_options(hass: HomeAssistant) -> None:
    """Options flow submit creates entry with new options."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Forecast",
        data={
            CONF_SOURCE_ENTITY: "sensor.test_power",
            CONF_HISTORY_DAYS: 7,
            CONF_FORECAST_TYPE: FORECAST_TYPE_HISTORICAL_SHIFT,
        },
        entry_id="options_test_id",
    )
    entry.add_to_hass(hass)

    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id

    result = await flow.async_step_init(user_input={CONF_HISTORY_DAYS: 14})

    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert result.get("data") == {CONF_HISTORY_DAYS: 14}


async def test_options_flow_aborts_when_entry_not_found(hass: HomeAssistant) -> None:
    """Options flow aborts when config entry is missing."""
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = "nonexistent_entry_id"

    result = await flow.async_step_init(user_input=None)

    assert result.get("type") == FlowResultType.ABORT
    assert result.get("reason") == "entry_not_found"


async def test_forecaster_uses_updated_options(hass: HomeAssistant) -> None:
    """Test that forecaster uses history_days from options after update.

    When a user edits the integration options, the new values are stored
    in entry.options. The forecaster should read from options first,
    falling back to data for initial values.
    """
    # Set up a test entity
    hass.states.async_set("sensor.test_power", "100.0", {"unit_of_measurement": "W"})

    # Create entry with initial history_days=7 in data
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Test Forecast",
        data={
            CONF_SOURCE_ENTITY: "sensor.test_power",
            CONF_HISTORY_DAYS: 7,
            CONF_FORECAST_TYPE: FORECAST_TYPE_HISTORICAL_SHIFT,
        },
        entry_id="test_entry_id",
    )
    entry.add_to_hass(hass)

    # Simulate options update: user changed history_days to 14
    hass.config_entries.async_update_entry(entry, options={CONF_HISTORY_DAYS: 14})

    # Create forecaster (as would happen during reload after options update)
    forecaster = create_forecaster(hass, entry)

    # Forecaster should use the updated value from options, not the original from data
    assert forecaster.history_days == 14, (
        f"Expected history_days=14 from options, got {forecaster.history_days}. "
        "Forecaster is not reading from entry.options."
    )


async def test_multiweek_flow_creates_separate_helper(hass: HomeAssistant, config_flow: HafoConfigFlow) -> None:
    """Multiweek settings have their own step and cannot replace the legacy helper."""
    hass.states.async_set("sensor.test_power", "1", {"friendly_name": "Test Power"})
    with (
        patch.object(config_flow, "async_set_unique_id", return_value=None) as set_id,
        patch.object(config_flow, "_abort_if_unique_id_configured", return_value=None),
    ):
        first = await config_flow.async_step_user(
            {CONF_SOURCE_ENTITY: "sensor.test_power", CONF_FORECAST_TYPE: "multiweek_average"}
        )
    assert first.get("step_id") == "multiweek"
    set_id.assert_awaited_once_with("hafo_sensor.test_power_multiweek_average")
    result = await config_flow.async_step_multiweek(
        {"history_weeks": 4, "forecast_days": 7, "day_grouping": "weekday_weekend"}
    )
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert result.get("title") == "Test Power Multiweek Average"
    assert result.get("data") == {
        CONF_SOURCE_ENTITY: "sensor.test_power",
        CONF_FORECAST_TYPE: "multiweek_average",
        "history_weeks": 4,
        "forecast_days": 7,
        "day_grouping": "weekday_weekend",
    }


async def test_multiweek_options_preserve_method_and_source(hass: HomeAssistant) -> None:
    """Editing multiweek options keeps the helper identity/source unchanged."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Average",
        data={
            CONF_SOURCE_ENTITY: "sensor.test_power",
            CONF_FORECAST_TYPE: "multiweek_average",
            "history_weeks": 4,
        },
    )
    entry.add_to_hass(hass)
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    form = await flow.async_step_init()
    assert form.get("type") == FlowResultType.FORM
    result = await flow.async_step_init({"history_weeks": 6, "forecast_days": 2, "day_grouping": "weekday"})
    assert result.get("data") == {"history_weeks": 6, "forecast_days": 2, "day_grouping": "weekday"}
    assert entry.data[CONF_SOURCE_ENTITY] == "sensor.test_power"


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({"history_weeks": 2.5}, {"history_weeks": "whole_number"}),
        ({"forecast_days": 7.5}, {"forecast_days": "whole_number"}),
        (
            {"history_weeks": 2.5, "forecast_days": 7.5},
            {"history_weeks": "whole_number", "forecast_days": "whole_number"},
        ),
        (
            {"history_weeks": 2, "day_grouping": "weekday"},
            {"history_weeks": "weekday_history_too_short"},
        ),
    ],
)
async def test_multiweek_creation_rejects_invalid_settings_before_saving(
    hass: HomeAssistant, config_flow: HafoConfigFlow, changes: dict[str, Any], errors: dict[str, str]
) -> None:
    """Validate actual selector output and keep invalid fields available to correct."""
    hass.states.async_set("sensor.test_power", "1")
    with (
        patch.object(config_flow, "async_set_unique_id", return_value=None),
        patch.object(config_flow, "_abort_if_unique_id_configured", return_value=None),
    ):
        form = await config_flow.async_step_user(
            {CONF_SOURCE_ENTITY: "sensor.test_power", CONF_FORECAST_TYPE: "multiweek_average"}
        )
    schema = form.get("data_schema")
    assert schema is not None
    valid = {"history_weeks": 4.0, "forecast_days": 7.0, "day_grouping": "weekday_weekend"}
    # HA NumberSelector accepts fractions even though its step is 1.
    submitted = schema({**valid, **changes})
    result = await config_flow.async_step_multiweek(submitted)
    assert result.get("type") == FlowResultType.FORM
    assert result.get("errors") == errors
    retry_schema = result.get("data_schema")
    assert retry_schema is not None
    assert retry_schema({}) == submitted

    saved = await config_flow.async_step_multiweek(retry_schema(valid))
    assert saved.get("type") == FlowResultType.CREATE_ENTRY
    data = saved.get("data")
    assert data is not None
    forecaster = create_forecaster(hass, MockConfigEntry(domain=DOMAIN, data=data))
    assert forecaster.history_days == 28


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({"history_weeks": 2.5}, {"history_weeks": "whole_number"}),
        ({"forecast_days": 7.5}, {"forecast_days": "whole_number"}),
        (
            {"history_weeks": 2.5, "forecast_days": 7.5},
            {"history_weeks": "whole_number", "forecast_days": "whole_number"},
        ),
        (
            {"history_weeks": 2, "day_grouping": "weekday"},
            {"history_weeks": "weekday_history_too_short"},
        ),
    ],
)
async def test_multiweek_options_reject_invalid_settings_without_changing_working_helper(
    hass: HomeAssistant, changes: dict[str, Any], errors: dict[str, str]
) -> None:
    """An invalid edit cannot replace the last working options or helper identity."""
    valid = {"history_weeks": 4.0, "forecast_days": 7.0, "day_grouping": "weekday_weekend"}
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="hafo_sensor.test_power",
        data={CONF_SOURCE_ENTITY: "sensor.test_power", CONF_FORECAST_TYPE: "multiweek_average"},
        options=valid,
    )
    entry.add_to_hass(hass)
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    form = await flow.async_step_init()
    schema = form.get("data_schema")
    assert schema is not None
    submitted = schema({**valid, **changes})
    result = await flow.async_step_init(submitted)
    assert result.get("type") == FlowResultType.FORM
    assert result.get("errors") == errors
    retry_schema = result.get("data_schema")
    assert retry_schema is not None
    assert retry_schema({}) == submitted
    assert entry.options == valid
    assert entry.unique_id == "hafo_sensor.test_power"
    assert entry.data[CONF_SOURCE_ENTITY] == "sensor.test_power"
    assert create_forecaster(hass, entry).history_days == 28

    saved = await flow.async_step_init(retry_schema(valid))
    assert saved.get("type") == FlowResultType.CREATE_ENTRY
    options = saved.get("data")
    assert options is not None
    hass.config_entries.async_update_entry(entry, options=options)
    assert create_forecaster(hass, entry).history_days == 28


async def test_multiweek_options_validate_grouping_against_saved_history(hass: HomeAssistant) -> None:
    """A grouping-only edit must consider the existing two-week setting."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOURCE_ENTITY: "sensor.test_power", CONF_FORECAST_TYPE: "multiweek_average"},
        options={"history_weeks": 2.0, "forecast_days": 7.0, "day_grouping": "weekday_weekend"},
    )
    entry.add_to_hass(hass)
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    result = await flow.async_step_init({"day_grouping": "weekday"})
    assert result.get("type") == FlowResultType.FORM
    assert result.get("errors") == {"history_weeks": "weekday_history_too_short"}
    assert entry.options["day_grouping"] == "weekday_weekend"


@pytest.mark.parametrize(
    "settings",
    [
        {"history_weeks": 2.0, "forecast_days": 14.0, "day_grouping": "weekday_weekend"},
        {"history_weeks": 3.0, "forecast_days": 14.0, "day_grouping": "weekday"},
    ],
)
async def test_multiweek_options_accept_valid_boundary_settings(hass: HomeAssistant, settings: dict[str, Any]) -> None:
    """Whole-valued selector floats and valid grouping minimums still save."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={CONF_SOURCE_ENTITY: "sensor.test_power", CONF_FORECAST_TYPE: "multiweek_average"},
    )
    entry.add_to_hass(hass)
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    form = await flow.async_step_init()
    schema = form.get("data_schema")
    assert schema is not None
    result = await flow.async_step_init(schema(settings))
    assert result.get("type") == FlowResultType.CREATE_ENTRY
    assert result.get("data") == {**settings, CONF_FORECAST_TYPE: "multiweek_average"}


async def test_multiweek_step_requires_selected_source(config_flow: HafoConfigFlow) -> None:
    """An out-of-order flow has no source and cannot create a broken helper."""
    result = await config_flow.async_step_multiweek()
    assert result.get("reason") == "source_missing"


async def test_existing_helper_can_switch_strategy_without_changing_identity(hass: HomeAssistant) -> None:
    """Switching is staged until new settings are saved; entity identity is stable."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Existing Load",
        unique_id="hafo_sensor.test_power",
        data={
            CONF_SOURCE_ENTITY: "sensor.test_power",
            CONF_FORECAST_TYPE: FORECAST_TYPE_HISTORICAL_SHIFT,
            CONF_HISTORY_DAYS: 7,
        },
        options={CONF_HISTORY_DAYS: 14},
    )
    entry.add_to_hass(hass)
    original_id = entry.entry_id
    flow = HafoOptionsFlow()
    flow.hass = hass
    flow.handler = entry.entry_id
    pending = await flow.async_step_init({CONF_FORECAST_TYPE: "multiweek_average", CONF_HISTORY_DAYS: 14})
    assert pending.get("type") == FlowResultType.FORM
    pending_schema = pending.get("data_schema")
    assert pending_schema is not None
    assert "history_weeks" in pending_schema.schema
    assert entry.options == {CONF_HISTORY_DAYS: 14}
    invalid = await flow.async_step_init({"history_weeks": 2.0, "forecast_days": 7.0, "day_grouping": "weekday"})
    assert invalid.get("type") == FlowResultType.FORM
    assert invalid.get("errors") == {"history_weeks": "weekday_history_too_short"}
    assert entry.options == {CONF_HISTORY_DAYS: 14}
    assert type(create_forecaster(hass, entry)).__name__ == "HistoricalShiftForecaster"
    settings = {"history_weeks": 4, "forecast_days": 7, "day_grouping": "weekday_weekend"}
    saved = await flow.async_step_init(settings)
    assert saved.get("type") == FlowResultType.CREATE_ENTRY
    saved_options = saved.get("data")
    assert saved_options is not None
    hass.config_entries.async_update_entry(entry, options=saved_options)
    forecaster = create_forecaster(hass, entry)
    assert type(forecaster).__name__ == "MultiweekAverageForecaster"
    assert forecaster.history_days == 28
    assert entry.entry_id == original_id
    assert entry.unique_id == "hafo_sensor.test_power"
    assert entry.title == "Existing Load"
    assert entry.data[CONF_SOURCE_ENTITY] == "sensor.test_power"

    # A fresh options flow recognises the override and can restore the old strategy.
    back = HafoOptionsFlow()
    back.hass = hass
    back.handler = entry.entry_id
    reopened = await back.async_step_init()
    reopened_schema = reopened.get("data_schema")
    assert reopened_schema is not None
    assert "history_weeks" in reopened_schema.schema
    pending_back = await back.async_step_init({CONF_FORECAST_TYPE: FORECAST_TYPE_HISTORICAL_SHIFT})
    assert pending_back.get("type") == FlowResultType.FORM
    back_schema = pending_back.get("data_schema")
    assert back_schema is not None
    assert CONF_HISTORY_DAYS in back_schema.schema
    restored = await back.async_step_init({CONF_HISTORY_DAYS: 14})
    restored_options = restored.get("data")
    assert restored_options is not None
    hass.config_entries.async_update_entry(entry, options=restored_options)
    assert type(create_forecaster(hass, entry)).__name__ == "HistoricalShiftForecaster"
    assert create_forecaster(hass, entry).history_days == 14
    assert entry.entry_id == original_id
