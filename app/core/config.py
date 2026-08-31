from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


ROOT = Path(__file__).resolve().parents[2]

NETWORKS = ("meshtastic", "meshcore")
TRANSPORTS = ("serial", "tcp", "ble")


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    # Not 8080: Pi-Spy-RF uses that, and the two are likely to share a Pi.
    port: int = 8090


class AuthConfig(BaseModel):
    enabled: bool = False
    username: str = "ops"
    password: str = ""


class DatabaseConfig(BaseModel):
    path: str = "data/mesh_spy.db"


class RadioConfig(BaseModel):
    """One configured radio. Both firmwares reach us over serial, TCP or BLE."""

    model_config = ConfigDict(extra="ignore")

    name: str
    network: Literal["meshtastic", "meshcore"]
    transport: Literal["serial", "tcp", "ble"] = "serial"
    enabled: bool = True

    port: str | None = None
    baud: int = 115200

    host: str | None = None
    tcp_port: int | None = None

    address: str | None = None
    # MeshCore supports BLE PIN pairing; Meshtastic does not use this.
    pin: str | None = None

    @model_validator(mode="after")
    def _require_transport_fields(self) -> RadioConfig:
        if self.transport == "serial" and not self.port:
            raise ValueError(f"radio {self.name!r}: serial transport needs 'port'")
        if self.transport == "tcp" and not self.host:
            raise ValueError(f"radio {self.name!r}: tcp transport needs 'host'")
        # BLE address may be omitted: both libraries can scan for a device.
        return self

    @property
    def key(self) -> str:
        return f"{self.network}:{self.name}"

    def describe_target(self) -> str:
        if self.transport == "serial":
            return f"{self.port}@{self.baud}"
        if self.transport == "tcp":
            return f"{self.host}:{self.tcp_port}" if self.tcp_port else str(self.host)
        return self.address or "scan"


class MeshConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # Transmitting is opt-in: a fresh install must not be able to key up a radio.
    read_only: bool = True
    # History is bounded because this normally runs off an SD card.
    retention_days: float = 14.0
    max_messages: int = 5000
    reconnect_min_seconds: float = 2.0
    reconnect_max_seconds: float = 60.0
    # Seconds a link may be silent before it is reported stale.
    stale_after_seconds: float = 300.0
    radios: list[RadioConfig] = Field(default_factory=list)

    def enabled_radios(self) -> list[RadioConfig]:
        return [r for r in self.radios if r.enabled]


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")
    server: ServerConfig = Field(default_factory=ServerConfig)
    auth: AuthConfig = Field(default_factory=AuthConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    mesh: MeshConfig = Field(default_factory=MeshConfig)


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config must be a mapping: {path}")
    return data


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


_config: AppConfig | None = None


def get_config(*, refresh: bool = False) -> AppConfig:
    global _config
    if _config is not None and not refresh:
        return _config
    example = ROOT / "config" / "config.example.yaml"
    local = ROOT / "config" / "config.yaml"
    merged = _deep_merge(_load_yaml(example), _load_yaml(local))
    _config = AppConfig.model_validate(merged)
    return _config


def db_path() -> Path:
    cfg = get_config()
    path = Path(cfg.database.path)
    if not path.is_absolute():
        path = ROOT / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path
