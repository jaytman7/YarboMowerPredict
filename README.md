# My Yarbo Mower

Standalone Home Assistant custom integration and dashboard for controlling a
Yarbo mower through `yarbo-data-sdk`. The project attempts to predict the best
mowing conditions from local sun and weather data, track grass growth and
wetness by mowing plan, and use that context to automate Yarbo scheduling so the
lawn is maintained when conditions are most favorable.

This project is separate from the existing YarboHA integration.

## Features

- Direct Home Assistant config flow for Yarbo account login and device selection.
- Native `lawn_mower` entity for start, pause, and dock controls.
- Direct command buttons for start, stop, dock, wake, refresh, and plan refresh.
- Local plan selector and persistent plan sequence queue.
- Per-plan grass growth estimates attached to the sequence list.
- Toggleable cold-weather and warm-weather grass growth models.
- Sensors for previous completed plan, next run plan, current sequence position, battery, RTK, recharge status, charging power, mower head, rain sensor, and errors.
- Weather and sun-derived mowing condition and grass wetness scores.
- Dashboard-selectable Home Assistant weather source, with AccuWeather preferred
  automatically when configured and no source has been explicitly selected.
- Three-hour weather lookahead that blocks starting when bad weather is expected.
- Daily best mow start prediction for the driest, coolest usable daylight
  forecast window, including `No candidate` when a day is not mowable.
- Configurable blackout windows after sunrise and before sunset.
- Optional sequence automation gate with pre-start wake checks, RTK readiness checks, and adjustable thresholds.
- Generated YAML dashboard at `yarbo_mower_app-dashboard.yaml` using the user's actual Home Assistant entity IDs.

## Repository Layout

```text
custom_components/my_yarbo_mower/   Home Assistant custom integration
custom_components/my_yarbo_mower/dashboard_template.yaml
                                    Source template for generated dashboard
examples/configuration.yaml         Dashboard configuration snippet
```

## Install Locally

1. Copy `custom_components/my_yarbo_mower` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from Settings, Devices and services, Add integration, `My Yarbo Mower`.
4. Log in with your Yarbo account and select the mower device.
5. Add the dashboard snippet from `examples/configuration.yaml` to your Home Assistant `configuration.yaml`, then restart Home Assistant.
6. The integration will generate `yarbo_mower_app-dashboard.yaml` automatically for a single selected mower when the file does not already exist. You can also press the `Generate Dashboard` button on the My Yarbo device, or call `my_yarbo_mower.generate_dashboard`, to regenerate it after renaming entities or changing devices.

## Install with HACS

This repository is structured as a HACS custom integration. Until it is accepted
into the default HACS catalog, add it to HACS as a custom repository:

1. In Home Assistant, open HACS, Custom repositories.
2. Add the repository URL, `https://github.com/jaytman7/YarboMowerPredict`.
3. Select category `Integration`.
4. Install `My Yarbo Mower` from HACS.
5. Restart Home Assistant.
6. Add the integration from Settings, Devices and services, Add integration,
   `My Yarbo Mower`.

The GitHub repository URL is `https://github.com/jaytman7/YarboMowerPredict`.

## Python Dependency Installation

HACS installs the integration files, but Home Assistant installs the Python
library dependency. This integration declares `yarbo-data-sdk>=0.2.1` in
`custom_components/my_yarbo_mower/manifest.json`:

```json
"requirements": ["yarbo-data-sdk>=0.2.1"]
```

On a clean Home Assistant install that has never had YarboHA installed, the flow
is:

1. HACS downloads this repository into `custom_components/my_yarbo_mower`.
2. Home Assistant restarts and reads the integration manifest.
3. Home Assistant installs `yarbo-data-sdk` from PyPI into its dependency
   environment before loading the integration.
4. The integration imports `yarbo_robot_sdk` from that installed package.

YarboHA and yarbo-cadence-eq are not required. If the package cannot be
downloaded, or if PyPI does not have a compatible build for the Home Assistant
Python version and platform, Home Assistant will fail to load this integration
and log a requirement installation error.

## Weather Providers and AccuWeather

My Yarbo Mower does not store third-party weather API keys. Add weather
providers to Home Assistant, then choose the resulting `weather.*` entity from
the `Weather source` selector on the My Yarbo dashboard.

To use AccuWeather:

1. In Home Assistant, open Settings, Devices and services, Add integration.
2. Search for `AccuWeather`.
3. Paste your AccuWeather API key into the AccuWeather setup flow.
4. Confirm the latitude and longitude. The defaults come from the Home
   Assistant home location.
5. After Home Assistant creates the AccuWeather weather entity, open the My
   Yarbo dashboard and set Conditions, Weather source to that entity.

If no My Yarbo weather source has been explicitly selected, the app prefers an
available AccuWeather weather entity automatically. If a different source was
already selected, the dashboard selector wins and must be changed manually.

## HACS Release Checklist

Before tagging a public release:

- Confirm `manifest.json` has the correct `documentation`, `issue_tracker`, and
  `codeowners` values for the GitHub repository.
- Keep `LICENSE` present so GitHub and HACS can detect the repository license.
- Run the HACS and hassfest GitHub Actions successfully.
- Add a GitHub repository description, topics, and brand assets before
  submitting to the default HACS catalog. The local HACS workflow keeps those
  repository-level checks ignored until that metadata is configured on GitHub.
- Keep `custom_components/my_yarbo_mower/manifest.json` `version` aligned with
  the release tag, for example manifest version `0.1.0` and Git tag `v0.1.0`.
- Create a GitHub release from the tag so HACS users can install a stable
  version instead of only the default branch.

## License

MIT License. See `LICENSE`.

## Dashboard Generation

The dashboard is rendered from `custom_components/my_yarbo_mower/dashboard_template.yaml`.
It does not guess entity IDs from a Yarbo serial number. Instead, it asks Home
Assistant's entity registry for each entity created by this integration and writes
the resolved IDs into `yarbo_mower_app-dashboard.yaml`.

If more than one Yarbo device is selected, call the service with a serial number:

```yaml
service: my_yarbo_mower.generate_dashboard
data:
  device_serial: YOUR_YARBO_SERIAL
  overwrite: true
```

## Plan Sequence Behavior

The sequence queue is stored by the integration and survives Home Assistant restarts.

- `Sequence Plan` chooses a plan for queue editing.
- `Add Sequence Plan` appends that plan to the queue.
- `Next Sequence Plan` advances the sequence pointer so `Run Sequence` starts
  from a different queued plan without marking anything complete.
- `Remove Sequence Plan` removes the selected plan from the queue, using the last matching entry.
- `Clear Sequence` empties the queue.
- `Next Run Plan` shows the plan that will run when the normal `Start` button is pressed.
- `Previous Completed Plan` updates after Yarbo reports that an app-started plan completed.
- `Run Sequence` starts the next queued sequence plan and advances the sequence after completion.

The normal Home Assistant `Start` command always uses the selected `Plan` in the
Mission section. A manually selected plan is not overridden just because the
sequence has queued plans. Use `Run Sequence` when you want the mower to work
through the queue.

## Sequence Automation

Automatic sequence behavior is opt-in and split into two switches:

- `Auto Wake Checks` periodically wakes the mower near the predicted best start
  time so online, battery, mower head, and RTK checks can settle.
- `Auto Sequence Start` starts the next queued sequence plan only when all
  readiness checks pass during the best-start grace window.

The `Sequence Auto Ready` binary sensor exposes each check as attributes. It
requires:

- a queued sequence plan
- current time inside the best-start grace window
- clear three-hour weather window
- mowing favorability above the configured minimum
- grass wetness below the configured maximum
- mower online
- battery above the configured minimum
- mower head attached
- RTK ready
- no active plan, return-to-charge, charging, obstacle, stuck, or error state

Defaults are conservative: auto wake and auto start are off, minimum battery is
`70%`, minimum favorability is `70%`, maximum wetness is `45%`, wake lead is
`45` minutes, wake interval is `10` minutes, and start grace is `30` minutes.

## Grass Growth Tracking

Each known plan gets a local growth counter. The `Plan Sequence` sensor exposes the queued plans as detailed attributes with:

- `growth_since_last_mow_in`
- `growth_days`
- `growth_started_at`
- `last_mowed_at`

The app estimates growth from current weather, temperature, cloud cover,
humidity, and sun state. The `Warm Weather Grass` switch changes the temperature
response curve used for future growth accumulation:

- Off: cold-weather/cool-season grass curve, centered around cooler growing
  temperatures.
- On: warm-weather grass curve, centered around hotter growing temperatures.

Changing this switch affects future accumulated growth. It does not rewrite
growth that has already been counted since the last mow. When a plan started by
this app completes, that plan's growth counter resets to `0.0` and its
`last_mowed_at` timestamp is updated.

## Weather Start Gate

The app checks Home Assistant's hourly weather forecast for the next three
hours every 15 minutes, and again immediately before a manual or automatic
start. Starting is blocked when the current weather or lookahead window contains
rain, storms, snow/hail, measurable precipitation, high precipitation
probability, or high wind.

The `Weather Window` sensor exposes the current decision, reason, checked timestamp, forecast horizon, and the forecast entries considered by the gate.

The `Best Mow Start` sensor reports the best start remaining today. Its
attributes include `daily_best_starts`, which lists the best candidate for each
available forecast day or `No candidate` when the day has no acceptable mowing
window. It uses the later of the configured sunrise blackout or a 6-hour
dew-drying window, and it also avoids the configured sunset blackout. It favors
dry, cool, sunny, lower-wind forecast slots and considers humidity, cloud cover,
and dew-point spread when the selected weather provider supplies those values.

## Notes

- This is a personal standalone integration, not an official Yarbo product.
- Credentials and tokens are stored by Home Assistant config entries, not in this repository.
- The root `configuration.yaml`, Home Assistant `.storage`, database files, logs, and legacy Yarbo/Cadence files are intentionally ignored.
