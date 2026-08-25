# Changelog

## 0.3.1 - 2026-08-25

- Preserved exact display-busy rejections for weather and train dashboards so
  Clock or assistant ownership is not misreported as a provider outage.
- Documented the Host MCP Cockpit state and command boundary used by the G2
  companion app.

## 0.3.0 - 2026-08-25

- Added twelve static G2 workflows with exact input and output schemas.
- Added digest-bound session capabilities, deterministic mutation identities,
  replay protection, cancellation, and bounded ambiguous-outcome retry.
- Added typed National Rail and Open-Meteo/UKMO presentation workflows.
- Added schema-pinned private phone routes for Work Tasks, Clock, apps, media,
  navigation, notifications, health, calendar, and final result delivery.
- Replaced prompt-driven reminder firing with a deterministic durable outbox in
  the private transport.
- Removed raw phone discovery, arbitrary calls, and background-notify authority
  from the model-facing package.
