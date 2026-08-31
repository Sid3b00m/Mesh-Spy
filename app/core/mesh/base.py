"""The shared vocabulary both mesh stacks are translated into.

Meshtastic and MeshCore model identity differently. Meshtastic uses a 32-bit
node number with long and short names; MeshCore keys contacts by public key and
learns about them through adverts. Rather than pick a winner, every adapter
normalizes into the records below and tags each one with its `network`.

Nodes are never merged across networks. A Meshtastic node and a MeshCore node
at the same physical site are separate entities, because nothing in either
protocol lets us prove they are the same radio.
"""
from __future__ import annotations

import time
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable

NETWORK_MESHTASTIC = "meshtastic"
NETWORK_MESHCORE = "meshcore"
NETWORKS = (NETWORK_MESHTASTIC, NETWORK_MESHCORE)

# Link lifecycle. "demo" marks the simulated network so the UI can label it
# rather than letting a fake radio pass for a real one.
LINK_CONNECTING = "connecting"
LINK_UP = "up"
LINK_DOWN = "down"
LINK_ERROR = "error"
LINK_DEMO = "demo"

# Event kinds carried on the registry queue.
EVENT_NODE = "node"
EVENT_MESSAGE = "message"
EVENT_TELEMETRY = "telemetry"
EVENT_LINK = "link"

BROADCAST = "^all"

_NAME_MAX = 64
_TEXT_MAX = 512


def utcnow() -> float:
    """Epoch seconds. Everything downstream sorts and plots on this."""
    return time.time()


def clean_text(value: Any, *, limit: int = _TEXT_MAX) -> str | None:
    """Strip control characters from node-supplied strings.

    Names routinely contain emoji, so this only removes the C0/C1 control
    ranges (which would corrupt logs and terminal output) and leaves the rest
    intact. This is not an HTML defence: the templates and the JS renderer
    escape separately.
    """
    if value is None:
        return None
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = str(value)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cc" or ch in "\n\t")
    text = text.strip()
    if not text:
        return None
    return text[:limit]


def clean_name(value: Any) -> str | None:
    return clean_text(value, limit=_NAME_MAX)


def coerce_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    # Protobuf defaults and bad GPS fixes both show up as NaN or inf.
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def coerce_int(value: Any) -> int | None:
    out = coerce_float(value)
    return None if out is None else int(out)


def meshtastic_node_id(num: Any) -> str | None:
    """Render a Meshtastic node number the way the firmware and apps do."""
    n = coerce_int(num)
    if n is None:
        return None
    return f"!{n & 0xFFFFFFFF:08x}"


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


@dataclass(slots=True)
class NodeRecord:
    """One node as last observed on one network."""

    network: str
    id: str
    link: str | None = None
    name: str | None = None
    short_name: str | None = None
    last_seen: float = field(default_factory=utcnow)
    lat: float | None = None
    lon: float | None = None
    altitude: float | None = None
    snr: float | None = None
    rssi: float | None = None
    hops: int | None = None
    battery: float | None = None
    voltage: float | None = None
    role: str | None = None
    hw_model: str | None = None
    is_self: bool = False
    # The untouched source payload, so a field we did not think to normalize
    # is still recoverable.
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.network}:{self.id}"

    def label(self) -> str:
        return self.name or self.short_name or self.id

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "network": self.network,
            "id": self.id,
            "key": self.key,
            "link": self.link,
            "name": self.name,
            "short_name": self.short_name,
            "label": self.label(),
            "last_seen": self.last_seen,
            "lat": self.lat,
            "lon": self.lon,
            "altitude": self.altitude,
            "snr": self.snr,
            "rssi": self.rssi,
            "hops": self.hops,
            "battery": self.battery,
            "voltage": self.voltage,
            "role": self.role,
            "hw_model": self.hw_model,
            "is_self": self.is_self,
        }
        if include_raw:
            out["raw"] = self.raw
        return out

    def merge(self, other: NodeRecord) -> NodeRecord:
        """Fold a newer sighting in without losing fields it did not carry.

        A position packet says nothing about battery, and a telemetry packet
        says nothing about position, so a naive overwrite would make the
        nodes table flicker.
        """
        if other.network != self.network or other.id != self.id:
            raise ValueError("refusing to merge records for different nodes")
        for attr in (
            "link", "name", "short_name", "lat", "lon", "altitude", "snr",
            "rssi", "hops", "battery", "voltage", "role", "hw_model",
        ):
            value = getattr(other, attr)
            if value is not None:
                setattr(self, attr, value)
        if other.is_self:
            self.is_self = True
        self.last_seen = max(self.last_seen, other.last_seen)
        if other.raw:
            self.raw = {**self.raw, **other.raw}
        return self


@dataclass(slots=True)
class MessageRecord:
    """A text message, received or sent."""

    network: str
    text: str
    link: str | None = None
    ts: float = field(default_factory=utcnow)
    from_id: str | None = None
    from_name: str | None = None
    to_id: str | None = None
    channel: str | None = None
    # "rx" or "tx". Transmissions are recorded so the log shows both sides.
    direction: str = "rx"
    snr: float | None = None
    rssi: float | None = None
    hops: int | None = None
    message_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_broadcast(self) -> bool:
        return self.to_id in (None, "", BROADCAST)

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        out: dict[str, Any] = {
            "network": self.network,
            "link": self.link,
            "ts": self.ts,
            "from_id": self.from_id,
            "from_name": self.from_name,
            "to_id": self.to_id,
            "channel": self.channel,
            "direction": self.direction,
            "text": self.text,
            "snr": self.snr,
            "rssi": self.rssi,
            "hops": self.hops,
            "message_id": self.message_id,
            "broadcast": self.is_broadcast,
        }
        if include_raw:
            out["raw"] = self.raw
        return out


@dataclass(slots=True)
class TelemetryRecord:
    """One metric sample.

    Stored long rather than wide: the two protocols expose overlapping but
    unequal metric sets, and a metric column per field would mean a migration
    every time either firmware adds a sensor.
    """

    network: str
    node_id: str
    metric: str
    value: float
    link: str | None = None
    ts: float = field(default_factory=utcnow)
    unit: str | None = None

    @property
    def series_key(self) -> tuple[str, str, str]:
        return (self.network, self.node_id, self.metric)

    def to_dict(self) -> dict[str, Any]:
        return {
            "network": self.network,
            "node_id": self.node_id,
            "link": self.link,
            "ts": self.ts,
            "metric": self.metric,
            "value": self.value,
            "unit": self.unit,
        }


# Metric names are normalized so a sparkline for "battery" means the same
# thing on both networks.
METRIC_UNITS = {
    "battery": "%",
    "voltage": "V",
    "snr": "dB",
    "rssi": "dBm",
    "temperature": "C",
    "humidity": "%",
    "pressure": "hPa",
    "channel_utilization": "%",
    "air_util_tx": "%",
    "uptime": "s",
    "tx_queue": "",
    "noise_floor": "dBm",
}


@dataclass(slots=True)
class LinkStatus:
    """Health of one configured radio, as shown in the links panel."""

    key: str
    network: str
    name: str
    transport: str
    target: str
    state: str = LINK_CONNECTING
    detail: str | None = None
    since: float = field(default_factory=utcnow)
    last_event_at: float | None = None
    attempts: int = 0
    node_id: str | None = None
    firmware: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "network": self.network,
            "name": self.name,
            "transport": self.transport,
            "target": self.target,
            "state": self.state,
            "detail": self.detail,
            "since": self.since,
            "last_event_at": self.last_event_at,
            "attempts": self.attempts,
            "node_id": self.node_id,
            "firmware": self.firmware,
        }


@dataclass(slots=True)
class MeshEvent:
    """The single event type that crosses from the adapters into the app."""

    kind: str
    payload: NodeRecord | MessageRecord | TelemetryRecord | LinkStatus

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "data": self.payload.to_dict()}


EmitFn = Callable[[MeshEvent], None]


class SendError(RuntimeError):
    """Raised when a transmit cannot be attempted or the radio rejects it."""


class MeshAdapter(ABC):
    """Wraps one radio and emits normalized events.

    Subclasses own their concurrency model. MeshCore is asyncio-native and
    subscribes directly; Meshtastic is synchronous and delivers pubsub
    callbacks on its own reader thread, which the adapter marshals back into
    the loop. Nothing above this class needs to know which is which.
    """

    network: str = ""

    def __init__(self, name: str, emit: EmitFn) -> None:
        self.name = name
        self._emit = emit
        self._link = LinkStatus(
            key=f"{self.network}:{name}",
            network=self.network,
            name=name,
            transport="",
            target="",
        )

    @property
    def key(self) -> str:
        return self._link.key

    @property
    def link(self) -> LinkStatus:
        return self._link

    def emit(self, event: MeshEvent) -> None:
        if event.kind != EVENT_LINK:
            self._link.last_event_at = utcnow()
        self._emit(event)

    def emit_node(self, node: NodeRecord) -> None:
        node.link = node.link or self.name
        self.emit(MeshEvent(EVENT_NODE, node))

    def emit_message(self, message: MessageRecord) -> None:
        message.link = message.link or self.name
        self.emit(MeshEvent(EVENT_MESSAGE, message))

    def emit_telemetry(self, sample: TelemetryRecord) -> None:
        sample.link = sample.link or self.name
        sample.unit = sample.unit or METRIC_UNITS.get(sample.metric)
        self.emit(MeshEvent(EVENT_TELEMETRY, sample))

    def set_state(self, state: str, detail: str | None = None) -> None:
        changed = self._link.state != state or self._link.detail != detail
        self._link.state = state
        self._link.detail = detail
        if changed:
            self._link.since = utcnow()
            self.emit(MeshEvent(EVENT_LINK, self._link))

    @abstractmethod
    async def start(self) -> None:
        """Open the radio. Raise to signal the supervisor to back off."""

    @abstractmethod
    async def stop(self) -> None:
        """Close the radio. Must be safe to call when never started."""

    @abstractmethod
    async def send_message(
        self,
        text: str,
        *,
        dest: str | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        """Transmit. Raise SendError on refusal."""

    @abstractmethod
    def describe(self) -> dict[str, Any]:
        """Static facts about the link, for the API and the links panel."""

    def _describe_base(self, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        out = self._link.to_dict()
        out.update(_drop_none(extra or {}))
        return out
