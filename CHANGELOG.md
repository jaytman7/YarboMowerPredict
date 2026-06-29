# Changelog

## Unreleased

- Adds a dashboard-selectable weather source so mowing decisions can use a
  chosen HA weather provider instead of always using `weather.forecast_home`.
- Prefers Home Assistant AccuWeather weather entities automatically when
  AccuWeather is configured and no weather source has been selected manually.
- Scores best mow starts by day, reports `No candidate` when a day has no safe
  mowing window, and keeps automation focused on today's remaining best start.
- Adds a Best Start Outlook pane to the generated dashboard.
- Filters noisy high-battery charging status so 95-100% battery does not keep
  the mower blocked as charging.

## 0.1.0

- Initial standalone My Yarbo Mower integration.
- Adds Yarbo account config flow, mower controls, plan selection, sequence
  queue, weather gates, grass growth tracking, best-start prediction, and
  generated dashboard support.
- Adds a Warm Weather Grass switch that changes future grass growth estimates
  between cold-weather and warm-weather temperature response curves.
- Shows the active growth model and estimated growth rate on the generated
  dashboard.
- Adds a Mowing Status dashboard pane that lists current start blockers or
  confirms that the next sequence plan is clear to mow.
