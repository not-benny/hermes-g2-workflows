# Changelog

## 0.4.0 - 2026-08-25

- Added one static `g2_kanban_task_create` workflow for exact existing Hermes
  Kanban board slugs or display names. It creates a blocked, unassigned card
  plus the canonical sticky-block event, and therefore cannot be auto-promoted,
  start a worker, or enter the default triage auto-decomposer.
- Made retries duplicate-proof by deriving a content-free operation identity,
  binding its normalized payload digest to the original board generation in a
  private durable global ledger, and serializing the canonical Kanban lookup
  and `create_task` call inside one immediate transaction. Permanent committed
  tombstones survive card/board deletion and rename/reuse; uncertain mutating
  tombstones fail closed rather than recreating.
- Historical receipts expose immutable `created_status` and
  `created_assignee` facts rather than claiming current card state, and a
  changed payload after a known commit returns typed `historical_conflict`.
- Added typed `board_not_found` and `board_ambiguous` results with at most 16
  canonical active board choices. A Kanban request never falls back to the
  phone's local Work Tasks app or interprets a status name as a board.

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
