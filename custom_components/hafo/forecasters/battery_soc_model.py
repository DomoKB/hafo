"""Pure interval energy balance with fixed-lead forecast parity fixtures."""

from itertools import pairwise
import math
from typing import Any, TypeIs, cast

HOUR_MS = 3_600_000
FULL_SOC = 100


def finite(value: Any) -> TypeIs[int | float]:
    """JSON numbers only; booleans and non-finite values are not measurements."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _intervals(
    entries: Any, label: str, now: float, end: float, issues: list[str]
) -> tuple[list[dict[str, Any]], float]:
    if not isinstance(entries, list) or not entries:
        issues.append(f"{label} forecast is unavailable.")
        return [], now
    result, seen, unsafe = [], set(), math.inf
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not finite(entry.get("start"))
            or not finite(entry.get("end"))
            or entry["end"] <= entry["start"]
        ):
            issues.append(f"{label} forecast contains an invalid interval.")
            return [], now
        if entry["end"] <= now or entry["start"] >= end:
            continue
        if not finite(entry.get("kw")) or entry["kw"] < 0:
            issues.append(f"{label} forecast contains an invalid power value.")
            unsafe = min(unsafe, max(now, entry["start"]))
            continue
        key = (entry["start"], entry["end"], entry["kw"])
        if key not in seen:
            seen.add(key)
            result.append(entry)
    result.sort(key=lambda item: (item["start"], item["end"]))
    for previous, current in pairwise(result):
        if current["start"] < previous["end"]:
            issues.append(f"{label} forecast contains overlapping intervals; SOC estimation stops before the overlap.")
            unsafe = min(unsafe, max(now, current["start"]))
            break
    return result, unsafe


def estimate_soc(options: dict[str, Any]) -> dict[str, Any]:
    """Estimate self-consumption SOC without guessing grid charging or exports."""
    issues, points = [], []
    energy = dict.fromkeys(
        (
            "solarKwh",
            "loadKwh",
            "chargeAcKwh",
            "dischargeAcKwh",
            "dischargeOverheadKwh",
            "storedChangeKwh",
            "gridImportKwh",
            "exportKwh",
        ),
        0.0,
    )
    now, end = options.get("now"), options.get("end")
    initial, capacity = options.get("initialSoc"), options.get("capacityKwh")
    lower_soc, upper_soc = options.get("minSoc"), options.get("maxSoc")
    charge_limit, discharge_limit = options.get("maxChargeKw"), options.get("maxDischargeKw")
    charge_eff, discharge_eff = options.get("chargeEfficiency"), options.get("dischargeEfficiency")
    overhead = options.get("dischargeOverheadKw", 0)

    def result() -> dict[str, Any]:
        return {
            "points": points,
            "issues": list(dict.fromkeys(issues)),
            "energy": energy,
            "coveredUntil": points[-1][0] if points else None,
            "complete": len(points) > 1 and points[-1][0] >= end,
        }

    if not finite(now) or not finite(end) or end <= now:
        issues.append("SOC forecast requires a valid future time range.")
    if not finite(initial) or not 0 <= initial <= FULL_SOC:
        issues.append("SOC forecast requires a current battery SOC from 0 to 100%.")
    if not finite(capacity) or capacity <= 0:
        issues.append("SOC forecast requires a positive battery capacity in kWh.")
    if not finite(lower_soc) or not finite(upper_soc) or lower_soc < 0 or upper_soc > FULL_SOC or lower_soc > upper_soc:
        issues.append("SOC forecast requires valid minimum and maximum SOC limits.")
    if not finite(charge_limit) or not finite(discharge_limit) or charge_limit < 0 or discharge_limit < 0:
        issues.append("SOC forecast requires non-negative charge and discharge power limits in kW.")
    if not finite(charge_eff) or not finite(discharge_eff) or not 0 < charge_eff <= 1 or not 0 < discharge_eff <= 1:
        issues.append("SOC forecast requires charge and discharge efficiencies greater than 0 and at most 1.")
    if not finite(overhead) or overhead < 0:
        issues.append("SOC forecast requires non-negative discharge overhead in kW.")
    if issues:
        return result()
    # Numeric validation above establishes these inputs for the energy balance.
    now, end = cast("float", now), cast("float", end)
    initial, capacity = cast("float", initial), cast("float", capacity)
    lower_soc, upper_soc = cast("float", lower_soc), cast("float", upper_soc)
    charge_limit, discharge_limit = cast("float", charge_limit), cast("float", discharge_limit)
    charge_eff, discharge_eff = cast("float", charge_eff), cast("float", discharge_eff)
    feeds = [
        _intervals(options.get(key), label, now, end, issues) for key, label in (("solar", "Solar"), ("load", "Load"))
    ]
    for (intervals, unsafe), label in zip(feeds, ("Solar", "Load"), strict=True):
        if unsafe <= now:
            return result()
        if not intervals or intervals[0]["start"] > now:
            issues.append(f"{label} forecast does not cover the forecast start.")
            return result()
    stored = initial_stored = capacity * initial / FULL_SOC
    lower, upper = capacity * lower_soc / FULL_SOC, capacity * upper_soc / FULL_SOC

    def add_point(time: float, value: float) -> None:
        point = [time, max(0, min(FULL_SOC, value))]
        if points and points[-1][0] == time:
            points[-1] = point
        else:
            points.append(point)

    add_point(now, initial)
    time, indexes = now, [0, 0]
    while time < end:
        usable = True
        for index, ((intervals, unsafe), label) in enumerate(zip(feeds, ("Solar", "Load"), strict=True)):
            if time >= unsafe:
                usable = False
                continue
            while indexes[index] < len(intervals) and intervals[indexes[index]]["end"] <= time:
                indexes[index] += 1
            interval = intervals[indexes[index]] if indexes[index] < len(intervals) else None
            if interval is None or interval["start"] > time:
                issues.append(
                    f"{label} forecast has a gap; SOC estimation stops at the gap."
                    if interval
                    else f"{label} forecast ends before the requested end."
                )
                usable = False
        if not usable:
            break
        solar, load = (feeds[index][0][indexes[index]] for index in (0, 1))
        stop = min(end, solar["end"], load["end"], *(feed[1] for feed in feeds))
        hours = (stop - time) / HOUR_MS
        net = solar["kw"] - load["kw"]
        charge_ac = discharge_ac = discharge_overhead = 0.0
        if net > 0 and stored < upper:
            power = min(net, charge_limit)
            if power > 0:
                required = (upper - stored) / charge_eff
                charge_ac = min(power * hours, required)
                stored += charge_ac * charge_eff
                if charge_ac >= required:
                    stored = upper
                    add_point(min(stop, time + required / power * HOUR_MS), upper_soc)
        elif net < 0 and stored > lower:
            power = min(-net, discharge_limit)
            if power > 0:
                battery_power = power / discharge_eff + overhead
                available = stored - lower
                active_hours = min(hours, available / battery_power)
                discharge_ac = power * active_hours
                discharge_overhead = overhead * active_hours
                stored -= discharge_ac / discharge_eff + discharge_overhead
                if active_hours >= available / battery_power:
                    stored = lower
                    add_point(min(stop, time + active_hours * HOUR_MS), lower_soc)
        energy["solarKwh"] += solar["kw"] * hours
        energy["loadKwh"] += load["kw"] * hours
        energy["chargeAcKwh"] += charge_ac
        energy["dischargeAcKwh"] += discharge_ac
        energy["dischargeOverheadKwh"] += discharge_overhead
        energy["exportKwh"] += max(0, net * hours - charge_ac)
        energy["gridImportKwh"] += max(0, -net * hours - discharge_ac)
        add_point(stop, stored / capacity * 100)
        time = stop
    energy["storedChangeKwh"] = stored - initial_stored
    return result()


def sample_soc(points: list[list[float]], time: float) -> float | None:
    """Interpolate a supported point, without extrapolating incomplete forecasts."""
    previous = None
    for point in points:
        if point[0] == time:
            return point[1]
        if point[0] > time:
            if previous is None or point[0] - previous[0] > HOUR_MS:
                return None
            return previous[1] + (point[1] - previous[1]) * (time - previous[0]) / (point[0] - previous[0])
        previous = point
    return None
