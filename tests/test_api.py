"""REST contracts, the auth gate on transmitting, and the SSE stream.

The HTTP tests run the app's real lifespan against a config pointed at a
temporary database, so the registry, store and middleware stack under test are
the ones that ship.

The stream is exercised by driving the response generator instead of over a
socket. An endless HTTP response is exactly the shape of test that hangs a CI
run, and nothing about the SSE framing needs a real connection to check.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

import pytest
from fastapi.testclient import TestClient

from app.api import routes
from app.core.mesh.base import LINK_UP, MeshEvent, NodeRecord, SendError
from app.core.mesh.registry import MeshRegistry, get_registry, set_registry
from tests.fakes import FakeRequest, ScriptedAdapter

PASSWORD = "correct horse battery staple"


@contextlib.contextmanager
def console(config_factory, **overrides):
    """A client against the real app, with the config overridden first."""
    config_factory(**overrides)
    from app.main import app

    with TestClient(app) as client:
        yield client


@contextlib.contextmanager
def signed_in_console(config_factory, monkeypatch, **overrides):
    """A client with auth on, a password set, and a session established."""
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    # Transmitting needs a real radio; the simulation refuses, which several of
    # these tests would otherwise trip over.
    monkeypatch.setenv("MESH_SPY_NO_DEMO", "1")
    overrides["auth"] = {"enabled": True, "username": "ops", **overrides.get("auth", {})}

    with console(config_factory, **overrides) as client:
        response = client.post("/login", data={"username": "ops", "password": PASSWORD})
        assert response.status_code == 200
        yield client


def wait_for_nodes(client, expected: int = 5) -> list[dict]:
    """Poll until the dispatcher has stored the seed nodes.

    The simulated network emits its nodes onto the queue as it starts, and the
    dispatcher drains that queue on the same loop the request runs on, so the
    first response after startup can legitimately arrive early.
    """
    for _ in range(200):
        nodes = client.get("/api/nodes?limit=1000").json()["nodes"]
        if len(nodes) >= expected:
            return nodes
    raise AssertionError(f"only {len(nodes)} nodes appeared, wanted {expected}")


def add_link(name: str = "base", *, network: str = "meshtastic") -> ScriptedAdapter:
    """Attach an already-open fake radio to the running registry."""
    reg = get_registry()
    adapter = ScriptedAdapter(name, reg._emit, network=network)
    reg._add(adapter)
    adapter.link.state = LINK_UP
    return adapter


# ---- read endpoints ----

def test_health_needs_no_auth_even_when_auth_is_on(config_factory, monkeypatch):
    """The service units poll this, and they have no session."""
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    with console(config_factory, auth={"enabled": True}) as client:
        response = client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["app"] == "mesh-spy"


def test_status_reports_the_headline_numbers(config_factory):
    with console(config_factory) as client:
        body = client.get("/api/status").json()
        assert body["ok"] is True
        assert body["read_only"] is True
        assert body["demo"] is True
        assert body["links"] == 2
        assert "version" in body


def test_links_describes_each_radio(config_factory):
    with console(config_factory) as client:
        body = client.get("/api/links").json()
        assert body["demo"] is True
        assert body["stale_after_seconds"] == 300.0
        assert {link["network"] for link in body["links"]} == {"meshtastic", "meshcore"}
        assert all(link["library"] == "simulated" for link in body["links"])


def test_nodes_are_listed_and_filterable_by_network(config_factory):
    with console(config_factory) as client:
        assert len(wait_for_nodes(client)) == 5

        just_meshcore = client.get("/api/nodes?network=meshcore").json()["nodes"]
        assert len(just_meshcore) == 2
        assert all(node["network"] == "meshcore" for node in just_meshcore)


def test_a_node_list_entry_carries_what_the_table_renders(config_factory):
    with console(config_factory) as client:
        node = wait_for_nodes(client)[0]
        for field in ("network", "id", "key", "label", "last_seen", "age", "stale"):
            assert field in node
        # raw is bulky and only wanted on the detail endpoint.
        assert "raw" not in node


def test_an_unknown_network_is_a_bad_request(config_factory):
    with console(config_factory) as client:
        response = client.get("/api/nodes?network=lora")
        assert response.status_code == 400
        assert "meshtastic" in response.json()["detail"]


def test_node_detail_includes_the_untouched_payload(config_factory):
    with console(config_factory) as client:
        wait_for_nodes(client)
        listed = client.get("/api/nodes?network=meshtastic").json()["nodes"][0]
        body = client.get(f"/api/nodes/meshtastic/{listed['id']}").json()
        assert body["node"]["id"] == listed["id"]
        assert body["node"]["raw"] == {"demo": True}


def test_an_unknown_node_is_a_404(config_factory):
    with console(config_factory) as client:
        assert client.get("/api/nodes/meshtastic/!nosuchnode").status_code == 404


def test_a_malformed_node_id_is_rejected_before_any_lookup(config_factory):
    with console(config_factory) as client:
        response = client.get("/api/nodes/meshtastic/'%20OR%201=1")
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid node id"


def test_a_quiet_console_reports_no_messages(config_factory):
    with console(config_factory) as client:
        assert client.get("/api/messages").json()["messages"] == []
        assert client.get("/api/messages/history").json()["messages"] == []


def test_telemetry_endpoints_report_a_series(config_factory):
    with console(config_factory) as client:
        body = client.get("/api/telemetry").json()
        assert body["ok"] is True
        assert isinstance(body["series"], list)

        detail = client.get("/api/telemetry/meshtastic/!433d061c/battery").json()
        assert detail["metric"] == "battery"
        assert detail["node_id"] == "!433d061c"
        assert detail["points"] == []


def test_a_telemetry_history_request_reaches_sqlite(config_factory):
    with console(config_factory) as client:
        body = client.get(
            "/api/telemetry/meshcore/3f9a1c7d0b28/voltage?history=true"
        ).json()
        assert body["points"] == []


def test_a_malformed_metric_name_is_rejected(config_factory):
    with console(config_factory) as client:
        response = client.get("/api/telemetry/meshtastic/!433d061c/Battery%20Level")
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid metric name"


def test_an_oversized_limit_is_rejected_by_the_signature(config_factory):
    with console(config_factory) as client:
        assert client.get("/api/nodes?limit=99999").status_code == 422
        # Inside the signature's bounds but above the endpoint's own cap, the
        # limit is clamped rather than refused.
        assert client.get("/api/nodes?limit=900").status_code == 200


def test_every_response_carries_the_hardening_headers(config_factory):
    with console(config_factory) as client:
        headers = client.get("/api/status").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        # No CDN anywhere, so the policy can stay strict.
        assert "script-src 'self'" in headers["Content-Security-Policy"]


# ---- the auth gate on reads ----

def test_with_auth_on_the_dashboard_redirects_to_the_login_page(
    config_factory, monkeypatch
):
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    with console(config_factory, auth={"enabled": True}) as client:
        response = client.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/login"


def test_with_auth_on_the_api_answers_401_rather_than_redirecting(
    config_factory, monkeypatch
):
    """A fetch() quietly following a redirect to HTML is a confusing failure."""
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    with console(config_factory, auth={"enabled": True}) as client:
        response = client.get("/api/nodes")
        assert response.status_code == 401
        assert response.json()["error"] == "auth required"


def test_auth_stays_off_when_enabled_but_no_password_is_set(config_factory):
    """Locking the console behind an empty password would lock the operator out."""
    with console(config_factory, auth={"enabled": True}) as client:
        assert client.get("/api/nodes").status_code == 200


def test_the_wrong_password_does_not_establish_a_session(config_factory, monkeypatch):
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    with console(config_factory, auth={"enabled": True}) as client:
        response = client.post(
            "/login",
            data={"username": "ops", "password": "guess"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/login?error=1"
        assert client.get("/api/nodes").status_code == 401


def test_signing_in_then_out_opens_and_closes_access(config_factory, monkeypatch):
    with signed_in_console(config_factory, monkeypatch) as client:
        assert client.get("/api/nodes").status_code == 200
        client.get("/logout")
        assert client.get("/api/nodes").status_code == 401


# ---- the send path ----

def test_the_send_form_limits_are_published_for_the_ui(config_factory):
    with console(config_factory) as client:
        body = client.get("/api/send/limits").json()
        assert body["read_only"] is True
        assert body["auth_required"] is True
        assert body["auth_configured"] is False
        assert body["max_chars"] == 200
        assert body["max_sends"] == 10


def test_transmitting_is_refused_outright_when_auth_is_off(config_factory):
    """Anything on the loopback interface could otherwise key up a radio."""
    with console(config_factory, mesh={"read_only": False}) as client:
        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 403
        assert "requires auth" in response.json()["detail"]


def test_transmitting_without_a_session_is_refused(config_factory, monkeypatch):
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    with console(
        config_factory, auth={"enabled": True}, mesh={"read_only": False}
    ) as client:
        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 401


def test_a_fresh_install_cannot_transmit_even_signed_in(config_factory, monkeypatch):
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": True}
    ) as client:
        add_link()
        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 403
        assert "read_only" in response.json()["detail"]


def test_a_signed_in_operator_can_transmit_once_read_only_is_off(
    config_factory, monkeypatch
):
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        adapter = add_link()

        response = client.post(
            "/api/send",
            json={
                "network": "meshtastic",
                "link": "base",
                "text": "net check, how copy",
                "dest": "!433d061c",
                "channel": 2,
            },
        )

        assert response.status_code == 200
        assert response.json()["ok"] is True
        assert adapter.sent == [("net check, how copy", "!433d061c", 2)]


@pytest.mark.parametrize(
    "payload, reason",
    [
        ({"network": "meshtastic", "text": "   "}, "message is empty"),
        ({"network": "meshtastic", "text": "x" * 201}, "200 characters or fewer"),
        ({"network": "meshtastic", "text": "hi", "channel": 9}, "between 0 and 7"),
        ({"network": "meshtastic", "text": "hi", "dest": "'; DROP"}, "invalid node id"),
        ({"network": "meshtastic", "text": "hi", "link": "../etc"}, "invalid radio name"),
        ({"network": "lora", "text": "hi"}, "network must be one of"),
    ],
)
def test_a_bad_send_is_refused_with_the_reason(
    config_factory, monkeypatch, payload, reason
):
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        add_link()
        response = client.post("/api/send", json=payload)
        assert response.status_code == 400
        assert reason in response.json()["detail"]


def test_an_unexpected_field_is_refused_rather_than_ignored(config_factory, monkeypatch):
    """A typo in the field naming the radio must not silently pick another."""
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        add_link()
        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hi", "lnik": "roof"}
        )
        assert response.status_code == 422


def test_sending_with_no_radio_up_is_a_conflict_not_a_crash(config_factory, monkeypatch):
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 409
        assert "no meshtastic link is currently up" in response.json()["detail"]


def test_a_radio_rejecting_a_message_is_reported_as_a_conflict(
    config_factory, monkeypatch
):
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        adapter = add_link()
        adapter.send_error = SendError("tx queue full")

        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 409
        assert "tx queue full" in response.json()["detail"]


def test_airtime_is_rate_limited(config_factory, monkeypatch):
    """A runaway script must not monopolise a channel everyone shares."""
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        add_link()
        body = {"network": "meshtastic", "text": "spam"}

        codes = [client.post("/api/send", json=body).status_code for _ in range(11)]

        assert codes[:10] == [200] * 10
        assert codes[10] == 429


def test_the_rate_limit_is_only_spent_on_a_valid_message(config_factory, monkeypatch):
    """A rejected message never reached a radio, so it must not cost airtime."""
    with signed_in_console(
        config_factory, monkeypatch, mesh={"read_only": False}
    ) as client:
        add_link()
        for _ in range(20):
            client.post("/api/send", json={"network": "meshtastic", "text": ""})

        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 200


def test_the_simulated_network_refuses_to_transmit_over_http(
    config_factory, monkeypatch
):
    """Demo mode must not let the UI pretend a message went out."""
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    with console(
        config_factory, auth={"enabled": True}, mesh={"read_only": False}
    ) as client:
        client.post("/login", data={"username": "ops", "password": PASSWORD})
        for adapter in get_registry()._adapters.values():
            adapter.link.state = LINK_UP

        response = client.post(
            "/api/send", json={"network": "meshtastic", "text": "hello"}
        )
        assert response.status_code == 409
        assert "no radio to transmit on" in response.json()["detail"]


# ---- compression ----

def test_a_large_node_list_is_compressed(config_factory, monkeypatch):
    monkeypatch.setenv("MESH_SPY_NO_DEMO", "1")
    with console(config_factory) as client:
        store = get_registry().store
        for i in range(300):
            node = NodeRecord(network="meshtastic", id=f"!{i:08x}", name=f"Node {i}")
            store._nodes[node.key] = node

        response = client.get(
            "/api/nodes?limit=1000", headers={"Accept-Encoding": "gzip"}
        )
        assert response.headers.get("content-encoding") == "gzip"
        assert len(response.json()["nodes"]) == 300


# ---- the SSE stream ----

async def open_stream_console(config_factory) -> MeshRegistry:
    config_factory()
    reg = MeshRegistry(allow_demo=False)
    await reg.start()
    set_registry(reg)
    return reg


async def test_the_stream_opens_with_a_snapshot(config_factory):
    reg = await open_stream_console(config_factory)
    try:
        response = await routes.stream(FakeRequest())
        events = response.body_iterator

        first = await events.__anext__()
        assert first.startswith("event: snapshot\n")
        payload = json.loads(first.split("data: ", 1)[1])
        # Without this a client would have to fetch the REST endpoints and
        # then connect, losing anything that landed in between.
        assert set(payload) == {"links", "nodes", "messages", "series", "status"}

        await events.aclose()
    finally:
        set_registry(None)
        await reg.stop()


async def test_the_stream_pushes_each_event_as_it_arrives(config_factory):
    reg = await open_stream_console(config_factory)
    try:
        response = await routes.stream(FakeRequest())
        events = response.body_iterator
        await events.__anext__()

        reg._emit(
            MeshEvent("node", NodeRecord(network="meshcore", id="abc", name="Shed"))
        )
        await asyncio.wait_for(reg._queue.join(), timeout=5)

        chunk = await asyncio.wait_for(events.__anext__(), timeout=5)
        assert chunk.startswith("event: node\n")
        assert json.loads(chunk.split("data: ", 1)[1])["name"] == "Shed"

        await events.aclose()
    finally:
        set_registry(None)
        await reg.stop()


async def test_a_quiet_mesh_still_gets_a_keepalive(config_factory, monkeypatch):
    """Idle proxies and phone radios drop a connection that says nothing."""
    monkeypatch.setattr(routes, "HEARTBEAT_S", 0.01)
    reg = await open_stream_console(config_factory)
    try:
        response = await routes.stream(FakeRequest())
        events = response.body_iterator
        await events.__anext__()

        chunk = await asyncio.wait_for(events.__anext__(), timeout=5)
        # A comment, not an event, so the client has nothing extra to handle.
        assert chunk == ": keepalive\n\n"

        await events.aclose()
    finally:
        set_registry(None)
        await reg.stop()


async def test_a_disconnected_client_is_unsubscribed(config_factory):
    reg = await open_stream_console(config_factory)
    try:
        response = await routes.stream(FakeRequest(disconnect_after=0))
        events = response.body_iterator
        await events.__anext__()
        assert reg.subscriber_count == 1

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(events.__anext__(), timeout=5)
        assert reg.subscriber_count == 0
    finally:
        set_registry(None)
        await reg.stop()


async def test_the_stream_is_marked_so_nothing_downstream_buffers_it(config_factory):
    reg = await open_stream_console(config_factory)
    try:
        response = await routes.stream(FakeRequest())
        assert response.media_type == "text/event-stream"
        # gzip would hold the whole stream, and nginx buffers by default.
        assert response.headers["content-encoding"] == "identity"
        assert response.headers["x-accel-buffering"] == "no"
        assert "no-cache" in response.headers["cache-control"]
        await response.body_iterator.aclose()
    finally:
        set_registry(None)
        await reg.stop()
