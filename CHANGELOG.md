# Changelog

## 0.3.2 - 2026-08-25

- Routed the isolated workflow process to its profile-scoped private relay.
  The sanitized MCP environment no longer falls back to another profile's
  missing socket, so all twelve typed workflows can reach native dispatch.
- Clarified exact National Rail station identity in the train tool contract,
  including the distinct Liverpool Central (`LVC`) and Liverpool Lime Street
  (`LIV`) codes, so a named station is never silently substituted.

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
