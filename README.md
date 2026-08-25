# Hermes G2 Workflows MCP

This package moves G2 workflow policy out of a personal `SOUL.md`. It exposes
intent-complete MCP tools for Work Tasks, Clock, one-shot reminders, typed
National Rail departures, typed Open-Meteo/UKMO weather with phone-owned
final presentation, and six grouped device workflows: Apps, Media, Navigation,
Notifications, Health Summary, and Calendar Agenda.

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
  and the status-only Cockpit resource. Companion is deliberately unavailable.

The authenticated WSS is transport only. Workflow tools reach the live device
through a same-UID, private Unix relay owned by the native platform adapter.
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

Durable mutators (Work Tasks, Clock, and reminders) derive a content-free
operation ID from trusted call identity so a bounded transport retry reuses the
same ID. Legacy phone actions without operation-ID support are never retried;
a lost response is reported as an unknown outcome instead of claiming success.

Run the deterministic fake-relay suite with:

```sh
PYTHONDONTWRITEBYTECODE=1 python -B -m unittest -v
```

This new package is Apache-2.0. The current native transport bridge remains
`UNLICENSED`; do not publish a combined distribution until its provenance and
redistribution rights are resolved or it is clean-room replaced.

The maintained end-to-end channel, workflow, reminder, configuration, and
release contract lives in the Hermes G2 app repository at
[`docs/hermes-mcp-architecture.md`](https://github.com/not-benny/hermes-g2/blob/main/docs/hermes-mcp-architecture.md).
