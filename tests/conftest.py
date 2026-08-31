"""Shared fixtures.

Two things every test needs isolating. The config is a process-wide singleton
read from `config/config.yaml`, so without an override the suite would pick up
whatever radios the developer running it happens to have configured. And the
store writes to a real SQLite file, so each test gets its own under tmp_path.
"""
from __future__ import annotations

from typing import Any, Callable

import pytest

import app.core.auth as auth_module
import app.core.config as config_module
from app.core.config import AppConfig, _deep_merge
from app.core.mesh.store import MeshStore
from app.core.security import login_limiter, send_limiter
from tests.fakes import Collector

ENV_VARS = (
    "MESH_SPY_PASSWORD",
    "MESH_SPY_NO_DEMO",
    "MESH_SPY_ALLOW_INSECURE_LAN",
    "MESH_SPY_SECURE_COOKIE",
)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The app reads these at call time, so a stray one would leak between tests."""
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def clean_globals() -> Any:
    """Reset the process-wide auth and rate-limit state."""
    yield
    auth_module._sessions.clear()
    send_limiter.reset()
    login_limiter._hits.clear()


@pytest.fixture
def config_factory(tmp_path) -> Callable[..., AppConfig]:
    """Install a test config, overriding the defaults with a nested dict."""
    def build(**overrides: Any) -> AppConfig:
        base: dict[str, Any] = {
            "server": {"host": "127.0.0.1", "port": 8090},
            "auth": {"enabled": False, "username": "ops", "password": ""},
            "database": {"path": str(tmp_path / "mesh_spy.db")},
            "mesh": {
                "read_only": True,
                "retention_days": 14.0,
                "max_messages": 5000,
                # Short enough that a backoff test does not sleep for seconds.
                "reconnect_min_seconds": 0.01,
                "reconnect_max_seconds": 0.08,
                "stale_after_seconds": 300.0,
                "radios": [],
            },
        }
        cfg = AppConfig.model_validate(_deep_merge(base, overrides))
        config_module._config = cfg
        return cfg

    return build


@pytest.fixture(autouse=True)
def baseline_config(config_factory) -> Any:
    config_factory()
    yield
    config_module._config = None


@pytest.fixture
async def store(tmp_path) -> Any:
    """An open store on its own database file."""
    db = MeshStore(tmp_path / "store.db")
    await db.open()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture
def collector() -> Collector:
    return Collector()


@pytest.fixture
def collector_factory() -> Callable[[], Collector]:
    """For tests that need to prove two adapters stay independent."""
    return Collector
