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

    def test_stream_read_is_bounded_and_notifies_nonempty_chunks_once(self):
        peer = FakeSerial(chunks=[b"\x00\xffabc", b"", b"z"], fragment_size=100)
        traffic = []
        transport = self.make_transport(peer)
        transport.on_rx = traffic.append
        with patch.object(peer, "read", wraps=peer.read) as read, \
             patch("hello_fpga.transport.time.monotonic", side_effect=AssertionError("no idle deadline")):
            self.assertEqual(transport.read_some(2), b"\x00\xff")
            self.assertEqual(transport.read_some(), b"abc")
            self.assertEqual(transport.read_some(), b"")
            self.assertEqual(transport.read_some(1), b"z")
        self.assertEqual([call.args for call in read.call_args_list], [(2,), (4096,), (4096,), (1,)])
        self.assertEqual(traffic, [b"\x00\xff", b"abc", b"z"])

    def test_stream_read_rejects_invalid_size_before_io(self):
        peer = FakeSerial()
        with patch.object(peer, "read") as read:
            for size in (0, -1, 1.5, "1", None, True, False):
                with self.subTest(size=size), self.assertRaises(ValueError):
                    self.make_transport(peer).read_some(size)
            read.assert_not_called()

    def test_stream_read_callback_failure_propagates(self):
        peer = FakeSerial(b"x")
        transport = self.make_transport(peer)
        def fail(data):
            raise RuntimeError("RX failed")
        transport.on_rx = fail
        with self.assertRaisesRegex(RuntimeError, "RX failed"):
            transport.read_some()

    def test_buffer_setup_failure_closes_connection(self):
        for method in ("reset_input_buffer", "reset_output_buffer"):
            peer = FakeSerial()
            primary = serial.SerialException("setup failed")
            with patch("serial.Serial", return_value=peer), \
                 patch.object(peer, method, side_effect=primary):
                with self.assertRaises(serial.SerialException) as error:
                    with SerialTransport("fake"):
                        self.fail("failed setup must not yield a transport")
            self.assertIs(error.exception, primary)
            self.assertTrue(peer.closed)

    def test_close_failure_preserves_pending_exception(self):
        peer = FakeSerial()
        primary, cleanup = ValueError("body failed"), serial.SerialException("close failed")
        with patch("serial.Serial", return_value=peer), \
             patch.object(peer, "close", side_effect=cleanup) as close:
            with self.assertRaises(ValueError) as error:
                with SerialTransport("fake"):
                    raise primary
        close.assert_called_once()
        self.assertIs(error.exception, primary)
        self.assertIs(error.exception.__cause__, cleanup)

    def test_close_failure_without_pending_exception_propagates(self):
        peer = FakeSerial()
        with patch("serial.Serial", return_value=peer), \
             patch.object(peer, "close", side_effect=serial.SerialException("close failed")):
            with self.assertRaisesRegex(serial.SerialException, "close failed"):
                with SerialTransport("fake"):
                    pass

    def test_unopened_transport_rejects_access(self):
        with self.assertRaisesRegex(RuntimeError, "serial port is not open"):
            SerialTransport("fake").serial

    def test_write_flush_order(self):
        peer = FakeSerial()
        transport = self.make_transport(peer)
        transport.write(b"one")
        transport.write(b"two")
        self.assertEqual(peer.events, [("write", b"one"), ("flush",), ("write", b"two"), ("flush",)])

    def test_traffic_callbacks_follow_io_and_skip_empty_reads(self):
        peer = FakeSerial(chunks=[b"a", b"", b"bc"])
        traffic = []
        def on_tx(data):
            self.assertEqual(peer.events[-2:], [("write", data), ("flush",)])
            traffic.append(("TX", data))
        transport = SerialTransport("fake", on_tx=on_tx, on_rx=lambda data: traffic.append(("RX", data)))
        transport.ser = peer
        with patch("hello_fpga.transport.time.monotonic", return_value=0):
            transport.write(b"command")
            self.assertEqual(transport.read_exact(3, "data"), b"abc")
        self.assertEqual(traffic, [("TX", b"command"), ("RX", b"a"), ("RX", b"bc")])

    def test_failed_write_or_flush_does_not_notify_tx(self):
        for method in ("write", "flush"):
            peer = FakeSerial()
            traffic = []
            transport = SerialTransport("fake", on_tx=traffic.append)
            transport.ser = peer
            with patch.object(peer, method, side_effect=serial.SerialException("offline")):
                with self.assertRaises(serial.SerialException):
                    transport.write(b"command")
            self.assertEqual(traffic, [])

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
