"""Generic settings for a read-only battery energy-balance forecast."""

from collections.abc import Mapping
import math
from typing import Any

MODEL_VERSION = "self-consumption-calibrated-solar-v2"
SOC_DEFAULTS: dict[str, Any] = {
    "solar_entity": "",
    "solar_tomorrow_entity": "",
    "load_forecast_entity": "",
    "load_power_entity": "",
    "capacity_entity": "",
    "charge_power_entity": "",
    "discharge_power_entity": "",
    "min_soc_entity": "",
    "backup_soc_entity": "",
    "grid_status_entity": "",
    "min_soc": None,
    "max_soc": 100,
    "capacity_kwh": None,
    "max_charge_kw": None,
    "max_discharge_kw": None,
    "charge_efficiency": 0.95,
    "discharge_efficiency": 0.95,
    "solar_forecast_factor": 1.0,
    "discharge_overhead_kw": 0.0,
    "forecast_hours": 48,
}

# Numeric limits also validate configuration imported outside the native form.
SOC_NUMERIC_LIMITS = {
    "charge_efficiency": (0.01, 1),
    "discharge_efficiency": (0.01, 1),
    "solar_forecast_factor": (0, 2),
    "discharge_overhead_kw": (0, 5),
    "min_soc": (0, 100),
    "max_soc": (0, 100),
    "capacity_kwh": (0.001, 1_000_000),
    "max_charge_kw": (0, 1_000_000),
    "max_discharge_kw": (0, 1_000_000),
    "forecast_hours": (1, 48),
}
SOC_OPTIONAL_NUMBERS = {"min_soc", "capacity_kwh", "max_charge_kw", "max_discharge_kw"}
SOC_REQUIRED_ENTITIES = {"solar_entity", "load_forecast_entity"}


def soc_settings_errors(settings: Mapping[str, Any]) -> dict[str, str]:
    """Validate finite numbers, reserve bounds and required energy inputs."""
    merged = {**SOC_DEFAULTS, **settings}
    errors: dict[str, str] = {}
    for key, (minimum, maximum) in SOC_NUMERIC_LIMITS.items():
        value = merged[key]
        if value is None and key in SOC_OPTIONAL_NUMBERS:
            continue
        try:
            numeric = float(value)
        except (TypeError, ValueError, OverflowError):
            errors[key] = "invalid_number"
            continue
        if isinstance(value, bool) or not math.isfinite(numeric) or not minimum <= numeric <= maximum:
            errors[key] = "invalid_number"
        elif key == "forecast_hours" and not numeric.is_integer():
            errors[key] = "whole_number"
    if (
        not errors.get("min_soc")
        and not errors.get("max_soc")
        and merged["min_soc"] is not None
        and float(merged["min_soc"]) > float(merged["max_soc"])
    ):
        errors["min_soc"] = "invalid_reserve"
    for key in SOC_REQUIRED_ENTITIES:
        if not merged.get(key):
            errors[key] = "entity_required"
    for value_key, entity_key in (
        ("capacity_kwh", "capacity_entity"),
        ("max_charge_kw", "charge_power_entity"),
        ("max_discharge_kw", "discharge_power_entity"),
    ):
        if merged[value_key] is None and not merged[entity_key]:
            errors[entity_key] = "entity_or_value_required"
    return errors
