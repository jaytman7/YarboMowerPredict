# My Yarbo Mower

Standalone Home Assistant custom integration and dashboard for controlling a Yarbo mower through `yarbo-data-sdk`.

This project is separate from the existing YarboHA integration.

## Features

- Direct Home Assistant config flow for Yarbo account login and device selection.
- Native `lawn_mower` entity for start, pause, and dock controls.
- Direct command buttons for start, stop, dock, wake, refresh, and plan refresh.
- Local plan selector and persistent plan sequence queue.
- Per-plan grass growth estimates attached to the sequence list.
- Sensors for previous completed plan, next run plan, current sequence position, battery, RTK, recharge status, charging power, mower head, rain sensor, and errors.
- Weather and sun-derived mowing condition and grass wetness scores.
- Three-hour weather lookahead that blocks starting when bad weather is expected.
- Best mow start prediction for the driest, coolest usable daylight forecast window.
- Configurable blackout windows after sunrise and before sunset.
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
- `Remove Sequence Plan` removes the selected plan from the queue, using the last matching entry.
- `Clear Sequence` empties the queue.
- `Next Run Plan` shows the plan that will run when Start is pressed.
- `Previous Completed Plan` updates after Yarbo reports that an app-started plan completed.

When the queue has at least one plan, Start runs the next queued plan. When the queue is empty, Start uses the normal selected plan.

## Grass Growth Tracking

Each known plan gets a local growth counter. The `Plan Sequence` sensor exposes the queued plans as detailed attributes with:

- `growth_since_last_mow_in`
- `growth_days`
- `growth_started_at`
- `last_mowed_at`

The app estimates growth from current weather, temperature, cloud cover, humidity, and sun state. When a plan started by this app completes, that plan's growth counter resets to `0.0` and its `last_mowed_at` timestamp is updated.

## Weather Start Gate

The app checks Home Assistant's hourly weather forecast for the next three hours. Starting is blocked when the current weather or lookahead window contains rain, storms, snow/hail, measurable precipitation, high precipitation probability, or high wind.

The `Weather Window` sensor exposes the current decision, reason, checked timestamp, forecast horizon, and the forecast entries considered by the gate.

The `Best Mow Start` sensor ranks the next 24 hours of hourly forecast entries that fall inside usable daylight. It uses the later of the configured sunrise blackout or a 5-hour dew-drying window, and it also avoids the configured sunset blackout. It favors dry, cool, sunny, lower-wind forecast slots and exposes the top candidates as attributes.

## Notes

- This is a personal standalone integration, not an official Yarbo product.
- Credentials and tokens are stored by Home Assistant config entries, not in this repository.
- The root `configuration.yaml`, Home Assistant `.storage`, database files, logs, and legacy Yarbo/Cadence files are intentionally ignored.
