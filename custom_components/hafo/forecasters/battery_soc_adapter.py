"""Translate native HA source states into the shared SOC model's units."""

from collections.abc import Mapping
from datetime import UTC, datetime
import math
from typing import Any

from .battery_soc_model import FULL_SOC, HOUR_MS, estimate_soc, sample_soc
from .battery_soc_settings import MODEL_VERSION, SOC_NUMERIC_LIMITS
from .battery_soc_settings import SOC_DEFAULTS as DEFAULTS


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _timestamp(value: Any) -> float | None:
    try:
        date = datetime.fromisoformat(value) if isinstance(value, str) else value
        return date.timestamp() * 1000 if isinstance(date, datetime) and date.tzinfo is not None else None
    except (TypeError, ValueError, OverflowError):
        return None


def _state(states: Mapping[str, Any], entity: str) -> tuple[Any, Mapping[str, Any]]:
    state = states.get(entity)
    if isinstance(state, dict):
        return state.get("state"), state.get("attributes", {})
    return (state.state, state.attributes) if state is not None else (None, {})


def _reading(states: Mapping[str, Any], entity: str, unit: str) -> float | None:
    value, attributes = _state(states, entity)
    factor = {"kWh": {"Wh": 0.001, "kWh": 1, "MWh": 1000}, "kW": {"W": 0.001, "kW": 1, "MW": 1000}, "%": {"%": 1}}[
        unit
    ].get(str(attributes.get("unit_of_measurement", "")))
    number = _number(value)
    return number * factor if number is not None and factor is not None else None


def _iso(time: float) -> str:
    return datetime.fromtimestamp(time / 1000, tz=UTC).isoformat()


def build_forecast(states: Mapping[str, Any], config: Mapping[str, Any], now: float, end: float) -> dict[str, Any]:
    """Build a fixed-lead scalar plus full forward curve, without cloud calls."""
    settings = {**DEFAULTS, **config}
    settings["soc_entity"] = config.get("source_entity", config.get("soc_entity", ""))
    solar_factor = _number(settings["solar_forecast_factor"])
    valid_solar_factor = (
        solar_factor is not None
        and SOC_NUMERIC_LIMITS["solar_forecast_factor"][0]
        <= solar_factor
        <= SOC_NUMERIC_LIMITS["solar_forecast_factor"][1]
    )

    def percent(key: str) -> float | None:
        return _reading(states, settings[key], "%")

    initial = percent("soc_entity")
    cutoff, backup = percent("min_soc_entity"), percent("backup_soc_entity")
    cutoff = cutoff if cutoff is not None and 0 <= cutoff <= FULL_SOC else None
    backup = backup if backup is not None and 0 <= backup <= FULL_SOC else None
    grid_label = " ".join(
        str(_state(states, settings["grid_status_entity"])[0] or "").lower().replace("_", " ").replace("-", " ").split()
    )
    grid_mode = (
        "off-grid"
        if grid_label in ("off grid", "grid disconnected", "disconnected")
        else "on-grid"
        if grid_label in ("on grid", "grid connected", "connected")
        else "unknown"
    )
    use_backup = grid_mode != "off-grid" and backup is not None and (cutoff is None or backup >= cutoff)
    floor = backup if use_backup else cutoff if cutoff is not None else 0
    floor_source = (
        settings["backup_soc_entity"]
        if use_backup
        else settings["min_soc_entity"]
        if cutoff is not None
        else "fallback"
    )
    floor_kind = "backup" if use_backup else "discharge" if cutoff is not None else "fallback"
    notes = []
    if settings["min_soc"] is not None:
        floor, floor_source, floor_kind = _number(settings["min_soc"]), "configured", "configured"
    else:
        if grid_mode == "unknown" and backup is not None:
            notes.append("Grid status unavailable; assumes on-grid operation.")
        if grid_mode != "off-grid" and backup is None:
            notes.append("Backup SOC unavailable; using the discharge cut-off or zero fallback.")
        if grid_mode == "off-grid":
            notes.append("Off-grid: backup reserve is available for household use.")

    def configured(key: str, entity_key: str, unit: str) -> float | None:
        return _number(settings[key]) if settings[key] is not None else _reading(states, settings[entity_key], unit)

    capacity = configured("capacity_kwh", "capacity_entity", "kWh")
    max_charge = configured("max_charge_kw", "charge_power_entity", "kW")
    max_discharge = configured("max_discharge_kw", "discharge_power_entity", "kW")
    solar, load = {}, {}
    for entity in (settings["solar_entity"], settings["solar_tomorrow_entity"]):
        raw_points = _state(states, entity)[1].get("detailedForecast", [])
        for point in raw_points if isinstance(raw_points, list) else []:
            if not isinstance(point, dict):
                continue
            start, value = _timestamp(point.get("period_start")), _number(point.get("pv_estimate"))
            if valid_solar_factor and start is not None and start < end and start + HOUR_MS / 2 > now:
                solar[start] = {
                    "start": start,
                    "end": start + HOUR_MS / 2,
                    "kw": value * solar_factor
                    if value is not None and value >= 0 and solar_factor is not None
                    else None,
                }
    _, load_attributes = _state(states, settings["load_forecast_entity"])
    factor = {"W": 0.001, "kW": 1, "MW": 1000}.get(str(load_attributes.get("unit_of_measurement", "")))
    raw_points = load_attributes.get("forecast", [])
    for point in raw_points if isinstance(raw_points, list) else []:
        if not isinstance(point, dict):
            continue
        start, value = _timestamp(point.get("time")), _number(point.get("value"))
        if start is not None and start < end + HOUR_MS and start + HOUR_MS > now:
            load[start] = {
                "start": start,
                "end": start + HOUR_MS,
                "kw": abs(value * factor) if value is not None and factor is not None else None,
            }
    first_load = min(load.values(), key=lambda point: point["start"]) if load else None
    current_load = _reading(states, settings["load_power_entity"], "kW")
    startup_until = None
    if first_load and 0 < first_load["start"] - now <= HOUR_MS and current_load is not None and current_load >= 0:
        startup_until = first_load["start"]
        load[now] = {"start": now, "end": min(end, startup_until), "kw": current_load}
    inputs = {
        "now": now,
        "end": end,
        "initialSoc": initial,
        "capacityKwh": capacity,
        "minSoc": floor,
        "maxSoc": _number(settings["max_soc"]),
        "maxChargeKw": max_charge,
        "maxDischargeKw": max_discharge,
        "chargeEfficiency": _number(settings["charge_efficiency"]),
        "dischargeEfficiency": _number(settings["discharge_efficiency"]),
        "dischargeOverheadKw": _number(settings["discharge_overhead_kw"]),
        "solar": list(solar.values()),
        "load": list(load.values()),
    }
    result = estimate_soc(inputs)
    if not valid_solar_factor:
        result["issues"].insert(0, "Solar forecast calibration factor must be between 0 and 2.")
    attributes = {
        "last_forecast_update": _iso(now),
        "prediction_time": _iso(now + HOUR_MS),
        "forecast_lead_hours": 1,
        "model_version": MODEL_VERSION,
        "solar_forecast_factor": solar_factor,
        "forecast": [{"time": _iso(time), "value": value} for time, value in result["points"]],
        "forecast_complete": result["complete"],
        "covered_until": _iso(result["coveredUntil"]) if result["coveredUntil"] is not None else None,
        "forecast_issues": result["issues"],
        "min_soc": floor,
        "min_soc_source": floor_source,
        "min_soc_kind": floor_kind,
        "reserve_note": " ".join(notes),
        "startup_load_held_until": startup_until,
        "assumptions": (
            "Local self-consumption estimate using HA solar/load forecasts and native battery reserves; "
            f"additional battery-side drain while discharging: {settings['discharge_overhead_kw']} kW. "
            f"Charge/discharge efficiency: {settings['charge_efficiency']}/{settings['discharge_efficiency']}. "
            f"SOC solar input multiplier: {solar_factor}. "
            "Excludes Amber/VPP dispatch. History values were issued one hour before their prediction time."
        ),
        "source_entities": {key: value for key, value in settings.items() if key.endswith("_entity")},
        "model_inputs": {key: value for key, value in inputs.items() if key not in ("solar", "load")},
        "forecast_energy": result["energy"],
        "load_forecast_issued_at": load_attributes.get("last_forecast_update"),
    }
    return {"value": sample_soc(result["points"], now + HOUR_MS), "attributes": attributes}
