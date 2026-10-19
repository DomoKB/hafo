"""Native form and validation for a generic battery SOC helper."""

from collections.abc import Mapping
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
import voluptuous as vol

from .const import CONF_SOURCE_ENTITY
from .forecasters.battery_soc_settings import (
    SOC_DEFAULTS,
    SOC_NUMERIC_LIMITS,
    SOC_OPTIONAL_NUMBERS,
    SOC_REQUIRED_ENTITIES,
    soc_settings_errors,
)


def soc_schema(settings: Mapping[str, Any], *, migration: bool = False) -> vol.Schema:
    """Show entity mappings and optional native-rating overrides."""
    values = {**SOC_DEFAULTS, **settings}
    fields: dict[Any, Any] = {}
    for key in SOC_DEFAULTS:
        if not key.endswith("_entity"):
            continue
        marker = vol.Required if key in SOC_REQUIRED_ENTITIES else vol.Optional
        field = marker(key, default=values[key]) if values.get(key) else marker(key)
        fields[field] = selector.EntitySelector()
    for key, (minimum, maximum) in SOC_NUMERIC_LIMITS.items():
        marker = vol.Optional if key in SOC_OPTIONAL_NUMBERS else vol.Required
        field = marker(key, default=values[key]) if values.get(key) is not None else marker(key)
        fields[field] = selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=minimum,
                max=maximum,
                step=1 if key == "forecast_hours" else 0.001,
                mode=selector.NumberSelectorMode.BOX,
            )
        )
    if migration:
        fields[vol.Required("migrate_existing", default=True)] = selector.BooleanSelector()
    return vol.Schema(fields)


def validate_soc_input(hass: HomeAssistant, settings: Mapping[str, Any]) -> dict[str, str]:
    """Reject invalid mappings/settings before an entry or options are saved."""
    errors = soc_settings_errors(settings)
    source = hass.states.get(settings.get(CONF_SOURCE_ENTITY, ""))
    if source is None:
        errors[CONF_SOURCE_ENTITY] = "entity_not_found"
    elif source.attributes.get("unit_of_measurement") != "%":
        errors[CONF_SOURCE_ENTITY] = "soc_unit_required"
    elif source.attributes.get("forecast_type") == "battery_soc" or source.attributes.get("forecast_lead_hours"):
        errors[CONF_SOURCE_ENTITY] = "measured_soc_required"
    for key, entity in settings.items():
        if key.endswith("_entity") and entity and hass.states.get(entity) is None:
            errors[key] = "entity_not_found"
    return errors
