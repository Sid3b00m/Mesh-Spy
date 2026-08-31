"use strict";

/* Mesh-Spy dashboard.
 *
 * No build step and no framework, matching the rest of the stack. Two rules
 * worth stating outright:
 *
 * 1. Every string that came off the mesh is written with textContent, never
 *    innerHTML. Node names, message bodies and hardware models are all
 *    attacker-controlled in the sense that anyone with a radio can set them.
 * 2. Rendering is coalesced into an animation frame. A mesh burst can deliver
 *    dozens of events at once and re-rendering per event would thrash.
 */
(function () {
  var MAX_MESSAGES = 200;
  var SPARK_POINTS = 120;

  var state = {
    links: new Map(),
    nodes: new Map(),
    messages: [],
    series: new Map(),
    status: {},
    network: "",
    staleAfter: 300
  };

  var dom = {
    links: document.getElementById("links"),
    linksEmpty: document.getElementById("links-empty"),
    nodes: document.getElementById("nodes"),
    nodesEmpty: document.getElementById("nodes-empty"),
    nodesCount: document.getElementById("nodes-count"),
    messages: document.getElementById("messages"),
    messagesEmpty: document.getElementById("messages-empty"),
    messagesCount: document.getElementById("messages-count"),
    series: document.getElementById("series"),
    seriesEmpty: document.getElementById("series-empty"),
    counts: document.getElementById("counts"),
    conn: document.getElementById("conn"),
    demoBanner: document.getElementById("demo-banner")
  };

  /* ---- small helpers ---- */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = String(text); }
    return node;
  }

  function cell(row, text, className) {
    row.appendChild(el("td", className, text === null || text === undefined ? "-" : text));
  }

  function num(value, digits, suffix) {
    if (value === null || value === undefined || isNaN(value)) { return null; }
    return Number(value).toFixed(digits === undefined ? 1 : digits) + (suffix || "");
  }

  function age(seconds) {
    if (seconds === null || seconds === undefined) { return null; }
    var s = Math.max(0, Math.floor(seconds));
    if (s < 60) { return s + "s"; }
    if (s < 3600) { return Math.floor(s / 60) + "m"; }
    if (s < 86400) { return Math.floor(s / 3600) + "h"; }
    return Math.floor(s / 86400) + "d";
  }

  function clockTime(epoch) {
    if (!epoch) { return ""; }
    var d = new Date(epoch * 1000);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  function position(node) {
    if (node.lat === null || node.lat === undefined || node.lon === null || node.lon === undefined) {
      return null;
    }
    return Number(node.lat).toFixed(4) + ", " + Number(node.lon).toFixed(4);
  }

  function networkTag(network) {
    var tag = el("span", "tag net-" + network, network === "meshtastic" ? "MT" : "MC");
    tag.title = network;
    return tag;
  }

  function seriesKey(network, nodeId, metric) {
    return network + "|" + nodeId + "|" + metric;
  }

  function visible(network) {
    return !state.network || state.network === network;
  }

  function nodeLabel(network, nodeId) {
    var node = state.nodes.get(network + ":" + nodeId);
    return node ? node.label : nodeId;
  }

  /* ---- render scheduling ---- */

  var dirty = {};
  var frame = null;

  function schedule() {
    if (frame !== null) { return; }
    frame = window.requestAnimationFrame(function () {
      frame = null;
      var work = dirty;
      dirty = {};
      if (work.links) { renderLinks(); }
      if (work.nodes) { renderNodes(); }
      if (work.messages) { renderMessages(); }
      if (work.series) { renderSeries(); }
      renderCounts();
    });
  }

  function markDirty(what) {
    dirty[what] = true;
    schedule();
  }

  function markAllDirty() {
    dirty.links = dirty.nodes = dirty.messages = dirty.series = true;
    schedule();
  }

  /* ---- panels ---- */

  function renderLinks() {
    var rows = Array.from(state.links.values()).filter(function (link) {
      return visible(link.network);
    });
    rows.sort(function (a, b) {
      return a.network.localeCompare(b.network) || a.name.localeCompare(b.name);
    });

    dom.links.replaceChildren();
    rows.forEach(function (link) {
      var tr = el("tr");
      cell(tr, link.name, "strong");
      var td = el("td");
      td.appendChild(networkTag(link.network));
      tr.appendChild(td);
      cell(tr, link.transport);
      cell(tr, link.target, "mono");

      var stateCell = el("td");
      var badge = el("span", "badge badge-" + link.state, link.state);
      if (link.detail) { badge.title = link.detail; }
      stateCell.appendChild(badge);
      if (link.stale) {
        stateCell.appendChild(el("span", "badge badge-stale", "stale"));
      }
      tr.appendChild(stateCell);

      cell(tr, link.node_id, "mono");
      cell(tr, age(link.silent_for));
      cell(tr, link.attempts ? link.attempts : null);
      dom.links.appendChild(tr);
    });
    dom.linksEmpty.hidden = rows.length > 0;
  }

  function renderNodes() {
    var rows = Array.from(state.nodes.values()).filter(function (node) {
      return visible(node.network);
    });
    rows.sort(function (a, b) { return (b.last_seen || 0) - (a.last_seen || 0); });

    var now = Date.now() / 1000;
    dom.nodes.replaceChildren();
    rows.forEach(function (node) {
      var seconds = node.last_seen ? now - node.last_seen : null;
      var tr = el("tr");
      if (seconds !== null && seconds > state.staleAfter) { tr.className = "is-stale"; }

      var nameCell = el("td", "strong");
      nameCell.appendChild(document.createTextNode(node.label));
      if (node.is_self) { nameCell.appendChild(el("span", "badge badge-self", "self")); }
      if (node.short_name && node.short_name !== node.label) {
        nameCell.appendChild(el("span", "muted", " " + node.short_name));
      }
      nameCell.title = node.id + (node.hw_model ? " / " + node.hw_model : "");
      tr.appendChild(nameCell);

      var netCell = el("td");
      netCell.appendChild(networkTag(node.network));
      tr.appendChild(netCell);

      cell(tr, node.hops === null || node.hops === undefined ? null : node.hops);
      cell(tr, num(node.snr, 1, " dB"));
      cell(tr, num(node.rssi, 0, " dBm"));
      cell(tr, num(node.battery, 0, "%"));
      cell(tr, position(node), "mono");
      cell(tr, age(seconds));
      dom.nodes.appendChild(tr);
    });
    dom.nodesEmpty.hidden = rows.length > 0;
    dom.nodesCount.textContent = rows.length ? "(" + rows.length + ")" : "";
  }

  function renderMessages() {
    var rows = state.messages.filter(function (message) {
      return visible(message.network);
    });

    dom.messages.replaceChildren();
    rows.forEach(function (message) {
      var li = el("li", "message" + (message.direction === "tx" ? " is-tx" : ""));

      var head = el("div", "message-head");
      head.appendChild(el("span", "mono muted", clockTime(message.ts)));
      head.appendChild(networkTag(message.network));

      var from = message.from_name || message.from_id ||
        (message.direction === "tx" ? "this station" : "unknown");
      head.appendChild(el("span", "strong", from));

      if (message.direction === "tx") {
        head.appendChild(el("span", "badge badge-tx", "sent"));
      }
      if (message.channel !== null && message.channel !== undefined) {
        head.appendChild(el("span", "badge badge-chan", "ch " + message.channel));
      } else if (!message.broadcast && message.to_id) {
        head.appendChild(el("span", "badge badge-direct", "direct"));
      }
      if (message.snr !== null && message.snr !== undefined) {
        head.appendChild(el("span", "muted", num(message.snr, 1, " dB")));
      }
      if (message.hops !== null && message.hops !== undefined) {
        head.appendChild(el("span", "muted", message.hops + " hop" + (message.hops === 1 ? "" : "s")));
      }
      li.appendChild(head);

      // textContent, so a node calling itself <img onerror=...> is just text.
      li.appendChild(el("div", "message-body", message.text));
      dom.messages.appendChild(li);
    });
    dom.messagesEmpty.hidden = rows.length > 0;
    dom.messagesCount.textContent = rows.length ? "(" + rows.length + ")" : "";
  }

  function renderSeries() {
    var rows = Array.from(state.series.values()).filter(function (s) {
      return visible(s.network) && s.points.length > 0;
    });
    rows.sort(function (a, b) {
      return a.network.localeCompare(b.network) ||
        nodeLabel(a.network, a.node_id).localeCompare(nodeLabel(b.network, b.node_id)) ||
        a.metric.localeCompare(b.metric);
    });

    dom.series.replaceChildren();
    rows.forEach(function (s) {
      var card = el("div", "series-card");

      var head = el("div", "series-head");
      head.appendChild(networkTag(s.network));
      head.appendChild(el("span", "strong", nodeLabel(s.network, s.node_id)));
      head.appendChild(el("span", "muted", s.metric.replace(/_/g, " ")));
      card.appendChild(head);

      var value = el("div", "series-value");
      value.appendChild(document.createTextNode(num(s.value, 2, "")));
      if (s.unit) { value.appendChild(el("span", "muted unit", s.unit)); }
      card.appendChild(value);

      var canvas = el("canvas", "spark");
      canvas.width = 240;
      canvas.height = 40;
      card.appendChild(canvas);

      var range = el("div", "series-range muted");
      range.textContent = num(s.min, 1, "") + " to " + num(s.max, 1, "") +
        "  |  " + s.points.length + " pts";
      card.appendChild(range);

      dom.series.appendChild(card);
      drawSpark(canvas, s.points);
    });
    dom.seriesEmpty.hidden = rows.length > 0;
  }

  function renderCounts() {
    var parts = [];
    parts.push(state.nodes.size + " nodes");
    parts.push(state.messages.length + " msgs");
    parts.push(state.series.size + " series");
    if (state.status.dropped_events) {
      parts.push(state.status.dropped_events + " dropped");
    }
    dom.counts.textContent = parts.join("  |  ");
    dom.demoBanner.hidden = !state.status.demo;
  }

  /* ---- sparklines ----
   * Drawn on a canvas rather than pulled in as a charting library, which
   * would mean either a CDN or vendored assets. Neither suits an offline Pi.
   */

  function drawSpark(canvas, points) {
    var ratio = window.devicePixelRatio || 1;
    var width = canvas.width;
    var height = canvas.height;
    if (ratio !== 1) {
      canvas.width = width * ratio;
      canvas.height = height * ratio;
      canvas.style.width = width + "px";
      canvas.style.height = height + "px";
    }

    var ctx = canvas.getContext("2d");
    if (!ctx) { return; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    if (points.length === 0) { return; }

    var min = Math.min.apply(null, points);
    var max = Math.max.apply(null, points);
    // A flat series would divide by zero; centre it instead.
    var span = max - min;
    if (span === 0) { min -= 1; max += 1; span = 2; }

    var pad = 3;
    var usable = height - pad * 2;
    var step = points.length > 1 ? width / (points.length - 1) : 0;

    function pointX(i) { return points.length > 1 ? i * step : width / 2; }
    function pointY(v) { return pad + usable - ((v - min) / span) * usable; }

    var accent = getComputedStyle(document.body).getPropertyValue("--accent").trim() || "#4fc3f7";

    ctx.beginPath();
    ctx.moveTo(pointX(0), pointY(points[0]));
    for (var i = 1; i < points.length; i++) {
      ctx.lineTo(pointX(i), pointY(points[i]));
    }
    ctx.strokeStyle = accent;
    ctx.lineWidth = 1.5;
    ctx.lineJoin = "round";
    ctx.stroke();

    ctx.lineTo(pointX(points.length - 1), height);
    ctx.lineTo(pointX(0), height);
    ctx.closePath();
    ctx.globalAlpha = 0.12;
    ctx.fillStyle = accent;
    ctx.fill();
    ctx.globalAlpha = 1;

    // Mark the latest sample so a single-point series is still visible.
    ctx.beginPath();
    ctx.arc(pointX(points.length - 1), pointY(points[points.length - 1]), 2, 0, Math.PI * 2);
    ctx.fillStyle = accent;
    ctx.fill();
  }

  /* ---- state updates ---- */

  function applySnapshot(data) {
    state.links = new Map();
    (data.links || []).forEach(function (link) { state.links.set(link.key, link); });

    state.nodes = new Map();
    (data.nodes || []).forEach(function (node) { state.nodes.set(node.key, node); });

    state.messages = (data.messages || []).slice(0, MAX_MESSAGES);

    state.series = new Map();
    (data.series || []).forEach(function (s) {
      state.series.set(seriesKey(s.network, s.node_id, s.metric), {
        network: s.network,
        node_id: s.node_id,
        metric: s.metric,
        unit: s.unit,
        value: s.value,
        ts: s.ts,
        min: s.min,
        max: s.max,
        points: (s.points || []).slice()
      });
    });

    state.status = data.status || {};
    markAllDirty();
  }

  function applyNode(node) {
    state.nodes.set(node.key, node);
    markDirty("nodes");
    // A rename changes the label shown beside every one of its series.
    markDirty("series");
  }

  function applyMessage(message) {
    state.messages.unshift(message);
    if (state.messages.length > MAX_MESSAGES) {
      state.messages.length = MAX_MESSAGES;
    }
    markDirty("messages");
  }

  function applyTelemetry(sample) {
    var key = seriesKey(sample.network, sample.node_id, sample.metric);
    var entry = state.series.get(key);
    if (!entry) {
      entry = {
        network: sample.network,
        node_id: sample.node_id,
        metric: sample.metric,
        unit: sample.unit,
        points: []
      };
      state.series.set(key, entry);
    }
    entry.points.push(sample.value);
    if (entry.points.length > SPARK_POINTS) {
      entry.points.splice(0, entry.points.length - SPARK_POINTS);
    }
    entry.value = sample.value;
    entry.ts = sample.ts;
    entry.unit = sample.unit || entry.unit;
    entry.min = Math.min.apply(null, entry.points);
    entry.max = Math.max.apply(null, entry.points);
    markDirty("series");
  }

  function applyLink(link) {
    var existing = state.links.get(link.key) || {};
    // Link events carry the LinkStatus fields but not the derived ones the
    // /api/links view adds, so keep whatever we already had.
    state.links.set(link.key, Object.assign({}, existing, link));
    markDirty("links");
  }

  /* ---- live stream ---- */

  function setConnection(status, text) {
    dom.conn.className = "badge badge-" + status;
    dom.conn.textContent = text;
  }

  function connect() {
    var source = new EventSource("/api/stream");

    source.addEventListener("open", function () { setConnection("up", "live"); });

    source.addEventListener("snapshot", function (event) {
      setConnection("up", "live");
      applySnapshot(JSON.parse(event.data));
    });
    source.addEventListener("node", function (event) { applyNode(JSON.parse(event.data)); });
    source.addEventListener("message", function (event) { applyMessage(JSON.parse(event.data)); });
    source.addEventListener("telemetry", function (event) { applyTelemetry(JSON.parse(event.data)); });
    source.addEventListener("link", function (event) { applyLink(JSON.parse(event.data)); });

    source.addEventListener("error", function () {
      // EventSource reconnects on its own; this only reports the gap.
      setConnection("down", "reconnecting");
    });
  }

  /* ---- filters ---- */

  function wireFilters() {
    var chips = document.querySelectorAll(".filters .chip");
    Array.prototype.forEach.call(chips, function (chip) {
      chip.addEventListener("click", function () {
        state.network = chip.getAttribute("data-network") || "";
        Array.prototype.forEach.call(chips, function (other) {
          other.classList.toggle("is-active", other === chip);
        });
        markAllDirty();
      });
    });
  }

  /* ---- send ----
   * Present only when mesh.read_only is false. The server still enforces
   * read-only and auth independently; this form is a convenience, not the
   * gate.
   */

  function wireSend() {
    var form = document.getElementById("send-form");
    if (!form) { return; }
    var status = document.getElementById("send-status");
    var button = document.getElementById("send-button");
    var text = document.getElementById("send-text");

    function report(message, className) {
      status.className = "empty " + (className || "");
      status.textContent = message;
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var body = {
        network: document.getElementById("send-network").value,
        text: text.value
      };
      var link = document.getElementById("send-link").value.trim();
      var dest = document.getElementById("send-dest").value.trim();
      var channel = document.getElementById("send-channel").value;
      if (link) { body.link = link; }
      if (dest) { body.dest = dest; }
      if (channel !== "") { body.channel = Number(channel); }

      button.disabled = true;
      report("transmitting...", "muted");

      fetch("/api/send", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body)
      })
        .then(function (response) {
          return response.json().then(function (data) {
            return { ok: response.ok, status: response.status, data: data };
          });
        })
        .then(function (result) {
          if (result.ok) {
            // The radio's own echo arrives over the stream as a tx message,
            // so there is nothing to insert here.
            text.value = "";
            report("sent on " + result.data.network + " via " + result.data.link, "muted");
          } else {
            report("refused: " + (result.data.detail || result.status), "error");
          }
        })
        .catch(function () { report("send failed: no response from the console", "error"); })
        .then(function () { button.disabled = false; });
    });
  }

  function boot() {
    wireFilters();
    wireSend();
    setConnection("wait", "connecting");
    // Paint whatever is already stored before the stream opens, so a slow
    // first event does not leave the console blank.
    fetch("/api/links")
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.staleAfter = data.stale_after_seconds || state.staleAfter;
      })
      .catch(function () { /* the stream snapshot will cover it */ });
    connect();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}());
