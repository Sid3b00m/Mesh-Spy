"""The summary line for collapsed motherboard UARTs.

Split from test_ports.py because it is about one specific claim: the range in
that line has to be true. A Fedora guest showed "32 ... hidden: /dev/ttyS0 ...
/dev/ttyS9", which reads as ten ports and silently omits twenty-two.
"""
from __future__ import annotations

from app.core.ports import SerialPort, format_ports


def legacy(n: int) -> SerialPort:
    device = "/dev/ttyS%d" % n
    return SerialPort(device=device, description=device, legacy=True)


def test_the_named_range_spans_every_hidden_port():
    text = format_ports([legacy(n) for n in range(32)])
    assert "32 empty motherboard serial ports hidden: /dev/ttyS0 ... /dev/ttyS31" in text


def test_a_handful_are_named_in_full_rather_than_as_a_range():
    text = format_ports([legacy(0), legacy(1)])
    assert "2 empty motherboard serial ports hidden: /dev/ttyS0, /dev/ttyS1" in text


def test_a_single_hidden_port_reads_as_singular():
    text = format_ports([legacy(0)])
    assert "1 empty motherboard serial port hidden: /dev/ttyS0" in text
    assert "ports hidden" not in text
