"""Serial port discovery, phrased the same way on every platform.

Finding the port is the step that actually stalls an install, and the answer
looks nothing alike across systems: COM7 on Windows, /dev/ttyUSB0 or
/dev/ttyACM0 on Linux, /dev/cu.usbserial-* on macOS. The old advice in the
README, `ls -l /dev/serial/by-id/`, is Linux-only and does not exist on a Pi
running a minimal image either.

pyserial enumerates all three through one API, and it is already present as a
meshtastic dependency. Matching on USB vendor and product IDs then separates a
mesh radio from a motherboard COM port, so the operator is shown one likely
answer rather than a list to guess from.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


# USB-serial bridges used by the boards these two firmwares run on. A vendor ID
# alone is enough: these chips are not otherwise common on a machine that is
# about to have a mesh radio plugged into it, and a false positive only costs a
# line of output.
KNOWN_USB_VENDORS = {
    0x10C4: "Silicon Labs CP210x",   # T-Beam, most Heltec, many ESP32 boards
    0x1A86: "QinHeng CH340/CH9102",  # Heltec V3, cheaper ESP32 boards
    0x0403: "FTDI",                  # older ESP32 boards
    0x239A: "Adafruit nRF52",        # some MeshCore companions
    0x303A: "Espressif native USB",  # ESP32-S3/C3 without a bridge chip
    0x2886: "Seeed Studio",          # Wio Tracker, XIAO nRF52
    0x1915: "Nordic Semiconductor",  # RAK4631 and other nRF52840 boards
}

# Boards that expose USB natively rather than through a bridge enumerate as a
# CDC ACM device, which is worth reporting even from an unrecognised vendor.
CDC_HINTS = ("cdc", "acm", "usb serial", "usbmodem")

# pyserial reports the literal string "n/a" rather than None when the driver
# tells it nothing about a port.
_NO_INFORMATION = ("n/a", "n/a n/a", "")

# A PC's Super I/O 16550 UARTs. A Fedora or Arch cloud image enumerates 32 of
# them, all empty, which buries the one line the operator actually needs.
_LEGACY_PC_UART = re.compile(r"^/dev/ttyS\d+$")


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text.lower() in _NO_INFORMATION else text


@dataclass
class SerialPort:
    device: str
    description: str
    vid: int | None = None
    pid: int | None = None
    serial_number: str | None = None
    manufacturer: str | None = None
    # A stable path that survives a replug reordering ttyUSB0 and ttyUSB1.
    # Linux only; there is no Windows or macOS equivalent.
    by_id: str | None = None
    likely_radio: bool = False
    chip: str | None = None
    # An empty motherboard UART, safe to collapse into a single summary line.
    legacy: bool = False

    @property
    def recommended(self) -> str:
        """What to actually put in config.yaml as `port`."""
        return self.by_id or self.device

    def to_dict(self) -> dict[str, Any]:
        return {
            "device": self.device,
            "recommended": self.recommended,
            "description": self.description,
            "vid": self.vid,
            "pid": self.pid,
            "serial_number": self.serial_number,
            "manufacturer": self.manufacturer,
            "by_id": self.by_id,
            "likely_radio": self.likely_radio,
            "chip": self.chip,
            "legacy": self.legacy,
        }


def _looks_like_radio(vid: int | None, description: str | None) -> tuple[bool, str | None]:
    if vid in KNOWN_USB_VENDORS:
        return True, KNOWN_USB_VENDORS[vid]
    haystack = (description or "").lower()
    if any(hint in haystack for hint in CDC_HINTS):
        return True, None
    return False, None


def _is_legacy_uart(device: str, vid: int | None, described: str | None) -> bool:
    """An empty /dev/ttyS* with nothing behind it.

    Deliberately narrow. It requires all three of: the legacy name pattern, no
    USB vendor, and no description at all. A Pi's GPIO UART (`/dev/ttyAMA0`,
    `/dev/serial0`) is a perfectly ordinary place to wire a radio and must
    never be collapsed, and neither must anything on USB.
    """
    return vid is None and described is None and bool(_LEGACY_PC_UART.match(device))


def _by_id_path(device: str) -> str | None:
    """Resolve a Linux tty to its /dev/serial/by-id/ alias, if there is one.

    Worth the directory walk: ttyUSB numbering depends on the order devices
    were probed, so a config pinned to /dev/ttyUSB0 can silently point at the
    wrong radio after a reboot with two nodes attached.
    """
    from pathlib import Path

    by_id_dir = Path("/dev/serial/by-id")
    try:
        if not by_id_dir.is_dir():
            return None
        target = Path(device).resolve()
        for link in by_id_dir.iterdir():
            try:
                if link.resolve() == target:
                    return str(link)
            except OSError:
                continue
    except OSError:
        return None
    return None


def list_ports() -> list[SerialPort]:
    """Every serial port on this machine, likely radios first.

    Returns an empty list rather than raising when pyserial is absent, so
    `--list-ports` on a half-finished install reports "none found" instead of
    an ImportError.
    """
    try:
        from serial.tools import list_ports as pyserial_ports
    except ImportError:
        return []

    found: list[SerialPort] = []
    for info in pyserial_ports.comports():
        described = _clean(info.description) or _clean(info.product)
        likely, chip = _looks_like_radio(info.vid, described)
        legacy = _is_legacy_uart(info.device, info.vid, described)
        found.append(
            SerialPort(
                device=info.device,
                description=described or info.device,
                vid=info.vid,
                pid=info.pid,
                serial_number=_clean(info.serial_number),
                manufacturer=_clean(info.manufacturer),
                by_id=None if legacy else _by_id_path(info.device),
                likely_radio=likely,
                chip=chip,
                legacy=legacy,
            )
        )

    found.sort(key=lambda p: (not p.likely_radio, p.legacy, p.device))
    return found


NOTHING_FOUND = (
    "No serial ports found.\n"
    "\n"
    "  - Is the radio plugged in, and is the cable a data cable rather\n"
    "    than a charge-only one?\n"
    "  - Windows: check Device Manager > Ports (COM & LPT). A device\n"
    "    shown with a warning triangle needs its CP210x or CH340 driver.\n"
    "  - Linux: run 'dmesg | tail' right after plugging it in.\n"
    "  - A node already connected to the phone app will not offer a\n"
    "    second connection."
)


def format_ports(ports: list[SerialPort]) -> str:
    """A ready-to-paste config snippet, not just a list of ports.

    The gap between "here is your port" and "here is what to write in
    config.yaml" is where most first installs stop, so close it here.
    """
    listed = [p for p in ports if not p.legacy]
    collapsed = [p for p in ports if p.legacy]

    if not listed and not collapsed:
        return NOTHING_FOUND

    lines = ["Serial ports on this machine:", ""]
    for port in listed:
        marker = "*" if port.likely_radio else " "
        detail = port.description
        if port.chip:
            detail += " [" + port.chip + "]"
        lines.append("  %s %-24s %s" % (marker, port.device, detail))
        if port.by_id:
            lines.append("      stable path: %s" % port.by_id)

    if collapsed:
        if listed:
            lines.append("")
        lines.append(
            "  (%d empty motherboard serial port%s hidden: %s)"
            % (
                len(collapsed),
                "" if len(collapsed) == 1 else "s",
                _summarise(collapsed),
            )
        )

    likely = [p for p in listed if p.likely_radio]
    lines.append("")
    if likely:
        lines.append("* marks a USB device that looks like a mesh radio.")
        lines.append("")
        lines.append("Add this to config/config.yaml under mesh.radios:")
        lines.append("")
        lines.append("  mesh:")
        lines.append("    radios:")
        lines.append('      - name: "base"')
        lines.append("        network: meshtastic    # or: meshcore")
        lines.append("        transport: serial")
        lines.append('        port: "%s"' % likely[0].recommended)
        lines.append("        baud: 115200")
    else:
        lines.append(
            "Nothing here looks like a mesh radio. Plug the node in and re-run;"
        )
        lines.append("if you know which port it is, use it as `port` anyway.")
    return "\n".join(lines)


def _summarise(ports: list[SerialPort]) -> str:
    # Ordered numerically, not as text. With 32 of them the lexicographic last
    # is ttyS9, so "ttyS0 ... ttyS9" would name a range omitting 22 ports.
    def index(port: SerialPort) -> int:
        match = re.search(r"(\d+)$", port.device)
        return int(match.group(1)) if match else 0

    names = [p.device for p in sorted(ports, key=index)]
    if len(names) <= 3:
        return ", ".join(names)
    return "%s ... %s" % (names[0], names[-1])
