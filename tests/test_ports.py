"""Serial port discovery, which is the step that stalls a first install.

These run against synthetic pyserial records rather than real hardware, so the
awkward cases are reachable: a Fedora cloud image enumerating 32 empty
motherboard UARTs, a Pi with its radio on the GPIO header, two identical
radios whose ttyUSB numbering can swap on reboot.
"""
from __future__ import annotations

from app.core import ports as ports_module
from app.core.ports import (
    KNOWN_USB_VENDORS,
    NOTHING_FOUND,
    SerialPort,
    format_ports,
    list_ports,
)


class FakeComport:
    """The subset of pyserial's ListPortInfo that list_ports() reads."""

    def __init__(self, device, description="n/a", vid=None, pid=None,
                 serial_number=None, manufacturer=None, product=None):
        self.device = device
        self.description = description
        self.vid = vid
        self.pid = pid
        self.serial_number = serial_number
        self.manufacturer = manufacturer
        self.product = product


def fake_comports(monkeypatch, entries):
    """Point list_ports() at a synthetic machine.

    list_ports imports pyserial inside the function so a missing dependency
    degrades rather than raises, which means the patch has to target the
    module it imports from.
    """
    import serial.tools.list_ports as pyserial_ports

    monkeypatch.setattr(pyserial_ports, "comports", lambda: entries)
    # /dev/serial/by-id only exists on Linux; keep the tests platform-neutral.
    monkeypatch.setattr(ports_module, "_by_id_path", lambda device: None)


CP210X = 0x10C4
CH340 = 0x1A86


# ---- identifying a radio ----

def test_a_known_usb_bridge_is_marked_as_a_likely_radio(monkeypatch):
    fake_comports(monkeypatch, [
        FakeComport("/dev/ttyUSB0", "CP2102 USB to UART Bridge", vid=CP210X, pid=0xEA60),
    ])
    found = list_ports()
    assert len(found) == 1
    assert found[0].likely_radio is True
    assert found[0].chip == KNOWN_USB_VENDORS[CP210X]


def test_a_native_usb_board_is_recognised_by_its_description(monkeypatch):
    """An nRF52 or ESP32-S3 with no bridge chip enumerates as CDC ACM."""
    fake_comports(monkeypatch, [
        FakeComport("/dev/ttyACM0", "RAK4631 CDC ACM device", vid=0xDEAD),
    ])
    assert list_ports()[0].likely_radio is True


def test_an_ordinary_motherboard_port_is_not_a_radio(monkeypatch):
    fake_comports(monkeypatch, [FakeComport("COM1", "Communications Port (COM1)")])
    found = list_ports()
    assert found[0].likely_radio is False
    assert found[0].legacy is False, "COM1 is not a Linux ttyS and must stay visible"


def test_likely_radios_sort_above_everything_else(monkeypatch):
    fake_comports(monkeypatch, [
        FakeComport("/dev/ttyS0"),
        FakeComport("/dev/ttyUSB0", "CH340", vid=CH340),
        FakeComport("/dev/ttyAMA0", "ttyAMA0"),
    ])
    assert list_ports()[0].device == "/dev/ttyUSB0"


# ---- the empty-UART noise a cloud image produces ----

def test_empty_motherboard_uarts_are_collapsed(monkeypatch):
    """Fedora and Arch enumerate 32 of these; listing them buries the answer."""
    entries = [FakeComport("/dev/ttyS%d" % n) for n in range(32)]
    entries.append(FakeComport("/dev/ttyUSB0", "CP2102 USB to UART Bridge", vid=CP210X))
    fake_comports(monkeypatch, entries)

    text = format_ports(list_ports())

    assert "/dev/ttyUSB0" in text
    assert "32 empty motherboard serial ports hidden" in text
    assert "/dev/ttyS17" not in text
    # The whole point is that the answer is still short enough to read.
    assert len(text.splitlines()) < 25


def test_a_pi_gpio_uart_is_never_collapsed(monkeypatch):
    """Wiring a radio to the Pi header is normal, and those have no USB vendor.

    The collapse rule has to be narrow enough that it cannot swallow the only
    port a Pi user has.
    """
    fake_comports(monkeypatch, [
        FakeComport("/dev/ttyAMA0"),
        FakeComport("/dev/serial0"),
        FakeComport("/dev/ttyS0"),
    ])
    found = {p.device: p.legacy for p in list_ports()}
    assert found["/dev/ttyAMA0"] is False
    assert found["/dev/serial0"] is False
    assert found["/dev/ttyS0"] is True

    text = format_ports(list_ports())
    assert "/dev/ttyAMA0" in text
    assert "/dev/serial0" in text


def test_a_described_legacy_port_is_kept(monkeypatch):
    """If the kernel knows anything about it, someone may have wired it up."""
    fake_comports(monkeypatch, [FakeComport("/dev/ttyS0", "USB Serial Adapter")])
    assert list_ports()[0].legacy is False


def test_pyserial_placeholder_descriptions_are_not_shown_as_text(monkeypatch):
    """pyserial says the string 'n/a', not None, when it knows nothing."""
    fake_comports(monkeypatch, [FakeComport("/dev/ttyAMA0", "n/a")])
    assert list_ports()[0].description == "/dev/ttyAMA0"


# ---- what the operator is told ----

def test_the_output_includes_a_config_block_naming_the_found_port(monkeypatch):
    fake_comports(monkeypatch, [
        FakeComport("COM7", "Silicon Labs CP210x USB to UART Bridge", vid=CP210X),
    ])
    text = format_ports(list_ports())
    assert 'port: "COM7"' in text
    assert "network: meshtastic" in text
    assert "transport: serial" in text
    assert "mesh:" in text and "radios:" in text


def test_the_stable_by_id_path_is_recommended_when_there_is_one(monkeypatch):
    """ttyUSB numbering follows probe order and can swap between reboots."""
    stable = "/dev/serial/by-id/usb-Silicon_Labs_CP2102-if00-port0"
    fake_comports(monkeypatch, [FakeComport("/dev/ttyUSB0", "CP2102", vid=CP210X)])
    import serial.tools.list_ports as pyserial_ports

    monkeypatch.setattr(ports_module, "_by_id_path", lambda device: stable)
    monkeypatch.setattr(pyserial_ports, "comports",
                        lambda: [FakeComport("/dev/ttyUSB0", "CP2102", vid=CP210X)])

    found = list_ports()[0]
    assert found.recommended == stable
    text = format_ports([found])
    assert 'port: "%s"' % stable in text
    assert "stable path" in text


def test_no_ports_at_all_explains_what_to_check(monkeypatch):
    fake_comports(monkeypatch, [])
    text = format_ports(list_ports())
    assert text == NOTHING_FOUND
    assert "charge-only" in text
    assert "Device Manager" in text


def test_ports_but_no_radio_says_so_without_a_config_block(monkeypatch):
    """Offering a paste-ready block naming COM1 would be actively misleading."""
    fake_comports(monkeypatch, [FakeComport("COM1", "Communications Port (COM1)")])
    text = format_ports(list_ports())
    assert "Nothing here looks like a mesh radio" in text
    assert "port:" not in text


def test_only_hidden_ports_still_produces_a_useful_answer(monkeypatch):
    """A cloud VM with nothing but empty UARTs must not print an empty list."""
    fake_comports(monkeypatch, [FakeComport("/dev/ttyS%d" % n) for n in range(4)])
    text = format_ports(list_ports())
    assert "4 empty motherboard serial ports hidden" in text
    assert "Nothing here looks like a mesh radio" in text


def test_missing_pyserial_reports_nothing_rather_than_raising(monkeypatch):
    """--list-ports on a half-finished install should not be an ImportError."""
    import builtins

    real_import = builtins.__import__

    def fail_on_pyserial(name, *rest):
        if name.startswith("serial"):
            raise ImportError("no pyserial")
        return real_import(name, *rest)

    monkeypatch.setattr(builtins, "__import__", fail_on_pyserial)
    assert list_ports() == []


# ---- the record itself ----

def test_to_dict_carries_what_an_api_caller_would_need():
    port = SerialPort(device="COM7", description="CP210x", vid=0x10C4,
                      likely_radio=True, chip="Silicon Labs CP210x")
    data = port.to_dict()
    assert data["device"] == "COM7"
    assert data["recommended"] == "COM7"
    assert data["likely_radio"] is True
    assert data["legacy"] is False
