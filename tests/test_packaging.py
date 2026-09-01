"""Guards on the things that only break after deployment.

This project is partly written on a Windows host that defaults to UTF-16 and
CRLF, and a shell script with CRLF line endings fails on Linux with a message
that names the wrong thing entirely ("bad interpreter"). The installer also
patches the service units with sed, so if an anchor line is reworded the
install silently produces a unit pointing at /home/pi.

Mesh-Spy installs on Windows, Raspberry Pi OS, any mainstream Linux and macOS,
and CI can only really exercise two of those. Most of what follows is therefore
static analysis standing in for the platforms no runner covers: that the
Windows scripts exist and agree with the Unix ones, that bootstrap.py stays
importable without any dependency installed, and that the docs still describe
what the scripts actually do.
"""
from __future__ import annotations

import ast
import re
import sys

import pytest
import yaml

from app.core.config import ROOT, AppConfig, _deep_merge

TEXT_SUFFIXES = {
    ".py", ".sh", ".yaml", ".yml", ".md", ".html", ".css", ".js",
    ".ini", ".txt", ".rules", ".service", ".openrc", ".cfg", ".toml",
    ".ps1",
}
# cmd.exe is the one interpreter here that can misparse an LF script, so .bat
# is deliberately absent above and checked separately below.
BATCH_SUFFIXES = {".bat", ".cmd"}
TEXT_NAMES = {"LICENSE", ".gitignore", ".gitattributes"}
SKIP_DIRS = {".git", ".venv", "venv", "data", "__pycache__", ".pytest_cache"}

SHELL_SCRIPTS = ("install.sh", "run.sh")
BATCH_SCRIPTS = ("install.bat", "run.bat")
POWERSHELL_SCRIPTS = ("install.ps1",)
SERVICE_UNIT = "scripts/mesh-spy.service"
OPENRC_UNIT = "scripts/mesh-spy.openrc"
UDEV_RULES = "scripts/60-mesh-spy-serial.rules"

ENTRYPOINT = "bootstrap.py"
INSTALL_GUIDE = "docs/INSTALL.md"

INSTALL_SH = (ROOT / "install.sh").read_text(encoding="utf-8")
RUN_SH = (ROOT / "run.sh").read_text(encoding="utf-8")
INSTALL_PS1 = (ROOT / "install.ps1").read_text(encoding="utf-8")
BOOTSTRAP = (ROOT / ENTRYPOINT).read_text(encoding="utf-8")
README = (ROOT / "README.md").read_text(encoding="utf-8")
GUIDE = (ROOT / INSTALL_GUIDE).read_text(encoding="utf-8")

# The sed expressions live inside double-quoted shell strings, so a quote in a
# pattern is escaped there but not in the file being patched.
INSTALL_SH_UNESCAPED = INSTALL_SH.replace('\\"', '"')


def _walk(suffixes: set, names: set = frozenset()) -> list:
    found = []
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.relative_to(ROOT).parts):
            continue
        if path.suffix in suffixes or path.name in names:
            found.append(path)
    return sorted(found)


def text_files() -> list:
    return _walk(TEXT_SUFFIXES, TEXT_NAMES)


def batch_files() -> list:
    return _walk(BATCH_SUFFIXES)


def rel(path) -> str:
    return path.relative_to(ROOT).as_posix()


# ---- encoding and line endings ----

def test_the_repository_has_text_files_to_check():
    """Guards the guard: a bad glob would make everything below vacuous."""
    names = {rel(p) for p in text_files()}
    assert "install.sh" in names
    assert "install.ps1" in names
    assert "app/main.py" in names
    assert len(names) > 20


def test_the_repository_has_batch_files_to_check():
    names = {rel(p) for p in batch_files()}
    assert names == set(BATCH_SCRIPTS), names


def test_every_text_file_is_utf8_without_a_byte_order_mark():
    offenders = []
    for path in text_files() + batch_files():
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


def test_batch_files_use_windows_line_endings():
    """The mirror image: cmd.exe can misparse an LF .bat, so those want CRLF."""
    offenders = [rel(p) for p in batch_files() if b"\r\n" not in p.read_bytes()]
    assert offenders == []


def test_git_is_told_to_keep_line_endings_that_way():
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "text=auto eol=lf" in attributes
    assert "*.bat text eol=crlf" in attributes


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
    for name in (SERVICE_UNIT, OPENRC_UNIT, UDEV_RULES, "run.sh", ENTRYPOINT):
        assert (ROOT / name).exists(), name
        assert name in INSTALL_SH, name


def test_the_files_bootstrap_needs_are_in_the_repository():
    """These moved out of install.sh when the Python setup was centralised."""
    for name in ("requirements.txt", "requirements-dev.txt",
                 "config/config.example.yaml"):
        assert (ROOT / name).exists(), name
        assert name.rsplit("/", 1)[-1] in BOOTSTRAP, name


def test_the_units_start_the_app_directly():
    """A service supervises the interpreter, not a wrapper that re-execs."""
    entry = "-m app.main"
    for name in (SERVICE_UNIT, OPENRC_UNIT):
        assert entry in (ROOT / name).read_text(encoding="utf-8"), name


@pytest.mark.parametrize(
    "script, body",
    [("install.sh", INSTALL_SH), ("run.sh", RUN_SH), ("install.ps1", INSTALL_PS1)],
)
def test_every_platform_script_delegates_to_the_one_entrypoint(script, body):
    """One definition of a working install, not one per platform.

    The moment a platform script grows its own `python -m venv` and `pip
    install` the three drift, and the one that drifts is always the one nobody
    is developing on.
    """
    assert ENTRYPOINT in body, script
    assert "-m venv" not in body, script
    assert "pip install" not in body, script


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


# ---- the cross-platform entrypoint ----

BOOTSTRAP_TREE = ast.parse(BOOTSTRAP, filename=ENTRYPOINT)

# Everything bootstrap.py may import. It runs before a single dependency
# exists, so anything outside this set is a chicken-and-egg failure on the
# fresh clone it is supposed to be setting up.
STDLIB_ONLY = {
    "__future__", "argparse", "hashlib", "os", "shutil", "subprocess", "sys",
    "pathlib", "typing",
}


def test_the_entrypoint_imports_nothing_it_is_meant_to_install():
    imported = set()
    for node in ast.walk(BOOTSTRAP_TREE):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= STDLIB_ONLY, imported - STDLIB_ONLY


def test_the_entrypoint_parses_on_every_python_the_project_claims():
    """It has to reach its own version check to be able to print it.

    A match statement or a 3.10-only annotation here turns "your Python is too
    old" into a SyntaxError, which tells the user nothing about the cause.
    """
    compile(BOOTSTRAP, ENTRYPOINT, "exec")
    assert "from __future__ import annotations" in BOOTSTRAP
    for too_new in ("match ", ":=", "ExceptionGroup"):
        assert too_new not in BOOTSTRAP, too_new


def test_the_entrypoint_knows_where_windows_puts_the_interpreter():
    """Scripts vs bin is the single difference that makes installers Unix-only."""
    assert "Scripts" in BOOTSTRAP
    assert "python.exe" in BOOTSTRAP
    assert 'os.name == "nt"' in BOOTSTRAP


def test_the_entrypoint_offers_the_flags_the_docs_promise():
    for flag in ("--setup-only", "--skip-pip", "--dev", "--recreate", "--list-ports"):
        assert flag in BOOTSTRAP, flag
        assert flag in GUIDE, flag


def test_the_version_floor_matches_what_meshtastic_supports():
    for name, expected in (("MIN_PYTHON", (3, 9)), ("MAX_PYTHON", (3, 15))):
        match = re.search(
            r"^%s = \((\d+), (\d+)\)$" % name, BOOTSTRAP, re.MULTILINE
        )
        assert match, name
        assert (int(match.group(1)), int(match.group(2))) == expected, name


# ---- the Windows scripts ----

@pytest.mark.parametrize("name", BATCH_SCRIPTS + POWERSHELL_SCRIPTS)
def test_the_windows_scripts_exist(name):
    assert (ROOT / name).exists(), name


@pytest.mark.parametrize("name", BATCH_SCRIPTS)
def test_batch_scripts_run_from_their_own_directory(name):
    """Double-clicking one starts it in C:\\Windows\\System32 often enough."""
    body = (ROOT / name).read_text(encoding="utf-8")
    assert 'cd /d "%~dp0"' in body, name
    assert "@echo off" in body, name


def test_the_batch_installer_only_exists_to_bypass_the_execution_policy():
    body = (ROOT / "install.bat").read_text(encoding="utf-8")
    assert "-ExecutionPolicy Bypass" in body
    assert "install.ps1" in body


def test_the_windows_installer_refuses_the_microsoft_store_python():
    """Its venvs appear to build and then fail to import, much later."""
    assert "WindowsApps" in INSTALL_PS1
    assert "windowsapps" in BOOTSTRAP.lower()


def test_the_windows_installer_never_puts_a_quote_inside_a_native_argument():
    """PowerShell 5.1 strips double quotes when building a native command line.

    This is not theoretical and it is silent. A probe written the obvious way,

        -c 'import sys; print("%d.%d" % sys.version_info[:2])'

    reaches python as `print(%d.%d % sys.version_info[:2])`, which is a
    SyntaxError. The installer then reports that no Python is installed, on a
    machine where Python is installed and on PATH, and nothing in the message
    points at quoting. Every snippet has to stay quote-free.
    """
    snippets = re.findall(r"^\$\w*Probe = '([^']*)'$", INSTALL_PS1, re.MULTILINE)
    assert snippets, "no probe snippets found; has the naming changed?"
    for snippet in snippets:
        assert '"' not in snippet, snippet
        assert "'" not in snippet, snippet


def test_the_windows_installer_survives_a_python_that_writes_to_stderr():
    """With ErrorActionPreference Stop, native stderr is a terminating error.

    An interpreter printing a deprecation warning would otherwise be judged
    unusable, so the capture helper has to lower the preference around the
    call and every probe has to go through it.
    """
    assert "function Invoke-Capture" in INSTALL_PS1
    assert "$ErrorActionPreference = 'Continue'" in INSTALL_PS1
    # No probe may call the interpreter directly and bypass the helper.
    direct = re.findall(r"& \$(?:Path|launcher\.Source)\b", INSTALL_PS1)
    assert direct == [], direct


def test_the_windows_installer_needs_no_administrator_by_default():
    """A per-user logon task is the whole reason this can skip the UAC prompt.

    Elevation is only for the firewall rule, which is opt-in.
    """
    assert "RunLevel Limited" in INSTALL_PS1
    assert "-AtLogOn" in INSTALL_PS1
    assert "Test-Administrator" in INSTALL_PS1
    elevation_required = INSTALL_PS1.count("Test-Administrator")
    # Defined once, called once, from the firewall path only.
    assert elevation_required == 2, elevation_required


def test_the_windows_autostart_task_logs_somewhere_readable():
    """pythonw has no stderr, so without a log file a failure is invisible."""
    assert "pythonw.exe" in INSTALL_PS1
    main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "sys.stderr is None" in main
    assert "mesh-spy.log" in main
    assert "MESH_SPY_LOG_FILE" in main


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
    for topic in (
        "install.sh",
        "mesh.radios",
        "read_only",
        "MESH_SPY_PASSWORD",
        "MESH_SPY_NO_DEMO",
        "/dev/serial/by-id",
        "journalctl -u mesh-spy",
    ):
        assert topic in README, topic


def test_the_readme_names_every_platform_and_its_install():
    for token in ("Windows", "Raspberry Pi", "macOS", "install.bat",
                  "install.sh", ENTRYPOINT, INSTALL_GUIDE):
        assert token in README, token


def test_the_install_guide_has_a_section_for_every_platform():
    for heading in ("## Windows", "## Raspberry Pi", "## Linux", "## macOS",
                    "## Troubleshooting"):
        assert heading in GUIDE, heading


def test_the_install_guide_covers_every_package_manager_family():
    for family in ("apt", "dnf", "pacman", "zypper", "apk", "xbps", "emerge"):
        assert family in GUIDE, family


def test_the_install_guide_shows_a_port_for_every_platform():
    """Finding the port is the step that actually stalls a first install."""
    for example in ("COM7", "/dev/ttyUSB0", "/dev/ttyACM0", "/dev/cu."):
        assert example in GUIDE, example
    assert "--list-ports" in GUIDE


def test_the_docs_do_not_still_tell_windows_users_to_list_a_unix_directory():
    """`ls -l /dev/serial/by-id/` was the old advice and is Linux-only.

    The by-id path is still worth recommending on Linux, so this pins the
    command rather than the path.
    """
    assert "ls -l /dev/serial/by-id" not in README
    assert "ls -l /dev/serial/by-id" not in GUIDE
    assert "ls -l /dev/serial/by-id" not in INSTALL_SH


# ---- dependencies and CI ----

def test_both_mesh_libraries_are_pinned_to_a_floor():
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert re.search(r"^meshtastic>=2\.", requirements, re.MULTILINE)
    assert re.search(r"^meshcore>=2\.", requirements, re.MULTILINE)


def test_the_test_dependencies_include_what_the_suite_imports():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for package in ("pytest", "pytest-asyncio", "pyflakes", "httpx"):
        assert package in dev, package


WORKFLOW = yaml.safe_load(
    (ROOT / ".github" / "workflows" / "tests.yml").read_text("utf-8")
)


def _job_commands(job: str) -> str:
    return " ".join(step.get("run", "") for step in WORKFLOW["jobs"][job]["steps"])


def test_ci_lints_every_shell_script_in_the_repository():
    commands = _job_commands("shellcheck")
    for name in SHELL_SCRIPTS + (OPENRC_UNIT,):
        assert name in commands, name
    assert "shellcheck" in commands


def test_ci_lints_the_powershell_too():
    """Nothing else catches a typo in install.ps1 before a user runs it."""
    commands = _job_commands("powershell")
    for name in POWERSHELL_SCRIPTS:
        assert name in commands, name
    assert "PSScriptAnalyzer" in commands


def test_the_ci_python_matrix_stays_inside_what_meshtastic_supports():
    versions = WORKFLOW["jobs"]["pytest"]["strategy"]["matrix"]["python-version"]
    assert versions
    for version in versions:
        major, minor = (int(part) for part in str(version).split("."))
        # meshtastic requires >=3.9,<3.15.
        assert major == 3 and 9 <= minor < 15, version


def test_ci_runs_the_suite_on_windows_as_well_as_linux():
    """The Scripts/bin split means a Linux-only run proves half the install."""
    systems = WORKFLOW["jobs"]["pytest"]["strategy"]["matrix"]["os"]
    assert any(str(s).startswith("ubuntu") for s in systems), systems
    assert any(str(s).startswith("windows") for s in systems), systems


def test_ci_exercises_the_entrypoint_rather_than_only_importing_it():
    commands = _job_commands("pytest")
    assert ENTRYPOINT in commands


# ---- this platform ----

def test_the_venv_layout_this_platform_uses_is_the_one_bootstrap_expects():
    """Runs on whichever OS the suite is on, and disagrees on the other."""
    sys.path.insert(0, str(ROOT))
    try:
        import bootstrap
    finally:
        sys.path.pop(0)

    expected = "Scripts" if sys.platform == "win32" else "bin"
    assert bootstrap.venv_python().parent.name == expected
    assert bootstrap.venv_python().parent.parent == ROOT / ".venv"
