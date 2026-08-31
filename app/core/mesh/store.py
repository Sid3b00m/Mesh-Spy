"""Persistence and the read cache behind the dashboard.

Two access patterns, deliberately split:

* Writes come from the dispatcher task, one event at a time, over `aiosqlite`
  so a slow SD card cannot stall the event loop.
* Dashboard reads come from an in-memory cache and never touch SQLite. They
  are plain synchronous methods, which on a single-threaded event loop makes
  them atomic with respect to the dispatcher and removes the need for a lock.

SQLite is the durable record for history that outlives the cache.
"""
from __future__ import annotations

import json
import sqlite3
from collections import deque
from typing import Any, Iterable

import aiosqlite

from app.core.config import db_path, get_config
from app.core.mesh.base import (
    MessageRecord,
    NodeRecord,
    TelemetryRecord,
    clean_name,
    clean_text,
    coerce_float,
    coerce_int,
    utcnow,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    network     TEXT NOT NULL,
    id          TEXT NOT NULL,
    link        TEXT,
    name        TEXT,
    short_name  TEXT,
    first_seen  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    lat         REAL,
    lon         REAL,
    altitude    REAL,
    snr         REAL,
    rssi        REAL,
    hops        INTEGER,
    battery     REAL,
    voltage     REAL,
    role        TEXT,
    hw_model    TEXT,
    is_self     INTEGER NOT NULL DEFAULT 0,
    raw_json    TEXT,
    PRIMARY KEY (network, id)
);

CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen DESC);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    network     TEXT NOT NULL,
    link        TEXT,
    ts          REAL NOT NULL,
    from_id     TEXT,
    from_name   TEXT,
    to_id       TEXT,
    channel     TEXT,
    direction   TEXT NOT NULL,
    text        TEXT NOT NULL,
    snr         REAL,
    rssi        REAL,
    hops        INTEGER,
    message_id  TEXT,
    raw_json    TEXT
);

CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(ts DESC);
CREATE INDEX IF NOT EXISTS idx_messages_network_ts ON messages(network, ts DESC);

-- A flooded mesh delivers the same packet by several paths, so an explicit
-- packet id is treated as unique. Partial, because not every payload carries
-- one and NULLs must stay insertable.
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_dedupe
    ON messages(network, message_id) WHERE message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS telemetry (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    network  TEXT NOT NULL,
    link     TEXT,
    node_id  TEXT NOT NULL,
    ts       REAL NOT NULL,
    metric   TEXT NOT NULL,
    value    REAL NOT NULL,
    unit     TEXT
);

CREATE INDEX IF NOT EXISTS idx_telemetry_lookup
    ON telemetry(network, node_id, metric, ts DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry(ts DESC);
"""

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    # NORMAL rather than FULL: losing the last few packets to a power cut is
    # an acceptable trade for far fewer SD-card writes.
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
)

# Points kept per metric per node for the sparklines.
SPARK_POINTS = 120


def init_db(path=None) -> None:
    """Create the schema. Synchronous, and only called before serving."""
    target = path or db_path()
    conn = sqlite3.connect(target, timeout=30)
    try:
        for pragma in _PRAGMAS:
            conn.execute(pragma)
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def _json(value: Any) -> str | None:
    if not value:
        return None
    try:
        return json.dumps(value, default=str)
    except (TypeError, ValueError):
        return None


def _row_to_node(row: sqlite3.Row | aiosqlite.Row) -> NodeRecord:
    return NodeRecord(
        network=row["network"],
        id=row["id"],
        link=row["link"],
        name=row["name"],
        short_name=row["short_name"],
        last_seen=row["last_seen"],
        lat=row["lat"],
        lon=row["lon"],
        altitude=row["altitude"],
        snr=row["snr"],
        rssi=row["rssi"],
        hops=row["hops"],
        battery=row["battery"],
        voltage=row["voltage"],
        role=row["role"],
        hw_model=row["hw_model"],
        is_self=bool(row["is_self"]),
        raw=json.loads(row["raw_json"]) if row["raw_json"] else {},
    )


def _row_to_message(row: sqlite3.Row | aiosqlite.Row) -> MessageRecord:
    return MessageRecord(
        network=row["network"],
        link=row["link"],
        ts=row["ts"],
        from_id=row["from_id"],
        from_name=row["from_name"],
        to_id=row["to_id"],
        channel=row["channel"],
        direction=row["direction"],
        text=row["text"],
        snr=row["snr"],
        rssi=row["rssi"],
        hops=row["hops"],
        message_id=row["message_id"],
    )


class MeshStore:
    def __init__(
        self,
        path=None,
        *,
        max_messages: int | None = None,
        retention_days: float | None = None,
        spark_points: int = SPARK_POINTS,
    ) -> None:
        cfg = get_config().mesh
        self._path = path or db_path()
        self.max_messages = max_messages if max_messages is not None else cfg.max_messages
        self.retention_days = (
            retention_days if retention_days is not None else cfg.retention_days
        )
        self.spark_points = spark_points

        self._db: aiosqlite.Connection | None = None
        # Cache. Series are keyed by a tuple rather than a joined string so a
        # node id containing a separator cannot be mis-parsed on the way out.
        self._nodes: dict[str, NodeRecord] = {}
        self._messages: deque[MessageRecord] = deque(maxlen=500)
        self._telemetry: dict[tuple[str, str, str], deque[tuple[float, float]]] = {}
        self._units: dict[tuple[str, str, str], str | None] = {}

    # ---- lifecycle ----

    async def open(self) -> None:
        init_db(self._path)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        for pragma in _PRAGMAS:
            await self._db.execute(pragma)
        await self._db.commit()
        await self._warm_cache()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _warm_cache(self) -> None:
        """Repopulate from disk so a restart does not show an empty console."""
        assert self._db is not None
        async with self._db.execute(
            "SELECT * FROM nodes ORDER BY last_seen DESC LIMIT 2000"
        ) as cur:
            for row in await cur.fetchall():
                node = _row_to_node(row)
                self._nodes[node.key] = node

        async with self._db.execute(
            "SELECT * FROM messages ORDER BY ts DESC LIMIT ?", (self._messages.maxlen,)
        ) as cur:
            rows = await cur.fetchall()
        for row in reversed(rows):
            self._messages.append(_row_to_message(row))

        async with self._db.execute(
            """
            SELECT network, node_id, metric, unit, ts, value FROM telemetry
            WHERE ts > ?
            ORDER BY ts ASC
            """,
            (utcnow() - 24 * 3600,),
        ) as cur:
            for row in await cur.fetchall():
                key = (row["network"], row["node_id"], row["metric"])
                series = self._telemetry.setdefault(
                    key, deque(maxlen=self.spark_points)
                )
                series.append((row["ts"], row["value"]))
                self._units[key] = row["unit"]

    # ---- writes ----

    async def record_node(self, node: NodeRecord) -> NodeRecord:
        """Upsert on identity, advancing last_seen.

        Returns the merged view, which is what the UI should see: a position
        packet must not blank out a battery reading from a minute ago.
        """
        node.name = clean_name(node.name)
        node.short_name = clean_name(node.short_name)
        node.lat = coerce_float(node.lat)
        node.lon = coerce_float(node.lon)
        node.hops = coerce_int(node.hops)

        existing = self._nodes.get(node.key)
        if existing is not None:
            merged = existing.merge(node)
        else:
            merged = node
            self._nodes[node.key] = merged

        if self._db is not None:
            await self._db.execute(
                """
                INSERT INTO nodes(
                    network, id, link, name, short_name, first_seen, last_seen,
                    lat, lon, altitude, snr, rssi, hops, battery, voltage,
                    role, hw_model, is_self, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(network, id) DO UPDATE SET
                    link       = COALESCE(excluded.link, nodes.link),
                    name       = COALESCE(excluded.name, nodes.name),
                    short_name = COALESCE(excluded.short_name, nodes.short_name),
                    last_seen  = MAX(excluded.last_seen, nodes.last_seen),
                    lat        = COALESCE(excluded.lat, nodes.lat),
                    lon        = COALESCE(excluded.lon, nodes.lon),
                    altitude   = COALESCE(excluded.altitude, nodes.altitude),
                    snr        = COALESCE(excluded.snr, nodes.snr),
                    rssi       = COALESCE(excluded.rssi, nodes.rssi),
                    hops       = COALESCE(excluded.hops, nodes.hops),
                    battery    = COALESCE(excluded.battery, nodes.battery),
                    voltage    = COALESCE(excluded.voltage, nodes.voltage),
                    role       = COALESCE(excluded.role, nodes.role),
                    hw_model   = COALESCE(excluded.hw_model, nodes.hw_model),
                    is_self    = MAX(excluded.is_self, nodes.is_self),
                    raw_json   = COALESCE(excluded.raw_json, nodes.raw_json)
                """,
                (
                    merged.network, merged.id, merged.link, merged.name,
                    merged.short_name, merged.last_seen, merged.last_seen,
                    merged.lat, merged.lon, merged.altitude, merged.snr,
                    merged.rssi, merged.hops, merged.battery, merged.voltage,
                    merged.role, merged.hw_model, int(merged.is_self),
                    _json(merged.raw),
                ),
            )
            await self._db.commit()
        return merged

    async def record_message(self, message: MessageRecord) -> MessageRecord | None:
        """Insert a message, or return None if it is a duplicate delivery."""
        message.text = clean_text(message.text) or ""
        message.from_name = clean_name(message.from_name)
        if not message.text:
            return None

        if self._db is not None:
            cur = await self._db.execute(
                """
                INSERT OR IGNORE INTO messages(
                    network, link, ts, from_id, from_name, to_id, channel,
                    direction, text, snr, rssi, hops, message_id, raw_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    message.network, message.link, message.ts, message.from_id,
                    message.from_name, message.to_id, message.channel,
                    message.direction, message.text, message.snr, message.rssi,
                    message.hops, message.message_id, _json(message.raw),
                ),
            )
            await self._db.commit()
            if cur.rowcount == 0:
                # Lost the race with the unique index: same packet, another hop.
                return None
        elif message.message_id and any(
            m.message_id == message.message_id and m.network == message.network
            for m in self._messages
        ):
            return None

        self._messages.append(message)
        return message

    async def record_telemetry(self, sample: TelemetryRecord) -> TelemetryRecord | None:
        value = coerce_float(sample.value)
        if value is None:
            return None
        sample.value = value

        key = (sample.network, sample.node_id, sample.metric)
        series = self._telemetry.setdefault(key, deque(maxlen=self.spark_points))
        series.append((sample.ts, value))
        self._units[key] = sample.unit

        if self._db is not None:
            await self._db.execute(
                """
                INSERT INTO telemetry(network, link, node_id, ts, metric, value, unit)
                VALUES(?,?,?,?,?,?,?)
                """,
                (
                    sample.network, sample.link, sample.node_id, sample.ts,
                    sample.metric, value, sample.unit,
                ),
            )
            await self._db.commit()
        return sample

    async def trim(self) -> dict[str, int]:
        """Drop history past the retention window and the message cap."""
        if self._db is None:
            return {"messages": 0, "telemetry": 0}
        cutoff = utcnow() - self.retention_days * 86400

        cur = await self._db.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
        dropped_messages = cur.rowcount or 0
        # Hard cap as well, because a busy mesh can blow past the size budget
        # long before anything ages out.
        cur = await self._db.execute(
            """
            DELETE FROM messages WHERE id NOT IN (
                SELECT id FROM messages ORDER BY ts DESC LIMIT ?
            )
            """,
            (self.max_messages,),
        )
        dropped_messages += cur.rowcount or 0

        cur = await self._db.execute("DELETE FROM telemetry WHERE ts < ?", (cutoff,))
        dropped_telemetry = cur.rowcount or 0
        await self._db.commit()
        return {"messages": dropped_messages, "telemetry": dropped_telemetry}

    # ---- cache reads (synchronous by design) ----

    def nodes(
        self,
        *,
        network: str | None = None,
        limit: int = 500,
        stale_after: float | None = None,
    ) -> list[dict[str, Any]]:
        items = [n for n in self._nodes.values() if network is None or n.network == network]
        items.sort(key=lambda n: n.last_seen, reverse=True)
        now = utcnow()
        out = []
        for node in items[:limit]:
            data = node.to_dict()
            data["age"] = max(0.0, now - node.last_seen)
            if stale_after:
                data["stale"] = data["age"] > stale_after
            out.append(data)
        return out

    def node(self, network: str, node_id: str) -> dict[str, Any] | None:
        found = self._nodes.get(f"{network}:{node_id}")
        return found.to_dict(include_raw=True) if found else None

    def messages(
        self, *, network: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        items = [
            m for m in self._messages if network is None or m.network == network
        ]
        # Newest first, which is how the panel renders.
        return [m.to_dict() for m in reversed(items[-limit:])]

    def telemetry_series(
        self, *, network: str, node_id: str, metric: str
    ) -> dict[str, Any]:
        key = (network, node_id, metric)
        series = self._telemetry.get(key, ())
        return {
            "network": network,
            "node_id": node_id,
            "metric": metric,
            "unit": self._units.get(key),
            "points": [{"ts": ts, "value": value} for ts, value in series],
        }

    def telemetry_summary(
        self, *, network: str | None = None, limit: int = 60
    ) -> list[dict[str, Any]]:
        """Latest value plus the sparkline points, per node and metric."""
        out: list[dict[str, Any]] = []
        for key, series in self._telemetry.items():
            if not series:
                continue
            net, node_id, metric = key
            if network is not None and net != network:
                continue
            node = self._nodes.get(f"{net}:{node_id}")
            last_ts, last_value = series[-1]
            values = [v for _, v in series]
            out.append(
                {
                    "network": net,
                    "node_id": node_id,
                    "node_label": node.label() if node else node_id,
                    "metric": metric,
                    "unit": self._units.get(key),
                    "value": last_value,
                    "ts": last_ts,
                    "min": min(values),
                    "max": max(values),
                    "points": values,
                }
            )
        out.sort(key=lambda r: (r["network"], r["node_label"], r["metric"]))
        return out[:limit]

    def counts(self) -> dict[str, int]:
        by_network: dict[str, int] = {}
        for node in self._nodes.values():
            by_network[node.network] = by_network.get(node.network, 0) + 1
        return {
            "nodes": len(self._nodes),
            "messages": len(self._messages),
            "series": len(self._telemetry),
            **{f"nodes_{k}": v for k, v in by_network.items()},
        }

    # ---- history straight from SQLite ----

    async def message_history(
        self,
        *,
        network: str | None = None,
        limit: int = 200,
        before: float | None = None,
    ) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        clauses: list[str] = []
        params: list[Any] = []
        if network:
            clauses.append("network = ?")
            params.append(network)
        if before:
            clauses.append("ts < ?")
            params.append(before)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        async with self._db.execute(
            f"SELECT * FROM messages {where} ORDER BY ts DESC LIMIT ?", params
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_message(r).to_dict() for r in rows]

    async def telemetry_history(
        self,
        *,
        network: str,
        node_id: str,
        metric: str,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        if self._db is None:
            return []
        async with self._db.execute(
            """
            SELECT ts, value FROM telemetry
            WHERE network = ? AND node_id = ? AND metric = ?
            ORDER BY ts DESC LIMIT ?
            """,
            (network, node_id, metric, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"ts": r["ts"], "value": r["value"]} for r in reversed(rows)]

    def reset_cache(self, nodes: Iterable[NodeRecord] = ()) -> None:
        """Used by the simulated network and by tests."""
        self._nodes = {n.key: n for n in nodes}
        self._messages.clear()
        self._telemetry.clear()
        self._units.clear()
