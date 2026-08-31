"""Input validation, rate limits, and HTTP hardening helpers."""
from __future__ import annotations

import re
import time
from collections import defaultdict
from threading import Lock

# A Meshtastic text payload tops out around 200 bytes once the header is
# accounted for, and MeshCore is in the same range. Reject early rather than
# letting the radio silently truncate.
MESSAGE_MAX_CHARS = 200
# Meshtastic supports 8 channel slots; MeshCore channel indexes are also small.
CHANNEL_INDEX_MAX = 7

_NETWORKS = ("meshtastic", "meshcore")
# Meshtastic node ids look like "!433a1b2c"; MeshCore keys are hex public keys.
# Names are user-set, so allow a readable set and nothing that could escape a
# shell, a path, or an SQL identifier.
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9!._:-]{1,80}$")
_RADIO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")


def validate_network(value: str | None) -> str | None:
    """None means 'both networks'."""
    if value is None or value == "":
        return None
    network = value.strip().lower()
    if network not in _NETWORKS:
        raise ValueError(f"network must be one of {', '.join(_NETWORKS)}")
    return network


def validate_node_id(value: str) -> str:
    node_id = (value or "").strip()
    if not _NODE_ID_RE.match(node_id):
        raise ValueError("invalid node id")
    return node_id


def validate_radio_name(value: str) -> str:
    name = (value or "").strip()
    if not _RADIO_NAME_RE.match(name):
        raise ValueError("invalid radio name")
    return name


def validate_message(text: str) -> str:
    msg = (text or "").strip()
    if not msg:
        raise ValueError("message is empty")
    if len(msg) > MESSAGE_MAX_CHARS:
        raise ValueError(f"message must be {MESSAGE_MAX_CHARS} characters or fewer")
    # A NUL would terminate the string inside the C serial layers.
    if "\x00" in msg:
        raise ValueError("message contains a null byte")
    return msg


def validate_channel(value: int | None) -> int | None:
    if value is None:
        return None
    channel = int(value)
    if channel < 0 or channel > CHANNEL_INDEX_MAX:
        raise ValueError(f"channel must be between 0 and {CHANNEL_INDEX_MAX}")
    return channel


def clamp_limit(value: int | None, default: int = 100, maximum: int = 1000) -> int:
    if value is None:
        return default
    return max(1, min(maximum, int(value)))


class LoginRateLimiter:
    """Simple in-memory login attempt limiter (per process)."""

    def __init__(self, max_attempts: int = 8, window_s: float = 300.0) -> None:
        self.max_attempts = max_attempts
        self.window_s = window_s
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self._lock:
            bucket = [t for t in self._hits[key] if now - t < self.window_s]
            if len(bucket) >= self.max_attempts:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            return True


login_limiter = LoginRateLimiter()


class SendRateLimiter:
    """Caps outbound messages.

    Transmitting is the one thing here that touches the spectrum and burns
    airtime for everyone on the mesh, so the limit is deliberately low.
    """

    def __init__(self, max_sends: int = 10, window_s: float = 60.0) -> None:
        self.max_sends = max_sends
        self.window_s = window_s
        self._hits: list[float] = []
        self._lock = Lock()

    def allow(self) -> bool:
        now = time.time()
        with self._lock:
            self._hits = [t for t in self._hits if now - t < self.window_s]
            if len(self._hits) >= self.max_sends:
                return False
            self._hits.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


send_limiter = SendRateLimiter()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}
