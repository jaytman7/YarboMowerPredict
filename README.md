# My Yarbo Mower

Standalone Home Assistant custom integration and dashboard for controlling a Yarbo mower through `yarbo-data-sdk`.

This project is separate from the existing YarboHA integration and does not depend on the older YarboCadenceEQ package.

## Features

- Direct Home Assistant config flow for Yarbo account login and device selection.
- Native `lawn_mower` entity for start, pause, and dock controls.
- Direct command buttons for start, stop, dock, wake, refresh, and plan refresh.
- Local plan selector and persistent plan sequence queue.
- Sensors for previous completed plan, next run plan, current sequence position, battery, RTK, recharge status, charging power, mower head, rain sensor, and errors.
- Weather and sun-derived mowing condition and grass wetness scores.
- Configurable blackout windows after sunrise and before sunset.
- YAML dashboard at `yarbo_mower_app-dashboard.yaml`.

## Repository Layout

```text
custom_components/my_yarbo_mower/   Home Assistant custom integration
yarbo_mower_app-dashboard.yaml      Optional Lovelace dashboard
examples/configuration.yaml         Dashboard configuration snippet
```

## Install Locally

1. Copy `custom_components/my_yarbo_mower` into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.
3. Add the integration from Settings, Devices and services, Add integration, `My Yarbo Mower`.
4. Log in with your Yarbo account and select the mower device.
5. Copy `yarbo_mower_app-dashboard.yaml` into your Home Assistant config directory if you want the included dashboard.
6. Add the dashboard snippet from `examples/configuration.yaml` to your Home Assistant `configuration.yaml`, then restart Home Assistant.

## Plan Sequence Behavior

The sequence queue is stored by the integration and survives Home Assistant restarts.

- `Sequence Plan` chooses a plan for queue editing.
- `Add Sequence Plan` appends that plan to the queue.
- `Remove Sequence Plan` removes the selected plan from the queue, using the last matching entry.
- `Clear Sequence` empties the queue.
- `Next Run Plan` shows the plan that will run when Start is pressed.
- `Previous Completed Plan` updates after Yarbo reports that an app-started plan completed.

When the queue has at least one plan, Start runs the next queued plan. When the queue is empty, Start uses the normal selected plan.

## Notes

- This is a personal standalone integration, not an official Yarbo product.
- Credentials and tokens are stored by Home Assistant config entries, not in this repository.
- The root `configuration.yaml`, Home Assistant `.storage`, database files, logs, and legacy Yarbo/Cadence files are intentionally ignored.
