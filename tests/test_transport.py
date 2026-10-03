import unittest
from unittest.mock import patch

import serial

from hello_fpga.errors import DeviceTimeout
from hello_fpga.transport import SerialTransport
from helpers import FakeSerial


class TransportTests(unittest.TestCase):
    def make_transport(self, peer, timeout=2):
        transport = SerialTransport("fake", timeout=timeout)
        transport.ser = peer
        return transport

    def test_fragmented_reads_and_empty_read(self):
        peer = FakeSerial(chunks=[b"a", b"", b"bc"])
        with patch("hello_fpga.transport.time.monotonic", return_value=0):
            self.assertEqual(self.make_transport(peer).read_exact(3, "data"), b"abc")

    def test_idle_deadline_resets_after_data(self):
        peer = FakeSerial(chunks=[b"a", b"", b"b"])
        with patch("hello_fpga.transport.time.monotonic", side_effect=[0, 1.5, 3, 3.1]):
            self.assertEqual(self.make_transport(peer).read_exact(2, "data"), b"ab")

    def test_timeout_reports_partial_read(self):
        peer = FakeSerial(chunks=[b"a", b""])
        with patch("hello_fpga.transport.time.monotonic", side_effect=[0, 1, 3.1]):
            with self.assertRaisesRegex(DeviceTimeout, "received 1/2 bytes"):
                self.make_transport(peer).read_exact(2, "data")

    def test_zero_byte_read(self):
        self.assertEqual(self.make_transport(FakeSerial()).read_exact(0, "data"), b"")

    def test_unopened_transport_rejects_access(self):
        with self.assertRaisesRegex(RuntimeError, "serial port is not open"):
            SerialTransport("fake").serial

    def test_write_flush_order(self):
        peer = FakeSerial()
        transport = self.make_transport(peer)
        transport.write(b"one")
        transport.write(b"two")
        self.assertEqual(peer.events, [("write", b"one"), ("flush",), ("write", b"two"), ("flush",)])

    def test_uart_configuration_and_close(self):
        for fail in (False, True):
            with self.subTest(fail=fail):
                peer = FakeSerial()
                with patch("serial.Serial", return_value=peer) as factory:
                    try:
                        with SerialTransport("fake", timeout=19):
                            self.assertEqual(peer.events, [("reset_input",), ("reset_output",)])
                            if fail:
                                raise RuntimeError("failure")
                    except RuntimeError:
                        if not fail:
                            raise
                    factory.assert_called_once_with(
                        "fake", 460800, bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                        timeout=0.25, write_timeout=5,
                    )
                self.assertTrue(peer.closed)
