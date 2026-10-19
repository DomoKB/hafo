"""Config flow for Home Assistant Forecaster integration."""

from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector
import voluptuous as vol

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
    FORECAST_TYPE_HISTORICAL_SHIFT,
    FORECAST_TYPE_MULTIWEEK_AVERAGE,
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


class HafoConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Home Assistant Forecaster."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize method-specific flow state."""
        super().__init__()
        self._multiweek_data: dict[str, Any] = {}
        self._multiweek_title = ""

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

        # Build the schema for user input
        schema = vol.Schema(
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
                ): selector.SelectSelector(
                    selector.SelectSelectorConfig(
                        options=[
                            selector.SelectOptionDict(
                                value=FORECAST_TYPE_HISTORICAL_SHIFT,
                                label="Historical Shift",
                            ),
                            selector.SelectOptionDict(
                                value=FORECAST_TYPE_MULTIWEEK_AVERAGE,
                                label="Multiweek Average",
                            ),
                        ],
                        mode=selector.SelectSelectorMode.DROPDOWN,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
        )

    async def async_step_multiweek(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Configure the multiweek method without changing the legacy helper."""
        if not self._multiweek_data:
            return self.async_abort(reason="source_missing")
        if user_input is not None:
            return self.async_create_entry(
                title=self._multiweek_title,
                data={**self._multiweek_data, **user_input},
            )
        return self.async_show_form(step_id="multiweek", data_schema=_multiweek_schema({}))


class HafoOptionsFlow(OptionsFlow):
    """Handle options flow for Home Assistant Forecaster."""

    async def async_step_init(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle options flow."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Get current config
        entry = self.hass.config_entries.async_get_entry(self.handler)  # type: ignore[arg-type]
        if entry is None:
            return self.async_abort(reason="entry_not_found")

        if entry.data.get(CONF_FORECAST_TYPE) == FORECAST_TYPE_MULTIWEEK_AVERAGE:
            return self.async_show_form(step_id="init", data_schema=_multiweek_schema({**entry.data, **entry.options}))

        # Read from options first (previously saved), fall back to data (initial config)
        current_history_days = entry.options.get(
            CONF_HISTORY_DAYS, entry.data.get(CONF_HISTORY_DAYS, DEFAULT_HISTORY_DAYS)
        )

        schema = vol.Schema(
            {
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
