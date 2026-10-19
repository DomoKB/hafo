"""Chronological evaluation of HAFO production forecasting functions.

Personal recorder data and generated reports should be stored outside the repo.
Run development first; holdout requires the selection file that stage creates.
"""

import argparse
from collections import defaultdict
from datetime import UTC, datetime, timedelta
import hashlib
import json
from math import isfinite
from pathlib import Path
from statistics import fmean
from typing import Any
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from custom_components.hafo.forecasters.historical_shift import shift_history_to_forecast
from custom_components.hafo.forecasters.multiweek_profile import average_history_to_forecast

HOUR = timedelta(hours=1)
NIGHT_END_HOUR = 6
CANDIDATES = ("weekday_weekend", "weekday")


def predictions(rows: list[dict[str, Any]], origin: datetime, grouping: str | None) -> dict[float, float]:
    """Use only statistics available before the issue time, for either method."""
    cutoff = origin.astimezone(UTC) - HOUR
    past = [r for r in rows if r["start"].astimezone(UTC) + HOUR <= cutoff]
    if grouping is None:
        earliest = (origin - timedelta(days=7)).astimezone(UTC)
        history = [r for r in past if r["start"].astimezone(UTC) >= earliest]
        shifted = shift_history_to_forecast(history, 7)
        return {p.time.timestamp(): p.value for p in shifted}
    return {
        time.timestamp(): value
        for time, value in average_history_to_forecast(
            past, now=origin, history_weeks=4, forecast_days=1, day_grouping=grouping
        )
    }


def score(rows: list[dict[str, Any]], start: datetime, end: datetime, groups: tuple[str, ...]) -> dict[str, Any]:
    """Score common known hours; energy metrics require complete paired days."""
    observed = {r["start"].timestamp(): r["mean"] for r in rows}
    errors: dict[str, list[float]] = defaultdict(list)
    day_errors: dict[str, list[float]] = defaultdict(list)
    night_errors: dict[str, list[float]] = defaultdict(list)
    per_day = []
    available = dict.fromkeys(("historical_shift", *groups), 0)
    total = 0
    current = start
    while current < end:
        next_day = current + timedelta(days=1)
        times = []
        time = current.astimezone(UTC)
        while time < next_day.astimezone(UTC):
            times.append(time.timestamp())
            time += HOUR
        forecasts = {"historical_shift": predictions(rows, current, None)}
        forecasts.update({key: predictions(rows, current, key) for key in groups})
        known = [t for t in times if t in observed]
        paired = [t for t in known if all(t in forecast for forecast in forecasts.values())]
        total += len(times)
        for key, forecast in forecasts.items():
            available[key] += sum(t in forecast for t in times)
            errors[key].extend(forecast[t] - observed[t] for t in paired)
        nightly = [t for t in times if datetime.fromtimestamp(t, start.tzinfo).hour < NIGHT_END_HOUR]
        day_record = {"date": current.date().isoformat(), "expected_hours": len(times), "paired_hours": len(paired)}
        if len(paired) == len(times):
            actual = sum(observed[t] for t in times)
            day_record["actual_kwh"] = actual
            for key, forecast in forecasts.items():
                error = sum(forecast[t] for t in times) - actual
                day_errors[key].append(error)
                day_record[key + "_energy_error_kwh"] = error
        if all(t in paired for t in nightly):
            actual_night = sum(observed[t] for t in nightly)
            for key, forecast in forecasts.items():
                night_errors[key].append(sum(forecast[t] for t in nightly) - actual_night)
        per_day.append(day_record)
        current = next_day
    result = {}
    for key, coverage_count in available.items():
        err = errors[key]
        result[key] = {
            "paired_hours": len(err),
            "forecast_hours": coverage_count,
            "expected_hours": total,
            "coverage": coverage_count / total if total else None,
            "hourly_mae_kw": fmean(abs(e) for e in err) if err else None,
            "hourly_rmse_kw": fmean(e * e for e in err) ** 0.5 if err else None,
            "bias_kw": fmean(err) if err else None,
            "complete_days": len(day_errors[key]),
            "daily_energy_mae_kwh": fmean(abs(e) for e in day_errors[key]) if day_errors[key] else None,
            "complete_nights": len(night_errors[key]),
            "overnight_energy_mae_kwh": fmean(abs(e) for e in night_errors[key]) if night_errors[key] else None,
        }
    return {"start": start.isoformat(), "end_exclusive": end.isoformat(), "metrics": result, "days": per_day}


def main() -> None:
    """Run one predeclared evaluation stage and record its provenance."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--stage", choices=["development", "holdout"], required=True)
    parser.add_argument("--start", required=True, help="First local date, YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="Exclusive final local date, YYYY-MM-DD")
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    if data["unit"] != "kW":
        msg = "Energy-error evaluation requires hourly kW statistics"
        raise ValueError(msg)
    timezone = ZoneInfo(data["time_zone"])
    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone)
    if end <= start:
        msg = "Evaluation end must follow start"
        raise ValueError(msg)
    rows = [
        {"start": datetime.fromtimestamp(r["start"] / 1000, timezone), "mean": float(r["mean"])}
        for r in data["rows"]
        if r.get("mean") is not None
    ]
    if any(not isfinite(row["mean"]) for row in rows) or len({row["start"].timestamp() for row in rows}) != len(rows):
        msg = "Recorder input must contain unique hours and finite means"
        raise ValueError(msg)
    dt_util.set_default_time_zone(timezone)
    args.output.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(args.input.read_bytes()).hexdigest()
    selection_path = args.output / "selection.json"
    if args.stage == "development":
        result = score(rows, start, end, CANDIDATES)
        if any(result["metrics"][key]["paired_hours"] == 0 for key in CANDIDATES):
            msg = "Not enough paired observations for method selection"
            raise ValueError(msg)
        selected = min(CANDIDATES, key=lambda key: result["metrics"][key]["hourly_mae_kw"])
        selection = {
            "selected_grouping": selected,
            "history_weeks": 4,
            "data_sha256": digest,
            "development_end_exclusive": args.end,
            "criterion": "minimum paired-hour MAE",
        }
        selection_path.write_text(json.dumps(selection, indent=2) + "\n")
    else:
        selection = json.loads(selection_path.read_text())
        if selection["data_sha256"] != digest:
            msg = "Dataset changed after development selection"
            raise ValueError(msg)
        if args.start < selection["development_end_exclusive"]:
            msg = "Holdout must begin after the development period"
            raise ValueError(msg)
        selected = selection["selected_grouping"]
        result = score(rows, start, end, (selected,))
    result.update(
        {
            "stage": args.stage,
            "source_entity": data["source_entity"],
            "data_sha256": digest,
            "selection": selection,
            "method": "rolling local-midnight origins; one-hour recorder publication allowance",
        }
    )
    (args.output / (args.stage + ".json")).write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"stage": args.stage, "selection": selection, "metrics": result["metrics"]}, indent=2))  # noqa: T201


if __name__ == "__main__":
    main()
