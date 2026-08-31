# How the two protocols are normalized

Meshtastic and MeshCore solve the same problem with different vocabularies.
This is the mapping Mesh-Spy applies so one dashboard can show both. It is
reference material for anyone extending an adapter; you do not need it to run
the console.

The target types are in `app/core/mesh/base.py`: `NodeRecord`, `MessageRecord`,
`TelemetryRecord` and `LinkStatus`. Every record keeps its untouched source
payload in `raw`, so a field no adapter normalizes is still recoverable through
`GET /api/nodes/{network}/{node_id}`.

## Concurrency, which drives everything else

| | Meshtastic | MeshCore |
| --- | --- | --- |
| Library | `meshtastic` 2.7+, GPL-3.0 | `meshcore` 2.3+, MIT |
| Model | synchronous, `pypubsub` callbacks on a reader thread | asyncio-native |
| Open | `SerialInterface(devPath=...)` | `await MeshCore.create_serial(...)` |
| Receive | `pub.subscribe(cb, "meshtastic.receive")` | `subscribe(EventType.CONTACT_MSG_RECV, cb)` |

`MeshtasticAdapter` opens the interface inside `asyncio.to_thread` and pushes
every pubsub callback back into the loop with `loop.call_soon_threadsafe`.
`MeshCoreAdapter` subscribes directly. Above the adapters, both look identical:
a stream of `MeshEvent` on one `asyncio.Queue`.

Because `pypubsub` is process-global, the Meshtastic adapter filters callbacks
by interface identity. Without that, two Meshtastic radios would each see the
other's packets.

## Identity

Meshtastic nodes carry a 32-bit node number, rendered `!a1b2c3d4` the way the
firmware and phone apps do. MeshCore keys contacts by public key, so the
adapter uses the key prefix as the id and resolves a name from the contact list
when it can.

These namespaces cannot be reconciled, and nothing in either protocol proves
two radios are the same device, so **nodes are never merged across networks**.
`NodeRecord.key` is `network:id`, which keeps them distinct everywhere
downstream.

`NodeRecord.merge` folds a newer sighting into an older one field by field
rather than overwriting. A position packet says nothing about battery and a
telemetry packet says nothing about position, so a wholesale replace would make
the nodes table flicker.

## Nodes

| `NodeRecord` | Meshtastic source | MeshCore source |
| --- | --- | --- |
| `id` | `num` as `!%08x` | public key prefix |
| `name` | `user.longName` | contact `adv_name` |
| `short_name` | `user.shortName` | — |
| `lat` / `lon` | `latitudeI` / `longitudeI`, degrees × 1e7 | advert lat/lon |
| `altitude` | `position.altitude` | — |
| `snr` | packet `rxSnr` | event SNR |
| `rssi` | packet `rxRssi` | event RSSI |
| `hops` | `hopStart - hopLimit` | path length, or `None` when flood-routed |
| `battery` | `deviceMetrics.batteryLevel` | `BATTERY` event |
| `voltage` | `deviceMetrics.voltage` | `BATTERY` event |
| `role` | `user.role` | contact type |
| `hw_model` | `user.hwModel` | — |

Two coordinate traps worth knowing about. Meshtastic's `latitudeI` of 0 is the
protobuf default meaning "no fix", not the equator, so it normalizes to `None`.
And a MeshCore contact reached by flood routing has no meaningful hop count, so
`hops` is `None` rather than 0 — reporting 0 would claim a direct neighbour.

## Messages

Meshtastic text arrives as a `TEXT_MESSAGE_APP` packet with a firmware-assigned
packet `id`, which the adapter uses for deduplication. MeshCore has no
equivalent stable identifier, so `message_id` is a SHA-256 over the sender
prefix, timestamp and text. Both let a repeated delivery be dropped.

`to_id` of `^all` (or absent) marks a broadcast. `direction` is `rx` or `tx`;
transmissions are recorded too, so the log shows both sides of a conversation.

## Telemetry

Stored long — one row per sample — rather than a column per metric, because the
two firmwares expose overlapping but unequal sensor sets and a wide table would
need a migration every time either adds one.

Metric names are normalized so a `battery` sparkline means the same thing on
both networks:

| Normalized | Meshtastic field | Unit |
| --- | --- | --- |
| `battery` | `deviceMetrics.batteryLevel` | % |
| `voltage` | `deviceMetrics.voltage` | V |
| `channel_utilization` | `deviceMetrics.channelUtilization` | % |
| `air_util_tx` | `deviceMetrics.airUtilTx` | % |
| `uptime` | `deviceMetrics.uptimeSeconds` | s |
| `temperature` | `environmentMetrics.temperature` | C |
| `humidity` | `environmentMetrics.relativeHumidity` | % |
| `pressure` | `environmentMetrics.barometricPressure` | hPa |

MeshCore reports battery and voltage through its `BATTERY` event, and any
sensors it exposes through `TELEMETRY_RESPONSE`, whose metric names are
lowercased with spaces replaced by underscores.

## Links

`LinkStatus.state` is one of `connecting`, `up`, `down`, `error` or `demo`.
`demo` is reserved for the simulated network so the UI can label a fake radio
instead of letting it pass for a real one. A link that is open but has heard
nothing for `mesh.stale_after_seconds` is reported stale in the UI rather than
down, since silence is normal on a quiet mesh.

## Adding a metric or field

1. Add it to the mapping table in the relevant adapter.
2. If it is a new metric, add its unit to `METRIC_UNITS` in `base.py`, or the
   sparkline axis will be unlabelled.
3. Add a case to the adapter's normalization test using a recorded payload.

New fields need no schema change: telemetry is already long-form, and node
fields not in `NodeRecord` survive in `raw`.
