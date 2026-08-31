"""MeshCore normalization, driven by recorded event payloads.

`meshcore` is asyncio-native, so there is no thread bridge to test. What needs
proving instead is the identity handling: nodes are keyed by a public-key
prefix, text frames carry no packet id, and several fields use sentinel values
that mean "unknown" rather than a real measurement.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.config import RadioConfig
from app.core.mesh.base import LINK_DOWN, LINK_UP, SendError
from app.core.mesh.meshcore_adapter import (
    PATH_LEN_DIRECT,
    MeshCoreAdapter,
    _synthetic_message_id,
)

SELF_KEY = "3f9a1c7d0b28ae4419f0c5d2b7a1e3f45c6d7e8f90a1b2c3d4e5f60718293a4b"
SELF_PREFIX = "3f9a1c7d0b28"
TRUCK_KEY = "a41e6b92d5f0c3b7284d1a9e6f0b5c3d2e1f4a5b6c7d8e9f0a1b2c3d4e5f6071"
TRUCK_PREFIX = "a41e6b92d5f0"

SELF_INFO = {
    "public_key": SELF_KEY,
    "name": "Shed Companion",
    "adv_lat": 45.5102,
    "adv_lon": -122.6431,
    "radio_freq": 906.875,
    "tx_power": 22,
}

TRUCK_CONTACT = {
    "public_key": TRUCK_KEY,
    "adv_name": "Truck Mobile",
    "adv_lat": 45.5544,
    "adv_lon": -122.6002,
    "out_path_len": 2,
    "out_path_hash_mode": 0,
    "last_advert": 1723472000,
    "type": 1,
}

CONTACT_MSG = {
    "pubkey_prefix": TRUCK_PREFIX,
    "path_len": 2,
    "txt_type": 0,
    "sender_timestamp": 1723472100,
    "text": "on route, eta 20",
    "SNR": -9.5,
    "RSSI": -112,
}

CHANNEL_MSG = {
    "channel_idx": 0,
    "path_len": PATH_LEN_DIRECT,
    "sender_timestamp": 1723472200,
    "text": "net check, how copy",
    "SNR": 4.25,
    "RSSI": -95,
}

LPP_TELEMETRY = {
    "public_key": TRUCK_KEY,
    "lpp": [
        {"channel": 1, "type": "Temperature", "value": 21.5},
        {"channel": 1, "type": "Relative Humidity", "value": 48.0},
        {"channel": 2, "type": "voltage", "value": 4.02},
    ],
}


class FakeEvent:
    def __init__(self, payload):
        self.payload = payload


class FakeCommands:
    def __init__(self):
        self.sent: list[tuple] = []
        self.result = SimpleNamespace(is_error=False, payload=None)

    async def send_msg(self, contact, text):
        self.sent.append(("direct", contact, text))
        return self.result

    async def send_chan_msg(self, index, text):
        self.sent.append(("channel", index, text))
        return self.result


class FakeMeshCore:
    """Stands in for a connected MeshCore companion radio."""

    def __init__(self, *, self_info=None, contacts=None):
        self.self_info = dict(self_info or {})
        self.contacts = dict(contacts or {})
        self.commands = FakeCommands()
        self.subscriptions: list[SimpleNamespace] = []
        self.auto_fetching = False
        self.disconnected = False
        self.ensure_contacts_calls = 0

    def subscribe(self, event_type, handler):
        sub = SimpleNamespace(event_type=event_type, handler=handler, live=True)
        sub.unsubscribe = lambda: setattr(sub, "live", False)
        self.subscriptions.append(sub)
        return sub

    async def ensure_contacts(self):
        self.ensure_contacts_calls += 1

    def get_contact_by_key_prefix(self, prefix):
        for contact in self.contacts.values():
            if str(contact.get("public_key", "")).lower().startswith(prefix.lower()):
                return contact
        return None

    def get_contact_by_name(self, name):
        for contact in self.contacts.values():
            if contact.get("adv_name") == name:
                return contact
        return None

    async def start_auto_message_fetching(self):
        self.auto_fetching = True

    async def stop_auto_message_fetching(self):
        self.auto_fetching = False

    async def disconnect(self):
        self.disconnected = True

    def handler_for(self, name: str):
        for sub in self.subscriptions:
            if getattr(sub.event_type, "name", None) == name:
                return sub.handler
        raise AssertionError(f"no subscription for {name}")


def radio(name: str = "companion") -> RadioConfig:
    return RadioConfig(
        name=name, network="meshcore", transport="serial", port="/dev/ttyACM0"
    )


def adapter(collector, *, mc=None, name="companion") -> MeshCoreAdapter:
    made = MeshCoreAdapter(radio(name), collector, factory=None)
    made._mc = mc if mc is not None else FakeMeshCore()
    return made


# ---- identity ----

async def test_self_info_names_the_link_and_marks_the_local_node(collector):
    made = adapter(collector)
    await made._on_self_info(FakeEvent(SELF_INFO))

    assert made.link.node_id == SELF_PREFIX
    assert made.link.firmware == "906.875 MHz"

    node = collector.nodes[0]
    # A 32-byte key is unwieldy as an id, and received messages only carry the
    # leading 6 bytes, so the prefix is what everything keys on.
    assert node.id == SELF_PREFIX
    assert node.is_self is True
    assert node.hops == 0
    assert node.name == "Shed Companion"
    # The full key stays recoverable.
    assert node.raw["public_key"] == SELF_KEY


async def test_a_contact_becomes_a_node_with_its_path_length_as_hops(collector):
    await adapter(collector)._on_contact(FakeEvent(TRUCK_CONTACT))

    node = collector.nodes[0]
    assert node.id == TRUCK_PREFIX
    assert node.name == "Truck Mobile"
    assert node.hops == 2
    assert node.lat == pytest.approx(45.5544)
    assert node.last_seen == 1723472000


async def test_a_direct_contact_reports_zero_hops(collector):
    """path_len is 255 for a direct contact, not 0."""
    contact = {**TRUCK_CONTACT, "out_path_len": PATH_LEN_DIRECT}
    await adapter(collector)._on_contact(FakeEvent(contact))
    assert collector.nodes[0].hops == 0


async def test_a_flood_routed_contact_has_unknown_hops(collector):
    """Reporting 0 would claim a direct neighbour we have no path to."""
    contact = {**TRUCK_CONTACT, "out_path_hash_mode": -1, "out_path_len": 0}
    await adapter(collector)._on_contact(FakeEvent(contact))
    assert collector.nodes[0].hops is None


async def test_an_unadvertised_position_is_not_the_gulf_of_guinea(collector):
    contact = {**TRUCK_CONTACT, "adv_lat": 0, "adv_lon": 0}
    await adapter(collector)._on_contact(FakeEvent(contact))

    node = collector.nodes[0]
    assert node.lat is None
    assert node.lon is None


async def test_a_contact_with_an_unset_advert_clock_gets_the_local_time(collector):
    contact = {**TRUCK_CONTACT, "last_advert": 0}
    await adapter(collector)._on_contact(FakeEvent(contact))
    assert collector.nodes[0].last_seen > 1_600_000_000


async def test_a_contact_without_a_public_key_is_skipped(collector):
    await adapter(collector)._on_contact(FakeEvent({"adv_name": "nameless"}))
    assert collector.nodes == []


async def test_a_bulk_contacts_event_ingests_every_entry(collector):
    payload = {"a": TRUCK_CONTACT, "b": {**TRUCK_CONTACT, "public_key": SELF_KEY}}
    await adapter(collector)._on_contacts(FakeEvent(payload))
    assert {n.id for n in collector.nodes} == {TRUCK_PREFIX, SELF_PREFIX}


async def test_an_advert_is_a_sighting(collector):
    await adapter(collector)._on_advert(FakeEvent({"public_key": TRUCK_KEY}))

    node = collector.nodes[0]
    assert node.id == TRUCK_PREFIX
    assert node.last_seen > 1_600_000_000


# ---- messages ----

async def test_a_direct_message_resolves_its_sender_and_records_a_sighting(collector):
    mc = FakeMeshCore(contacts={"truck": TRUCK_CONTACT})
    made = adapter(collector, mc=mc)
    await made._on_self_info(FakeEvent(SELF_INFO))
    collector.clear()

    await made._on_contact_message(FakeEvent(CONTACT_MSG))

    message = collector.messages[0]
    assert message.text == "on route, eta 20"
    assert message.from_id == TRUCK_PREFIX
    assert message.from_name == "Truck Mobile"
    # A contact message is addressed to us specifically.
    assert message.to_id == SELF_PREFIX
    assert message.is_broadcast is False
    assert message.snr == -9.5
    assert message.rssi == -112
    assert message.hops == 2
    assert message.ts == 1723472100

    assert collector.nodes[0].id == TRUCK_PREFIX


async def test_a_channel_message_has_no_identifiable_sender(collector):
    """Channel traffic is encrypted to the channel key, not to a contact."""
    await adapter(collector)._on_channel_message(FakeEvent(CHANNEL_MSG))

    message = collector.messages[0]
    assert message.text == "net check, how copy"
    assert message.from_id is None
    assert message.from_name is None
    assert message.channel == "0"
    assert message.hops == 0
    assert message.is_broadcast is True
    # No sender to attribute, so no node sighting either.
    assert collector.nodes == []


async def test_an_unresolvable_sender_still_produces_the_message(collector):
    made = adapter(collector, mc=FakeMeshCore())
    await made._on_contact_message(FakeEvent(CONTACT_MSG))

    message = collector.messages[0]
    assert message.from_id == TRUCK_PREFIX
    assert message.from_name is None


async def test_an_empty_message_is_ignored(collector):
    made = adapter(collector)
    await made._on_contact_message(FakeEvent({**CONTACT_MSG, "text": "   "}))
    await made._on_channel_message(FakeEvent({**CHANNEL_MSG, "text": ""}))
    assert collector.messages == []


async def test_an_unset_sender_clock_falls_back_to_the_local_time(collector):
    made = adapter(collector)
    await made._on_contact_message(FakeEvent({**CONTACT_MSG, "sender_timestamp": 0}))
    assert collector.messages[0].ts > 1_600_000_000


async def test_a_sender_clock_far_in_the_future_is_not_trusted(collector):
    made = adapter(collector)
    await made._on_contact_message(
        FakeEvent({**CONTACT_MSG, "sender_timestamp": 4_000_000_000})
    )
    # Otherwise one badly set radio would pin itself to the top of the panel.
    assert collector.messages[0].ts < 4_000_000_000


def test_the_synthetic_message_id_is_stable_but_distinguishes_messages():
    """MeshCore text frames carry no packet id, so one is derived."""
    first = _synthetic_message_id("priv", TRUCK_PREFIX, 1723472100, "on route")
    again = _synthetic_message_id("priv", TRUCK_PREFIX, 1723472100, "on route")
    other_text = _synthetic_message_id("priv", TRUCK_PREFIX, 1723472100, "delayed")
    other_time = _synthetic_message_id("priv", TRUCK_PREFIX, 1723472101, "on route")

    assert first == again
    assert first != other_text
    assert first != other_time


async def test_refetching_after_a_reconnect_yields_the_same_message_id(collector):
    """Otherwise every reconnect would duplicate the backlog on screen."""
    made = adapter(collector)
    await made._on_contact_message(FakeEvent(CONTACT_MSG))
    await made._on_contact_message(FakeEvent(dict(CONTACT_MSG)))

    ids = [m.message_id for m in collector.messages]
    assert ids[0] == ids[1]


async def test_a_channel_and_a_direct_message_never_share_an_id(collector):
    made = adapter(collector)
    same_words = "identical text"
    await made._on_contact_message(
        FakeEvent({**CONTACT_MSG, "text": same_words, "sender_timestamp": 1723472100})
    )
    await made._on_channel_message(
        FakeEvent({**CHANNEL_MSG, "text": same_words, "sender_timestamp": 1723472100})
    )

    ids = {m.message_id for m in collector.messages}
    assert len(ids) == 2


# ---- telemetry ----

async def test_a_battery_reading_in_millivolts_becomes_a_voltage(collector):
    made = adapter(collector)
    await made._on_self_info(FakeEvent(SELF_INFO))
    collector.clear()

    await made._on_battery(FakeEvent({"level": 4021}))

    sample = collector.telemetry[0]
    assert sample.metric == "voltage"
    assert sample.value == pytest.approx(4.021)
    assert sample.unit == "V"
    assert sample.node_id == SELF_PREFIX


async def test_a_battery_reading_small_enough_to_be_a_percentage_is_one(collector):
    made = adapter(collector)
    await made._on_self_info(FakeEvent(SELF_INFO))
    collector.clear()

    await made._on_battery(FakeEvent({"level": 87}))

    sample = collector.telemetry[0]
    # Otherwise this would read as a 87 mV battery.
    assert sample.metric == "battery"
    assert sample.value == 87.0
    assert sample.unit == "%"


async def test_a_battery_event_before_self_info_is_dropped(collector):
    """There is no node to attribute it to yet."""
    await adapter(collector)._on_battery(FakeEvent({"level": 4021}))
    assert collector.telemetry == []


async def test_cayenne_readings_are_normalized_to_the_shared_metric_names(collector):
    await adapter(collector)._on_telemetry(FakeEvent(LPP_TELEMETRY))

    values = {s.metric: s.value for s in collector.telemetry}
    assert values == {
        "temperature": 21.5,
        "relative_humidity": 48.0,
        "voltage": 4.02,
    }
    assert all(s.node_id == TRUCK_PREFIX for s in collector.telemetry)


async def test_flat_telemetry_fields_are_read_when_there_is_no_cayenne_payload(collector):
    made = adapter(collector)
    await made._on_telemetry(
        FakeEvent({"public_key": TRUCK_KEY, "battery": 88, "temperature": 19.0})
    )

    values = {s.metric: s.value for s in collector.telemetry}
    assert values == {"battery": 88.0, "temperature": 19.0}


async def test_telemetry_without_a_key_is_attributed_to_the_local_node(collector):
    made = adapter(collector)
    await made._on_self_info(FakeEvent(SELF_INFO))
    collector.clear()

    await made._on_telemetry(FakeEvent({"battery": 55}))

    assert collector.telemetry[0].node_id == SELF_PREFIX


# ---- lifecycle ----

async def test_start_seeds_from_the_radio_and_enables_message_fetching(collector):
    mc = FakeMeshCore(self_info=SELF_INFO, contacts={"truck": TRUCK_CONTACT})
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))

    await made.start()
    try:
        assert made.link.state == LINK_UP
        assert made.link.node_id == SELF_PREFIX
        assert {n.id for n in collector.nodes} == {SELF_PREFIX, TRUCK_PREFIX}
        assert mc.ensure_contacts_calls == 1
        # Without this the firmware holds messages until something polls.
        assert mc.auto_fetching is True
        assert len(mc.subscriptions) == 11
        assert made.describe()["self_name"] == "Shed Companion"
    finally:
        await made.stop()

    assert mc.auto_fetching is False
    assert mc.disconnected is True
    assert all(sub.live is False for sub in mc.subscriptions)
    assert made.link.state == LINK_DOWN


async def test_the_radio_reporting_a_disconnect_marks_the_link_down(collector):
    mc = FakeMeshCore(self_info=SELF_INFO)
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    try:
        await mc.handler_for("DISCONNECTED")(FakeEvent({}))
        assert made.link.state == LINK_DOWN
    finally:
        await made.stop()


async def test_stop_is_safe_before_start(collector):
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(FakeMeshCore()))
    await made.stop()


async def test_a_radio_with_no_contacts_yet_is_not_a_failure(collector):
    class Grumpy(FakeMeshCore):
        async def ensure_contacts(self):
            raise RuntimeError("no contacts stored")

    mc = Grumpy(self_info=SELF_INFO)
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    try:
        assert made.link.state == LINK_UP
    finally:
        await made.stop()


# ---- transmit ----

async def test_a_send_with_no_destination_goes_to_a_channel(collector):
    mc = FakeMeshCore(self_info=SELF_INFO)
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    collector.clear()
    try:
        result = await made.send_message("all stations", channel=2)

        kind, index, text = mc.commands.sent[0]
        assert (kind, index, text) == ("channel", 2, "all stations")
        assert result["link"] == "companion"

        sent = collector.messages[0]
        assert sent.direction == "tx"
        assert sent.channel == "2"
        assert sent.from_id == SELF_PREFIX
        assert sent.to_id is None
    finally:
        await made.stop()


async def test_a_send_to_a_key_prefix_resolves_the_contact(collector):
    mc = FakeMeshCore(self_info=SELF_INFO, contacts={"truck": TRUCK_CONTACT})
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    collector.clear()
    try:
        await made.send_message("rogers", dest=TRUCK_PREFIX)

        kind, contact, text = mc.commands.sent[0]
        # The library wants the whole contact record, not an id.
        assert kind == "direct"
        assert contact["adv_name"] == "Truck Mobile"
        assert text == "rogers"
        assert collector.messages[0].to_id == TRUCK_PREFIX
    finally:
        await made.stop()


async def test_a_send_falls_back_to_matching_a_contact_by_name(collector):
    mc = FakeMeshCore(self_info=SELF_INFO, contacts={"truck": TRUCK_CONTACT})
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    try:
        await made.send_message("rogers", dest="Truck Mobile")
        assert mc.commands.sent[0][1]["adv_name"] == "Truck Mobile"
    finally:
        await made.stop()


async def test_a_send_to_an_unknown_contact_is_refused(collector):
    mc = FakeMeshCore(self_info=SELF_INFO)
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    try:
        with pytest.raises(SendError, match="no MeshCore contact"):
            await made.send_message("hello", dest="nobody")
        assert mc.commands.sent == []
    finally:
        await made.stop()


async def test_a_radio_that_rejects_a_message_surfaces_as_a_send_error(collector):
    mc = FakeMeshCore(self_info=SELF_INFO)
    mc.commands.result = SimpleNamespace(is_error=True, payload="tx queue full")
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(mc))
    await made.start()
    collector.clear()
    try:
        with pytest.raises(SendError, match="tx queue full"):
            await made.send_message("hello")
        # A failed transmit must not appear in the log as though it went out.
        assert collector.messages == []
    finally:
        await made.stop()


async def test_sending_without_a_radio_is_refused(collector):
    made = MeshCoreAdapter(radio(), collector, factory=lambda: _ready(FakeMeshCore()))
    with pytest.raises(SendError, match="not connected"):
        await made.send_message("hello")


async def _ready(mc):
    """The factory is awaited, matching MeshCore.create_serial."""
    return mc
