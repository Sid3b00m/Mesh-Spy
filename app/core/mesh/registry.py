"""Supervises the configured radios and owns the single event pipeline.

Each adapter gets a supervisor task that opens it, waits for it to fail, and
reopens it with exponential backoff. Adapters push normalized events onto one
queue; a dispatcher task drains that queue into the store and fans out to any
SSE subscribers. That funnel is the whole point of the design: the two
libraries' incompatible concurrency models stop mattering past this file.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
from typing import Any

from app.core.config import AppConfig, RadioConfig, get_config
from app.core.mesh.base import (
    EVENT_LINK,
    EVENT_MESSAGE,
    EVENT_NODE,
    EVENT_TELEMETRY,
    LINK_DEMO,
    LINK_DOWN,
    LINK_ERROR,
    LINK_UP,
    NETWORK_MESHCORE,
    NETWORK_MESHTASTIC,
    MeshAdapter,
    MeshEvent,
    MessageRecord,
    NodeRecord,
    SendError,
    TelemetryRecord,
    utcnow,
)
from app.core.mesh.meshcore_adapter import MeshCoreAdapter
from app.core.mesh.meshtastic_adapter import MeshtasticAdapter
from app.core.mesh.store import MeshStore

log = logging.getLogger("mesh-spy.registry")

# Bounded so a radio spraying packets cannot grow the queue without limit.
# Dropping the newest event is better than an OOM on a Pi Zero.
QUEUE_MAX = 2000

# Per-subscriber SSE buffer. A browser tab that stops reading gets its events
# dropped rather than stalling the dispatcher for everyone.
SUBSCRIBER_MAX = 200

TRIM_INTERVAL_S = 3600.0


def demo_disabled() -> bool:
    return os.environ.get("MESH_SPY_NO_DEMO", "").strip().lower() in ("1", "true", "yes")


class SimulatedAdapter(MeshAdapter):
    """A synthetic mesh, so the console works before any hardware exists.

    Mirrors the placeholder pattern Pi-Spy-RF uses for absent SDRs. It is
    labelled `demo` in the links panel so a simulated node can never be
    mistaken for a real one, it refuses to transmit, and it is suppressed the
    moment a real radio is configured.
    """

    SEED_NODES = {
        NETWORK_MESHTASTIC: (
            ("!433d061c", "Base Station", "BASE", "ROUTER", "TBEAM", 45.5231, -122.6765, 0),
            ("!7a2b91e4", "Ridge Repeater", "RDGE", "REPEATER", "RAK4631", 45.6012, -122.5504, 1),
            ("!c81f4a20", "Handheld", "HH01", "CLIENT", "HELTEC_V3", 45.4899, -122.7011, 2),
        ),
        NETWORK_MESHCORE: (
            ("3f9a1c7d0b28", "Shed Companion", "SHED", None, None, 45.5102, -122.6431, 0),
            ("a41e6b92d5f0", "Truck Mobile", "TRUK", None, None, 45.5544, -122.6002, 1),
        ),
    }

    CHATTER = (
        "net check, how copy",
        "signal report: readable",
        "heading out, back in an hour",
        "battery at half, switching to solar",
        "weather looks clear tonight",
    )

    def __init__(self, network: str, emit, *, interval: float = 12.0) -> None:
        self.network = network
        super().__init__(f"{network}-demo", emit)
        self._link.transport = "simulated"
        self._link.target = "no hardware"
        self._interval = interval
        self._task: asyncio.Task[None] | None = None
        self._rng = random.Random(f"mesh-spy:{network}")

    async def start(self) -> None:
        self.set_state(LINK_DEMO, "simulated network, no radio attached")
        now = utcnow()
        for node_id, name, short, role, hw, lat, lon, hops in self.SEED_NODES[self.network]:
            self._link.node_id = self._link.node_id or node_id
            self.emit_node(
                NodeRecord(
                    network=self.network,
                    id=node_id,
                    name=name,
                    short_name=short,
                    role=role,
                    hw_model=hw,
                    lat=lat,
                    lon=lon,
                    hops=hops,
                    snr=round(self._rng.uniform(-14.0, 12.0), 2),
                    rssi=round(self._rng.uniform(-120.0, -60.0), 1),
                    battery=round(self._rng.uniform(45.0, 100.0), 1),
                    voltage=round(self._rng.uniform(3.5, 4.2), 2),
                    is_self=hops == 0,
                    last_seen=now,
                    raw={"demo": True},
                )
            )
        self._task = asyncio.create_task(self._run(), name=f"demo-{self.network}")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.set_state(LINK_DOWN, "stopped")

    async def _run(self) -> None:
        """Emit plausible traffic forever."""
        while True:
            await asyncio.sleep(self._interval)
            node_id, name = self._pick_node()
            now = utcnow()
            for metric, low, high in (
                ("battery", 40.0, 100.0),
                ("voltage", 3.4, 4.2),
                ("snr", -16.0, 12.0),
                ("temperature", 4.0, 28.0),
            ):
                self.emit_telemetry(
                    TelemetryRecord(
                        network=self.network,
                        node_id=node_id,
                        metric=metric,
                        value=round(self._rng.uniform(low, high), 2),
                        ts=now,
                    )
                )
            # Not every tick produces chatter; a real mesh is mostly quiet.
            if self._rng.random() < 0.5:
                self.emit_message(
                    MessageRecord(
                        network=self.network,
                        text=self._rng.choice(self.CHATTER),
                        ts=now,
                        from_id=node_id,
                        from_name=name,
                        channel="0",
                        snr=round(self._rng.uniform(-14.0, 10.0), 2),
                        hops=self._rng.randint(0, 3),
                        message_id=f"demo-{self.network}-{now:.3f}",
                        raw={"demo": True},
                    )
                )
            self.emit_node(
                NodeRecord(
                    network=self.network,
                    id=node_id,
                    name=name,
                    snr=round(self._rng.uniform(-14.0, 12.0), 2),
                    last_seen=now,
                )
            )

    def _pick_node(self) -> tuple[str, str]:
        entry = self._rng.choice(self.SEED_NODES[self.network])
        return entry[0], entry[1]

    async def send_message(self, text: str, **_kwargs: Any) -> dict[str, Any]:
        raise SendError("this is the simulated network; it has no radio to transmit on")

    def describe(self) -> dict[str, Any]:
        return self._describe_base({"library": "simulated", "demo": True})


def build_adapter(config: RadioConfig, emit) -> MeshAdapter:
    if config.network == NETWORK_MESHTASTIC:
        return MeshtasticAdapter(config, emit)
    if config.network == NETWORK_MESHCORE:
        return MeshCoreAdapter(config, emit)
    raise ValueError(f"unknown network {config.network!r}")


class MeshRegistry:
    def __init__(
        self,
        config: AppConfig | None = None,
        *,
        store: MeshStore | None = None,
        allow_demo: bool = True,
    ) -> None:
        self.config = config or get_config()
        self.store = store or MeshStore()
        self._allow_demo = allow_demo

        self._queue: asyncio.Queue[MeshEvent] = asyncio.Queue(maxsize=QUEUE_MAX)
        self._adapters: dict[str, MeshAdapter] = {}
        self._supervisors: dict[str, asyncio.Task[None]] = {}
        # Signalled when a link reports itself down, which is what wakes the
        # supervisor to reconnect.
        self._down: dict[str, asyncio.Event] = {}
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._dispatcher: asyncio.Task[None] | None = None
        self._trimmer: asyncio.Task[None] | None = None
        self._stopping = False
        self.dropped_events = 0
        self.demo_active = False

    # ---- lifecycle ----

    async def start(self) -> None:
        await self.store.open()
        self._dispatcher = asyncio.create_task(self._dispatch_loop(), name="mesh-dispatch")
        self._trimmer = asyncio.create_task(self._trim_loop(), name="mesh-trim")

        radios = self.config.mesh.enabled_radios()
        for radio in radios:
            self._add(build_adapter(radio, self._emit))

        if not radios and self._allow_demo and not demo_disabled():
            # No hardware configured, so stand up the simulated mesh rather
            # than serving an empty dashboard.
            self.demo_active = True
            for network in (NETWORK_MESHTASTIC, NETWORK_MESHCORE):
                self._add(SimulatedAdapter(network, self._emit))
        elif not radios:
            log.info("no radios configured and the simulated network is disabled")

        for key, adapter in self._adapters.items():
            self._supervisors[key] = asyncio.create_task(
                self._supervise(adapter), name=f"supervise:{key}"
            )

    def _add(self, adapter: MeshAdapter) -> None:
        if adapter.key in self._adapters:
            raise ValueError(f"duplicate radio {adapter.key!r}")
        self._adapters[adapter.key] = adapter
        self._down[adapter.key] = asyncio.Event()

    async def stop(self) -> None:
        self._stopping = True
        for event in self._down.values():
            event.set()
        await self._cancel(list(self._supervisors.values()))
        self._supervisors.clear()

        for adapter in self._adapters.values():
            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001 - shutdown is best effort
                log.debug("stopping %s failed", adapter.key, exc_info=True)

        await self._cancel([t for t in (self._dispatcher, self._trimmer) if t])
        self._dispatcher = None
        self._trimmer = None
        self._subscribers.clear()
        await self.store.close()

    @staticmethod
    async def _cancel(tasks: list[asyncio.Task[None]]) -> None:
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001
                log.debug("task %r raised while cancelling", task.get_name(), exc_info=True)

    # ---- supervision ----

    async def _supervise(self, adapter: MeshAdapter) -> None:
        mesh = self.config.mesh
        delay = mesh.reconnect_min_seconds
        down = self._down[adapter.key]

        while not self._stopping:
            down.clear()
            failure: str | None = None
            try:
                await adapter.start()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - any failure is a retry
                failure = str(exc) or exc.__class__.__name__
                adapter.set_state(LINK_ERROR, failure)
                log.warning("%s: open failed: %s", adapter.key, exc)
            else:
                # A link that opened cleanly starts its backoff over, so a
                # radio that drops once an hour does not creep up to the cap.
                delay = mesh.reconnect_min_seconds
                adapter.link.attempts = 0
                await down.wait()
                if self._stopping:
                    break
                log.info("%s: link went down, reconnecting", adapter.key)

            try:
                await adapter.stop()
            except Exception:  # noqa: BLE001
                log.debug("%s: stop after failure raised", adapter.key, exc_info=True)

            if self._stopping:
                break
            adapter.link.attempts += 1
            if failure is not None:
                # stop() just reported "down", which for a radio that never
                # opened tells the operator nothing. Put the reason back so
                # the links panel shows it for the length of the backoff.
                adapter.set_state(LINK_ERROR, failure)
            # Jitter so several radios failing together do not retry in lockstep.
            await asyncio.sleep(delay + random.uniform(0.0, delay * 0.25))
            delay = min(delay * 2.0, mesh.reconnect_max_seconds)

    # ---- event pipeline ----

    def _emit(self, event: MeshEvent) -> None:
        """Called by adapters, always on the loop thread."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            self.dropped_events += 1
            if self.dropped_events % 100 == 1:
                log.warning(
                    "event queue full, dropped %d events so far", self.dropped_events
                )

    async def _dispatch_loop(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._apply(event)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad packet must not stop the loop
                log.exception("failed to handle %s event", event.kind)
            finally:
                self._queue.task_done()

    async def _apply(self, event: MeshEvent) -> None:
        kind = event.kind
        payload = event.payload
        if kind == EVENT_NODE and isinstance(payload, NodeRecord):
            merged = await self.store.record_node(payload)
            self._fanout({"kind": kind, "data": merged.to_dict()})
        elif kind == EVENT_MESSAGE and isinstance(payload, MessageRecord):
            stored = await self.store.record_message(payload)
            if stored is None:
                # A duplicate delivery. Already on screen, so say nothing.
                return
            self._fanout({"kind": kind, "data": stored.to_dict()})
        elif kind == EVENT_TELEMETRY and isinstance(payload, TelemetryRecord):
            recorded = await self.store.record_telemetry(payload)
            if recorded is not None:
                self._fanout({"kind": kind, "data": recorded.to_dict()})
        elif kind == EVENT_LINK:
            data = payload.to_dict()
            key = data.get("key")
            state = data.get("state")
            if key in self._down and state in (LINK_DOWN, LINK_ERROR):
                self._down[key].set()
            self._fanout({"kind": kind, "data": data})

    def _fanout(self, message: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Slow client. Drop for that subscriber only.
                pass

    async def _trim_loop(self) -> None:
        while True:
            try:
                dropped = await self.store.trim()
                if any(dropped.values()):
                    log.info(
                        "trimmed %d messages and %d telemetry rows",
                        dropped["messages"],
                        dropped["telemetry"],
                    )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                log.exception("retention trim failed")
            await asyncio.sleep(TRIM_INTERVAL_S)

    # ---- SSE fanout ----

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=SUBSCRIBER_MAX)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ---- reads ----

    def links(self) -> list[dict[str, Any]]:
        stale_after = self.config.mesh.stale_after_seconds
        now = utcnow()
        out = []
        for adapter in self._adapters.values():
            data = adapter.describe()
            last = data.get("last_event_at")
            data["silent_for"] = (now - last) if last else None
            data["stale"] = bool(
                data["state"] == LINK_UP and last and (now - last) > stale_after
            )
            out.append(data)
        out.sort(key=lambda d: (d["network"], d["name"]))
        return out

    def adapter(self, network: str, name: str) -> MeshAdapter | None:
        return self._adapters.get(f"{network}:{name}")

    def status(self) -> dict[str, Any]:
        links = self.links()
        return {
            "links": len(links),
            "up": sum(1 for link in links if link["state"] in (LINK_UP, LINK_DEMO)),
            "demo": self.demo_active,
            "read_only": self.config.mesh.read_only,
            "queue_depth": self._queue.qsize(),
            "dropped_events": self.dropped_events,
            "subscribers": self.subscriber_count,
            **self.store.counts(),
        }

    # ---- transmit ----

    async def send_message(
        self,
        *,
        network: str,
        link: str | None,
        text: str,
        dest: str | None = None,
        channel: int | None = None,
    ) -> dict[str, Any]:
        if self.config.mesh.read_only:
            raise SendError(
                "mesh.read_only is enabled; set it to false in config to transmit"
            )
        adapter = self._pick_adapter(network, link)
        return await adapter.send_message(text, dest=dest, channel=channel)

    def _pick_adapter(self, network: str, link: str | None) -> MeshAdapter:
        if link:
            found = self.adapter(network, link)
            if found is None:
                raise SendError(f"no {network} link named {link!r}")
            return found
        candidates = [
            a
            for a in self._adapters.values()
            if a.network == network and a.link.state == LINK_UP
        ]
        if not candidates:
            raise SendError(f"no {network} link is currently up")
        if len(candidates) > 1:
            raise SendError(
                f"several {network} links are up; name one: "
                + ", ".join(sorted(a.name for a in candidates))
            )
        return candidates[0]


_registry: MeshRegistry | None = None


def get_registry() -> MeshRegistry:
    global _registry
    if _registry is None:
        _registry = MeshRegistry()
    return _registry


def set_registry(registry: MeshRegistry | None) -> None:
    """Used by the app lifespan and by tests."""
    global _registry
    _registry = registry
