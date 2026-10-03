import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import serial

from hello_fpga.errors import DeviceTimeout, ProtocolError
from hello_fpga.freeze import FlashFreezeController
from hello_fpga.operations import execute_freeze_enter, execute_freeze_exit
from hello_fpga.transport import SerialTransport
from helpers import FakeSerial


class FreezeTests(unittest.TestCase):
    def test_enter_exit_and_unsigned_ticks(self):
        for ticks in (0, 269, 0xffffffff):
            with self.subTest(ticks=ticks):
                peer = FakeSerial(b"$@" + ticks.to_bytes(4, "little"), fragment_size=1)
                transport = SerialTransport("fake")
                transport.ser = peer
                pic = FlashFreezeController(transport)
                out, err = io.StringIO(), io.StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    self.assertIsNone(pic.flashfreeze_enter())
                    self.assertEqual(pic.flashfreeze_exit(), ticks)
                self.assertEqual(out.getvalue(), "")
                self.assertEqual(err.getvalue(), "")
                self.assertEqual(peer.events, [
                    ("write", b"$"), ("flush",), ("write", b"@"), ("flush",),
                ])
                self.assertFalse(peer.closed)  # The caller owns this transport.

    def test_no_board_state_assumptions(self):
        for method, reply, command in (
            ("flashfreeze_enter", b"$$", b"$"),
            ("flashfreeze_exit", b"@\x00\x00\x00\x00" * 2, b"@"),
        ):
            peer = FakeSerial(reply)
            transport = SerialTransport("fake")
            transport.ser = peer
            operation = getattr(FlashFreezeController(transport), method)
            operation()
            operation()
            self.assertEqual(peer.writes, [command, command])

    def test_wrong_echo_does_not_read_ticks(self):
        for operation, command, expected in (
            (execute_freeze_enter, b"$", "0x24"),
            (execute_freeze_exit, b"@", "0x40"),
        ):
            peer = FakeSerial(b"?\x0d\x01\x00\x00")
            with patch("serial.Serial", return_value=peer), \
                 patch.object(peer, "read", wraps=peer.read) as read:
                with self.assertRaisesRegex(ProtocolError, f"expected {expected}, received 0x3F"):
                    operation(port="fake")
            read.assert_called_once_with(1)
            self.assertEqual(peer.writes, [command])
            self.assertTrue(peer.closed)

    def test_operations_return_and_close(self):
        for operation, reply, expected in (
            (execute_freeze_enter, b"$", None),
            (execute_freeze_exit, b"@\x0d\x01\x00\x00", 269),
        ):
            peer = FakeSerial(reply)
            with patch("serial.Serial", return_value=peer) as factory:
                self.assertEqual(operation(port="fake"), expected)
            self.assertEqual(factory.call_args.args, ("fake", 460800))
            self.assertTrue(peer.closed)

    def test_missing_echo_and_partial_ticks_timeout(self):
        for operation, incoming, clock, label, received in (
            (execute_freeze_enter, b"", [0, 3], "enter echo", "0/1"),
            (execute_freeze_exit, b"", [0, 3], "exit echo", "0/1"),
            (execute_freeze_exit, b"@\x0d\x01", [0, 0, 0, 0, 3], "exit ticks", "2/4"),
        ):
            peer = FakeSerial(incoming)
            traffic = []
            with patch("serial.Serial", return_value=peer), \
                 patch("hello_fpga.transport.time.monotonic", side_effect=clock):
                with self.assertRaisesRegex(DeviceTimeout, f"{label}: received {received} bytes"):
                    operation(port="fake", on_rx=traffic.append)
            self.assertEqual(traffic, [b"@", b"\x0d\x01"] if incoming else [])
            self.assertTrue(peer.closed)

    def test_invalid_timeout_before_open(self):
        with patch("serial.Serial") as factory:
            for operation in (execute_freeze_enter, execute_freeze_exit):
                for timeout in (0, -1, 0.5, float("nan"), float("inf"), -float("inf")):
                    with self.subTest(operation=operation, timeout=timeout):
                        with self.assertRaisesRegex(ValueError, "finite and at least 1"):
                            operation(port="fake", timeout=timeout)
            factory.assert_not_called()

    def test_io_interrupt_and_callback_failures_close(self):
        for operation in (execute_freeze_enter, execute_freeze_exit):
            for method in ("write", "flush", "read"):
                for error in (serial.SerialException("offline"), KeyboardInterrupt()):
                    peer = FakeSerial()
                    with patch("serial.Serial", return_value=peer), \
                         patch.object(peer, method, side_effect=error):
                        with self.assertRaises(type(error)):
                            operation(port="fake")
                    self.assertTrue(peer.closed)
            for name in ("on_tx", "on_rx"):
                peer = FakeSerial(b"$" if operation is execute_freeze_enter else b"@")
                def fail(data):
                    raise RuntimeError("observer failed")
                with patch("serial.Serial", return_value=peer):
                    with self.assertRaisesRegex(RuntimeError, "observer failed"):
                        operation(port="fake", **{name: fail})
                self.assertTrue(peer.closed)

    def test_open_error_propagates(self):
        with patch("serial.Serial", side_effect=serial.SerialException("no port")):
            for operation in (execute_freeze_enter, execute_freeze_exit):
                with self.assertRaisesRegex(serial.SerialException, "no port"):
                    operation(port="fake")
