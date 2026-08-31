"""The shared record types and the coercions every adapter leans on."""
from __future__ import annotations

import pytest

from app.core.mesh.base import (
    BROADCAST,
    LINK_UP,
    METRIC_UNITS,
    MessageRecord,
    NodeRecord,
    TelemetryRecord,
    clean_name,
    clean_text,
    coerce_float,
    coerce_int,
    meshtastic_node_id,
)


def test_node_id_is_rendered_the_way_the_firmware_does():
    assert meshtastic_node_id(0x433D061C) == "!433d061c"
    # Node numbers arrive as unsigned 32-bit values; a signed reading of the
    # same bits must not produce a different id for the same radio.
    assert meshtastic_node_id(-1) == "!ffffffff"
    assert meshtastic_node_id(None) is None
    assert meshtastic_node_id("not a number") is None


def test_clean_text_strips_control_characters_but_keeps_emoji():
    assert clean_text("Base\x07 Station") == "Base Station"
    assert clean_text("Ridge \U0001f4e1") == "Ridge \U0001f4e1"
    assert clean_text("  padded  ") == "padded"
    assert clean_text("") is None
    assert clean_text(None) is None
    assert clean_text(b"bytes payload") == "bytes payload"


def test_clean_text_truncates_and_clean_name_is_shorter():
    assert len(clean_text("x" * 5000)) == 512
    assert len(clean_name("x" * 5000)) == 64


def test_coercions_reject_the_values_protobuf_defaults_produce():
    assert coerce_float("3.5") == 3.5
    assert coerce_float(float("nan")) is None
    assert coerce_float(float("inf")) is None
    # A bool is an int in Python, and a battery level of True is not 1%.
    assert coerce_float(True) is None
    assert coerce_int(4.9) == 4
    assert coerce_int("junk") is None


def test_node_key_namespaces_by_network():
    mt = NodeRecord(network="meshtastic", id="!abc")
    mc = NodeRecord(network="meshcore", id="!abc")
    assert mt.key != mc.key
    assert mt.key == "meshtastic:!abc"


def test_merge_keeps_fields_the_newer_sighting_did_not_carry():
    existing = NodeRecord(
        network="meshtastic",
        id="!433d061c",
        name="Base Station",
        battery=88.0,
        lat=45.5,
        lon=-122.6,
        last_seen=1000.0,
    )
    # A position packet says nothing about battery.
    update = NodeRecord(
        network="meshtastic", id="!433d061c", lat=45.6, lon=-122.7, last_seen=2000.0
    )

    merged = existing.merge(update)

    assert merged.battery == 88.0
    assert merged.name == "Base Station"
    assert merged.lat == 45.6
    assert merged.last_seen == 2000.0


def test_merge_never_moves_last_seen_backwards():
    node = NodeRecord(network="meshcore", id="3f9a1c7d0b28", last_seen=2000.0)
    # Packets can arrive out of order on a flooded mesh.
    node.merge(NodeRecord(network="meshcore", id="3f9a1c7d0b28", last_seen=1000.0))
    assert node.last_seen == 2000.0


def test_merge_is_sticky_for_is_self():
    node = NodeRecord(network="meshtastic", id="!a", is_self=True)
    node.merge(NodeRecord(network="meshtastic", id="!a"))
    assert node.is_self is True


def test_merge_refuses_records_for_different_nodes():
    mt = NodeRecord(network="meshtastic", id="!a")
    with pytest.raises(ValueError):
        mt.merge(NodeRecord(network="meshcore", id="!a"))
    with pytest.raises(ValueError):
        mt.merge(NodeRecord(network="meshtastic", id="!b"))


def test_raw_is_only_serialised_when_asked_for():
    node = NodeRecord(network="meshtastic", id="!a", raw={"secret": 1})
    assert "raw" not in node.to_dict()
    assert node.to_dict(include_raw=True)["raw"] == {"secret": 1}


def test_node_label_falls_back_through_name_then_id():
    assert NodeRecord(network="meshcore", id="abc", name="Shed").label() == "Shed"
    assert NodeRecord(network="meshcore", id="abc", short_name="SH").label() == "SH"
    assert NodeRecord(network="meshcore", id="abc").label() == "abc"


@pytest.mark.parametrize("to_id", [None, "", BROADCAST])
def test_broadcast_detection(to_id):
    msg = MessageRecord(network="meshtastic", text="hi", to_id=to_id)
    assert msg.is_broadcast is True
    assert msg.to_dict()["broadcast"] is True


def test_direct_message_is_not_broadcast():
    msg = MessageRecord(network="meshtastic", text="hi", to_id="!433d061c")
    assert msg.is_broadcast is False


def test_telemetry_series_key_is_a_tuple_not_a_joined_string():
    sample = TelemetryRecord(
        network="meshcore", node_id="a:b", metric="battery", value=50.0
    )
    # A node id containing the separator must not be mis-parsed on the way out.
    assert sample.series_key == ("meshcore", "a:b", "battery")


def test_every_metric_the_adapters_emit_has_a_unit():
    for metric in (
        "battery", "voltage", "temperature", "humidity", "pressure",
        "channel_utilization", "air_util_tx", "uptime",
    ):
        assert metric in METRIC_UNITS


class _Adapter:
    """Minimal concrete adapter, to exercise the ABC's emit helpers."""

    def __init__(self, emit):
        from app.core.mesh.base import MeshAdapter

        class Impl(MeshAdapter):
            network = "meshtastic"

            async def start(self): ...
            async def stop(self): ...
            async def send_message(self, text, **kwargs): ...
            def describe(self): return self._describe_base()

        self.impl = Impl("base", emit)


def test_emit_helpers_stamp_the_link_and_fill_in_units(collector):
    adapter = _Adapter(collector).impl

    adapter.emit_node(NodeRecord(network="meshtastic", id="!a"))
    adapter.emit_telemetry(
        TelemetryRecord(
            network="meshtastic", node_id="!a", metric="battery", value=91.0
        )
    )

    assert collector.nodes[0].link == "base"
    sample = collector.telemetry[0]
    assert sample.link == "base"
    assert sample.unit == "%"


def test_link_state_change_emits_once_and_records_when(collector):
    adapter = _Adapter(collector).impl

    adapter.set_state(LINK_UP, "connected")
    adapter.set_state(LINK_UP, "connected")

    assert len(collector.links) == 1
    assert collector.links[0].state == LINK_UP


def test_link_events_do_not_count_as_traffic(collector):
    """A link that only ever reports its own state is silent, not busy."""
    adapter = _Adapter(collector).impl

    adapter.set_state(LINK_UP)
    assert adapter.link.last_event_at is None

    adapter.emit_node(NodeRecord(network="meshtastic", id="!a"))
    assert adapter.link.last_event_at is not None
