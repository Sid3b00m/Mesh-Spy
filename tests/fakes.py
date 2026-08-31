"""Stand-ins shared by more than one test module.

Adapter-specific fakes live next to the tests that use them; these are the
pieces the registry and API tests both need.
"""
from __future__ import annotations

from typing import Any

from app.core.mesh.base import (
    LINK_DOWN,
    LINK_UP,
    MeshAdapter,
    MeshEvent,
)


class Collector:
    """Stands in for the registry's emit callback.

    Adapters are tested without a registry, so events are captured here and
    asserted on directly.
    """

    def __init__(self) -> None:
        self.events: list[MeshEvent] = []

    def __call__(self, event: MeshEvent) -> None:
        self.events.append(event)

    def of_kind(self, kind: str) -> list[Any]:
        return [e.payload for e in self.events if e.kind == kind]

    @property
    def nodes(self) -> list[Any]:
        return self.of_kind("node")

    @property
    def messages(self) -> list[Any]:
        return self.of_kind("message")

    @property
    def telemetry(self) -> list[Any]:
        return self.of_kind("telemetry")

    @property
    def links(self) -> list[Any]:
        return self.of_kind("link")

    def metric(self, name: str) -> Any:
        for sample in self.telemetry:
            if sample.metric == name:
                return sample
        return None

    def clear(self) -> None:
        self.events.clear()


class ScriptedAdapter(MeshAdapter):
    """A radio that fails to open a set number of times, then succeeds."""

    def __init__(
        self,
        name: str,
        emit,
        *,
        network: str = "meshtastic",
        failures: int = 0,
        drop_after_start: bool = False,
    ) -> None:
        self.network = network
        super().__init__(name, emit)
        self._link.transport = "scripted"
        self._link.target = "fake"
        self.failures = failures
        self.drop_after_start = drop_after_start
        self.registry: Any = None
        self.starts = 0
        self.stops = 0
        self.sent: list[tuple] = []
        self.send_error: Exception | None = None

    async def start(self) -> None:
        self.starts += 1
        if self.starts <= self.failures:
            raise RuntimeError(f"open failed #{self.starts}")
        self.set_state(LINK_UP, "connected")
        if self.drop_after_start and self.registry is not None:
            # Simulates a radio that opens and then falls off the bus.
            self.registry._down[self.key].set()

    async def stop(self) -> None:
        self.stops += 1
        self.set_state(LINK_DOWN, "stopped")

    async def send_message(self, text, *, dest=None, channel=None) -> dict[str, Any]:
        if self.send_error is not None:
            raise self.send_error
        self.sent.append((text, dest, channel))
        return {"ok": True, "network": self.network, "link": self.name, "ts": 1.0}

    def describe(self) -> dict[str, Any]:
        return self._describe_base({"library": "scripted"})


class FakeRequest:
    """Enough of a Starlette Request for the SSE endpoint.

    Driving the generator directly keeps the stream tests deterministic: a real
    HTTP client against an endless response is exactly the shape of test that
    hangs a CI run.
    """

    def __init__(self, *, disconnect_after: int | None = None) -> None:
        self.checks = 0
        self._disconnect_after = disconnect_after

    async def is_disconnected(self) -> bool:
        self.checks += 1
        if self._disconnect_after is None:
            return False
        return self.checks > self._disconnect_after
