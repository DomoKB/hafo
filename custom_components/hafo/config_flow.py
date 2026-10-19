"""Config flow for Home Assistant Forecaster integration."""

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

from .battery_soc_config import soc_schema, validate_soc_input
from .battery_soc_migration import LEGACY_DOMAIN, MIGRATION_SOURCE
from .const import (
    CONF_DAY_GROUPING,
    CONF_FORECAST_DAYS,
    CONF_FORECAST_TYPE,
    CONF_HISTORY_DAYS,
    CONF_HISTORY_WEEKS,
    CONF_SOURCE_ENTITY,
    DEFAULT_DAY_GROUPING,
    DEFAULT_FORECAST_DAYS,
    DEFAULT_FORECAST_TYPE,
    DEFAULT_HISTORY_DAYS,
    DEFAULT_HISTORY_WEEKS,
    DOMAIN,
    FORECAST_TYPE_BATTERY_SOC,
    FORECAST_TYPE_HISTORICAL_SHIFT,
    FORECAST_TYPE_MULTIWEEK_AVERAGE,
)
from .forecasters.battery_soc_settings import SOC_DEFAULTS
from .forecasters.multiweek_profile import MIN_SAMPLES


def _forecast_type_selector(*, include_soc: bool = True) -> selector.Selector[selector.SelectSelectorConfig]:
    """Offer the same strategies for new and existing helpers."""
    return selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value=FORECAST_TYPE_HISTORICAL_SHIFT, label="Historical Shift"),
                selector.SelectOptionDict(value=FORECAST_TYPE_MULTIWEEK_AVERAGE, label="Multiweek Average"),
            ]
            + (
                [selector.SelectOptionDict(value=FORECAST_TYPE_BATTERY_SOC, label="Battery SOC")] if include_soc else []
            ),
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    )


def _multiweek_schema(settings: Mapping[str, Any]) -> vol.Schema:
    """Show only settings used by the multiweek method."""
    return vol.Schema(
        {
            vol.Optional(
                CONF_HISTORY_WEEKS, default=settings.get(CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=2, max=12, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="weeks"
                )
            ),
            vol.Optional(
                CONF_FORECAST_DAYS, default=settings.get(CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS)
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(
                    min=1, max=14, step=1, mode=selector.NumberSelectorMode.BOX, unit_of_measurement="days"
                )
            ),
            vol.Optional(
                CONF_DAY_GROUPING, default=settings.get(CONF_DAY_GROUPING, DEFAULT_DAY_GROUPING)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="weekday_weekend", label="Weekdays / weekends"),
                        selector.SelectOptionDict(value="weekday", label="Each weekday separately"),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _multiweek_errors(settings: Mapping[str, Any]) -> dict[str, str]:
    """Reject settings that the number selector's step does not enforce."""
    errors: dict[str, str] = {}
    whole_numbers: dict[str, int] = {}
    for field, default in (
        (CONF_HISTORY_WEEKS, DEFAULT_HISTORY_WEEKS),
        (CONF_FORECAST_DAYS, DEFAULT_FORECAST_DAYS),
    ):
        raw = settings.get(field, default)
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            errors[field] = "whole_number"
            continue
        if isinstance(raw, bool) or not number.is_integer():
            errors[field] = "whole_number"
        else:
            whole_numbers[field] = int(number)

    if (
        CONF_HISTORY_WEEKS in whole_numbers
        and whole_numbers[CONF_HISTORY_WEEKS] < MIN_SAMPLES
        and settings.get(CONF_DAY_GROUPING, DEFAULT_DAY_GROUPING) == "weekday"
    ):
        errors[CONF_HISTORY_WEEKS] = "weekday_history_too_short"
    return errors


class HafoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Home Assistant Forecaster."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize method-specific flow state."""
        super().__init__()
        self._multiweek_data: dict[str, Any] = {}
        self._multiweek_title = ""
        self._soc_data: dict[str, Any] = {}
        self._soc_title = ""
        self._soc_legacy_id: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "HafoOptionsFlow":  # noqa: ARG004
        """Get the options flow for this handler."""
        return HafoOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # Validate the source entity exists
            source_entity = user_input[CONF_SOURCE_ENTITY]
            if self.hass.states.get(source_entity) is None:
                errors[CONF_SOURCE_ENTITY] = "entity_not_found"
            else:
                # Create unique ID from source entity
                forecast_type = user_input.get(CONF_FORECAST_TYPE, DEFAULT_FORECAST_TYPE)
                if forecast_type == FORECAST_TYPE_BATTERY_SOC:
                    state = self.hass.states.get(source_entity)
                    if state is None or state.attributes.get("unit_of_measurement") != "%":
                        errors[CONF_SOURCE_ENTITY] = "soc_unit_required"
                    else:
                        await self.async_set_unique_id(f"{DOMAIN}_{source_entity}_battery_soc")
                        self._abort_if_unique_id_configured()
                        self._soc_data = {CONF_SOURCE_ENTITY: source_entity, CONF_FORECAST_TYPE: forecast_type}
                        self._soc_title = str(state.attributes.get("friendly_name", source_entity))
                        legacy = [
                            entry
                            for entry in self.hass.config_entries.async_entries(LEGACY_DOMAIN)
                            if {**entry.data, **entry.options}.get("soc_entity") == source_entity
                        ]
                        if len(legacy) == 1:
                            self._soc_legacy_id = legacy[0].entry_id
                            old = {**legacy[0].data, **legacy[0].options}
                            self._soc_data.update({key: old[key] for key in SOC_DEFAULTS if key in old})
                        return await self.async_step_battery_soc()
                else:
                    return await self._async_create_historical(user_input, source_entity, forecast_type)

        return self.async_show_form(step_id="user", data_schema=self._user_schema(), errors=errors)

    async def _async_create_historical(
        self, user_input: dict[str, Any], source_entity: str, forecast_type: str
    ) -> ConfigFlowResult:
        """Continue the existing history-based helper creation path."""
        suffix = "_multiweek_average" if forecast_type == FORECAST_TYPE_MULTIWEEK_AVERAGE else ""
        await self.async_set_unique_id(f"{DOMAIN}_{source_entity}{suffix}")
        self._abort_if_unique_id_configured()

        # Create a friendly title from the source entity
        state = self.hass.states.get(source_entity)
        friendly_name = state.attributes.get("friendly_name", source_entity) if state else source_entity
        title = friendly_name

        if forecast_type == FORECAST_TYPE_MULTIWEEK_AVERAGE:
            self._multiweek_data = {CONF_SOURCE_ENTITY: source_entity, CONF_FORECAST_TYPE: forecast_type}
            self._multiweek_title = f"{title} Multiweek Average"
            return await self.async_step_multiweek()

        return self.async_create_entry(
            title=title,
            data=user_input,
        )

    @staticmethod
    def _user_schema() -> vol.Schema:
        """Build the initial source/type form."""
        return vol.Schema(
            {
                vol.Required(CONF_SOURCE_ENTITY): selector.EntitySelector(
                    selector.EntitySelectorConfig(domain=["sensor", "input_number"])
                ),
                vol.Optional(
                    CONF_HISTORY_DAYS,
                    default=DEFAULT_HISTORY_DAYS,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=30,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
                vol.Optional(
                    CONF_FORECAST_TYPE,
                    default=DEFAULT_FORECAST_TYPE,
                ): _forecast_type_selector(),
            }
        )

    async def async_step_battery_soc(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure a separate SOC helper and optionally retain its old identity."""
        if not self._soc_data:
            return self.async_abort(reason="source_missing")
        settings = {
            **SOC_DEFAULTS,
            **self._soc_data,
            **(user_input or {}),
            CONF_SOURCE_ENTITY: self._soc_data[CONF_SOURCE_ENTITY],
            CONF_FORECAST_TYPE: FORECAST_TYPE_BATTERY_SOC,
        }
        errors = validate_soc_input(self.hass, settings) if user_input is not None else {}
        if user_input is not None and not errors:
            settings.pop("migrate_existing", None)
            if self._soc_legacy_id and user_input.get("migrate_existing", True):
                settings[MIGRATION_SOURCE] = self._soc_legacy_id
            return self.async_create_entry(title=self._soc_title, data=settings)
        return self.async_show_form(
            step_id="battery_soc",
            data_schema=soc_schema(settings, migration=bool(self._soc_legacy_id)),
            errors=errors,
        )

    async def async_step_multiweek(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the multiweek method without changing the legacy helper."""
        if not self._multiweek_data:
            return self.async_abort(reason="source_missing")
        errors = _multiweek_errors(user_input) if user_input is not None else {}
        if user_input is not None and not errors:
            return self.async_create_entry(
                title=self._multiweek_title,
                data={**self._multiweek_data, **user_input},
            )
        return self.async_show_form(step_id="multiweek", data_schema=_multiweek_schema(user_input or {}), errors=errors)


class HafoOptionsFlow(OptionsFlow):
    """Handle options flow for Home Assistant Forecaster."""

    _pending_forecast_type: str | None = None

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle options flow."""
        # Get current config
        entry = self.hass.config_entries.async_get_entry(self.handler)  # type: ignore[arg-type]
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        settings = {**entry.data, **entry.options}
        errors: dict[str, str] = {}
        forecast_type = self._pending_forecast_type or settings.get(CONF_FORECAST_TYPE, DEFAULT_FORECAST_TYPE)
        if forecast_type == FORECAST_TYPE_BATTERY_SOC:
            if user_input is not None:
                settings.update(user_input)
                errors = validate_soc_input(self.hass, settings)
                if settings.get(CONF_FORECAST_TYPE) != FORECAST_TYPE_BATTERY_SOC:
                    errors[CONF_FORECAST_TYPE] = "new_helper_required"
                if settings.get(CONF_SOURCE_ENTITY) != entry.data[CONF_SOURCE_ENTITY]:
                    errors[CONF_SOURCE_ENTITY] = "new_helper_required"
                if not errors:
                    return self.async_create_entry(title="", data={**entry.options, **user_input})
            return self.async_show_form(step_id="init", data_schema=soc_schema(settings), errors=errors)
        if user_input is not None:
            selected_type = user_input.get(CONF_FORECAST_TYPE, forecast_type)
            if selected_type == FORECAST_TYPE_BATTERY_SOC:
                return self.async_show_form(
                    step_id="init",
                    data_schema=_multiweek_schema(settings).extend(
                        {
                            vol.Optional(CONF_FORECAST_TYPE, default=forecast_type): _forecast_type_selector(
                                include_soc=False
                            )
                        }
                    ),
                    errors={CONF_FORECAST_TYPE: "new_helper_required"},
                )
            if selected_type != forecast_type:
                # Show the selected strategy's settings before saving or reloading.
                self._pending_forecast_type = forecast_type = selected_type
            else:
                settings.update(user_input)
                if forecast_type == FORECAST_TYPE_MULTIWEEK_AVERAGE:
                    errors = _multiweek_errors(settings)
                if not errors:
                    options = {**entry.options, **user_input}
                    if self._pending_forecast_type is not None:
                        options[CONF_FORECAST_TYPE] = self._pending_forecast_type
                    return self.async_create_entry(title="", data=options)

        if forecast_type == FORECAST_TYPE_MULTIWEEK_AVERAGE:
            schema = _multiweek_schema(settings).extend(
                {vol.Optional(CONF_FORECAST_TYPE, default=forecast_type): _forecast_type_selector(include_soc=False)}
            )
            return self.async_show_form(step_id="init", data_schema=schema, errors=errors)

        # Read from options first (previously saved), fall back to data (initial config)
        current_history_days = entry.options.get(
            CONF_HISTORY_DAYS, entry.data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        )

        schema = vol.Schema(
            {
                vol.Optional(CONF_FORECAST_TYPE, default=forecast_type): _forecast_type_selector(include_soc=False),
                vol.Optional(
                    CONF_HISTORY_DAYS,
                    default=current_history_days,
                ): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=1,
                        max=30,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="days",
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
        )
