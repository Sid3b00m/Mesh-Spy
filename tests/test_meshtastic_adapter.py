"""Meshtastic normalization, driven by recorded packet dicts.

The payloads below are shaped the way `meshtastic` 2.7 hands them to a pubsub
subscriber: camelCase protobuf field names, positions as scaled integers, and
a `decoded` sub-dict whose `portnum` decides everything.

The last few tests cover the part that is genuinely hard rather than merely
fiddly: packets arrive on the library's reader thread, and `pypubsub` topics
are global to the process.
"""
from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.core.config import RadioConfig
from app.core.mesh.base import LINK_DOWN, LINK_UP, SendError
from app.core.mesh.meshtastic_adapter import TOPIC_RECEIVE, MeshtasticAdapter

BASE_NUM = 0x433D061C
BASE_ID = "!433d061c"
RIDGE_ID = "!7a2b91e4"

TEXT_PACKET = {
    "from": BASE_NUM,
    "to": 0xFFFFFFFF,
    "decoded": {
        "portnum": "TEXT_MESSAGE_APP",
        "payload": b"net check, how copy",
        "text": "net check, how copy",
    },
    "id": 1043227649,
    "rxTime": 1723472000,
    "rxSnr": -7.25,
    "rxRssi": -103,
    "hopStart": 3,
    "hopLimit": 2,
    "channel": 0,
    "fromId": BASE_ID,
    "toId": "^all",
}

DIRECT_TEXT_PACKET = {
    "from": 0x7A2B91E4,
    "to": BASE_NUM,
    "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "for you only"},
    "id": 55,
    "rxTime": 1723472050,
    "fromId": RIDGE_ID,
    "toId": BASE_ID,
}

POSITION_PACKET = {
    "from": 0x7A2B91E4,
    "to": 0xFFFFFFFF,
    "decoded": {
        "portnum": "POSITION_APP",
        "position": {
            "latitudeI": 456012000,
            "longitudeI": -1225504000,
            "altitude": 214,
            "time": 1723472100,
        },
    },
    "id": 8811,
    "rxTime": 1723472100,
    "rxSnr": 5.0,
    "rxRssi": -88,
    "hopStart": 3,
    "hopLimit": 3,
    "fromId": RIDGE_ID,
    "toId": "^all",
}

NODEINFO_PACKET = {
    "from": 0xC81F4A20,
    "to": 0xFFFFFFFF,
    "decoded": {
        "portnum": "NODEINFO_APP",
        "user": {
            "id": "!c81f4a20",
            "longName": "Handheld \U0001f4e1",
            "shortName": "HH01",
            "hwModel": "HELTEC_V3",
            "role": "CLIENT",
        },
    },
    "id": 4242,
    "rxTime": 1723472200,
    "rxSnr": -12.0,
    "hopStart": 3,
    "hopLimit": 1,
    "fromId": "!c81f4a20",
    "toId": "^all",
}

DEVICE_TELEMETRY_PACKET = {
    "from": BASE_NUM,
    "to": 0xFFFFFFFF,
    "decoded": {
        "portnum": "TELEMETRY_APP",
        "telemetry": {
            "time": 1723472300,
            "deviceMetrics": {
                "batteryLevel": 88,
                "voltage": 4.021,
                "channelUtilization": 6.5,
                "airUtilTx": 1.25,
                "uptimeSeconds": 98765,
            },
        },
    },
    "id": 9001,
    "rxTime": 1723472300,
    "rxSnr": 6.5,
    "rxRssi": -71,
    "fromId": BASE_ID,
    "toId": "^all",
}

ENVIRONMENT_TELEMETRY_PACKET = {
    "from": BASE_NUM,
    "decoded": {
        "portnum": "TELEMETRY_APP",
        "telemetry": {
            "environmentMetrics": {
                "temperature": 21.5,
                "relativeHumidity": 48.0,
                "barometricPressure": 1013.25,
            }
        },
    },
    "id": 9002,
    "rxTime": 1723472400,
    "fromId": BASE_ID,
}

ENCRYPTED_PACKET = {
    "from": 0xC81F4A20,
    "to": 0xFFFFFFFF,
    "encrypted": "3f9a1c7d",
    "id": 7777,
    "rxTime": 1723472500,
    "rxSnr": -14.75,
    "rxRssi": -119,
    "hopStart": 3,
    "hopLimit": 1,
    "fromId": "!c81f4a20",
    "toId": "^all",
}

NODE_DB_ENTRY = {
    "num": BASE_NUM,
    "user": {
        "id": BASE_ID,
        "longName": "Base Station",
        "shortName": "BASE",
        "hwModel": "TBEAM",
        "role": "ROUTER",
    },
    "position": {
        "latitudeI": 455231000,
        "longitudeI": -1226765000,
        "altitude": 48,
        "time": 1723471000,
    },
    "snr": 6.25,
    "lastHeard": 1723472000,
    "hopsAway": 0,
    "deviceMetrics": {"batteryLevel": 101, "voltage": 4.19, "uptimeSeconds": 100},
}


class FakeInterface:
    """Stands in for SerialInterface without a radio on the other end."""

    def __init__(self, *, nodes=None, my_num=None, firmware=None):
        self.nodes = dict(nodes or {})
        self._my_num = my_num
        self.metadata = SimpleNamespace(firmware_version=firmware)
        self.sent: list[tuple[str, dict]] = []
        self.closed = False
        self.send_error: Exception | None = None

    def getMyNodeInfo(self):
        return {"num": self._my_num} if self._my_num is not None else None

    def sendText(self, text, **kwargs):
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((text, kwargs))

    def close(self):
        self.closed = True


def radio(name: str = "base") -> RadioConfig:
    return RadioConfig(
        name=name, network="meshtastic", transport="serial", port="/dev/ttyUSB0"
    )


def adapter(collector, *, iface=None, name="base") -> MeshtasticAdapter:
    iface = iface or FakeInterface()
    made = MeshtasticAdapter(radio(name), collector, factory=lambda: iface)
    # Normalization runs on the loop thread and needs no live interface, so
    # these tests call the handlers directly.
    made._iface = iface
    return made


# ---- messages ----

def test_a_broadcast_text_packet_becomes_a_message_and_a_sighting(collector):
    adapter(collector)._handle_packet(TEXT_PACKET)

    message = collector.messages[0]
    assert message.text == "net check, how copy"
    assert message.from_id == BASE_ID
    # ^all is a broadcast, not a node called ^all.
    assert message.to_id is None
    assert message.is_broadcast is True
    assert message.channel == "0"
    assert message.snr == -7.25
    assert message.rssi == -103
    assert message.hops == 1
    assert message.message_id == "1043227649"
    assert message.ts == 1723472000

    # Hearing the packet at all is a sighting of its sender.
    sighting = collector.nodes[0]
    assert sighting.id == BASE_ID
    assert sighting.snr == -7.25


def test_a_direct_text_packet_keeps_its_destination(collector):
    adapter(collector)._handle_packet(DIRECT_TEXT_PACKET)

    message = collector.messages[0]
    assert message.to_id == BASE_ID
    assert message.is_broadcast is False


def test_text_is_recovered_from_the_payload_when_the_text_field_is_missing(collector):
    packet = {**TEXT_PACKET, "decoded": {
        "portnum": "TEXT_MESSAGE_APP", "payload": b"payload only"
    }}
    adapter(collector)._handle_packet(packet)
    assert collector.messages[0].text == "payload only"


def test_an_empty_text_packet_produces_no_message(collector):
    packet = {**TEXT_PACKET, "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": "  "}}
    adapter(collector)._handle_packet(packet)
    assert collector.messages == []


# ---- positions ----

def test_a_position_packet_scales_the_coordinates(collector):
    adapter(collector)._handle_packet(POSITION_PACKET)

    node = collector.nodes[0]
    assert node.id == RIDGE_ID
    assert node.lat == pytest.approx(45.6012)
    assert node.lon == pytest.approx(-122.5504)
    assert node.altitude == 214
    # hopStart == hopLimit means nothing was spent: a direct neighbour.
    assert node.hops == 0


def test_a_zero_coordinate_is_no_fix_rather_than_the_equator(collector):
    packet = {**POSITION_PACKET, "decoded": {
        "portnum": "POSITION_APP",
        "position": {"latitudeI": 0, "longitudeI": 0, "altitude": 0},
    }}
    adapter(collector)._handle_packet(packet)

    node = collector.nodes[0]
    assert node.lat is None
    assert node.lon is None


# ---- node info ----

def test_a_nodeinfo_packet_carries_the_names_through_unmangled(collector):
    adapter(collector)._handle_packet(NODEINFO_PACKET)

    node = collector.nodes[0]
    assert node.id == "!c81f4a20"
    assert node.name == "Handheld \U0001f4e1"
    assert node.short_name == "HH01"
    assert node.hw_model == "HELTEC_V3"
    assert node.role == "CLIENT"
    assert node.hops == 2


# ---- telemetry ----

def test_device_telemetry_is_split_into_normalized_metrics(collector):
    adapter(collector)._handle_packet(DEVICE_TELEMETRY_PACKET)

    values = {s.metric: s.value for s in collector.telemetry}
    assert values == {
        "battery": 88.0,
        "voltage": 4.021,
        "channel_utilization": 6.5,
        "air_util_tx": 1.25,
        "uptime": 98765.0,
    }
    assert collector.metric("battery").unit == "%"
    assert collector.metric("battery").ts == 1723472300

    # The node row is updated too, so the nodes table shows battery directly.
    node = collector.nodes[0]
    assert node.battery == 88.0
    assert node.voltage == 4.021


def test_environment_telemetry_uses_the_shared_metric_names(collector):
    adapter(collector)._handle_packet(ENVIRONMENT_TELEMETRY_PACKET)

    values = {s.metric: s.value for s in collector.telemetry}
    assert values == {"temperature": 21.5, "humidity": 48.0, "pressure": 1013.25}
    assert collector.metric("pressure").unit == "hPa"


# ---- packets we cannot read ----

def test_an_encrypted_packet_is_still_a_sighting(collector):
    """No key for the channel, but the radio quality is still real data."""
    adapter(collector)._handle_packet(ENCRYPTED_PACKET)

    assert collector.messages == []
    node = collector.nodes[0]
    assert node.id == "!c81f4a20"
    assert node.snr == -14.75
    assert node.rssi == -119
    assert node.hops == 2


def test_an_app_we_do_not_handle_is_still_a_sighting(collector):
    packet = {**TEXT_PACKET, "decoded": {"portnum": "ROUTING_APP", "routing": {}}}
    adapter(collector)._handle_packet(packet)

    assert collector.messages == []
    assert collector.nodes[0].id == BASE_ID


# ---- hop and timestamp edge cases ----

@pytest.mark.parametrize(
    "overrides, expected",
    [
        ({"hopStart": 3, "hopLimit": 3}, 0),
        ({"hopStart": 7, "hopLimit": 4}, 3),
        # Absent on some firmware versions.
        ({}, None),
        # A relayed packet can report a limit above the start.
        ({"hopStart": 1, "hopLimit": 3}, None),
    ],
)
def test_hops_are_derived_from_the_spent_hop_budget(collector, overrides, expected):
    packet = {
        "fromId": BASE_ID, "from": BASE_NUM, "rxTime": 1723472000, **overrides
    }
    adapter(collector)._emit_sighting(packet)
    assert collector.nodes[0].hops == expected


def test_a_radio_with_no_clock_gets_the_local_time(collector):
    packet = {**TEXT_PACKET, "rxTime": 0}
    adapter(collector)._handle_packet(packet)
    # rxTime is 0 until the radio has a clock, which would sort to 1970.
    assert collector.messages[0].ts > 1_600_000_000


def test_a_node_id_is_reconstructed_when_the_library_omits_it(collector):
    packet = {"from": BASE_NUM, "rxTime": 1723472000, "rxSnr": 1.0}
    adapter(collector)._emit_sighting(packet)
    assert collector.nodes[0].id == BASE_ID


# ---- the node database ----

def test_a_node_database_entry_is_normalized_and_yields_metrics(collector):
    made = adapter(collector)
    made._my_num = BASE_NUM
    made._handle_node(NODE_DB_ENTRY)

    node = collector.nodes[0]
    assert node.id == BASE_ID
    assert node.name == "Base Station"
    assert node.lat == pytest.approx(45.5231)
    assert node.hops == 0
    assert node.battery == 101.0
    assert node.is_self is True
    assert node.last_seen == 1723472000
    assert {s.metric for s in collector.telemetry} >= {"battery", "voltage", "uptime"}


def test_a_node_database_entry_with_no_identity_is_skipped(collector):
    adapter(collector)._handle_node({"snr": 5.0})
    assert collector.nodes == []


# ---- lifecycle and the thread bridge ----

async def test_start_reads_identity_and_publishes_the_node_database(collector):
    iface = FakeInterface(
        nodes={"!433d061c": NODE_DB_ENTRY}, my_num=BASE_NUM, firmware="2.7.10.abcdef"
    )
    made = MeshtasticAdapter(radio(), collector, factory=lambda: iface)

    await made.start()
    try:
        assert made.link.state == LINK_UP
        assert made.link.node_id == BASE_ID
        assert made.link.firmware == "2.7.10.abcdef"
        assert [n.id for n in collector.nodes] == [BASE_ID]
        assert made.describe()["known_nodes"] == 1
    finally:
        await made.stop()

    assert iface.closed is True
    assert made.link.state == LINK_DOWN


async def test_a_packet_from_the_reader_thread_reaches_the_loop(collector):
    """The one crossing point in the whole design."""
    from pubsub import pub

    iface = FakeInterface(my_num=BASE_NUM)
    made = MeshtasticAdapter(radio(), collector, factory=lambda: iface)
    await made.start()
    collector.clear()
    try:
        thread = threading.Thread(
            target=pub.sendMessage,
            args=(TOPIC_RECEIVE,),
            kwargs={"packet": TEXT_PACKET, "interface": iface},
        )
        thread.start()
        thread.join(timeout=5)

        # Nothing may be normalized on the reader thread, so the event only
        # appears once the loop has run the scheduled callback.
        assert collector.messages == []
        await asyncio.sleep(0.05)
        assert collector.messages[0].text == "net check, how copy"
    finally:
        await made.stop()


async def test_two_radios_do_not_record_each_others_traffic(collector, collector_factory):
    """pypubsub topics are process-global, so identity has to be checked."""
    from pubsub import pub

    other = collector_factory()
    iface_a = FakeInterface()
    iface_b = FakeInterface()
    first = MeshtasticAdapter(radio("base"), collector, factory=lambda: iface_a)
    second = MeshtasticAdapter(radio("roof"), other, factory=lambda: iface_b)
    await first.start()
    await second.start()
    collector.clear()
    other.clear()
    try:
        pub.sendMessage(TOPIC_RECEIVE, packet=TEXT_PACKET, interface=iface_a)
        await asyncio.sleep(0.05)

        assert len(collector.messages) == 1
        assert other.messages == []
    finally:
        await first.stop()
        await second.stop()


async def test_a_packet_arriving_after_stop_is_dropped(collector):
    from pubsub import pub

    iface = FakeInterface()
    made = MeshtasticAdapter(radio(), collector, factory=lambda: iface)
    await made.start()
    await made.stop()
    collector.clear()

    pub.sendMessage(TOPIC_RECEIVE, packet=TEXT_PACKET, interface=iface)
    await asyncio.sleep(0.05)

    assert collector.events == []


async def test_stop_is_safe_before_start(collector):
    made = MeshtasticAdapter(radio(), collector, factory=FakeInterface)
    await made.stop()
    assert made.link.state == LINK_DOWN


# ---- transmit ----

async def test_send_message_passes_keywords_the_library_expects(collector):
    iface = FakeInterface(my_num=BASE_NUM)
    made = MeshtasticAdapter(radio(), collector, factory=lambda: iface)
    await made.start()
    collector.clear()
    try:
        result = await made.send_message("rogers", dest=RIDGE_ID, channel=2)

        text, kwargs = iface.sent[0]
        assert text == "rogers"
        assert kwargs["destinationId"] == RIDGE_ID
        assert kwargs["channelIndex"] == 2
        # An ack round-trip would pin the worker thread for a mesh timeout.
        assert kwargs["wantAck"] is False
        assert result["link"] == "base"

        # A transmission is logged too, so the panel shows both sides.
        sent = collector.messages[0]
        assert sent.direction == "tx"
        assert sent.from_id == BASE_ID
        assert sent.to_id == RIDGE_ID
    finally:
        await made.stop()


async def test_a_send_with_no_destination_broadcasts(collector):
    iface = FakeInterface()
    made = MeshtasticAdapter(radio(), collector, factory=lambda: iface)
    await made.start()
    collector.clear()
    try:
        await made.send_message("all stations")
        _, kwargs = iface.sent[0]
        assert kwargs["destinationId"] == "^all"
        assert collector.messages[0].to_id is None
    finally:
        await made.stop()


async def test_sending_without_a_radio_is_refused(collector):
    made = MeshtasticAdapter(radio(), collector, factory=FakeInterface)
    with pytest.raises(SendError, match="not connected"):
        await made.send_message("hello")


async def test_a_radio_that_rejects_a_message_surfaces_as_a_send_error(collector):
    iface = FakeInterface()
    iface.send_error = RuntimeError("serial port disappeared")
    made = MeshtasticAdapter(radio(), collector, factory=lambda: iface)
    await made.start()
    try:
        with pytest.raises(SendError, match="serial port disappeared"):
            await made.send_message("hello")
        # A failed transmit must not appear in the log as though it went out.
        assert [m for m in collector.messages if m.direction == "tx"] == []
    finally:
        await made.stop()
