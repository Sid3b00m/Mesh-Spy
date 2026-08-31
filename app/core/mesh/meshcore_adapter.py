"""MeshCore adapter.

`meshcore` is asyncio-native, so this is the easy half: subscribe to the event
types we care about and translate each payload. No thread bridging is needed,
unlike the Meshtastic side.

MeshCore identifies nodes by a 32-byte public key, but received messages only
carry the leading 6 bytes as `pubkey_prefix`. The prefix is therefore what we
use as the node id, so a message and an advert for the same radio land on the
same node without a lookup that might not resolve yet. The full key is kept in
`raw`.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Awaitable, Callable

from app.core.config import RadioConfig
from app.core.mesh.base import (
    LINK_CONNECTING,
    LINK_DOWN,
    LINK_UP,
    NETWORK_MESHCORE,
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
    utcnow,
)

log = logging.getLogger("mesh-spy.meshcore")

# The companion TCP service does not have a registered default the way
# Meshtastic's 4403 does, so this is only a fallback when config omits it.
DEFAULT_TCP_PORT = 5000

# Length of the public-key prefix used as the node id, in hex characters
# (6 bytes, matching what the firmware puts in a received message).
KEY_PREFIX_HEX = 12

# `path_len` is 255 for a direct contact rather than 0.
PATH_LEN_DIRECT = 255

# Timestamps below this are an unset clock, not a real date.
_MIN_PLAUSIBLE_TS = 946_684_800.0  # 2000-01-01


def _hops_from_path_len(path_len: Any) -> int | None:
    value = coerce_int(path_len)
    if value is None:
        return None
    if value == PATH_LEN_DIRECT:
        return 0
    return value


def _sender_ts(payload: dict[str, Any]) -> float:
    """Trust the sender's clock only when it looks set."""
    now = utcnow()
    ts = coerce_float(payload.get("sender_timestamp"))
    if ts is None or ts < _MIN_PLAUSIBLE_TS or ts > now + 86400:
        return now
    return ts


def _synthetic_message_id(*parts: Any) -> str:
    """MeshCore text frames carry no packet id.

    Hashing the sender, its timestamp and the text gives a stable id, so
    re-fetching after a reconnect re-inserts the same row instead of a
    duplicate. Two different messages would have to share a sender, a
    one-second timestamp and identical text to collide, which is the same
    packet by any useful definition.
    """
    digest = hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8"))
    return digest.hexdigest()[:16]


def _key_prefix(public_key: Any) -> str | None:
    key = clean_text(public_key, limit=64)
    if not key:
        return None
    return key[:KEY_PREFIX_HEX].lower()


class MeshCoreAdapter(MeshAdapter):
    network = NETWORK_MESHCORE

    def __init__(
        self,
        config: RadioConfig,
        emit: EmitFn,
        *,
        factory: Callable[[], Awaitable[Any]] | None = None,
    ) -> None:
        super().__init__(config.name, emit)
        self.config = config
        self._link.transport = config.transport
        self._link.target = config.describe_target()
        # Injected so tests can drive the adapter with a fake device.
        self._factory = factory or self._open_transport
        self._mc: Any = None
        self._subscriptions: list[Any] = []

    # ---- transport ----

    async def _open_transport(self) -> Any:
        from meshcore import MeshCore

        cfg = self.config
        if cfg.transport == "serial":
            return await MeshCore.create_serial(port=cfg.port, baudrate=cfg.baud)
        if cfg.transport == "tcp":
            return await MeshCore.create_tcp(
                host=cfg.host, port=cfg.tcp_port or DEFAULT_TCP_PORT
            )
        if cfg.transport == "ble":
            return await MeshCore.create_ble(address=cfg.address, pin=cfg.pin)
        raise SendError(f"unsupported transport {cfg.transport!r}")

    async def start(self) -> None:
        self.set_state(LINK_CONNECTING, f"opening {self._link.target}")
        self._mc = await self._factory()
        self._subscribe()
        await self._load_self_info()
        await self._load_contacts()
        # Without this the firmware holds messages until polled, so the
        # dashboard would only update when something else happened to ask.
        starter = getattr(self._mc, "start_auto_message_fetching", None)
        if starter is not None:
            result = starter()
            if hasattr(result, "__await__"):
                await result
        self.set_state(LINK_UP, self._link.firmware)

    async def stop(self) -> None:
        mc, self._mc = self._mc, None
        for sub in self._subscriptions:
            unsub = getattr(sub, "unsubscribe", None)
            try:
                if callable(unsub):
                    unsub()
                elif mc is not None:
                    mc.unsubscribe(sub)
            except Exception:  # noqa: BLE001 - teardown must not raise
                log.debug("%s: unsubscribe failed", self.name, exc_info=True)
        self._subscriptions.clear()
        if mc is None:
            return
        for method in ("stop_auto_message_fetching", "disconnect", "stop"):
            fn = getattr(mc, method, None)
            if fn is None:
                continue
            try:
                result = fn()
                if hasattr(result, "__await__"):
                    await result
            except Exception:  # noqa: BLE001
                log.debug("%s: %s failed", self.name, method, exc_info=True)
        self.set_state(LINK_DOWN, "stopped")

    def _subscribe(self) -> None:
        from meshcore import EventType

        handlers = (
            (EventType.CONTACT_MSG_RECV, self._on_contact_message),
            (EventType.CHANNEL_MSG_RECV, self._on_channel_message),
            (EventType.ADVERTISEMENT, self._on_advert),
            (EventType.NEW_CONTACT, self._on_contact),
            (EventType.NEXT_CONTACT, self._on_contact),
            (EventType.PATH_UPDATE, self._on_advert),
            (EventType.CONTACTS, self._on_contacts),
            (EventType.SELF_INFO, self._on_self_info),
            (EventType.BATTERY, self._on_battery),
            (EventType.TELEMETRY_RESPONSE, self._on_telemetry),
            (EventType.DISCONNECTED, self._on_disconnected),
        )
        for event_type, handler in handlers:
            self._subscriptions.append(self._mc.subscribe(event_type, handler))

    # ---- seeding ----

    async def _load_self_info(self) -> None:
        info = getattr(self._mc, "self_info", None)
        if isinstance(info, dict) and info:
            self._ingest_self_info(info)

    async def _load_contacts(self) -> None:
        ensure = getattr(self._mc, "ensure_contacts", None)
        if ensure is not None:
            try:
                await ensure()
            except Exception:  # noqa: BLE001
                # A radio with no contacts yet is normal, not a failure.
                log.debug("%s: ensure_contacts failed", self.name, exc_info=True)
        contacts = getattr(self._mc, "contacts", None) or {}
        for contact in list(contacts.values()):
            if isinstance(contact, dict):
                self._ingest_contact(contact)

    # ---- normalization ----

    def _ingest_self_info(self, info: dict[str, Any]) -> None:
        node_id = _key_prefix(info.get("public_key"))
        if not node_id:
            return
        self._link.node_id = node_id
        freq = coerce_float(info.get("radio_freq"))
        self._link.firmware = f"{freq:g} MHz" if freq else None
        self.emit_node(
            NodeRecord(
                network=self.network,
                id=node_id,
                name=clean_name(info.get("name")),
                lat=coerce_float(info.get("adv_lat")),
                lon=coerce_float(info.get("adv_lon")),
                hops=0,
                is_self=True,
                last_seen=utcnow(),
                raw=dict(info),
            )
        )

    def _ingest_contact(self, contact: dict[str, Any]) -> None:
        node_id = _key_prefix(contact.get("public_key"))
        if not node_id:
            return
        last_advert = coerce_float(contact.get("last_advert"))
        if last_advert is None or last_advert < _MIN_PLAUSIBLE_TS:
            last_advert = utcnow()
        lat = coerce_float(contact.get("adv_lat"))
        lon = coerce_float(contact.get("adv_lon"))
        # A flood-routed contact reports -1 for the hash mode. It has no known
        # path, so reporting 0 hops would claim it is a direct neighbour.
        flood = coerce_int(contact.get("out_path_hash_mode")) == -1
        self.emit_node(
            NodeRecord(
                network=self.network,
                id=node_id,
                name=clean_name(contact.get("adv_name")),
                hops=None if flood else _hops_from_path_len(contact.get("out_path_len")),
                # 0,0 is the firmware default for "location not advertised",
                # not a position in the Gulf of Guinea.
                lat=lat if lat or lon else None,
                lon=lon if lat or lon else None,
                last_seen=last_advert,
                raw=dict(contact),
            )
        )

    def _resolve_contact(self, prefix: str) -> dict[str, Any] | None:
        getter = getattr(self._mc, "get_contact_by_key_prefix", None)
        if getter is None:
            return None
        try:
            found = getter(prefix)
        except Exception:  # noqa: BLE001
            return None
        return found if isinstance(found, dict) else None

    async def _on_contact_message(self, event: Any) -> None:
        payload = dict(getattr(event, "payload", None) or {})
        text = clean_text(payload.get("text"))
        if not text:
            return
        prefix = _key_prefix(payload.get("pubkey_prefix"))
        ts = _sender_ts(payload)
        contact = self._resolve_contact(prefix) if prefix else None
        name = clean_name((contact or {}).get("adv_name"))

        self.emit_message(
            MessageRecord(
                network=self.network,
                text=text,
                ts=ts,
                from_id=prefix,
                from_name=name,
                to_id=self._link.node_id,
                channel=None,
                snr=coerce_float(payload.get("SNR")),
                rssi=coerce_float(payload.get("RSSI")),
                hops=_hops_from_path_len(payload.get("path_len")),
                message_id=_synthetic_message_id("priv", prefix, payload.get("sender_timestamp"), text),
                raw=payload,
            )
        )
        if prefix:
            # Hearing from a node is itself a sighting.
            self.emit_node(
                NodeRecord(
                    network=self.network,
                    id=prefix,
                    name=name,
                    snr=coerce_float(payload.get("SNR")),
                    rssi=coerce_float(payload.get("RSSI")),
                    hops=_hops_from_path_len(payload.get("path_len")),
                    last_seen=ts,
                )
            )

    async def _on_channel_message(self, event: Any) -> None:
        payload = dict(getattr(event, "payload", None) or {})
        text = clean_text(payload.get("text"))
        if not text:
            return
        channel = coerce_int(payload.get("channel_idx"))
        ts = _sender_ts(payload)
        self.emit_message(
            MessageRecord(
                network=self.network,
                text=text,
                ts=ts,
                from_id=None,
                # Channel messages are encrypted to the channel key, so the
                # firmware cannot tell us who sent one.
                from_name=None,
                to_id=None,
                channel=str(channel) if channel is not None else None,
                snr=coerce_float(payload.get("SNR")),
                rssi=coerce_float(payload.get("RSSI")),
                hops=_hops_from_path_len(payload.get("path_len")),
                message_id=_synthetic_message_id("chan", channel, payload.get("sender_timestamp"), text),
                raw=payload,
            )
        )

    async def _on_advert(self, event: Any) -> None:
        payload = dict(getattr(event, "payload", None) or {})
        node_id = _key_prefix(payload.get("public_key"))
        if not node_id:
            return
        self.emit_node(
            NodeRecord(
                network=self.network,
                id=node_id,
                last_seen=utcnow(),
                raw=payload,
            )
        )

    async def _on_contact(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            self._ingest_contact(payload)

    async def _on_contacts(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return
        for contact in list(payload.values()):
            if isinstance(contact, dict):
                self._ingest_contact(contact)

    async def _on_self_info(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if isinstance(payload, dict):
            self._ingest_self_info(payload)

    async def _on_battery(self, event: Any) -> None:
        payload = dict(getattr(event, "payload", None) or {})
        level = coerce_float(payload.get("level"))
        if level is None or not self._link.node_id:
            return
        # The firmware reports millivolts. Values small enough to be a
        # percentage are treated as one rather than as a 41 mV battery.
        if level > 1000:
            metric, value, unit = "voltage", level / 1000.0, "V"
        else:
            metric, value, unit = "battery", level, "%"
        self.emit_telemetry(
            TelemetryRecord(
                network=self.network,
                node_id=self._link.node_id,
                metric=metric,
                value=value,
                unit=unit,
            )
        )

    async def _on_telemetry(self, event: Any) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return
        node_id = _key_prefix(payload.get("public_key")) or self._link.node_id
        if not node_id:
            return
        readings = payload.get("lpp") if isinstance(payload.get("lpp"), list) else None
        if readings:
            # CayenneLPP: a list of {channel, type, value} entries.
            for entry in readings:
                if not isinstance(entry, dict):
                    continue
                metric = clean_text(entry.get("type"), limit=32)
                value = coerce_float(entry.get("value"))
                if metric and value is not None:
                    self.emit_telemetry(
                        TelemetryRecord(
                            network=self.network,
                            node_id=node_id,
                            metric=metric.lower().replace(" ", "_"),
                            value=value,
                        )
                    )
            return
        for key, metric in (
            ("battery", "battery"),
            ("voltage", "voltage"),
            ("temperature", "temperature"),
            ("humidity", "humidity"),
            ("pressure", "pressure"),
        ):
            value = coerce_float(payload.get(key))
            if value is not None:
                self.emit_telemetry(
                    TelemetryRecord(
                        network=self.network, node_id=node_id, metric=metric, value=value
                    )
                )

    async def _on_disconnected(self, event: Any) -> None:
        self.set_state(LINK_DOWN, "radio reported disconnect")

    # ---- transmit ----

    async def send_message(
        self,
        text: str,
        *,
        dest: str | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        if self._mc is None:
            raise SendError("radio is not connected")
        commands = getattr(self._mc, "commands", None)
        if commands is None:
            raise SendError("radio exposes no command interface")

        if dest:
            contact = self._resolve_contact(dest)
            if contact is None:
                getter = getattr(self._mc, "get_contact_by_name", None)
                if getter is not None:
                    try:
                        found = getter(dest)
                        contact = found if isinstance(found, dict) else None
                    except Exception:  # noqa: BLE001
                        contact = None
            if contact is None:
                raise SendError(f"no MeshCore contact matches {dest!r}")
            result = await commands.send_msg(contact, text)
            to_id = _key_prefix(contact.get("public_key"))
            channel_label = None
        else:
            index = channel or 0
            result = await commands.send_chan_msg(index, text)
            to_id = None
            channel_label = str(index)

        if getattr(result, "is_error", False):
            raise SendError(f"radio rejected the message: {getattr(result, 'payload', '')}")

        ts = utcnow()
        self.emit_message(
            MessageRecord(
                network=self.network,
                text=text,
                ts=ts,
                from_id=self._link.node_id,
                to_id=to_id,
                channel=channel_label,
                direction="tx",
                message_id=_synthetic_message_id("tx", to_id, channel_label, ts, text),
            )
        )
        return {"ok": True, "network": self.network, "link": self.name, "ts": ts}

    def describe(self) -> dict[str, Any]:
        info = getattr(self._mc, "self_info", None) if self._mc else None
        extra: dict[str, Any] = {"library": "meshcore"}
        if isinstance(info, dict):
            extra["self_name"] = clean_name(info.get("name"))
            extra["radio_freq"] = coerce_float(info.get("radio_freq"))
            extra["tx_power"] = coerce_int(info.get("tx_power"))
        return self._describe_base(extra)
