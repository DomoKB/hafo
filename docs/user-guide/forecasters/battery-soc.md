# Battery SOC

Battery SOC estimates how the battery will charge or discharge from the difference
between forecast solar generation and household demand. It does not schedule or
control equipment. It has no HAEO dependency or direct cloud connection.

## Create the helper

1. In Settings, Devices & services, Helpers, choose Create helper and HAFO Forecast.
2. Select the **measured battery SOC sensor** and **Battery SOC** forecast type.
3. Select a solar sensor with Solcast-style half-hourly detailedForecast attributes
    and a load sensor with HAFO-style hourly forecast attributes.
4. Optionally select tomorrow's solar forecast, current household load, native
    battery capacity and charge/discharge power ratings, reserves and grid status.
    Capacity and power limits can instead be supplied as numeric overrides.
5. Set efficiencies and the forecast horizon. The default horizon is 48 hours;
    prediction ends when either input forecast's continuous coverage ends.

The SOC sensor and reserve entities use percent. Ratings accept Wh/kWh/MWh and
W/kW/MW. Load forecasts use the sensor's declared power unit. Solar pv_estimate
values are average kW for each half-hour interval.

## Calculation

At each input boundary, integrate solar minus household load over the actual
elapsed time. Surplus charges the battery subject to its charging limit and
charge efficiency. Deficits discharge it subject to its discharge limit,
discharge efficiency and optional battery-side overhead. Exact reserve/full
crossing times are included. Missing or ambiguous forecast intervals stop the
curve; later inputs never restart an unknown SOC.

The reserve is the greater of the native discharge cut-off and backup reserve
while on grid. Off grid, the discharge cut-off applies. Unknown grid status
explicitly assumes on-grid operation. A configured minimum overrides these
entities. If neither native reserve is available, the fallback is visibly zero.

A solar calibration factor scales solar input **only for this SOC estimate**.
Efficiencies and battery-side discharge overhead are installation assumptions,
not measurements. Defaults are factor 1, efficiencies 0.95, overhead 0 kW. They
must be calibrated for each site rather than copied as universal constants.

If the load forecast starts within the next hour, measured household load can
cover that bounded lead-in. It cannot fill later holes in the forecast.

Grid charging, battery exports, Amber/VPP dispatch, outages and unusual loads are
not predicted. This is a self-consumption estimate, not an optimiser's schedule.

## Sensor and history contract

- State: SOC predicted **one hour after issuance**, or unknown if not covered.
- Unit: percent; device class battery; state class measurement.
- Attributes: forecast (time/value points), last_forecast_update,
    prediction_time, forecast_lead_hours: 1, model inputs, assumptions, reserves,
    coverage and explicit issues.
- Updates: every five minutes and when core source/configuration values change.
    Changes to instantaneous household load alone do not create high-rate history.

Recorder stores the one-hour-ahead scalar predictions. Compare them against
measured SOC at prediction_time, not their issuance time. Full curves describe
only the latest issue. After detailed history expires, hourly long-term means
remain averages of predictions issued an hour earlier. Retention follows the
installation's Recorder configuration; this integration never edits the database.

Historical Shift and Multiweek Average retain their existing nearest-time state
semantics. Create a new helper to change between a historical strategy and SOC.

## Migrate the standalone Sigen SOC helper

When exactly one existing sigen_soc_forecast helper uses the selected measured
SOC sensor, its settings are prefilled and the migration checkbox is offered.
Migration stops that writer, transfers its existing sensor using HA's entity
registry API, and starts the HAFO helper. The old config entry is removed only
after the replacement sensor has loaded. Failure restores the old ownership
where recovery is possible.

The sensor's entity ID and entity-registry ID are retained, preserving Recorder
history and statistics under the same entity. No historical predictions are
recreated or renamed. Back up both integrations and configuration before a live
migration. After verification, archive the unused standalone component directory
outside custom_components. Do not remove its sensor history.

A dashboard can read the HAFO sensor's forward curve directly, while using that
same entity's Recorder history for elapsed one-hour-ahead predictions. If the
helper becomes unavailable, show the saved history and an unavailable status;
do not silently replace its curve with another model.
