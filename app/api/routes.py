from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from app import __version__
from app.core.auth import COOKIE, auth_enabled, session_valid
from app.core.mesh.base import SendError
from app.core.mesh.registry import MeshRegistry, get_registry
from app.core.security import (
    MESSAGE_MAX_CHARS,
    clamp_limit,
    send_limiter,
    validate_channel,
    validate_message,
    validate_metric,
    validate_network,
    validate_node_id,
    validate_radio_name,
)

log = logging.getLogger("mesh-spy.api")

router = APIRouter(prefix="/api")

# How long to wait for an event before emitting an SSE comment. Idle proxies
# and phone radios drop a silent connection, and a mesh can easily be quiet
# for minutes at a time.
HEARTBEAT_S = 20.0


def _registry() -> MeshRegistry:
    return get_registry()


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


def _network(value: str | None) -> str | None:
    """None means 'both networks', which the filters treat as no filter."""
    try:
        return validate_network(value)
    except ValueError as exc:
        raise _bad_request(exc) from exc


def _require_network(value: str) -> str:
    net = _network(value)
    if net is None:
        raise HTTPException(status_code=400, detail="network is required")
    return net


@router.get("/health")
def health() -> dict[str, Any]:
    """Unauthenticated liveness probe, used by the service units."""
    return {"ok": True, "app": "mesh-spy", "version": __version__}


@router.get("/status")
def status() -> dict[str, Any]:
    return {"ok": True, "version": __version__, **_registry().status()}


@router.get("/links")
def links() -> dict[str, Any]:
    reg = _registry()
    return {
        "ok": True,
        "links": reg.links(),
        "stale_after_seconds": reg.config.mesh.stale_after_seconds,
        "demo": reg.demo_active,
    }


@router.get("/nodes")
def nodes(
    network: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict[str, Any]:
    reg = _registry()
    return {
        "ok": True,
        "nodes": reg.store.nodes(
            network=_network(network),
            limit=clamp_limit(limit, default=200, maximum=1000),
            stale_after=reg.config.mesh.stale_after_seconds,
        ),
    }


@router.get("/nodes/{network}/{node_id}")
def node_detail(network: str, node_id: str) -> dict[str, Any]:
    net = _require_network(network)
    try:
        ident = validate_node_id(node_id)
    except ValueError as exc:
        raise _bad_request(exc) from exc
    found = _registry().store.node(net, ident)
    if found is None:
        raise HTTPException(status_code=404, detail="unknown node")
    return {"ok": True, "node": found}


@router.get("/messages")
def messages(
    network: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
) -> dict[str, Any]:
    return {
        "ok": True,
        "messages": _registry().store.messages(
            network=_network(network),
            limit=clamp_limit(limit, default=100, maximum=500),
        ),
    }


@router.get("/messages/history")
async def message_history(
    network: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=1000),
    before: float | None = Query(default=None),
) -> dict[str, Any]:
    """Paging straight from SQLite, for scrolling past the in-memory window."""
    return {
        "ok": True,
        "messages": await _registry().store.message_history(
            network=_network(network),
            limit=clamp_limit(limit, default=200, maximum=1000),
            before=before,
        ),
    }


@router.get("/telemetry")
def telemetry(
    network: str | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=500),
) -> dict[str, Any]:
    return {
        "ok": True,
        "series": _registry().store.telemetry_summary(
            network=_network(network),
            limit=clamp_limit(limit, default=60, maximum=200),
        ),
    }


@router.get("/telemetry/{network}/{node_id}/{metric}")
async def telemetry_detail(
    network: str,
    node_id: str,
    metric: str,
    history: bool = Query(default=False),
) -> dict[str, Any]:
    net = _require_network(network)
    try:
        ident = validate_node_id(node_id)
        metric_name = validate_metric(metric)
    except ValueError as exc:
        raise _bad_request(exc) from exc

    store = _registry().store
    series = store.telemetry_series(network=net, node_id=ident, metric=metric_name)
    if history:
        series["points"] = await store.telemetry_history(
            network=net, node_id=ident, metric=metric_name
        )
    return {"ok": True, **series}


class SendRequest(BaseModel):
    # Reject unknown fields rather than silently ignoring a typo in a field
    # that controls which radio transmits.
    model_config = ConfigDict(extra="forbid")

    network: str
    text: str
    link: str | None = None
    dest: str | None = None
    channel: int | None = None


def _require_send_auth(request: Request) -> None:
    """Transmitting needs a signed-in operator, even on localhost.

    Read endpoints are open when auth is off, which is fine for a dashboard.
    Keying up a transmitter is not: anything on the loopback interface,
    including another user on the same box, could otherwise put a radio on the
    air. CSRF is covered separately by the SameSite=Lax session cookie and by
    there being no CORS middleware, so a cross-site POST carries no session.
    """
    if not auth_enabled():
        raise HTTPException(
            status_code=403,
            detail=(
                "transmitting requires auth: set auth.enabled and "
                "MESH_SPY_PASSWORD, then sign in"
            ),
        )
    if not session_valid(request.cookies.get(COOKIE)):
        raise HTTPException(status_code=401, detail="sign in to transmit")


@router.post("/send")
async def send(payload: SendRequest, request: Request) -> dict[str, Any]:
    _require_send_auth(request)
    reg = _registry()

    # Checked before anything else so the reason is unambiguous in the UI.
    if reg.config.mesh.read_only:
        raise HTTPException(
            status_code=403,
            detail="mesh.read_only is enabled; set it to false in config to transmit",
        )

    net = _require_network(payload.network)
    try:
        text = validate_message(payload.text)
        channel = validate_channel(payload.channel)
        dest = validate_node_id(payload.dest) if payload.dest else None
        link = validate_radio_name(payload.link) if payload.link else None
    except ValueError as exc:
        raise _bad_request(exc) from exc

    # Airtime is a shared resource; a runaway script must not monopolise it.
    if not send_limiter.allow():
        raise HTTPException(
            status_code=429,
            detail=(
                f"send rate limit reached ({send_limiter.max_sends} per "
                f"{int(send_limiter.window_s)}s)"
            ),
        )

    try:
        result = await reg.send_message(
            network=net, link=link, text=text, dest=dest, channel=channel
        )
    except SendError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    log.info("transmitted on %s via %s", net, result.get("link"))
    return {"ok": True, **result}


@router.get("/send/limits")
def send_limits() -> dict[str, Any]:
    """What the UI needs to render and pre-validate the send form."""
    reg = _registry()
    return {
        "ok": True,
        "read_only": reg.config.mesh.read_only,
        "auth_required": True,
        "auth_configured": auth_enabled(),
        "max_chars": MESSAGE_MAX_CHARS,
        "max_sends": send_limiter.max_sends,
        "window_seconds": send_limiter.window_s,
    }


def _snapshot(reg: MeshRegistry) -> dict[str, Any]:
    """Sent first on every SSE connection.

    Without this a client would have to fetch the REST endpoints and then
    connect, losing any event that landed in between.
    """
    return {
        "kind": "snapshot",
        "data": {
            "links": reg.links(),
            "nodes": reg.store.nodes(
                limit=200, stale_after=reg.config.mesh.stale_after_seconds
            ),
            "messages": reg.store.messages(limit=100),
            "series": reg.store.telemetry_summary(limit=60),
            "status": reg.status(),
        },
    }


def _sse(payload: dict[str, Any]) -> str:
    return f"event: {payload['kind']}\ndata: {json.dumps(payload['data'])}\n\n"


@router.get("/stream")
async def stream(request: Request) -> StreamingResponse:
    """Live updates. Mesh traffic is bursty and sparse, so pushing beats polling."""
    reg = _registry()
    queue = reg.subscribe()

    async def events() -> AsyncIterator[str]:
        try:
            yield _sse(_snapshot(reg))
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except asyncio.TimeoutError:
                    # A comment line keeps the connection alive without
                    # inventing an event the client would have to handle.
                    yield ": keepalive\n\n"
                    continue
                yield _sse(message)
        except asyncio.CancelledError:
            raise
        finally:
            reg.unsubscribe(queue)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            # Tell nginx not to buffer, and mark the body as already-encoded so
            # GZipMiddleware leaves the stream alone. Recent Starlette excludes
            # text/event-stream by default, but older releases do not, and a
            # buffered SSE stream fails silently.
            "Content-Encoding": "identity",
            "X-Accel-Buffering": "no",
        },
    )
