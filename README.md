# Hermes G2 Workflows MCP

This package moves G2 workflow policy out of a personal `SOUL.md`. It exposes
intent-complete MCP tools for Work Tasks, parked Hermes Kanban cards, Clock,
one-shot reminders, typed National Rail departures, typed Open-Meteo/UKMO
weather with phone-owned final presentation, and six grouped device workflows:
Apps, Media, Navigation, Notifications, Health Summary, and Calendar Agenda.

The MCP stdio server uses only the Python standard library. Its packaged
launch is `python -I -S -B server.py` with bytecode writes disabled, so ambient
user/site startup hooks cannot execute outside the approved package digest and
imports cannot create an unapproved cache that revokes the grant. It does not
depend on Hermes' private site packages or install dependencies at startup. It
names `python` portably in the manifest, but Hermes rewrites an approved
capability server to the host's resolved absolute interpreter rather than
re-resolving mutable `PATH` bytes. The executed script must be a regular,
non-symlink file inside the digest-bound package. It
implements the handshake, tool listing, tool calls, bounded JSON-RPC framing,
and standard request cancellation needed by Hermes' MCP client.

It is one half of a dual-role bridge:

- `g2-device`: the phone is the MCP server for device and lens operations.
- `hermes-control`: Hermes is the MCP server for voice turns, cancellation,
  a bounded Cockpit state resource, and exact Cockpit commands. Companion is
  deliberately unavailable.

The authenticated WSS is transport only. Workflow tools reach the live device
through a same-UID, private Unix relay owned by the native platform adapter.
The packaged MCP environment receives only the exact profile-local relay path,
derived from its host-owned plugin-data root; it does not need the broader
Hermes profile home.
The socket is transport isolation, not authority: Hermes signs a package-,
workflow-, argument-, and turn-bound capability in MCP request `_meta`, and the
native relay verifies it in process. Platform, profile, phone route, schema
hash, and turn authority are never accepted as model arguments.

The device workflows expose no discovery, arbitrary-call, render, dynamic-app,
context-state, or raw phone tool. Before each fixed phone call, the bridge
force-refreshes the private phone catalog and pins MCP protocol
`2025-06-18`, server `hermes-g2` version `1.0.0`, and the reviewed structural
input-schema fingerprint. Phone results are reduced to exact bounded receipts;
hourly health samples and raw MCP envelopes cannot cross this boundary.

Durable mutators (Work Tasks, Hermes Kanban, Clock, and reminders) derive a content-free
operation ID from trusted call identity so a bounded transport retry reuses the
same ID. Legacy phone actions without operation-ID support are never retried;
a lost response is reported as an unknown outcome instead of claiming success.

`g2_kanban_task_create` requires an exact case-insensitive match to one active
board slug or display name. Missing or duplicate names return a typed bounded
list of canonical choices; the workflow never falls back to local Work Tasks
and never treats a lane or status as a board. A successful call creates one
blocked, unassigned card. This is intentionally not `triage`: Hermes enables
triage auto-decomposition by default, while a blocked card stays parked until
the user explicitly changes it. The create transaction also records Hermes'
canonical sticky-block event; `initial_status="blocked"` alone would otherwise
be auto-promoted by dependency recomputation. The native bridge durably binds
the content-free operation ID and normalized payload digest to the original
canonical board generation before it writes. Its profile-private global ledger
and canonical immediate create transaction prevent concurrent retries, card
hard-deletion, board rename/reuse, or board deletion/recreation from producing
a second card. Historical success reports only immutable creation facts
(`created_status: blocked`, `created_assignee: null`), never the card's current
status or assignee. A payload mismatch after a known commit is returned as a
typed `historical_conflict`, not downgraded to an unknown outcome. A crash that
may have begun mutation but has no recoverable exact canonical row stays
outcome-unknown and will never recreate; this is the deliberate fail-closed
tradeoff that prevents resurrection.

Weather and train workflows distinguish data-provider failures from two exact
pre-delivery display conflicts. An active Clock alert or another assistant
presentation returns a typed `presentation_blocked` result with no claim that
the underlying feed is unavailable. Terminal Clock feedback can yield
atomically to a new dashboard; an actively sounding Clock alert retains display
and ring priority.

Train calls use the exact CRS for the station the wearer named. The public tool
contract explicitly distinguishes Blundellsands & Crosby (`BLN`), Liverpool
Central (`LVC`), and Liverpool Lime Street (`LIV`) so the model cannot replace
Liverpool Central with the city's mainline station.

Run the deterministic fake-relay suite with:

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest -v
```

This package and the clean-history native transport bridge are Apache-2.0.

The maintained end-to-end channel, workflow, reminder, configuration, and
release contract lives in the Hermes G2 app repository at
[`docs/hermes-mcp-architecture.md`](https://github.com/not-benny/hermes-g2/blob/main/docs/hermes-mcp-architecture.md).
