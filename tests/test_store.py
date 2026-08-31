"""Persistence and the read cache.

The cache is what the dashboard actually reads, so most of these assert that
the cache and SQLite agree after a write, and that a restart does not lose
what was on screen.
"""
from __future__ import annotations

from app.core.mesh.base import (
    MessageRecord,
    NodeRecord,
    TelemetryRecord,
    utcnow,
)
from app.core.mesh.store import MeshStore, init_db


async def test_recording_a_node_upserts_rather_than_duplicating(store):
    await store.record_node(
        NodeRecord(network="meshtastic", id="!433d061c", name="Base", last_seen=1000.0)
    )
    await store.record_node(
        NodeRecord(network="meshtastic", id="!433d061c", snr=-7.5, last_seen=2000.0)
    )

    nodes = store.nodes()
    assert len(nodes) == 1
    assert nodes[0]["name"] == "Base"
    assert nodes[0]["snr"] == -7.5
    assert nodes[0]["last_seen"] == 2000.0


async def test_the_same_id_on_each_network_stays_two_nodes(store):
    await store.record_node(NodeRecord(network="meshtastic", id="abc123", name="MT"))
    await store.record_node(NodeRecord(network="meshcore", id="abc123", name="MC"))

    assert len(store.nodes()) == 2
    assert len(store.nodes(network="meshcore")) == 1
    assert store.nodes(network="meshcore")[0]["name"] == "MC"


async def test_an_out_of_order_packet_does_not_rewind_last_seen(store):
    await store.record_node(
        NodeRecord(network="meshtastic", id="!a", last_seen=2000.0)
    )
    await store.record_node(
        NodeRecord(network="meshtastic", id="!a", last_seen=1000.0)
    )
    assert store.nodes()[0]["last_seen"] == 2000.0


async def test_node_names_are_cleaned_on_the_way_in(store):
    await store.record_node(
        NodeRecord(network="meshtastic", id="!a", name="Base\x00\x07 Station")
    )
    assert store.nodes()[0]["name"] == "Base Station"


async def test_nodes_are_returned_newest_first_and_carry_an_age(store):
    now = utcnow()
    await store.record_node(NodeRecord(network="meshtastic", id="!old", last_seen=now - 600))
    await store.record_node(NodeRecord(network="meshtastic", id="!new", last_seen=now))

    nodes = store.nodes(stale_after=300.0)
    assert [n["id"] for n in nodes] == ["!new", "!old"]
    assert nodes[0]["stale"] is False
    assert nodes[1]["stale"] is True
    assert nodes[1]["age"] >= 600


async def test_node_detail_includes_raw_and_missing_returns_none(store):
    await store.record_node(
        NodeRecord(network="meshcore", id="3f9a1c7d0b28", raw={"public_key": "3f9a…"})
    )
    found = store.node("meshcore", "3f9a1c7d0b28")
    assert found["raw"]["public_key"] == "3f9a…"
    assert store.node("meshcore", "nope") is None


async def test_a_repeated_delivery_of_the_same_packet_is_dropped(store):
    first = await store.record_message(
        MessageRecord(network="meshtastic", text="net check", message_id="9911")
    )
    # A flooded mesh delivers the same packet by several paths.
    second = await store.record_message(
        MessageRecord(network="meshtastic", text="net check", message_id="9911")
    )

    assert first is not None
    assert second is None
    assert len(store.messages()) == 1


async def test_the_same_message_id_on_each_network_is_not_a_duplicate(store):
    assert await store.record_message(
        MessageRecord(network="meshtastic", text="hi", message_id="1")
    )
    assert await store.record_message(
        MessageRecord(network="meshcore", text="hi", message_id="1")
    )
    assert len(store.messages()) == 2


async def test_messages_without_an_id_are_all_kept(store):
    """Not every payload carries a packet id, and the index must allow NULLs."""
    for _ in range(3):
        assert await store.record_message(
            MessageRecord(network="meshcore", text="same words, no id")
        )
    assert len(store.messages()) == 3


async def test_an_empty_message_is_refused(store):
    assert await store.record_message(MessageRecord(network="meshtastic", text="   ")) is None
    assert await store.record_message(MessageRecord(network="meshtastic", text="\x07")) is None
    assert store.messages() == []


async def test_messages_read_newest_first_and_honour_the_limit(store):
    for i in range(5):
        await store.record_message(
            MessageRecord(network="meshtastic", text=f"msg {i}", ts=1000.0 + i)
        )

    recent = store.messages(limit=2)
    assert [m["text"] for m in recent] == ["msg 4", "msg 3"]


async def test_telemetry_builds_a_series_per_node_and_metric(store):
    for i, value in enumerate((80.0, 78.0, 75.0)):
        await store.record_telemetry(
            TelemetryRecord(
                network="meshtastic",
                node_id="!a",
                metric="battery",
                value=value,
                ts=1000.0 + i,
            )
        )

    series = store.telemetry_series(network="meshtastic", node_id="!a", metric="battery")
    assert [p["value"] for p in series["points"]] == [80.0, 78.0, 75.0]
    assert series["unit"] is None

    summary = store.telemetry_summary()
    assert len(summary) == 1
    assert summary[0]["value"] == 75.0
    assert summary[0]["min"] == 75.0
    assert summary[0]["max"] == 80.0


async def test_a_series_is_capped_at_the_sparkline_length(tmp_path):
    db = MeshStore(tmp_path / "spark.db", spark_points=5)
    await db.open()
    try:
        for i in range(20):
            await db.record_telemetry(
                TelemetryRecord(
                    network="meshcore",
                    node_id="abc",
                    metric="voltage",
                    value=float(i),
                    ts=1000.0 + i,
                )
            )
        points = db.telemetry_series(
            network="meshcore", node_id="abc", metric="voltage"
        )["points"]
        assert [p["value"] for p in points] == [15.0, 16.0, 17.0, 18.0, 19.0]
    finally:
        await db.close()


async def test_telemetry_summary_labels_the_node_when_it_is_known(store):
    await store.record_node(
        NodeRecord(network="meshtastic", id="!433d061c", name="Base Station")
    )
    await store.record_telemetry(
        TelemetryRecord(
            network="meshtastic", node_id="!433d061c", metric="battery", value=91.0
        )
    )
    await store.record_telemetry(
        TelemetryRecord(
            network="meshtastic", node_id="!unknown", metric="battery", value=50.0
        )
    )

    labels = {r["node_id"]: r["node_label"] for r in store.telemetry_summary()}
    assert labels["!433d061c"] == "Base Station"
    # An unnamed node still has to render as something.
    assert labels["!unknown"] == "!unknown"


async def test_an_unusable_telemetry_value_is_refused(store):
    assert await store.record_telemetry(
        TelemetryRecord(network="meshtastic", node_id="!a", metric="battery", value=float("nan"))
    ) is None
    assert store.telemetry_summary() == []


async def test_trim_drops_history_past_the_retention_window(tmp_path):
    db = MeshStore(tmp_path / "trim.db", retention_days=1.0, max_messages=1000)
    await db.open()
    try:
        now = utcnow()
        await db.record_message(
            MessageRecord(network="meshtastic", text="ancient", ts=now - 5 * 86400)
        )
        await db.record_message(MessageRecord(network="meshtastic", text="recent", ts=now))
        await db.record_telemetry(
            TelemetryRecord(
                network="meshtastic",
                node_id="!a",
                metric="battery",
                value=50.0,
                ts=now - 5 * 86400,
            )
        )

        dropped = await db.trim()

        assert dropped["messages"] == 1
        assert dropped["telemetry"] == 1
        history = await db.message_history()
        assert [m["text"] for m in history] == ["recent"]
    finally:
        await db.close()


async def test_trim_also_enforces_the_hard_message_cap(tmp_path):
    """A busy mesh blows past the size budget long before anything ages out."""
    db = MeshStore(tmp_path / "cap.db", retention_days=365.0, max_messages=3)
    await db.open()
    try:
        now = utcnow()
        for i in range(10):
            await db.record_message(
                MessageRecord(network="meshtastic", text=f"msg {i}", ts=now + i)
            )

        dropped = await db.trim()

        assert dropped["messages"] == 7
        history = await db.message_history()
        assert [m["text"] for m in history] == ["msg 9", "msg 8", "msg 7"]
    finally:
        await db.close()


async def test_a_restart_repopulates_the_cache_from_disk(tmp_path):
    path = tmp_path / "warm.db"
    first = MeshStore(path)
    await first.open()
    await first.record_node(
        NodeRecord(network="meshtastic", id="!433d061c", name="Base", battery=77.0)
    )
    await first.record_message(
        MessageRecord(network="meshtastic", text="still here", message_id="1")
    )
    await first.record_telemetry(
        TelemetryRecord(
            network="meshtastic", node_id="!433d061c", metric="battery", value=77.0
        )
    )
    await first.close()

    second = MeshStore(path)
    await second.open()
    try:
        assert second.nodes()[0]["name"] == "Base"
        assert second.nodes()[0]["battery"] == 77.0
        assert [m["text"] for m in second.messages()] == ["still here"]
        assert second.telemetry_summary()[0]["value"] == 77.0
    finally:
        await second.close()


async def test_stale_telemetry_is_not_warmed_back_into_the_sparklines(tmp_path):
    """A sparkline showing yesterday's reading as current would be a lie."""
    path = tmp_path / "old.db"
    first = MeshStore(path)
    await first.open()
    await first.record_telemetry(
        TelemetryRecord(
            network="meshtastic",
            node_id="!a",
            metric="battery",
            value=50.0,
            ts=utcnow() - 48 * 3600,
        )
    )
    await first.close()

    second = MeshStore(path)
    await second.open()
    try:
        assert second.telemetry_summary() == []
        # It is still in SQLite, just not in the live cache.
        history = await second.telemetry_history(
            network="meshtastic", node_id="!a", metric="battery"
        )
        assert len(history) == 1
    finally:
        await second.close()


async def test_message_history_pages_backwards_through_sqlite(store):
    for i in range(5):
        await store.record_message(
            MessageRecord(network="meshtastic", text=f"msg {i}", ts=1000.0 + i)
        )

    page = await store.message_history(limit=2)
    assert [m["text"] for m in page] == ["msg 4", "msg 3"]

    older = await store.message_history(limit=2, before=page[-1]["ts"])
    assert [m["text"] for m in older] == ["msg 2", "msg 1"]


async def test_message_history_filters_by_network(store):
    await store.record_message(MessageRecord(network="meshtastic", text="mt"))
    await store.record_message(MessageRecord(network="meshcore", text="mc"))

    page = await store.message_history(network="meshcore")
    assert [m["text"] for m in page] == ["mc"]


async def test_counts_break_nodes_down_by_network(store):
    await store.record_node(NodeRecord(network="meshtastic", id="!a"))
    await store.record_node(NodeRecord(network="meshtastic", id="!b"))
    await store.record_node(NodeRecord(network="meshcore", id="abc"))
    await store.record_message(MessageRecord(network="meshcore", text="hi"))

    counts = store.counts()
    assert counts["nodes"] == 3
    assert counts["nodes_meshtastic"] == 2
    assert counts["nodes_meshcore"] == 1
    assert counts["messages"] == 1


async def test_the_cache_works_with_no_database_attached():
    """Reads must not require SQLite, so a disk problem degrades rather than fails."""
    db = MeshStore(":memory:")
    await db.record_node(NodeRecord(network="meshtastic", id="!a", name="Base"))
    assert await db.record_message(
        MessageRecord(network="meshtastic", text="hi", message_id="1")
    )
    # Without SQLite's unique index, dedupe falls back to scanning the cache.
    assert await db.record_message(
        MessageRecord(network="meshtastic", text="hi", message_id="1")
    ) is None
    assert db.nodes()[0]["name"] == "Base"
    assert await db.message_history() == []
    assert await db.trim() == {"messages": 0, "telemetry": 0}


def test_init_db_is_idempotent(tmp_path):
    path = tmp_path / "schema.db"
    init_db(path)
    init_db(path)
    assert path.exists()
