import io
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import serial

from hello_fpga.bridge import HelloFpgaBridge
from hello_fpga.errors import DeviceTimeout, ProtocolError
from hello_fpga.operations import open_bridge
from hello_fpga.transport import SerialTransport
from helpers import FakeSerial


class BridgeTests(unittest.TestCase):
    def make_bridge(self, peer):
        transport = SerialTransport("fake", timeout=2)
        transport.ser = peer
        return HelloFpgaBridge(transport)

    def test_entry_exit_and_reentry_without_ack(self):
        peer = FakeSerial(b"&&")
        bridge = self.make_bridge(peer)
        with patch.object(peer, "read", wraps=peer.read) as read:
            self.assertFalse(bridge.active)
            bridge.enter()
            self.assertTrue(bridge.active)
            bridge.exit()
            self.assertFalse(bridge.active)
            bridge.enter()
            bridge.exit()
        self.assertEqual([call.args for call in read.call_args_list], [(1,), (1,)])
        self.assertEqual(peer.events, [
            ("write", b"&"), ("flush",), ("write", b"%ExIt3"), ("flush",),
            ("write", b"&"), ("flush",), ("write", b"%ExIt3"), ("flush",),
        ])
        self.assertFalse(peer.closed)  # Caller owns the low-level transport.

    def test_echo_validation_and_activation_order(self):
        for echo in (b"&", b"?"):
            peer = FakeSerial(echo + b"payload")
            bridge = self.make_bridge(peer)
            bridge.transport.on_rx = lambda data: self.assertFalse(bridge.active)
            if echo == b"&":
                bridge.enter()
                self.assertTrue(bridge.active)
            else:
                with self.assertRaisesRegex(ProtocolError, "expected 0x26, received 0x3F"):
                    bridge.enter()
                self.assertFalse(bridge.active)
            self.assertEqual(peer.chunks, [b"payload"])

    def test_inactive_operations_and_double_entry_exit(self):
        peer = FakeSerial(b"&")
        bridge = self.make_bridge(peer)
        for operation in (
            lambda: bridge.write(b"data"), lambda: bridge.read(),
            lambda: bridge.read_exact(0), bridge.exit,
        ):
            with self.assertRaisesRegex(ProtocolError, "not active"):
                operation()
        self.assertEqual(peer.events, [])
        bridge.enter()
        with self.assertRaisesRegex(ProtocolError, "already active"):
            bridge.enter()
        bridge.exit()
        with self.assertRaisesRegex(ProtocolError, "not active"):
            bridge.exit()
        self.assertEqual(peer.writes, [b"&", b"%ExIt3"])

    def test_payload_binary_commands_and_reserved_sequences_are_unchanged(self):
        peer = FakeSerial(b"&")
        bridge = self.make_bridge(peer)
        bridge.enter()
        payloads = [b"\x00\x01\xffhello", b"*$#&!@", b"%ExXYZ", b"%E", b"x", b"\x00\xff\x01"]
        for payload in payloads:
            bridge.write(payload)
        self.assertEqual(peer.writes, [b"&"] + payloads)
        self.assertTrue(bridge.active)  # Local belief, not PIC state discovery.

    def test_stream_and_fragmented_exact_reads_with_callbacks(self):
        peer = FakeSerial(chunks=[b"&", b"\0\xff", b"", b"a", b"bc"])
        bridge = self.make_bridge(peer)
        traffic = []
        bridge.transport.on_rx = traffic.append
        bridge.enter()
        self.assertEqual(bridge.read(2), b"\0\xff")
        self.assertEqual(bridge.read(), b"")
        self.assertEqual(bridge.read_exact(3), b"abc")
        self.assertEqual(bridge.read_exact(0), b"")
        self.assertEqual(traffic, [b"&", b"\0\xff", b"a", b"bc"])

    def test_invalid_read_sizes_do_not_reach_serial(self):
        peer = FakeSerial(b"&")
        bridge = self.make_bridge(peer)
        bridge.enter()
        with patch.object(peer, "read") as read:
            for size in (-1, 1.5, "1", None, True, False):
                for operation in (bridge.read, bridge.read_exact):
                    with self.subTest(size=size, operation=operation), self.assertRaises(ValueError):
                        operation(size)
            with self.assertRaises(ValueError):
                bridge.read(0)
            self.assertEqual(bridge.read_exact(0), b"")
            read.assert_not_called()


class BridgeOperationTests(unittest.TestCase):
    def test_connection_lifetime_callbacks_and_silence(self):
        peer = FakeSerial(b"&a", fragment_size=1)
        traffic = []
        out, err = io.StringIO(), io.StringIO()
        with patch("serial.Serial", return_value=peer) as factory, \
             redirect_stdout(out), redirect_stderr(err):
            with open_bridge(
                port="fake", on_tx=lambda data: traffic.append(("TX", data)),
                on_rx=lambda data: traffic.append(("RX", data)),
            ) as bridge:
                self.assertTrue(bridge.active)
                self.assertFalse(peer.closed)
                bridge.write(b"h")
                self.assertEqual(bridge.read_exact(1, "FPGA handshake"), b"a")
                self.assertFalse(peer.closed)
            self.assertFalse(bridge.active)
        factory.assert_called_once()
        self.assertEqual(factory.call_args.args, ("fake", 460800))
        self.assertEqual(factory.call_args.kwargs["timeout"], 0.25)
        self.assertEqual(peer.events.count(("reset_input",)), 1)
        self.assertEqual(peer.events.count(("reset_output",)), 1)
        self.assertEqual(traffic, [
            ("TX", b"&"), ("RX", b"&"), ("TX", b"h"),
            ("RX", b"a"), ("TX", b"%ExIt3"),
        ])
        self.assertTrue(peer.closed)
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_manual_exit_and_reentry_cleanup(self):
        for reenter in (False, True):
            peer = FakeSerial(b"&&")
            with patch("serial.Serial", return_value=peer):
                with open_bridge(port="fake") as bridge:
                    bridge.exit()
                    if reenter:
                        bridge.enter()
            self.assertEqual(peer.writes, [b"&", b"%ExIt3"] * (2 if reenter else 1))
            self.assertTrue(peer.closed)

    def test_opt_out_and_application_errors_or_interruption(self):
        for auto_exit in (False, True):
            for primary in (None, ValueError("app failed"), KeyboardInterrupt()):
                peer = FakeSerial(b"&")
                with patch("serial.Serial", return_value=peer):
                    try:
                        with open_bridge(port="fake", auto_exit=auto_exit) as bridge:
                            if primary is not None:
                                raise primary
                    except BaseException as error:
                        self.assertIs(error, primary)
                    else:
                        self.assertIsNone(primary)
                self.assertEqual(peer.writes, [b"&", b"%ExIt3"] if auto_exit else [b"&"])
                self.assertEqual(bridge.active, not auto_exit)
                self.assertTrue(peer.closed)

    def test_failed_entry_never_guesses_exit(self):
        peer = FakeSerial(b"?")
        with patch("serial.Serial", return_value=peer):
            with self.assertRaises(ProtocolError):
                with open_bridge(port="fake"):
                    self.fail("wrong echo must not yield")
        self.assertEqual(peer.writes, [b"&"])
        self.assertTrue(peer.closed)
        for name in ("on_tx", "on_rx"):
            peer = FakeSerial(b"&")
            def fail(data):
                raise RuntimeError("entry callback failed")
            with patch("serial.Serial", return_value=peer):
                with self.assertRaisesRegex(RuntimeError, "entry callback failed"):
                    with open_bridge(port="fake", **{name: fail}):
                        self.fail("callback failure must not yield")
            self.assertEqual(peer.writes, [b"&"])
            self.assertTrue(peer.closed)

    def test_entry_and_exact_read_timeouts_and_labels(self):
        for incoming, times, label, count in (
            (b"", [0, 3], "bridge-command echo", "0/1"),
            (b"&ab", [0, 0, 0, 0, 3], "FPGA reply", "2/4"),
        ):
            peer = FakeSerial(incoming)
            with patch("serial.Serial", return_value=peer), \
                 patch("hello_fpga.transport.time.monotonic", side_effect=times):
                with self.assertRaisesRegex(DeviceTimeout, f"{label}: received {count} bytes"):
                    with open_bridge(port="fake") as bridge:
                        bridge.read_exact(4, "FPGA reply")
            self.assertEqual(peer.writes, [b"&", b"%ExIt3"] if incoming else [b"&"])
            self.assertTrue(peer.closed)

    def test_io_and_callback_failures_in_body_exit_and_close(self):
        for method in ("write", "flush", "read"):
            for primary in (serial.SerialException("offline"), KeyboardInterrupt()):
                peer = FakeSerial(b"&")
                with patch("serial.Serial", return_value=peer):
                    with self.assertRaises(type(primary)) as error:
                        with open_bridge(port="fake") as bridge:
                            with patch.object(peer, method, side_effect=primary):
                                bridge.read() if method == "read" else bridge.write(b"h")
                self.assertIs(error.exception, primary)
                self.assertEqual(peer.writes[-1], b"%ExIt3")
                self.assertTrue(peer.closed)
        for name in ("on_tx", "on_rx"):
            peer = FakeSerial(b"&a")
            def fail_payload(data):
                if data in (b"h", b"a"):
                    raise RuntimeError("payload callback failed")
            with patch("serial.Serial", return_value=peer):
                with self.assertRaisesRegex(RuntimeError, "payload callback failed"):
                    with open_bridge(port="fake", **{name: fail_payload}) as bridge:
                        bridge.write(b"h") if name == "on_tx" else bridge.read()
            self.assertEqual(peer.writes[-1], b"%ExIt3")
            self.assertTrue(peer.closed)

    def test_cleanup_failures_preserve_primary_and_do_not_retry(self):
        for primary in (None, ValueError("app failed"), KeyboardInterrupt()):
            for failure_point in ("write", "flush", "callback"):
                peer = FakeSerial(b"&")
                cleanup = serial.SerialException("exit failed")
                original_write, original_flush = peer.write, peer.flush
                attempts = []
                def write(data):
                    if data == b"%ExIt3":
                        attempts.append(data)
                        if failure_point == "write":
                            raise cleanup
                    return original_write(data)
                def flush():
                    if peer.writes[-1] == b"%ExIt3" and failure_point == "flush":
                        raise cleanup
                    return original_flush()
                def on_tx(data):
                    if data == b"%ExIt3" and failure_point == "callback":
                        raise cleanup
                with patch("serial.Serial", return_value=peer), \
                     patch.object(peer, "write", side_effect=write), \
                     patch.object(peer, "flush", side_effect=flush):
                    with self.assertRaises(type(primary or cleanup)) as error:
                        with open_bridge(port="fake", on_tx=on_tx) as bridge:
                            if primary is not None:
                                raise primary
                self.assertIs(error.exception, primary or cleanup)
                if primary is not None:
                    self.assertIs(error.exception.__cause__, cleanup)
                self.assertTrue(bridge.active)  # Failed exit cannot confirm inactivity.
                self.assertEqual(attempts, [b"%ExIt3"])
                self.assertTrue(peer.closed)

    def test_failed_manual_exit_is_not_repeated_even_if_caught(self):
        for caught in (False, True):
            peer = FakeSerial(b"&")
            def fail(data):
                if data == b"%ExIt3":
                    raise RuntimeError("exit observer failed")
            with patch("serial.Serial", return_value=peer):
                try:
                    with open_bridge(port="fake", on_tx=fail) as bridge:
                        if caught:
                            with self.assertRaisesRegex(RuntimeError, "exit observer failed"):
                                bridge.exit()
                            with self.assertRaisesRegex(ProtocolError, "already attempted"):
                                bridge.exit()
                        else:
                            bridge.exit()
                except RuntimeError as error:
                    self.assertFalse(caught)
                    self.assertEqual(str(error), "exit observer failed")
            self.assertEqual(peer.writes, [b"&", b"%ExIt3"])
            self.assertTrue(peer.closed)

    def test_invalid_timeout_and_open_errors(self):
        with patch("serial.Serial") as factory:
            for timeout in (0, -1, 0.5, float("nan"), float("inf"), float("-inf")):
                with self.assertRaisesRegex(ValueError, "finite and at least 1"):
                    with open_bridge(port="fake", timeout=timeout):
                        pass
            factory.assert_not_called()
        with patch("serial.Serial", side_effect=serial.SerialException("no port")):
            with self.assertRaisesRegex(serial.SerialException, "no port"):
                with open_bridge(port="fake"):
                    pass

    def test_core_without_typer_and_freeze_controller_import(self):
        project = Path(__file__).resolve().parents[1]
        script = '''
import importlib.abc
import sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
class BlockTyper(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "typer" or fullname.startswith("typer."):
            raise ImportError("Core must not import Typer")
sys.meta_path.insert(0, BlockTyper())
from hello_fpga.bridge import HelloFpgaBridge
from hello_fpga.operations import open_bridge
from hello_fpga.freeze import FlashFreezeController
from helpers import FakeSerial
import serial
peer = FakeSerial(b"&a")
serial.Serial = lambda *a, **kw: peer
with open_bridge(port="fake") as bridge:
    assert isinstance(bridge, HelloFpgaBridge)
    bridge.write(b"h")
    assert bridge.read_exact(1) == b"a"
assert peer.writes == [b"&", b"h", b"%ExIt3"]
assert peer.closed
assert "typer" not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
