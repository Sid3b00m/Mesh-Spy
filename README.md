# Mesh-Spy

One web console for both mesh stacks. Meshtastic and MeshCore radios attach
side by side, and their nodes, messages and telemetry land in a single
dashboard that updates live over server-sent events.

Runs on Windows, a Raspberry Pi, or any Linux. No JavaScript build step, no
CDN, no cloud service.

```
                                 ┌──────────────────┐
  Meshtastic radio ──▶ meshtastic│                  │
   serial/BLE/TCP     (thread)   │   normalized     │──▶ SQLite + cache ──▶ REST
                                 │   event queue    │
  MeshCore radio  ──▶ meshcore   │                  │──▶ SSE /api/stream ──▶ UI
   serial/BLE/TCP     (asyncio)  └──────────────────┘
```

## What it does

- **Links** — one row per configured radio with its state, transport and last
  traffic, so a radio that quietly stopped talking is visible.
- **Nodes** — everything heard on either network, with name, SNR, RSSI, hops,
  battery, position and when it was last seen. Filterable by network.
- **Messages** — channel and direct traffic as it arrives, deduplicated.
- **Telemetry** — battery, voltage, temperature, humidity and channel
  utilisation, with `<canvas>` sparklines showing the recent trend.
- **Send** — text to a channel or a specific node, on either network. Off by
  default; see [Transmitting](#transmitting).

Nodes are never merged across the two networks. A Meshtastic node and a
MeshCore node at the same site stay separate entities, tagged by network,
because the protocols identify nodes in genuinely different ways: Meshtastic
uses a 32-bit node number, MeshCore uses a public key.

A map is out of scope for now. Leaflet would mean a CDN dependency or vendored
tiles, which fights the offline-Pi story, so positions appear as coordinates in
the nodes table.

## Install

One command, the same on Windows, Raspberry Pi OS, any Linux and macOS:

```
python bootstrap.py
```

That builds the virtualenv, installs dependencies, writes the first config and
starts the console at <http://127.0.0.1:8090>. It needs nothing but Python 3.9
or newer, never needs root, and writes nothing outside the project directory.

With no radio configured, the console starts a **simulated network** so the
dashboard is fully usable before any hardware arrives. Simulated links are
labelled `demo` and cannot transmit.

For auto-start at boot, serial permissions and system packages, use the
platform installer instead:

| Platform | Install | Starts automatically via |
| --- | --- | --- |
| Windows 10, 11 | double-click `install.bat` | a logon scheduled task |
| Raspberry Pi OS, Debian, Ubuntu, Mint, Fedora, RHEL, Arch, openSUSE, Gentoo | `sudo ./install.sh` | systemd |
| Alpine, Void | `sudo ./install.sh` | OpenRC |
| macOS | `python3 bootstrap.py` | not provided |

The Windows installer also installs Python for you if it is missing, and needs
no Administrator prompt. Both installers delegate the Python setup to the same
`bootstrap.py`, so there is one definition of a working install rather than
one per platform.

Day to day:

| | Windows | Linux, Pi, macOS |
| --- | --- | --- |
| Start | `run.bat` | `./run.sh` |
| List serial ports | `run.bat --list-ports` | `./run.sh --list-ports` |
| Rebuild the virtualenv | `python bootstrap.py --recreate` | same |

Dependencies are only reinstalled when `requirements.txt` changes, so a restart
takes about a second rather than re-resolving the tree.

[docs/INSTALL.md](docs/INSTALL.md) has the per-OS detail: Windows drivers,
LAN exposure, updating, uninstalling and troubleshooting.

## Attaching a radio

Find the port. This works the same on every platform, and prints the config
block to paste:

```
python bootstrap.py --list-ports
```

Ports look like `COM7` on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux,
and `/dev/cu.usbserial-0001` on macOS. On Linux the command also reports a
`/dev/serial/by-id/` path where one exists, which is worth preferring: `ttyUSB`
numbering follows probe order, so a config pinned to `ttyUSB0` can point at the
wrong radio after a reboot with two attached.

Then add the radio to `config/config.yaml` under `mesh.radios`. Each entry
needs a unique `name` and a `network` of `meshtastic` or `meshcore`:

```yaml
mesh:
  radios:
    - name: "base"
      network: meshtastic
      transport: serial
      port: "/dev/ttyUSB0"
      baud: 115200

    - name: "companion"
      network: meshcore
      transport: serial
      port: "/dev/ttyACM0"
```

`transport` can be `serial`, `tcp` or `ble`. TCP takes `host` and an optional
`tcp_port`; BLE takes an optional `address` (omit it to scan) and, for MeshCore,
a `pin`. Pair a BLE device first: `bluetoothctl` on Linux, Settings > Bluetooth
on Windows.

Adding any real radio suppresses the simulated network. `MESH_SPY_NO_DEMO=1`
disables it unconditionally, which is what CI uses.

Restart after editing the config:

```bash
sudo systemctl restart mesh-spy    # or: sudo rc-service mesh-spy restart
```

```powershell
Stop-ScheduledTask Mesh-Spy; Start-ScheduledTask Mesh-Spy
```

If a radio fails to open, the link is marked down and retried with exponential
backoff between `reconnect_min_seconds` and `reconnect_max_seconds`. The rest of
the console keeps working, so one dead USB port does not take the dashboard
with it.

## Transmitting

Sending keys up a real transmitter, so it is deliberately harder to reach than
the read-only panels:

1. `mesh.read_only` defaults to `true`. While it is true, `POST /api/send` is
   refused even for an authenticated user, and the send form is not rendered.
2. The send path requires auth **even on localhost**. With `auth.enabled: false`
   there is no way to transmit.

To enable it, set `read_only: false`, turn on auth, and set a password:

```yaml
auth:
  enabled: true
  username: "ops"
mesh:
  read_only: false
```

```bash
sudo systemctl edit mesh-spy   # add: Environment=MESH_SPY_PASSWORD=yoursecret
```

Prefer the `MESH_SPY_PASSWORD` environment variable over a password in the YAML
file. An `EnvironmentFile=-/etc/mesh-spy.env` at mode 600 is tidier than an
override that shows up in `systemctl cat`.

Messages are capped at 200 characters and rate limited per client. You are
responsible for operating within your local licensing rules.

## Exposing it on the LAN

The app **refuses to bind `0.0.0.0` with auth disabled** and exits rather than
publishing your mesh traffic and a transmit button to the network. Enable auth
first:

```yaml
server:
  host: "0.0.0.0"
auth:
  enabled: true
```

`MESH_SPY_ALLOW_INSECURE_LAN=1` overrides the refusal. Behind a TLS reverse
proxy, also set `MESH_SPY_SECURE_COOKIE=1` so the session cookie is marked
secure.

## Configuration

Everything lives in `config/config.yaml`, created from
`config/config.example.yaml` on first run. Anything you leave out falls back to
the example file, so a short config is fine.

| Key | Default | Notes |
| --- | --- | --- |
| `server.host` | `127.0.0.1` | 8090, not 8080, so Mesh-Spy and Pi-Spy-RF can share a Pi |
| `server.port` | `8090` | |
| `auth.enabled` | `false` | required for sending, and for any non-loopback bind |
| `database.path` | `data/mesh_spy.db` | |
| `mesh.read_only` | `true` | while true, nothing transmits |
| `mesh.retention_days` | `14` | history is trimmed at startup and hourly |
| `mesh.max_messages` | `5000` | this usually runs off an SD card |
| `mesh.stale_after_seconds` | `300` | a quiet link is reported stale, not down |
| `mesh.radios` | `[]` | empty means the simulated network |

Environment variables: `MESH_SPY_PASSWORD`, `MESH_SPY_NO_DEMO`,
`MESH_SPY_ALLOW_INSECURE_LAN`, `MESH_SPY_SECURE_COOKIE`.

## API

Read-only unless noted. All return JSON; `?network=meshtastic|meshcore` filters
where it makes sense.

| Endpoint | Returns |
| --- | --- |
| `GET /api/health` | liveness |
| `GET /api/status` | version, link counts, whether demo mode is active |
| `GET /api/links` | per-radio state and last traffic |
| `GET /api/nodes` | known nodes |
| `GET /api/nodes/{network}/{node_id}` | one node with its raw payload |
| `GET /api/messages` | recent messages from the cache |
| `GET /api/messages/history` | older messages from SQLite |
| `GET /api/telemetry` | latest metric per node |
| `GET /api/telemetry/{network}/{node_id}/{metric}` | that metric's series |
| `GET /api/send/limits` | length cap and rate limits |
| `POST /api/send` | transmit. Auth required, refused while `read_only` |
| `GET /api/stream` | SSE: a snapshot, then live deltas |

`/api/stream` is exempt from gzip, which would otherwise buffer the stream.

## Development

```
python bootstrap.py --dev --setup-only
```

Then, on Linux, a Pi or macOS:

```bash
.venv/bin/python -m pytest
.venv/bin/python -m pyflakes app tests bootstrap.py
```

On Windows the interpreter is at `.venv\Scripts\python.exe` instead; that path
difference is the whole reason `bootstrap.py` exists rather than a shell script
per platform.

The tests need no radio. Adapters take an injected transport, so they run
against recorded `meshtastic` packet dicts and `meshcore` event objects.
[docs/normalization.md](docs/normalization.md) has the field-by-field mapping
between the two protocols, which is what to read before touching an adapter.

CI runs pytest on Python 3.11 and 3.12 (`meshtastic` requires `>=3.9,<3.15`) on
both Ubuntu and Windows, plus shellcheck on the shell scripts and
PSScriptAnalyzer on the PowerShell. Text files must use LF endings, except
`.bat` which cmd.exe wants as CRLF; `.gitattributes` enforces both and a test
asserts it, because this was partly written on a Windows host that defaults to
UTF-16 and CRLF.

## Troubleshooting

[docs/INSTALL.md](docs/INSTALL.md#troubleshooting) covers this per platform.
The ones that come up most:

**Everything says `demo`.** No radio is configured, so the simulated network is
running. Add one under `mesh.radios`.

**No serial ports found.** Run `./run.sh --list-ports` (or `run.bat
--list-ports`). If it finds nothing, suspect the cable: a lot of USB cables
sold with small electronics are charge-only.

**Permission denied on the serial port (Linux).** Group membership applies at
next login, so log out and back in after installing, and replug the node so the
udev rule takes effect. Check with `id` that you are in `dialout` or `uucp`.

**Access is denied on COM7 (Windows).** Something else holds the port, usually
the Meshtastic web flasher or a serial terminal left open. If the port never
appears at all, the CP210x or CH340 driver is missing.

**The service dies with `203/EXEC` on Fedora or RHEL.** SELinux will not let
systemd execute anything labelled `user_home_t`. `install.sh` relabels
`.venv/bin` as `bin_t` to fix this; if you built the venv by hand afterwards,
run `sudo restorecon -R .venv/bin`.

**A BLE radio never connects.** Pair it first, confirm the Bluetooth stack is
running, and remember that a node already connected to a phone app will not
accept a second connection.

**Logs.** `journalctl -u mesh-spy -f` under systemd, `/var/log/mesh-spy.log`
under OpenRC, `data\mesh-spy.log` for the Windows logon task. Setting
`MESH_SPY_LOG_FILE` overrides all three.

## License

GPL-3.0. See [LICENSE](LICENSE).

This is not a preference, it is an obligation: Mesh-Spy imports the official
`meshtastic` Python library, which is GPL-3.0, so any work distributing them
together must be GPL-3.0 as well. The `meshcore` library is MIT and imposes no
such condition.

Mesh-Spy is an independent project and is not affiliated with or endorsed by
Meshtastic LLC or the MeshCore project.
