"""Supervision, reconnect behaviour, the event pipeline and demo mode.

The supervisor is the piece with no visible failure mode: if the backoff is
wrong, a radio that drops overnight either hammers the port or gives up. These
tests drive it with a scripted adapter and a recording sleep so the timing is
asserted rather than waited for.
"""
from __future__ import annotations

import asyncio
import random

import pytest

from app.core.mesh import registry as registry_module
from app.core.mesh.base import (
    LINK_CONNECTING,
    LINK_DEMO,
    LINK_DOWN,
    LINK_ERROR,
    LINK_UP,
    MeshEvent,
    MessageRecord,
    NodeRecord,
    SendError,
    utcnow,
)
from app.core.mesh.meshcore_adapter import MeshCoreAdapter
from app.core.mesh.meshtastic_adapter import MeshtasticAdapter
from app.core.mesh.registry import (
    MeshRegistry,
    SimulatedAdapter,
    build_adapter,
    get_registry,
    set_registry,
)
from tests.fakes import ScriptedAdapter

MESHTASTIC_RADIO = {
    "name": "base",
    "network": "meshtastic",
    "transport": "serial",
    "port": "/dev/ttyUSB0",
}
MESHCORE_RADIO = {
    "name": "companion",
    "network": "meshcore",
    "transport": "serial",
    "port": "/dev/ttyACM0",
}


async def drain(reg: MeshRegistry) -> None:
    """Wait for the dispatcher to finish with everything queued so far."""
    await asyncio.wait_for(reg._queue.join(), timeout=5)


async def settle(reg: MeshRegistry) -> None:
    """Let the supervisors open their adapters, then drain what they emitted.

    `start()` only creates the supervisor tasks, so without this a test would
    inspect the links before any adapter had opened.
    """
    for _ in range(200):
        await asyncio.sleep(0.005)
        if all(link["state"] != LINK_CONNECTING for link in reg.links()):
            break
    await drain(reg)


async def wait_for_state(adapter: ScriptedAdapter, state: str) -> None:
    for _ in range(400):
        if adapter.link.state == state:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"{adapter.name} never reached {state!r}")


def patch_adapters(monkeypatch, adapters: dict[str, ScriptedAdapter]) -> None:
    """Make the registry build our scripted adapters instead of real ones."""
    def factory(config, emit):
        made = adapters[config.key]
        made._emit = emit
        return made

    monkeypatch.setattr(registry_module, "build_adapter", factory)


# ---- demo mode ----

async def test_with_no_radios_the_simulated_network_stands_in(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=True)
    await reg.start()
    try:
        await settle(reg)

        assert reg.demo_active is True
        links = reg.links()
        assert {link["network"] for link in links} == {"meshtastic", "meshcore"}
        # Labelled so a simulated node can never pass for a real one.
        assert all(link["state"] == LINK_DEMO for link in links)
        assert all(link["transport"] == "simulated" for link in links)

        nodes = reg.store.nodes()
        assert len(nodes) == 5
        assert reg.status()["demo"] is True
    finally:
        await reg.stop()


async def test_configuring_a_real_radio_suppresses_the_simulation(
    config_factory, monkeypatch
):
    cfg = config_factory(mesh={"radios": [MESHTASTIC_RADIO]})
    adapter = ScriptedAdapter("base", lambda e: None)
    patch_adapters(monkeypatch, {"meshtastic:base": adapter})

    reg = MeshRegistry(cfg, allow_demo=True)
    await reg.start()
    try:
        await asyncio.sleep(0)
        assert reg.demo_active is False
        assert [link["name"] for link in reg.links()] == ["base"]
    finally:
        await reg.stop()


async def test_the_environment_can_switch_the_simulation_off(
    config_factory, monkeypatch
):
    """CI has no radio and must not be shown invented traffic."""
    config_factory()
    monkeypatch.setenv("MESH_SPY_NO_DEMO", "1")

    reg = MeshRegistry(allow_demo=True)
    await reg.start()
    try:
        assert reg.demo_active is False
        assert reg.links() == []
        assert reg.store.nodes() == []
    finally:
        await reg.stop()


async def test_the_simulated_network_refuses_to_transmit(collector):
    adapter = SimulatedAdapter("meshtastic", collector)
    with pytest.raises(SendError, match="no radio to transmit on"):
        await adapter.send_message("hello")


async def test_the_simulated_network_produces_plausible_traffic(collector):
    adapter = SimulatedAdapter("meshcore", collector, interval=0.01)
    await adapter.start()
    try:
        await asyncio.sleep(0.05)
    finally:
        await adapter.stop()

    assert {n.id for n in collector.nodes} >= {"3f9a1c7d0b28", "a41e6b92d5f0"}
    assert {s.metric for s in collector.telemetry} == {
        "battery", "voltage", "snr", "temperature"
    }
    assert all(node.raw.get("demo") or not node.raw for node in collector.nodes)


# ---- reconnect and backoff ----

async def test_a_radio_that_fails_to_open_is_retried_until_it_works(
    config_factory, monkeypatch
):
    cfg = config_factory(mesh={"radios": [MESHTASTIC_RADIO]})
    adapter = ScriptedAdapter("base", lambda e: None, failures=2)
    patch_adapters(monkeypatch, {"meshtastic:base": adapter})

    reg = MeshRegistry(cfg, allow_demo=False)
    await reg.start()
    try:
        await wait_for_state(adapter, LINK_UP)

        assert adapter.starts == 3
        assert adapter.link.state == LINK_UP
        # A link that opened cleanly starts its counter over.
        assert adapter.link.attempts == 0
    finally:
        await reg.stop()


async def test_the_backoff_doubles_and_stops_at_the_ceiling(config_factory, monkeypatch):
    cfg = config_factory(
        mesh={"reconnect_min_seconds": 1.0, "reconnect_max_seconds": 8.0}
    )
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", reg._emit, failures=99)
    reg._add(adapter)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds, *args, **kwargs):
        delays.append(seconds)
        if len(delays) >= 6:
            reg._stopping = True
        await real_sleep(0)

    # Jitter is what keeps several radios from retrying in lockstep, but it
    # makes the sequence unassertable, so it is pinned to zero here.
    monkeypatch.setattr(registry_module.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    await asyncio.wait_for(reg._supervise(adapter), timeout=5)

    assert delays == [1.0, 2.0, 4.0, 8.0, 8.0, 8.0]


async def test_jitter_keeps_retries_out_of_lockstep(config_factory, monkeypatch):
    cfg = config_factory(
        mesh={"reconnect_min_seconds": 4.0, "reconnect_max_seconds": 4.0}
    )
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", reg._emit, failures=99)
    reg._add(adapter)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds, *args, **kwargs):
        delays.append(seconds)
        if len(delays) >= 4:
            reg._stopping = True
        await real_sleep(0)

    monkeypatch.setattr(registry_module.random, "uniform", random.Random(7).uniform)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    await asyncio.wait_for(reg._supervise(adapter), timeout=5)

    assert len(set(delays)) > 1
    assert all(4.0 <= d <= 5.0 for d in delays)


async def test_a_successful_open_resets_the_backoff(config_factory, monkeypatch):
    """A radio that drops once an hour must not creep up to the ceiling."""
    cfg = config_factory(
        mesh={"reconnect_min_seconds": 1.0, "reconnect_max_seconds": 16.0}
    )
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", reg._emit, failures=2, drop_after_start=True)
    adapter.registry = reg
    reg._add(adapter)

    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def recording_sleep(seconds, *args, **kwargs):
        delays.append(seconds)
        if len(delays) >= 4:
            reg._stopping = True
        await real_sleep(0)

    monkeypatch.setattr(registry_module.random, "uniform", lambda a, b: 0.0)
    monkeypatch.setattr(asyncio, "sleep", recording_sleep)

    await asyncio.wait_for(reg._supervise(adapter), timeout=5)

    # Two failures, then a clean open, then the drop: back to the floor.
    assert delays[:3] == [1.0, 2.0, 1.0]


async def test_an_open_failure_is_reported_on_the_link(config_factory, monkeypatch):
    cfg = config_factory(mesh={"radios": [MESHTASTIC_RADIO]})
    adapter = ScriptedAdapter("base", lambda e: None, failures=99)
    patch_adapters(monkeypatch, {"meshtastic:base": adapter})

    reg = MeshRegistry(cfg, allow_demo=False)
    await reg.start()
    try:
        await wait_for_state(adapter, LINK_ERROR)

        # Reporting a bare "down" while the backoff runs would hide the one
        # thing the operator needs, which is why it will not open.
        link = reg.links()[0]
        assert link["state"] == LINK_ERROR
        assert "open failed" in link["detail"]
        assert link["attempts"] >= 1
    finally:
        await reg.stop()


async def test_one_dead_radio_does_not_take_the_others_down(config_factory, monkeypatch):
    cfg = config_factory(mesh={"radios": [MESHTASTIC_RADIO, MESHCORE_RADIO]})
    good = ScriptedAdapter("companion", lambda e: None, network="meshcore")
    bad = ScriptedAdapter("base", lambda e: None, failures=99)
    patch_adapters(monkeypatch, {"meshtastic:base": bad, "meshcore:companion": good})

    reg = MeshRegistry(cfg, allow_demo=False)
    await reg.start()
    try:
        await wait_for_state(good, LINK_UP)
        await wait_for_state(bad, LINK_ERROR)

        states = {link["name"]: link["state"] for link in reg.links()}
        assert states == {"companion": LINK_UP, "base": LINK_ERROR}
    finally:
        await reg.stop()


async def test_stopping_closes_every_adapter(config_factory, monkeypatch):
    cfg = config_factory(mesh={"radios": [MESHTASTIC_RADIO]})
    adapter = ScriptedAdapter("base", lambda e: None)
    patch_adapters(monkeypatch, {"meshtastic:base": adapter})

    reg = MeshRegistry(cfg, allow_demo=False)
    await reg.start()
    await asyncio.sleep(0.02)
    await reg.stop()

    assert adapter.stops >= 1
    assert adapter.link.state == LINK_DOWN


async def test_an_adapter_that_raises_on_stop_does_not_block_shutdown(
    config_factory, monkeypatch
):
    class Stubborn(ScriptedAdapter):
        async def stop(self):
            raise RuntimeError("serial handle wedged")

    cfg = config_factory(mesh={"radios": [MESHTASTIC_RADIO]})
    adapter = Stubborn("base", lambda e: None)
    patch_adapters(monkeypatch, {"meshtastic:base": adapter})

    reg = MeshRegistry(cfg, allow_demo=False)
    await reg.start()
    await asyncio.sleep(0.02)
    await reg.stop()


async def test_two_radios_may_not_share_a_name(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    reg._add(ScriptedAdapter("base", lambda e: None))
    with pytest.raises(ValueError, match="duplicate radio"):
        reg._add(ScriptedAdapter("base", lambda e: None))


async def test_the_same_name_on_each_network_is_allowed(config_factory):
    """The networks are separate namespaces everywhere else, so also here."""
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    reg._add(ScriptedAdapter("base", lambda e: None, network="meshtastic"))
    reg._add(ScriptedAdapter("base", lambda e: None, network="meshcore"))
    assert len(reg._adapters) == 2


# ---- the event pipeline ----

async def test_events_reach_the_store_and_every_subscriber(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    await reg.start()
    try:
        first = reg.subscribe()
        second = reg.subscribe()

        reg._emit(MeshEvent("node", NodeRecord(network="meshtastic", id="!a", name="Base")))
        await drain(reg)

        assert reg.store.nodes()[0]["name"] == "Base"
        for queue in (first, second):
            message = queue.get_nowait()
            assert message["kind"] == "node"
            assert message["data"]["name"] == "Base"
    finally:
        await reg.stop()


async def test_unsubscribing_stops_delivery(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    await reg.start()
    try:
        queue = reg.subscribe()
        assert reg.subscriber_count == 1
        reg.unsubscribe(queue)
        assert reg.subscriber_count == 0

        reg._emit(MeshEvent("node", NodeRecord(network="meshtastic", id="!a")))
        await drain(reg)
        assert queue.empty()
    finally:
        await reg.stop()


async def test_a_duplicate_message_is_not_pushed_to_the_dashboard(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    await reg.start()
    try:
        queue = reg.subscribe()
        for _ in range(2):
            reg._emit(
                MeshEvent(
                    "message",
                    MessageRecord(network="meshtastic", text="net check", message_id="1"),
                )
            )
        await drain(reg)

        assert queue.qsize() == 1
    finally:
        await reg.stop()


async def test_a_subscriber_that_stops_reading_is_dropped_not_waited_for(config_factory):
    """A backgrounded browser tab must not stall the pipeline for everyone."""
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    await reg.start()
    try:
        slow = reg.subscribe()
        attentive = reg.subscribe()
        for _ in range(registry_module.SUBSCRIBER_MAX):
            slow.put_nowait({"kind": "filler", "data": {}})

        reg._emit(MeshEvent("node", NodeRecord(network="meshtastic", id="!a")))
        await drain(reg)

        assert slow.qsize() == registry_module.SUBSCRIBER_MAX
        assert attentive.qsize() == 1
    finally:
        await reg.stop()


async def test_a_flood_of_events_is_dropped_rather_than_growing_without_limit(
    config_factory,
):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    reg._queue = asyncio.Queue(maxsize=1)

    for _ in range(3):
        reg._emit(MeshEvent("node", NodeRecord(network="meshtastic", id="!a")))

    # Dropping the newest event beats an OOM on a Pi Zero.
    assert reg.dropped_events == 2
    assert reg.status()["dropped_events"] == 2


async def test_a_bad_event_does_not_kill_the_dispatcher(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    await reg.start()
    try:
        # network is part of the store's primary key, so a record missing it
        # fails the insert. That must not take the pipeline down with it.
        reg._emit(MeshEvent("node", NodeRecord(network=None, id=None)))
        await drain(reg)

        reg._emit(MeshEvent("node", NodeRecord(network="meshtastic", id="!a", name="Base")))
        await drain(reg)
        assert reg.store.node("meshtastic", "!a")["name"] == "Base"
    finally:
        await reg.stop()


async def test_a_link_reporting_itself_down_wakes_the_supervisor(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    adapter = ScriptedAdapter("base", reg._emit)
    reg._add(adapter)
    await reg.store.open()
    reg._dispatcher = asyncio.create_task(reg._dispatch_loop())
    try:
        adapter.set_state(LINK_DOWN, "radio reported connection lost")
        await drain(reg)
        assert reg._down[adapter.key].is_set()
    finally:
        reg._dispatcher.cancel()
        await reg.store.close()


# ---- link health ----

async def test_a_link_with_no_recent_traffic_is_reported_stale(config_factory):
    cfg = config_factory(mesh={"stale_after_seconds": 60.0})
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", lambda e: None)
    reg._add(adapter)
    await adapter.start()

    adapter.link.last_event_at = utcnow() - 600
    link = reg.links()[0]
    # Silence is normal on a quiet mesh, so this is stale rather than down.
    assert link["stale"] is True
    assert link["state"] == LINK_UP
    assert link["silent_for"] > 60


async def test_a_busy_link_is_not_stale(config_factory):
    cfg = config_factory(mesh={"stale_after_seconds": 60.0})
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", lambda e: None)
    reg._add(adapter)
    await adapter.start()

    adapter.link.last_event_at = utcnow()
    assert reg.links()[0]["stale"] is False


async def test_links_are_ordered_predictably(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    reg._add(ScriptedAdapter("roof", lambda e: None))
    reg._add(ScriptedAdapter("base", lambda e: None))
    reg._add(ScriptedAdapter("shed", lambda e: None, network="meshcore"))

    assert [link["name"] for link in reg.links()] == ["shed", "base", "roof"]


# ---- transmit routing ----

async def test_a_fresh_install_cannot_transmit(config_factory):
    cfg = config_factory(mesh={"read_only": True})
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", lambda e: None)
    reg._add(adapter)
    await adapter.start()

    with pytest.raises(SendError, match="read_only"):
        await reg.send_message(network="meshtastic", link="base", text="hello")
    assert adapter.sent == []


async def test_a_named_link_is_used_as_given(config_factory):
    cfg = config_factory(mesh={"read_only": False})
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", lambda e: None)
    reg._add(adapter)
    await adapter.start()

    result = await reg.send_message(
        network="meshtastic", link="base", text="hello", dest="!abc", channel=3
    )

    assert adapter.sent == [("hello", "!abc", 3)]
    assert result["link"] == "base"


async def test_a_single_link_that_is_up_needs_no_naming(config_factory):
    cfg = config_factory(mesh={"read_only": False})
    reg = MeshRegistry(cfg, allow_demo=False)
    adapter = ScriptedAdapter("base", lambda e: None)
    reg._add(adapter)
    await adapter.start()

    await reg.send_message(network="meshtastic", link=None, text="hello")
    assert adapter.sent[0][0] == "hello"


async def test_an_unknown_link_name_is_refused(config_factory):
    cfg = config_factory(mesh={"read_only": False})
    reg = MeshRegistry(cfg, allow_demo=False)
    reg._add(ScriptedAdapter("base", lambda e: None))

    with pytest.raises(SendError, match="no meshtastic link named 'roof'"):
        await reg.send_message(network="meshtastic", link="roof", text="hello")


async def test_transmitting_with_nothing_up_is_refused(config_factory):
    cfg = config_factory(mesh={"read_only": False})
    reg = MeshRegistry(cfg, allow_demo=False)
    reg._add(ScriptedAdapter("base", lambda e: None))

    with pytest.raises(SendError, match="no meshtastic link is currently up"):
        await reg.send_message(network="meshtastic", link=None, text="hello")


async def test_with_several_links_up_the_caller_must_choose(config_factory):
    """Picking one at random would put a message out on the wrong antenna."""
    cfg = config_factory(mesh={"read_only": False})
    reg = MeshRegistry(cfg, allow_demo=False)
    for name in ("base", "roof"):
        adapter = ScriptedAdapter(name, lambda e: None)
        reg._add(adapter)
        await adapter.start()

    with pytest.raises(SendError, match="several meshtastic links are up; name one: base, roof"):
        await reg.send_message(network="meshtastic", link=None, text="hello")


async def test_a_link_on_the_other_network_is_not_a_candidate(config_factory):
    cfg = config_factory(mesh={"read_only": False})
    reg = MeshRegistry(cfg, allow_demo=False)
    other = ScriptedAdapter("companion", lambda e: None, network="meshcore")
    reg._add(other)
    await other.start()

    with pytest.raises(SendError, match="no meshtastic link is currently up"):
        await reg.send_message(network="meshtastic", link=None, text="hello")


# ---- wiring ----

def test_the_right_adapter_is_built_for_each_network(config_factory):
    from app.core.config import RadioConfig

    cfg = config_factory()
    assert isinstance(
        build_adapter(RadioConfig(**MESHTASTIC_RADIO), lambda e: None), MeshtasticAdapter
    )
    assert isinstance(
        build_adapter(RadioConfig(**MESHCORE_RADIO), lambda e: None), MeshCoreAdapter
    )
    assert cfg.mesh.radios == []


def test_the_module_level_registry_is_replaceable(config_factory):
    config_factory()
    try:
        first = get_registry()
        assert get_registry() is first

        set_registry(None)
        assert get_registry() is not first
    finally:
        set_registry(None)


async def test_a_disabled_radio_is_not_started(config_factory, monkeypatch):
    cfg = config_factory(
        mesh={"radios": [MESHTASTIC_RADIO, {**MESHCORE_RADIO, "enabled": False}]}
    )
    adapter = ScriptedAdapter("base", lambda e: None)
    patch_adapters(monkeypatch, {"meshtastic:base": adapter})

    reg = MeshRegistry(cfg, allow_demo=False)
    await reg.start()
    try:
        assert [link["name"] for link in reg.links()] == ["base"]
    finally:
        await reg.stop()


async def test_status_reports_what_the_dashboard_header_shows(config_factory):
    config_factory()
    reg = MeshRegistry(allow_demo=True)
    await reg.start()
    try:
        await settle(reg)
        status = reg.status()
        assert status["links"] == 2
        assert status["up"] == 2
        assert status["demo"] is True
        assert status["read_only"] is True
        assert status["nodes"] == 5
        assert status["subscribers"] == 0
    finally:
        await reg.stop()
