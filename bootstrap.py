#!/usr/bin/env python3
"""One command that sets up and starts Mesh-Spy, identically on every platform.

    python bootstrap.py

Creates the virtualenv, installs dependencies, writes the first config, then
starts the console. Safe to run repeatedly: dependencies are only reinstalled
when requirements.txt actually changes, so the second run on a Pi Zero starts
in about a second rather than re-resolving the whole tree.

Standard library only, and it must stay that way. This is the file that runs
before any dependency exists, so importing anything from requirements.txt here
would be a chicken-and-egg failure on a fresh clone. It also has to parse on an
interpreter too old to run the app, so that such a user gets the version
message instead of a SyntaxError.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn, Optional, Sequence

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
DEV_REQUIREMENTS = ROOT / "requirements-dev.txt"
EXAMPLE_CONFIG = ROOT / "config" / "config.example.yaml"
CONFIG = ROOT / "config" / "config.yaml"
DATA = ROOT / "data"

# Recording which requirements the venv was built from is what makes it safe to
# skip pip on an unchanged tree, which is the difference between a one second
# start and a thirty second one on a Pi.
STAMP = VENV / ".mesh-spy-requirements"

IS_WINDOWS = os.name == "nt"

# meshtastic declares >=3.9,<3.15 and we are not going to be luckier than it is.
MIN_PYTHON = (3, 9)
MAX_PYTHON = (3, 15)


def say(message: str) -> None:
    # ASCII only: a Windows console still defaults to cp1252 in plenty of
    # places, where a stray dash or arrow raises UnicodeEncodeError and the
    # installer dies while printing a progress message.
    print("[Mesh-Spy] " + message, flush=True)


def die(message: str, *hints: str) -> NoReturn:
    print("[Mesh-Spy] error: " + message, file=sys.stderr, flush=True)
    for hint in hints:
        print("           " + hint, file=sys.stderr, flush=True)
    raise SystemExit(1)


def venv_python(venv: Path = VENV) -> Path:
    """The interpreter inside a virtualenv.

    Windows puts it in Scripts/ and everything else in bin/, and this one
    difference is most of what makes naive install scripts Linux-only.
    """
    if IS_WINDOWS:
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def check_python_version() -> None:
    if sys.version_info < MIN_PYTHON:
        die(
            "Python %d.%d is too old; Mesh-Spy needs %d.%d or newer."
            % (sys.version_info[0], sys.version_info[1], MIN_PYTHON[0], MIN_PYTHON[1]),
            "Install a newer Python and run this script with it.",
        )
    if sys.version_info >= MAX_PYTHON:
        say(
            "warning: Python %d.%d is newer than meshtastic supports (%d.%d-%d.%d);"
            % (
                sys.version_info[0],
                sys.version_info[1],
                MIN_PYTHON[0],
                MIN_PYTHON[1],
                MAX_PYTHON[0],
                MAX_PYTHON[1] - 1,
            )
        )
        say("         the install may fail. Continuing anyway.")


def check_not_store_python() -> None:
    """The Microsoft Store python.exe stub cannot build a working virtualenv.

    Windows ships an alias at %LOCALAPPDATA%\\Microsoft\\WindowsApps\\python.exe
    that opens the Store instead of running Python. Its venvs are also
    redirected under a per-app writable layer, so pip appears to succeed and
    then imports fail. Better to refuse than to debug that later.
    """
    if not IS_WINDOWS:
        return
    if "windowsapps" in sys.executable.lower():
        die(
            "This is the Microsoft Store Python stub, which cannot create a "
            "usable virtualenv.",
            "Install Python from https://www.python.org/downloads/ (tick 'Add "
            "python.exe to PATH'),",
            "or run: winget install --id Python.Python.3.12 -e",
        )


def create_venv(recreate: bool = False) -> Path:
    python = venv_python()
    if recreate and VENV.exists():
        say("Removing the existing virtualenv...")
        shutil.rmtree(VENV, ignore_errors=True)

    if python.exists():
        return python

    say("Creating the virtual environment in .venv ...")
    try:
        subprocess.run(
            [sys.executable, "-m", "venv", str(VENV)],
            check=True,
        )
    except subprocess.CalledProcessError:
        # Debian and Ubuntu split venv out of the base interpreter, and the
        # error python prints for that names ensurepip rather than the package
        # you actually have to install.
        die(
            "Could not create the virtual environment.",
            "On Debian, Ubuntu, Mint or Raspberry Pi OS: sudo apt install python3-venv",
            "On Fedora or RHEL: sudo dnf install python3-virtualenv",
        )

    if not python.exists():
        die("The virtual environment was created but %s is missing." % python)
    return python


def requirements_fingerprint(dev: bool) -> str:
    digest = hashlib.sha256()
    files = [REQUIREMENTS]
    if dev:
        files.append(DEV_REQUIREMENTS)
    for path in files:
        digest.update(path.read_bytes() if path.exists() else b"")
        digest.update(b"\0")
    # Rebuilding against a different interpreter needs a fresh install even
    # when the requirements themselves are untouched.
    digest.update(("%d.%d" % sys.version_info[:2]).encode())
    return digest.hexdigest()


def sync_dependencies(python: Path, *, dev: bool = False, force: bool = False) -> None:
    wanted = requirements_fingerprint(dev)
    if not force and STAMP.exists():
        try:
            if STAMP.read_text(encoding="utf-8").strip() == wanted:
                return
        except OSError:
            pass

    target = DEV_REQUIREMENTS if dev else REQUIREMENTS
    say("Installing dependencies from %s (this takes a while the first time)..." % target.name)
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "--upgrade", "pip"],
        check=False,
    )
    result = subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", "-r", str(target)]
    )
    if result.returncode != 0:
        die(
            "Installing dependencies failed.",
            "Re-run for the full pip output: %s -m pip install -r %s"
            % (python, target.name),
            "On Alpine or another musl system, pip may need a compiler: "
            "apk add build-base python3-dev linux-headers libffi-dev",
        )
    STAMP.write_text(wanted + "\n", encoding="utf-8")


def ensure_config() -> None:
    if CONFIG.exists():
        return
    if not EXAMPLE_CONFIG.exists():
        die("config/config.example.yaml is missing; this clone is incomplete.")
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(EXAMPLE_CONFIG, CONFIG)
    say("Wrote config/config.yaml from the example.")


def ensure_data() -> None:
    DATA.mkdir(parents=True, exist_ok=True)


def setup(*, dev: bool = False, skip_pip: bool = False, recreate: bool = False) -> Path:
    check_python_version()
    check_not_store_python()
    python = create_venv(recreate=recreate)
    if not skip_pip:
        sync_dependencies(python, dev=dev, force=recreate)
    ensure_config()
    ensure_data()
    return python


def launch(python: Path, args: Sequence[str]) -> NoReturn:
    """Hand over to the app, replacing this process where the OS allows it."""
    command = [str(python), "-m", "app.main"] + list(args)
    os.chdir(str(ROOT))
    if IS_WINDOWS:
        # Windows has no exec that replaces the process in a way consoles and
        # Ctrl+C cope with, so stay alive as a parent instead.
        try:
            raise SystemExit(subprocess.call(command))
        except KeyboardInterrupt:
            raise SystemExit(130)
    os.execv(str(python), command)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="Set up and start Mesh-Spy on Windows, macOS, "
        "Raspberry Pi OS or any Linux.",
    )
    parser.add_argument(
        "--setup-only",
        action="store_true",
        help="prepare the virtualenv and config, then exit without starting",
    )
    parser.add_argument(
        "--skip-pip",
        action="store_true",
        help="do not check or install dependencies",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="install requirements-dev.txt as well, for running the tests",
    )
    parser.add_argument(
        "--recreate",
        action="store_true",
        help="delete and rebuild the virtualenv from scratch",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="list serial ports that look like a mesh radio, then exit",
    )
    args = parser.parse_args(argv)

    # Honoured for continuity with the older run.sh, which documented it.
    skip_pip = args.skip_pip or os.environ.get(
        "MESH_SPY_SKIP_PIP", ""
    ).strip().lower() in ("1", "true", "yes")

    python = setup(dev=args.dev, skip_pip=skip_pip, recreate=args.recreate)

    if args.list_ports:
        return subprocess.call([str(python), "-m", "app.main", "--list-ports"])

    if args.setup_only:
        say("Setup complete. Start it with: %s -m app.main" % python)
        return 0

    launch(python, [])
    return 0  # not reached


if __name__ == "__main__":
    sys.exit(main())
