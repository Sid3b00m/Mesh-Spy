"""Guards on the things that only break after deployment.

This project is partly written on a Windows host that defaults to UTF-16 and
CRLF, and a shell script with CRLF line endings fails on Linux with a message
that names the wrong thing entirely ("bad interpreter"). The installer also
patches the service units with sed, so if an anchor line is reworded the
install silently produces a unit pointing at /home/pi.
"""
from __future__ import annotations

import re

import pytest
import yaml

from app.core.config import ROOT, AppConfig, _deep_merge

TEXT_SUFFIXES = {
    ".py", ".sh", ".yaml", ".yml", ".md", ".html", ".css", ".js",
    ".ini", ".txt", ".rules", ".service", ".openrc", ".cfg", ".toml",
}
TEXT_NAMES = {"LICENSE", ".gitignore", ".gitattributes"}
SKIP_DIRS = {".git", ".venv", "venv", "data", "__pycache__", ".pytest_cache"}

SHELL_SCRIPTS = ("install.sh", "run.sh")
SERVICE_UNIT = "scripts/mesh-spy.service"
OPENRC_UNIT = "scripts/mesh-spy.openrc"
UDEV_RULES = "scripts/60-mesh-spy-serial.rules"

INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
# The sed expressions live inside double-quoted shell strings, so a quote in a
# pattern is escaped there but not in the file being patched.
INSTALL_SH_UNESCAPED = INSTALL_SH.replace('\\"', '"')


def text_files() -> list:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix in TEXT_SUFFIXES or path.name in TEXT_NAMES:
            found.append(path)
    return sorted(found)


def rel(path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---- encoding and line endings ----

def test_the_repository_has_text_files_to_check():
    """Guards the guard: a bad glob would make everything below vacuous."""
    names = {rel(p) for p in text_files()}
    assert "install.sh" in names
    assert "app/main.py" in names
    assert len(names) > 20


def test_every_text_file_is_utf8_without_a_byte_order_mark():
    offenders = []
    for path in text_files():
        raw = path.read_bytes()
        if raw.startswith(b"\xef\xbb\xbf") or raw.startswith(b"\xff\xfe"):
            offenders.append(rel(path))
            continue
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError:
            offenders.append(rel(path))
    assert offenders == []


def test_no_text_file_uses_windows_line_endings():
    """A .sh with CRLF fails on Linux as a confusing "bad interpreter"."""
    offenders = [rel(p) for p in text_files() if b"\r\n" in p.read_bytes()]
    assert offenders == []


def test_git_is_told_to_keep_line_endings_that_way():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "text=auto eol=lf" in attributes


# ---- shebangs and executability ----

@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_declare_bash_by_env(name):
    """The scripts use [[ ]] and arrays, so /bin/sh would not do."""
    first = (ROOT / name).read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/usr/bin/env bash"


def test_the_openrc_unit_declares_the_openrc_interpreter():
    first = (ROOT / OPENRC_UNIT).read_text(encoding="utf-8").splitlines()[0]
    assert first == "#!/sbin/openrc-run"


@pytest.mark.parametrize("name", SHELL_SCRIPTS)
def test_shell_scripts_fail_fast(name):
    body = (ROOT / name).read_text(encoding="utf-8")
    assert "set -euo pipefail" in body


# ---- the installer's sed anchors ----

@pytest.mark.parametrize(
    "target, anchors",
    [
        (SERVICE_UNIT, ("User=pi", "SupplementaryGroups=dialout", "/home/pi/Mesh-Spy")),
        (OPENRC_UNIT, ('command_user="pi"', "/home/pi/Mesh-Spy")),
        (UDEV_RULES, ('GROUP="dialout"',)),
    ],
)
def test_the_installer_rewrites_lines_that_actually_exist(target, anchors):
    """Reword one of these and the install quietly points at /home/pi."""
    body = (ROOT / target).read_text(encoding="utf-8")
    for anchor in anchors:
        assert anchor in body, f"{anchor!r} missing from {target}"
        assert anchor in INSTALL_SH_UNESCAPED, (
            f"{anchor!r} no longer patched by install.sh"
        )


def test_every_file_the_installer_copies_is_in_the_repository():
    for name in (
        SERVICE_UNIT,
        OPENRC_UNIT,
        UDEV_RULES,
        "run.sh",
        "requirements.txt",
        "config/config.example.yaml",
    ):
        assert (ROOT / name).exists(), name
        assert name in INSTALL_SH, name


def test_the_units_start_the_app_the_way_run_sh_does():
    entry = "-m app.main"
    for name in (SERVICE_UNIT, OPENRC_UNIT, "run.sh"):
        assert entry in (ROOT / name).read_text(encoding="utf-8"), name


def test_the_service_can_still_reach_the_serial_port():
    """PrivateDevices would hide /dev/ttyUSB* from the unit entirely."""
    unit = (ROOT / SERVICE_UNIT).read_text(encoding="utf-8")
    assert "PrivateDevices" not in unit
    assert "SupplementaryGroups=" in unit


def test_the_service_may_write_only_where_it_needs_to():
    unit = (ROOT / SERVICE_UNIT).read_text(encoding="utf-8")
    assert "ProtectSystem=strict" in unit
    assert "NoNewPrivileges=yes" in unit
    writable = re.findall(r"^ReadWritePaths=(.+)$", unit, re.MULTILINE)
    assert any("/data" in line and "/config" in line for line in writable)


def test_the_installer_offers_both_init_systems():
    assert "systemctl enable mesh-spy.service" in INSTALL_SH
    assert "rc-update add mesh-spy default" in INSTALL_SH


def test_the_installer_covers_the_package_managers_the_readme_claims():
    for manager in ("apt-get", "dnf", "yum", "pacman", "zypper", "apk",
                    "xbps-install", "emerge"):
        assert manager in INSTALL_SH, manager


def test_serial_access_is_granted_by_group_not_by_running_as_root():
    for token in ("dialout", "uucp", "usermod -aG", "udev/rules.d"):
        assert token in INSTALL_SH, token


# ---- configuration ----

def test_the_example_config_is_valid_and_ships_read_only():
    """A fresh install must not be able to transmit."""
    raw = yaml.safe_load((ROOT / "config" / "config.example.yaml").read_text("utf-8"))
    cfg = AppConfig.model_validate(raw)

    assert cfg.mesh.read_only is True
    assert cfg.auth.enabled is False
    assert cfg.server.host == "127.0.0.1"
    # 8090 rather than 8080, so this can share a Pi with Pi-Spy-RF.
    assert cfg.server.port == 8090
    assert cfg.mesh.radios == []


def test_the_example_config_documents_every_transport():
    body = (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
    for transport in ("serial", "tcp", "ble"):
        assert f"transport: {transport}" in body
    for network in ("meshtastic", "meshcore"):
        assert f"network: {network}" in body


def test_a_partial_config_still_validates():
    """Anything left out of config.yaml falls back to the example file."""
    example = yaml.safe_load(
        (ROOT / "config" / "config.example.yaml").read_text("utf-8")
    )
    merged = _deep_merge(example, {"server": {"port": 9000}})
    cfg = AppConfig.model_validate(merged)
    assert cfg.server.port == 9000
    assert cfg.mesh.read_only is True


def test_local_config_and_the_database_are_not_committed():
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in ("config/config.yaml", "data/*.db", ".venv/"):
        assert pattern in ignored, pattern


# ---- licensing ----

def test_the_project_is_gpl3_because_meshtastic_is():
    licence = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in licence
    assert "Version 3" in licence

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "GPL-3.0" in readme
    # The obligation, not just the choice, is what the README has to state.
    assert "meshtastic" in readme.lower()


def test_the_readme_covers_what_an_operator_has_to_know():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for topic in (
        "install.sh",
        "mesh.radios",
        "read_only",
        "MESH_SPY_PASSWORD",
        "MESH_SPY_NO_DEMO",
        "/dev/serial/by-id",
        "journalctl -u mesh-spy",
    ):
        assert topic in readme, topic


# ---- dependencies and CI ----

def test_both_mesh_libraries_are_pinned_to_a_floor():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^meshtastic>=2\.", requirements, re.MULTILINE)
    assert re.search(r"^meshcore>=2\.", requirements, re.MULTILINE)


def test_the_test_dependencies_include_what_the_suite_imports():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for package in ("pytest", "pytest-asyncio", "pyflakes", "httpx"):
        assert package in dev, package


def test_ci_lints_every_shell_script_in_the_repository():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text("utf-8")
    )
    steps = workflow["jobs"]["shellcheck"]["steps"]
    commands = " ".join(step.get("run", "") for step in steps)

    for name in SHELL_SCRIPTS + (OPENRC_UNIT,):
        assert name in commands, name
    assert "shellcheck" in commands


def test_the_ci_python_matrix_stays_inside_what_meshtastic_supports():
    workflow = yaml.safe_load(
        (ROOT / ".github" / "workflows" / "tests.yml").read_text("utf-8")
    )
    versions = workflow["jobs"]["pytest"]["strategy"]["matrix"]["python-version"]
    assert versions
    for version in versions:
        major, minor = (int(part) for part in str(version).split("."))
        # meshtastic requires >=3.9,<3.15.
        assert major == 3 and 9 <= minor < 15, version
