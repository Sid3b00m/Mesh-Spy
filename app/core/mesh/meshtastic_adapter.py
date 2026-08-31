"""Meshtastic adapter: the thread-to-loop bridge.

`meshtastic` is synchronous. Opening an interface blocks while the node
database downloads, and from then on packets arrive as `pypubsub` callbacks on
the library's own reader thread. Two consequences drive this file:

* Anything that blocks runs through `asyncio.to_thread`, so the event loop is
  never stalled by a serial read or a 300-second connect timeout.
* Every pubsub callback immediately hands off with `call_soon_threadsafe` and
  does no normalization on the reader thread. All adapter state is therefore
  mutated on the loop thread only, which is what makes the lock-free store
  safe.

`pypubsub` topics are global, so a callback fires for every open interface in
the process. Each handler filters on identity first; without that, two
configured radios would each record the other's traffic.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from app.core.config import RadioConfig
from app.core.mesh.base import (
    BROADCAST,
    LINK_CONNECTING,
    LINK_DOWN,
    LINK_UP,
    NETWORK_MESHTASTIC,
    EmitFn,
    MeshAdapter,
    MessageRecord,
    NodeRecord,
    SendError,
    TelemetryRecord,
    clean_name,
    clean_text,
    coerce_float,
    coerce_int,
    meshtastic_node_id,
    utcnow,
)

log = logging.getLogger("mesh-spy.meshtastic")

DEFAULT_TCP_PORT = 4403

# Position is transmitted as a scaled integer.
_COORD_SCALE = 1e-7

TOPIC_RECEIVE = "meshtastic.receive"
TOPIC_CONNECTION_ESTABLISHED = "meshtastic.connection.established"
TOPIC_CONNECTION_LOST = "meshtastic.connection.lost"
TOPIC_NODE_UPDATED = "meshtastic.node.updated"

# deviceMetrics / environmentMetrics field name -> normalized metric name.
_DEVICE_METRICS = {
    "batteryLevel": "battery",
    "voltage": "voltage",
    "channelUtilization": "channel_utilization",
    "airUtilTx": "air_util_tx",
    "uptimeSeconds": "uptime",
}
_ENVIRONMENT_METRICS = {
    "temperature": "temperature",
    "relativeHumidity": "humidity",
    "barometricPressure": "pressure",
}


def _coord(value: Any) -> float | None:
    """latitudeI / longitudeI are degrees * 1e7."""
    raw = coerce_int(value)
    if raw is None or raw == 0:
        # 0 is the protobuf default for "no position", not the equator.
        return None
    return raw * _COORD_SCALE


def _hops(packet: dict[str, Any]) -> int | None:
    """Hops traversed, derived from how much of the hop budget was spent."""
    start = coerce_int(packet.get("hopStart"))
    limit = coerce_int(packet.get("hopLimit"))
    if start is None or limit is None:
        return None
    travelled = start - limit
    return travelled if travelled >= 0 else None


def _packet_ts(packet: dict[str, Any]) -> float:
    ts = coerce_float(packet.get("rxTime"))
    # rxTime is 0 until the radio has a clock.
    if ts is None or ts <= 0:
        return utcnow()
    return ts


def _node_id_from(packet: dict[str, Any], key: str, num_key: str) -> str | None:
    """Prefer the library's rendered id, fall back to the raw node number."""
    text = clean_text(packet.get(key), limit=32)
    if text and text != BROADCAST:
        return text
    return meshtastic_node_id(packet.get(num_key))


class MeshtasticAdapter(MeshAdapter):
    network = NETWORK_MESHTASTIC

    def __init__(
        self,
        config: RadioConfig,
        emit: EmitFn,
        *,
        factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__(config.name, emit)
        self.config = config
        self._link.transport = config.transport
        self._link.target = config.describe_target()
        # Injected so tests can supply a fake interface without a radio.
        self._factory = factory or self._open_interface
        self._iface: Any = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._my_num: int | None = None
        self._subscribed = False

    # ---- transport ----

    def _open_interface(self) -> Any:
        """Blocking. Always called through asyncio.to_thread."""
        cfg = self.config
        if cfg.transport == "serial":
            from meshtastic.serial_interface import SerialInterface

            return SerialInterface(devPath=cfg.port)
        if cfg.transport == "tcp":
            from meshtastic.tcp_interface import TCPInterface

            return TCPInterface(
                hostname=cfg.host, portNumber=cfg.tcp_port or DEFAULT_TCP_PORT
            )
        if cfg.transport == "ble":
            from meshtastic.ble_interface import BLEInterface

            return BLEInterface(address=cfg.address)
        raise SendError(f"unsupported transport {cfg.transport!r}")

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self.set_state(LINK_CONNECTING, f"opening {self._link.target}")
        # Blocks until the node database has downloaded.
        self._iface = await asyncio.to_thread(self._factory)
        self._subscribe()
        self._read_my_info()
        self._seed_nodes()
        self.set_state(LINK_UP, self._link.firmware)

    async def stop(self) -> None:
        self._unsubscribe()
        iface, self._iface = self._iface, None
        if iface is not None:
            try:
                await asyncio.to_thread(iface.close)
            except Exception:  # noqa: BLE001 - teardown must not raise
                log.debug("%s: close failed", self.name, exc_info=True)
        self._loop = None
        self.set_state(LINK_DOWN, "stopped")

    # ---- pubsub plumbing ----

    def _subscribe(self) -> None:
        from pubsub import pub

        pub.subscribe(self._pubsub_receive, TOPIC_RECEIVE)
        pub.subscribe(self._pubsub_connected, TOPIC_CONNECTION_ESTABLISHED)
        pub.subscribe(self._pubsub_lost, TOPIC_CONNECTION_LOST)
        pub.subscribe(self._pubsub_node_updated, TOPIC_NODE_UPDATED)
        self._subscribed = True

    def _unsubscribe(self) -> None:
        if not self._subscribed:
            return
        from pubsub import pub

        for listener, topic in (
            (self._pubsub_receive, TOPIC_RECEIVE),
            (self._pubsub_connected, TOPIC_CONNECTION_ESTABLISHED),
            (self._pubsub_lost, TOPIC_CONNECTION_LOST),
            (self._pubsub_node_updated, TOPIC_NODE_UPDATED),
        ):
            try:
                pub.unsubscribe(listener, topic)
            except Exception:  # noqa: BLE001
                log.debug("%s: unsubscribe %s failed", self.name, topic, exc_info=True)
        self._subscribed = False

    def _mine(self, interface: Any) -> bool:
        """Topics are process-global, so reject other interfaces' traffic."""
        return interface is not None and interface is self._iface

    def _to_loop(self, fn: Callable[..., None], *args: Any) -> None:
        """Hand off from the reader thread. The only crossing point."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            # Loop shut down between the check and the call.
            log.debug("%s: loop gone, dropping event", self.name)

    # These four run on the meshtastic reader thread. They must not touch
    # adapter state or the store.

    def _pubsub_receive(self, packet=None, interface=None, **_kwargs: Any) -> None:
        if not self._mine(interface) or not isinstance(packet, dict):
            return
        self._to_loop(self._handle_packet, dict(packet))

    def _pubsub_connected(self, interface=None, **_kwargs: Any) -> None:
        if not self._mine(interface):
            return
        self._to_loop(self.set_state, LINK_UP, "radio reported connected")

    def _pubsub_lost(self, interface=None, **_kwargs: Any) -> None:
        if not self._mine(interface):
            return
        self._to_loop(self.set_state, LINK_DOWN, "radio reported connection lost")

    def _pubsub_node_updated(self, node=None, interface=None, **_kwargs: Any) -> None:
        if not self._mine(interface) or not isinstance(node, dict):
            return
        self._to_loop(self._handle_node, dict(node))

    # ---- seeding (loop thread) ----

    def _read_my_info(self) -> None:
        iface = self._iface
        try:
            info = iface.getMyNodeInfo() if iface is not None else None
        except Exception:  # noqa: BLE001
            info = None
        if isinstance(info, dict):
            self._my_num = coerce_int(info.get("num"))
            self._link.node_id = meshtastic_node_id(self._my_num)
        metadata = getattr(iface, "metadata", None)
        version = getattr(metadata, "firmware_version", None)
        self._link.firmware = clean_text(version, limit=32)

    def _seed_nodes(self) -> None:
        """Publish the downloaded node database.

        Also covers the gap between the interface constructor firing pubsub
        events and this adapter having subscribed.
        """
        nodes = getattr(self._iface, "nodes", None) or {}
        for entry in list(nodes.values()):
            if isinstance(entry, dict):
                self._handle_node(entry)

    # ---- normalization (loop thread) ----

    def _handle_node(self, entry: dict[str, Any]) -> None:
        num = coerce_int(entry.get("num"))
        user = entry.get("user") if isinstance(entry.get("user"), dict) else {}
        node_id = clean_text(user.get("id"), limit=32) or meshtastic_node_id(num)
        if not node_id:
            return

        position = entry.get("position") if isinstance(entry.get("position"), dict) else {}
        metrics = (
            entry.get("deviceMetrics")
            if isinstance(entry.get("deviceMetrics"), dict)
            else {}
        )
        last_heard = coerce_float(entry.get("lastHeard"))

        self.emit_node(
            NodeRecord(
                network=self.network,
                id=node_id,
                name=clean_name(user.get("longName")),
                short_name=clean_name(user.get("shortName")),
                hw_model=clean_name(user.get("hwModel")),
                role=clean_name(user.get("role")),
                lat=_coord(position.get("latitudeI")),
                lon=_coord(position.get("longitudeI")),
                altitude=coerce_float(position.get("altitude")),
                snr=coerce_float(entry.get("snr")),
                hops=coerce_int(entry.get("hopsAway")),
                battery=coerce_float(metrics.get("batteryLevel")),
                voltage=coerce_float(metrics.get("voltage")),
                is_self=num is not None and num == self._my_num,
                last_seen=last_heard if last_heard else utcnow(),
                raw=entry,
            )
        )
        if metrics:
            self._emit_metrics(node_id, metrics, _DEVICE_METRICS, utcnow())

    def _handle_packet(self, packet: dict[str, Any]) -> None:
        decoded = packet.get("decoded")
        if not isinstance(decoded, dict):
            # An encrypted packet we hold no key for. Still a sighting.
            self._emit_sighting(packet)
            return

        portnum = clean_text(decoded.get("portnum"), limit=48) or ""
        from_id = _node_id_from(packet, "fromId", "from")
        ts = _packet_ts(packet)

        if portnum == "TEXT_MESSAGE_APP":
            self._handle_text(packet, decoded, from_id, ts)
        elif portnum == "POSITION_APP":
            self._handle_position(packet, decoded, from_id, ts)
        elif portnum == "NODEINFO_APP":
            self._handle_user(packet, decoded, from_id, ts)
        elif portnum == "TELEMETRY_APP":
            self._handle_telemetry(packet, decoded, from_id, ts)
        else:
            self._emit_sighting(packet)

    def _emit_sighting(self, packet: dict[str, Any]) -> None:
        """Record radio quality for a packet we cannot otherwise interpret."""
        from_id = _node_id_from(packet, "fromId", "from")
        if not from_id:
            return
        self.emit_node(
            NodeRecord(
                network=self.network,
                id=from_id,
                snr=coerce_float(packet.get("rxSnr")),
                rssi=coerce_float(packet.get("rxRssi")),
                hops=_hops(packet),
                last_seen=_packet_ts(packet),
            )
        )

    def _handle_text(
        self, packet: dict[str, Any], decoded: dict[str, Any], from_id: str | None, ts: float
    ) -> None:
        text = clean_text(decoded.get("text"))
        if not text:
            payload = decoded.get("payload")
            text = clean_text(payload) if isinstance(payload, (bytes, str)) else None
        if not text:
            return
        to_id = _node_id_from(packet, "toId", "to")
        channel = coerce_int(packet.get("channel"))
        packet_id = coerce_int(packet.get("id"))
        self.emit_message(
            MessageRecord(
                network=self.network,
                text=text,
                ts=ts,
                from_id=from_id,
                to_id=None if packet.get("toId") == BROADCAST else to_id,
                channel=str(channel) if channel is not None else None,
                snr=coerce_float(packet.get("rxSnr")),
                rssi=coerce_float(packet.get("rxRssi")),
                hops=_hops(packet),
                message_id=str(packet_id) if packet_id else None,
                raw=packet,
            )
        )
        self._emit_sighting(packet)

    def _handle_position(
        self, packet: dict[str, Any], decoded: dict[str, Any], from_id: str | None, ts: float
    ) -> None:
        position = decoded.get("position")
        if not from_id or not isinstance(position, dict):
            return
        self.emit_node(
            NodeRecord(
                network=self.network,
                id=from_id,
                lat=_coord(position.get("latitudeI")),
                lon=_coord(position.get("longitudeI")),
                altitude=coerce_float(position.get("altitude")),
                snr=coerce_float(packet.get("rxSnr")),
                rssi=coerce_float(packet.get("rxRssi")),
                hops=_hops(packet),
                last_seen=ts,
                raw=position,
            )
        )

    def _handle_user(
        self, packet: dict[str, Any], decoded: dict[str, Any], from_id: str | None, ts: float
    ) -> None:
        user = decoded.get("user")
        if not isinstance(user, dict):
            return
        node_id = clean_text(user.get("id"), limit=32) or from_id
        if not node_id:
            return
        self.emit_node(
            NodeRecord(
                network=self.network,
                id=node_id,
                name=clean_name(user.get("longName")),
                short_name=clean_name(user.get("shortName")),
                hw_model=clean_name(user.get("hwModel")),
                role=clean_name(user.get("role")),
                snr=coerce_float(packet.get("rxSnr")),
                rssi=coerce_float(packet.get("rxRssi")),
                hops=_hops(packet),
                last_seen=ts,
                raw=user,
            )
        )

    def _handle_telemetry(
        self, packet: dict[str, Any], decoded: dict[str, Any], from_id: str | None, ts: float
    ) -> None:
        telemetry = decoded.get("telemetry")
        if not from_id or not isinstance(telemetry, dict):
            return
        device = telemetry.get("deviceMetrics")
        if isinstance(device, dict):
            self._emit_metrics(from_id, device, _DEVICE_METRICS, ts)
            self.emit_node(
                NodeRecord(
                    network=self.network,
                    id=from_id,
                    battery=coerce_float(device.get("batteryLevel")),
                    voltage=coerce_float(device.get("voltage")),
                    snr=coerce_float(packet.get("rxSnr")),
                    rssi=coerce_float(packet.get("rxRssi")),
                    hops=_hops(packet),
                    last_seen=ts,
                )
            )
        environment = telemetry.get("environmentMetrics")
        if isinstance(environment, dict):
            self._emit_metrics(from_id, environment, _ENVIRONMENT_METRICS, ts)

    def _emit_metrics(
        self,
        node_id: str,
        source: dict[str, Any],
        mapping: dict[str, str],
        ts: float,
    ) -> None:
        for field, metric in mapping.items():
            value = coerce_float(source.get(field))
            if value is None:
                continue
            self.emit_telemetry(
                TelemetryRecord(
                    network=self.network,
                    node_id=node_id,
                    metric=metric,
                    value=value,
                    ts=ts,
                )
            )

    # ---- transmit ----

    async def send_message(
        self,
        text: str,
        *,
        dest: str | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        iface = self._iface
        if iface is None:
            raise SendError("radio is not connected")

        destination = dest or BROADCAST
        index = channel or 0
        try:
            await asyncio.to_thread(
                iface.sendText,
                text,
                destinationId=destination,
                # An ack round-trip would hold the worker thread open for the
                # length of a mesh timeout.
                wantAck=False,
                channelIndex=index,
            )
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            raise SendError(f"radio rejected the message: {exc}") from exc

        ts = utcnow()
        self.emit_message(
            MessageRecord(
                network=self.network,
                text=text,
                ts=ts,
                from_id=self._link.node_id,
                to_id=None if destination == BROADCAST else destination,
                channel=str(index),
                direction="tx",
            )
        )
        return {"ok": True, "network": self.network, "link": self.name, "ts": ts}

    def describe(self) -> dict[str, Any]:
        extra: dict[str, Any] = {"library": "meshtastic"}
        nodes = getattr(self._iface, "nodes", None)
        if isinstance(nodes, dict):
            extra["known_nodes"] = len(nodes)
        return self._describe_base(extra)
