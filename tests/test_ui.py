"""What the dashboard renders, and the two invariants that keep it safe.

Node names, message bodies and hardware models are all attacker-controlled in
the practical sense: anyone with a radio can set them. So the renderer must use
textContent throughout, and the page must stay loadable under a CSP with no
inline script and no external origin.
"""
from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.core.config import ROOT

JS = (ROOT / "app" / "static" / "js" / "app.js").read_text(encoding="utf-8")
CSS = (ROOT / "app" / "static" / "css" / "app.css").read_text(encoding="utf-8")
INDEX = (ROOT / "app" / "templates" / "index.html").read_text(encoding="utf-8")
LOGIN = (ROOT / "app" / "templates" / "login.html").read_text(encoding="utf-8")


PASSWORD = "hunter2"


def dashboard(config_factory, **overrides) -> str:
    config_factory(**overrides)
    from app.core.auth import auth_enabled
    from app.main import app

    # No lifespan: rendering the page touches templates and config only.
    client = TestClient(app)
    if auth_enabled():
        # The template branches on whether auth is on, so a test covering that
        # branch has to get past the login page first.
        client.post("/login", data={"username": "ops", "password": PASSWORD})
    response = client.get("/")
    assert response.status_code == 200
    return response.text


# ---- the send form is gated in the markup as well as on the server ----

def test_a_read_only_console_renders_no_send_form(config_factory):
    html = dashboard(config_factory, mesh={"read_only": True})
    assert 'id="send-form"' not in html
    assert "Read-only." in html


def test_turning_read_only_off_reveals_the_form(config_factory, monkeypatch):
    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    html = dashboard(
        config_factory, mesh={"read_only": False}, auth={"enabled": True}
    )
    assert 'id="send-form"' in html
    assert "disabled>Transmit" not in html


def test_without_auth_the_form_appears_but_cannot_be_used(config_factory):
    """Explaining why beats a form that silently 403s."""
    html = dashboard(config_factory, mesh={"read_only": False})
    assert 'id="send-form"' in html
    assert "disabled>Transmit" in html
    assert "requires auth even on localhost" in html


def test_the_message_field_advertises_the_server_side_cap(config_factory):
    html = dashboard(config_factory, mesh={"read_only": False})
    assert 'maxlength="200"' in html


def test_the_sign_out_link_only_appears_when_signed_in_is_possible(
    config_factory, monkeypatch
):
    assert "/logout" not in dashboard(config_factory)

    monkeypatch.setenv("MESH_SPY_PASSWORD", PASSWORD)
    assert "/logout" in dashboard(config_factory, auth={"enabled": True})


def test_the_page_names_the_networks_and_offers_a_filter(config_factory):
    html = dashboard(config_factory)
    assert 'data-network="meshtastic"' in html
    assert 'data-network="meshcore"' in html
    # Nodes are never merged across networks, so the filter is the way to
    # look at one of them.
    assert 'data-network=""' in html


def test_every_panel_the_plan_calls_for_is_present(config_factory):
    html = dashboard(config_factory, mesh={"read_only": False})
    for panel in ("Links", "Nodes", "Messages", "Telemetry", "Send"):
        assert f"<h2>{panel}" in html


# ---- output encoding ----

def test_the_renderer_never_assigns_html(config_factory):
    """A regression here is silent, so the mechanism is asserted directly."""
    # Written with the leading dot so the rule stated in the file's own header
    # comment does not trip the check.
    for sink in (".innerHTML", ".outerHTML", ".insertAdjacentHTML(", "document.write("):
        assert sink not in JS
    assert re.findall(r"\.(?:inner|outer)HTML\s*\+?=", JS) == []


def test_the_renderer_never_evaluates_strings(config_factory):
    assert "eval(" not in JS
    assert "new Function" not in JS


def test_message_bodies_and_node_names_go_through_text_nodes(config_factory):
    # The el() helper is the only place text reaches the DOM, and it uses
    # textContent.
    assert "node.textContent = String(text)" in JS
    assert 'el("div", "message-body", message.text)' in JS
    assert "createTextNode(node.label)" in JS


# ---- content security policy ----

def test_no_page_carries_inline_script(config_factory):
    for html in (INDEX, LOGIN):
        assert "<script>" not in html
        # An inline handler would need script-src 'unsafe-inline'.
        assert not re.search(r"\son(click|load|error|submit)=", html)


def test_the_only_script_is_served_from_this_app(config_factory):
    assert INDEX.count("<script") == 1
    assert '<script src="/static/js/app.js">' in INDEX


def test_nothing_is_fetched_from_another_origin(config_factory):
    """A CDN would break the offline-Pi deployment the whole design is for."""
    for name, text in (("index.html", INDEX), ("login.html", LOGIN), ("app.css", CSS)):
        assert "http://" not in text, name
        assert "https://" not in text, name
    # The one URL in the JS is the app's own SSE endpoint.
    assert 'EventSource("/api/stream")' in JS
    assert "//cdn" not in JS


# ---- the charting decision ----

def test_sparklines_are_drawn_rather_than_charted(config_factory):
    """No charting library, hence no CDN and no vendored bundle."""
    assert 'getContext("2d")' in JS
    assert "<canvas" in JS or 'el("canvas"' in JS


def test_a_flat_series_does_not_divide_by_zero(config_factory):
    assert "if (span === 0)" in JS


# ---- live updates ----

def test_updates_arrive_over_the_stream_rather_than_by_polling(config_factory):
    assert "EventSource" in JS
    # setInterval polling of the REST endpoints would defeat the point.
    assert "setInterval" not in JS


def test_a_burst_of_events_is_coalesced_into_one_repaint(config_factory):
    assert "requestAnimationFrame" in JS


def test_every_stream_event_kind_the_server_sends_is_handled(config_factory):
    for kind in ("snapshot", "node", "message", "telemetry", "link"):
        assert f'addEventListener("{kind}"' in JS


def test_a_dropped_stream_is_reported_to_the_operator(config_factory):
    assert 'setConnection("down", "reconnecting")' in JS
