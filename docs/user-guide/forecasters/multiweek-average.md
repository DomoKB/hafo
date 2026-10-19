# Multiweek Average

Multiweek Average estimates a typical hourly profile by taking the arithmetic
mean of comparable local hours over several preceding weeks. It uses Home
Assistant recorder hourly means and does not require HAEO.

Create a Forecast helper, choose **Multiweek Average**, and select:

- **History to average:** 2–12 whole weeks; default 4.
- **Forecast horizon:** 1–14 whole days; default 7.
- **Day pattern:** weekdays/weekends together (default), or each weekday separately
    (requires at least 3 weeks).

Settings are checked before saving. Invalid edits leave the working helper unchanged.

A separate helper can coexist with Historical Shift for the same source entity.
The original helper keeps its existing identity, settings and forecast. Changing
settings on the new helper preserves its identity and source.

To switch an existing helper, open its **Configure** options and change
**Forecast strategy**. Submit to display the new strategy's settings, then submit
again to apply them. This preserves the entity ID, history and existing consumers.
The previous Historical Shift interval is retained when switching back.

At least three different local dates are required in a comparable hour bucket.
Otherwise the method uses the known hour one week earlier. If that is also
missing, the point is absent; missing consumption is never replaced by zero.
Each historical date contributes equally, including when a DST hour repeats.
Non-finite readings and conflicting duplicate measurements are ignored.

The current incomplete hour and a one-hour recorder publication allowance are
excluded from training. The remaining current hour can still be forecast using
older observations. Forecast timestamps follow real elapsed hours through DST.

## Interpretation and limitations

This first implementation is an arithmetic average. It does not apply weather
corrections, recency weighting, outlier trimming or predict unusual events.
Regular intermittent loads remain represented in the average. It forecasts
typical consumption, not exact appliance switching.

The usual time/value forecast format and source units are unchanged. The
`history_days` attribute reports the averaging lookback in days, not the
forecast horizon.

## Evaluation

`scripts/evaluate_forecasters.py` compares the production averaging function
with the production Historical Shift function. It uses rolling local-midnight
origins, excludes observations unavailable at the issue time, chooses between
two day-grouping candidates on an earlier development period, then evaluates
the frozen choice on a later holdout. It reports paired hourly errors, forecast
coverage, daily energy errors and midnight-to-06:00 energy errors.

Supply a JSON export with `source_entity`, `time_zone`, `unit: "kW"` and
`rows` containing Unix-millisecond `start` and numeric `mean` fields. Choose
chronological periods explicitly with `--start YYYY-MM-DD --end YYYY-MM-DD`
(end is exclusive). Run `--stage development` first and `--stage holdout`
second, with the same `--input` and `--output` paths. Holdout dates must follow
the frozen development period. Personal exports/reports belong outside the repo.
Historical comparisons use recorder statistics retrieved after the fact; later
manual revisions to old statistics cannot be reconstructed.
