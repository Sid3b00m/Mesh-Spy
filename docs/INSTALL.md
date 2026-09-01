# Installing Mesh-Spy

Mesh-Spy runs on Windows 10 and 11, Raspberry Pi OS, any mainstream Linux, and
macOS. The same Python application runs everywhere; only the wrapper that sets
it up differs.

**The short version, on every platform:**

```
python bootstrap.py
```

That creates the virtualenv, installs dependencies, writes the first config and
starts the console at <http://127.0.0.1:8090>. Nothing else is required, and
there is no radio to configure first: with none attached, Mesh-Spy runs a
simulated network so the dashboard is immediately usable.

Everything below is either a shortcut for that, or the extra system integration
(auto-start at boot, serial permissions) that a one-liner cannot do.

## Contents

- [Which install do I want?](#which-install-do-i-want)
- [Windows](#windows)
- [Raspberry Pi](#raspberry-pi)
- [Linux](#linux)
- [macOS](#macos)
- [Finding your radio's port](#finding-your-radios-port)
- [Running it](#running-it)
- [Updating](#updating)
- [Uninstalling](#uninstalling)
- [Troubleshooting](#troubleshooting)

## Which install do I want?

| You want | Do this |
| --- | --- |
| To try it right now | `python bootstrap.py` |
| It to start with the machine | Windows: `install.bat`. Linux and Pi: `sudo ./install.sh` |
| To develop against it | `python bootstrap.py --dev --setup-only` |

The full installers additionally handle system packages, serial port
permissions, and an auto-start service. `bootstrap.py` deliberately does none
of that: it never needs root and never touches anything outside the project
directory.

## Windows

### Requirements

Windows 10 or 11. Python is installed for you if it is missing.

### Install

Download or clone the project, then **double-click `install.bat`**.

From a terminal instead:

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

`install.bat` exists only to get past PowerShell's execution policy, which
blocks `install.ps1` on a default Windows install and reports it as a security
error rather than something you can fix. The bypass applies to that one
invocation and changes no machine setting.

The installer:

1. Finds a suitable Python, or installs 3.12 with winget if there is none.
2. Builds `.venv` and installs dependencies.
3. Writes `config\config.yaml`.
4. Registers a **scheduled task** so Mesh-Spy starts when you log in.
5. Prints the serial ports it found and the config block to paste.

**No Administrator prompt is needed.** Everything is per-user. The one
exception is `-OpenFirewall`, covered under [LAN access](#lan-access).

Options:

| Flag | Effect |
| --- | --- |
| `-NoAutoStart` | do not register the logon task |
| `-Recreate` | delete and rebuild the virtualenv |
| `-OpenFirewall` | allow LAN access on TCP 8090 (needs Administrator) |

### Starting and stopping

Double-click **`run.bat`**, or:

```powershell
python bootstrap.py
```

With the logon task installed, Mesh-Spy is already running in the background
after you log in, with no console window. Control it with:

```powershell
Start-ScheduledTask Mesh-Spy
Stop-ScheduledTask Mesh-Spy
Get-ScheduledTask Mesh-Spy | Get-ScheduledTaskInfo
```

Because the background task has no console, its logs go to
`data\mesh-spy.log` instead. Follow them with:

```powershell
Get-Content data\mesh-spy.log -Wait -Tail 20
```

### Windows drivers

A USB radio shows up in Device Manager under **Ports (COM & LPT)**. If it
appears with a warning triangle, or under **Other devices**, Windows is missing
the USB-serial driver for its bridge chip:

| Board | Chip | Driver |
| --- | --- | --- |
| T-Beam, most Heltec | Silicon Labs CP210x | [CP210x VCP drivers](https://www.silabs.com/developer-tools/usb-to-uart-bridge-vcp-drivers) |
| Heltec V3, cheaper ESP32 | CH340 / CH9102 | [WCH CH341SER](https://www.wch-ic.com/downloads/CH341SER_EXE.html) |
| RAK4631, XIAO nRF52, ESP32-S3 | native USB | none needed |

Windows 11 usually fetches the CP210x driver on its own; CH340 often needs the
manual install.

## Raspberry Pi

Tested on Raspberry Pi OS (Bookworm and later), on a Pi Zero 2 W and up. A Pi
Zero works but takes several minutes for the first dependency install.

```bash
git clone https://github.com/YOURNAME/Mesh-Spy.git
cd Mesh-Spy
sudo ./install.sh
```

Then open <http://127.0.0.1:8090>, or from another machine on the LAN see
[LAN access](#lan-access).

`install.sh` installs system packages, builds the virtualenv, adds you to the
serial group, installs a udev rule so the radio has predictable permissions,
and enables a **systemd** service.

```bash
sudo systemctl status mesh-spy
sudo systemctl restart mesh-spy
journalctl -u mesh-spy -f
```

**Log out and back in after installing.** Group membership only applies to new
logins, so until then the service can reach the serial port but your shell
cannot.

### Pi-specific notes

- The retention defaults (14 days, 5000 messages) are set for an SD card. Raise
  them in `config/config.yaml` if you are running from an SSD.
- Port 8090 rather than 8080, so Mesh-Spy and Pi-Spy-RF can share a Pi.
- For BLE, `bluez` is installed as an optional package. Pair the node with
  `bluetoothctl` before pointing Mesh-Spy at it.

## Linux

The same installer covers Debian, Ubuntu, Mint, Fedora, RHEL, Rocky, Alma,
Arch, Manjaro, openSUSE, Alpine, Void and Gentoo. It detects the package
manager and the init system rather than assuming either.

```bash
git clone https://github.com/YOURNAME/Mesh-Spy.git
cd Mesh-Spy
sudo ./install.sh
```

| Family | Packages via | Service |
| --- | --- | --- |
| Debian, Ubuntu, Mint, Raspberry Pi OS | `apt-get` | systemd |
| Fedora, RHEL, Rocky, Alma | `dnf` or `yum` | systemd |
| Arch, Manjaro, EndeavourOS | `pacman` | systemd |
| openSUSE | `zypper` | systemd |
| Alpine | `apk` | OpenRC |
| Void | `xbps-install` | OpenRC or runit |
| Gentoo | `emerge` | systemd or OpenRC |

On anything unrecognised the installer still sets up the Python application and
prints the packages to install by hand.

### Overrides

| Variable | Effect |
| --- | --- |
| `INSTALL_PACKAGES=0` | set up the Python app only, skip system packages |
| `ENABLE_SERVICE=0` | do not install an auto-start service |
| `SERVICE_USER=name` | run the service as this user |
| `SERIAL_GROUP=name` | grant serial access via this group instead of autodetecting |
| `RECREATE_VENV=1` | delete and rebuild the virtualenv |

### Without root

If you do not want to install system packages or a service:

```bash
python3 bootstrap.py
```

You still need read and write access to the serial device. Either add yourself
to `dialout` (Debian, Fedora, Alpine) or `uucp` (Arch, openSUSE), or use a TCP
or BLE radio, which need no serial permissions at all.

### Other init systems

runit, s6 and dinit are not detected. Set the app up with `./install.sh` and
`ENABLE_SERVICE=0`, then write a service by hand; `scripts/mesh-spy.openrc` is
the shortest reference. The command to supervise is:

```
/path/to/Mesh-Spy/.venv/bin/python -m app.main
```

with the working directory set to the project root.

## macOS

Not a target platform, but the application is pure Python and does work.

```bash
brew install python@3.12
git clone https://github.com/YOURNAME/Mesh-Spy.git
cd Mesh-Spy
python3 bootstrap.py
```

There is no `install.sh` path on macOS: no udev, no systemd, and serial devices
(`/dev/cu.usbserial-*`) are user-accessible already. For auto-start, write a
launchd plist calling `.venv/bin/python -m app.main`.

## Finding your radio's port

This is the step that stalls most first installs, so there is a command for it
that works the same everywhere:

```
python bootstrap.py --list-ports
```

It lists every serial port, marks the ones whose USB vendor ID belongs to a
chip these radios use, and prints the exact YAML to paste into
`config/config.yaml`:

```
Serial ports on this machine:

  * COM7                     Silicon Labs CP210x USB to UART Bridge [Silicon Labs CP210x]
    COM1                     Communications Port (COM1)

* marks a USB device that looks like a mesh radio.

Add this to config/config.yaml under mesh.radios:

  mesh:
    radios:
      - name: "base"
        network: meshtastic    # or: meshcore
        transport: serial
        port: "COM7"
        baud: 115200
```

On Linux you may instead see a line like `(32 empty motherboard serial ports
hidden: /dev/ttyS0 ... /dev/ttyS31)`. Those are the PC's own UARTs with nothing
attached, and most desktops and cloud images enumerate dozens of them. A radio
wired to a Raspberry Pi's GPIO header (`/dev/ttyAMA0`, `/dev/serial0`) is
never hidden.

What a port looks like per platform:

| Platform | Typical port |
| --- | --- |
| Windows | `COM7` |
| Linux, Raspberry Pi | `/dev/ttyUSB0`, `/dev/ttyACM0` |
| macOS | `/dev/cu.usbserial-0001`, `/dev/cu.usbmodem14201` |

On Linux the command also reports a `/dev/serial/by-id/...` path when one
exists. **Prefer it.** `ttyUSB0` and `ttyUSB1` are assigned in probe order, so
with two radios attached a config pinned to `ttyUSB0` can point at the wrong
one after a reboot. The `by-id` path is tied to the device itself and does not
move.

After editing the config, restart:

```bash
sudo systemctl restart mesh-spy      # Linux with systemd
sudo rc-service mesh-spy restart     # Alpine, OpenRC
```

```powershell
Stop-ScheduledTask Mesh-Spy; Start-ScheduledTask Mesh-Spy   # Windows
```

## Running it

| | Windows | Linux, Pi, macOS |
| --- | --- | --- |
| Start in a terminal | `run.bat` | `./run.sh` |
| Start, any platform | `python bootstrap.py` | `python3 bootstrap.py` |
| List serial ports | `run.bat --list-ports` | `./run.sh --list-ports` |
| Set up without starting | `python bootstrap.py --setup-only` | same |
| Rebuild the virtualenv | `python bootstrap.py --recreate` | same |
| Install test dependencies | `python bootstrap.py --dev --setup-only` | same |
| Start without checking dependencies | `python bootstrap.py --skip-pip` | same |

`bootstrap.py` only reinstalls dependencies when `requirements.txt` changes, so
after the first run it starts in about a second rather than re-resolving the
dependency tree. `--skip-pip`, or `MESH_SPY_SKIP_PIP=1`, skips even that check.

### LAN access

Mesh-Spy binds `127.0.0.1` by default and **refuses to bind a LAN address with
auth disabled**, exiting rather than publishing your mesh traffic and a
transmit button to the network. To expose it, enable auth first:

```yaml
server:
  host: "0.0.0.0"
auth:
  enabled: true
  username: "ops"
```

Set the password out of band rather than in the YAML file:

```bash
sudo systemctl edit mesh-spy          # add: Environment=MESH_SPY_PASSWORD=yoursecret
```

```powershell
[Environment]::SetEnvironmentVariable('MESH_SPY_PASSWORD', 'yoursecret', 'User')
```

Then open the firewall:

```bash
sudo ufw allow 8090/tcp                                    # Debian, Ubuntu
sudo firewall-cmd --add-port=8090/tcp --permanent && sudo firewall-cmd --reload
```

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1 -OpenFirewall   # as Administrator
```

Behind a TLS reverse proxy, also set `MESH_SPY_SECURE_COOKIE=1` so the session
cookie is marked secure.

## Updating

```bash
git pull
./run.sh                 # picks up changed requirements automatically
```

```powershell
git pull
.\run.bat
```

If a dependency change breaks something, rebuild cleanly with
`python bootstrap.py --recreate`. Your `config/config.yaml` and `data/` are
never touched by any of this.

## Uninstalling

Nothing is installed outside the project directory except the service and the
udev rule, so:

```bash
sudo systemctl disable --now mesh-spy
sudo rm /etc/systemd/system/mesh-spy.service /etc/udev/rules.d/60-mesh-spy-serial.rules
sudo systemctl daemon-reload
cd .. && rm -rf Mesh-Spy
```

```powershell
Unregister-ScheduledTask -TaskName Mesh-Spy -Confirm:$false
Remove-NetFirewallRule -DisplayName 'Mesh-Spy console'   # only if you added it
Remove-Item -Recurse -Force .\Mesh-Spy
```

## Troubleshooting

### The install fails

**`No module named venv` on Debian, Ubuntu, Mint or Pi OS.** The base
interpreter is split from its venv module: `sudo apt install python3-venv`.

**Windows says the script cannot be loaded because running scripts is
disabled.** Use `install.bat`, or run
`powershell -ExecutionPolicy Bypass -File .\install.ps1`.

**Windows opens the Microsoft Store when you type `python`.** That is the Store
alias stub, which cannot build a working virtualenv. `bootstrap.py` and
`install.ps1` both detect and refuse it. Install real Python from
[python.org](https://www.python.org/downloads/), ticking *Add python.exe to
PATH*, or run `winget install --id Python.Python.3.12 -e`.

**pip fails to build a wheel on Alpine or another musl system.** musl wheels
are not published for everything, so pip has to compile:
`apk add build-base python3-dev linux-headers libffi-dev`.

**The service dies with `203/EXEC` on Fedora or RHEL.** SELinux will not let
systemd execute anything labelled `user_home_t`. `install.sh` relabels
`.venv/bin` as `bin_t`; if you rebuilt the venv by hand afterwards, run
`sudo restorecon -R .venv/bin`.

### It runs, but no radio

**Everything says `demo`.** No radio is configured, so the simulated network is
running. Add one under `mesh.radios`. `MESH_SPY_NO_DEMO=1` disables the
simulation.

**No serial ports are listed at all.** Check the cable: many USB cables sold
with small electronics are charge-only and have no data lines. Then check
Device Manager on Windows, or `dmesg | tail` on Linux immediately after
plugging the radio in.

**Permission denied on the serial port (Linux).** Group membership applies at
next login, so log out and back in after installing, and replug the node so the
udev rule takes effect. Confirm with `id` that you are in `dialout` or `uucp`.

**Access is denied on COM7 (Windows).** Something else holds the port. The
usual culprits are the Meshtastic web flasher, Arduino IDE, or a serial
terminal left open.

**A BLE radio never connects.** Pair it first (`bluetoothctl` on Linux,
Settings > Bluetooth on Windows), confirm `bluez` is installed and
`bluetooth.service` is running, and remember that a node already connected to a
phone app will not accept a second connection.

**A radio opens, then drops.** That is expected to be survivable: the link is
marked down and retried with exponential backoff between
`reconnect_min_seconds` and `reconnect_max_seconds`, and the rest of the
console keeps working. Persistent drops on a Pi usually mean the USB port
cannot supply enough current during transmit; try a powered hub.

### Where are the logs?

| How it is running | Logs |
| --- | --- |
| systemd | `journalctl -u mesh-spy -f` |
| OpenRC | `/var/log/mesh-spy.log` |
| Windows logon task | `data\mesh-spy.log` |
| A terminal | the terminal |

Setting `MESH_SPY_LOG_FILE` sends logs to that file instead, on any platform.
The file rotates at 2 MB and keeps three old copies, because this often runs
off an SD card.
