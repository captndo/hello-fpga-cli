import io
import subprocess
import sys
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

import serial

from hello_fpga.errors import DeviceTimeout, ProtocolError
from hello_fpga.operations import execute_power, iter_power
from hello_fpga.power import decode_power_reply, read_power
from hello_fpga.transport import SerialTransport
from helpers import FakeClock, FakeSerial, POWER_REPLY


class PowerProtocolTests(unittest.TestCase):
    def test_captured_readings(self):
        for low, code, current, power in (
            (0x40, 68, 0.1063019052271617, 0.12457254518808011),
            (0x50, 69, 0.10786516853932585, 0.12640449438202245),
        ):
            with self.subTest(code=code):
                raw = bytes([4, low]) + POWER_REPLY[2:]
                reading = decode_power_reply(raw)
                self.assertEqual(reading.raw, raw)
                self.assertEqual(reading.vsense_raw, 0x400 + low)
                self.assertEqual(reading.vsource_raw, 0x0780)
                self.assertEqual(reading.vsense_code, code)
                self.assertEqual(reading.vsource_code, 60)
                self.assertEqual(reading.voltage_v, 1.171875)
                self.assertAlmostEqual(reading.current_a, current)
                self.assertAlmostEqual(reading.power_w, power)

    def test_immutable_snapshot(self):
        raw = bytearray(POWER_REPLY)
        reading = decode_power_reply(raw)
        raw[0] = 0
        self.assertEqual(reading.raw, POWER_REPLY)
        with self.assertRaises(FrozenInstanceError):
            reading.raw = b"changed"

    def test_signed_boundaries_and_zero(self):
        for raw, code, current in (
            (0x0000, 0, 0), (0x0010, 1, 3.2 / 2047),
            (0x7FF0, 2047, 3.2), (0x8000, -2048, -3.2 * 2048 / 2047),
            (0xFFF0, -1, -3.2 / 2047),
        ):
            with self.subTest(raw=raw):
                reading = decode_power_reply(raw.to_bytes(2, "big") + POWER_REPLY[2:])
                self.assertEqual(reading.vsense_code, code)
                self.assertAlmostEqual(reading.current_a, current)
                self.assertAlmostEqual(reading.power_w, current * 1.171875)
        self.assertEqual(decode_power_reply(POWER_REPLY[:2] + b"\0\0" + POWER_REPLY[4:]).power_w, 0)

    def test_reserved_bits_are_discarded(self):
        reading = decode_power_reply(bytes.fromhex("04 5F 07 9F 58 5D 61"))
        self.assertEqual(reading.vsense_code, 69)
        self.assertEqual(reading.vsource_code, 60)

    def test_invalid_reply_fields_and_lengths(self):
        for raw in (b"", POWER_REPLY[:-1], POWER_REPLY + b"a"):
            with self.assertRaisesRegex(ProtocolError, f"expected 7 bytes, received {len(raw)}"):
                decode_power_reply(raw)
        for index, label, expected in (
            (4, "product ID", "58"), (5, "manufacturer ID", "5D"),
            (6, "terminator", "61"),
        ):
            raw = bytearray(POWER_REPLY)
            raw[index] = 0
            with self.assertRaisesRegex(ProtocolError, f"{label}: expected 0x{expected}, received 0x00"):
                decode_power_reply(raw)

    def test_fragmentation_and_exact_transaction(self):
        variants = (
            ([b"*" + POWER_REPLY], 100),
            ([bytes([b]) for b in b"*" + POWER_REPLY], 1),
            ([b"", b"*", b"\x04", bytes.fromhex("50 07 80 58 5D"), b"a"], 100),
        )
        for chunks, size in variants:
            with self.subTest(chunks=chunks):
                peer = FakeSerial(chunks=chunks, fragment_size=size)
                transport = SerialTransport("fake")
                transport.ser = peer
                with patch("hello_fpga.transport.time.monotonic", return_value=0):
                    self.assertEqual(read_power(transport).raw, POWER_REPLY)
                self.assertEqual(peer.events, [("write", b"*"), ("flush",)])
                self.assertEqual(peer.chunks, [])

    def test_embedded_terminator_and_back_to_back_samples(self):
        raw = bytes.fromhex("61 10 61 20 58 5D 61")
        peer = FakeSerial(b"*" + raw + b"*" + POWER_REPLY, fragment_size=100)
        transport = SerialTransport("fake")
        transport.ser = peer
        self.assertEqual(read_power(transport).raw, raw)
        self.assertEqual(read_power(transport).raw, POWER_REPLY)
        self.assertEqual(peer.writes, [b"*", b"*"])

    def test_bad_echo_stops_before_payload(self):
        peer = FakeSerial(b"?" + POWER_REPLY, fragment_size=100)
        transport = SerialTransport("fake")
        transport.ser = peer
        with self.assertRaisesRegex(ProtocolError, "echo: expected 0x2A, received 0x3F"):
            read_power(transport)
        self.assertEqual(peer.chunks, [POWER_REPLY])
        self.assertEqual(peer.writes, [b"*"])

    def test_missing_echo_and_truncated_payload(self):
        for incoming, label, received in (
            (b"", "power-command echo", "0/1"),
            (b"*", "power reply", "0/7"),
            (b"*" + POWER_REPLY[:3], "power reply", "3/7"),
        ):
            clock = FakeClock()
            peer = FakeSerial(incoming)
            original_read = peer.read

            def read(size):
                chunk = original_read(size)
                if not chunk:
                    clock.now += 1.1
                return chunk

            with patch("serial.Serial", return_value=peer), \
                 patch.object(peer, "read", side_effect=read), \
                 patch("hello_fpga.transport.time.monotonic", side_effect=clock.monotonic):
                with self.assertRaisesRegex(DeviceTimeout, f"{label}: received {received} bytes"):
                    execute_power(port="fake")
            self.assertTrue(peer.closed)
            self.assertEqual(peer.writes, [b"*"])


class PowerOperationTests(unittest.TestCase):
    def test_single_read_is_silent_and_closes(self):
        peer = FakeSerial(b"*" + POWER_REPLY)
        out, err = io.StringIO(), io.StringIO()
        with patch("serial.Serial", return_value=peer) as factory, \
             redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(execute_power(port="fake").raw, POWER_REPLY)
        self.assertEqual(factory.call_args.args, ("fake", 460800))
        self.assertTrue(peer.closed)
        self.assertEqual(out.getvalue() + err.getvalue(), "")

    def test_iterator_reuses_connection_and_closes_early(self):
        peer = FakeSerial((b"*" + POWER_REPLY) * 2)
        clock = FakeClock()
        with patch("serial.Serial", return_value=peer) as factory, \
             patch("hello_fpga.operations.time.monotonic", side_effect=clock.monotonic), \
             patch("hello_fpga.operations.time.sleep", side_effect=clock.sleep):
            with closing(iter_power(port="fake")) as readings:
                self.assertEqual(next(readings).raw, POWER_REPLY)
                self.assertEqual(clock.sleeps, [])
                self.assertFalse(peer.closed)
                self.assertEqual(next(readings).raw, POWER_REPLY)
                self.assertEqual(clock.sleeps, [2.0])
            self.assertTrue(peer.closed)
        factory.assert_called_once()
        self.assertEqual(peer.events.count(("reset_input",)), 1)
        self.assertEqual(peer.events.count(("reset_output",)), 1)
        self.assertEqual(peer.writes, [b"*", b"*"])

    def test_interval_includes_io_and_consumer_time_without_catchup(self):
        clock = FakeClock()
        peer = FakeSerial()
        starts = []

        def sample(transport):
            starts.append(clock.now)
            clock.now += 0.25
            return decode_power_reply(POWER_REPLY)

        with patch("serial.Serial", return_value=peer), \
             patch("hello_fpga.operations.read_power", side_effect=sample), \
             patch("hello_fpga.operations.time.monotonic", side_effect=clock.monotonic), \
             patch("hello_fpga.operations.time.sleep", side_effect=clock.sleep):
            with closing(iter_power(port="fake", interval=2)) as readings:
                next(readings)
                clock.now += 0.5
                next(readings)
                clock.now += 5
                next(readings)
                next(readings)
        self.assertEqual(starts, [0, 2, 7.25, 9.25])
        self.assertEqual(clock.sleeps, [1.25, 1.75])

    def test_failures_and_interrupts_close_connection(self):
        for method in ("read", "write"):
            for error in (serial.SerialException("offline"), KeyboardInterrupt()):
                for watch in (False, True):
                    with self.subTest(method=method, error=error, watch=watch):
                        peer = FakeSerial(b"*" + POWER_REPLY)
                        with patch("serial.Serial", return_value=peer), \
                             patch.object(peer, method, side_effect=error):
                            with self.assertRaises(type(error)):
                                if watch:
                                    next(iter_power(port="fake"))
                                else:
                                    execute_power(port="fake")
                        self.assertTrue(peer.closed)

    def test_bad_watch_reply_and_sleep_interrupt_close(self):
        for error in (ProtocolError("bad reply"), KeyboardInterrupt()):
            peer = FakeSerial(b"*" + POWER_REPLY + b"?")
            with patch("serial.Serial", return_value=peer), \
                 patch("hello_fpga.operations.time.sleep", side_effect=error):
                readings = iter_power(port="fake")
                next(readings)
                with self.assertRaises(type(error)):
                    next(readings)
            self.assertTrue(peer.closed)
        peer = FakeSerial(b"?")
        with patch("serial.Serial", return_value=peer):
            with self.assertRaises(ProtocolError):
                next(iter_power(port="fake"))
        self.assertTrue(peer.closed)

    def test_invalid_options_before_serial_open(self):
        for invalid in (0, -1, float("nan"), float("inf"), float("-inf")):
            with patch("serial.Serial") as factory:
                with self.assertRaises(ValueError):
                    next(iter_power(port="fake", interval=invalid))
                with self.assertRaises(ValueError):
                    execute_power(port="fake", timeout=invalid)
                with self.assertRaises(ValueError):
                    next(iter_power(port="fake", timeout=invalid))
                factory.assert_not_called()

    def test_core_without_typer(self):
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
from hello_fpga.operations import execute_power, iter_power
from helpers import FakeSerial, POWER_REPLY
import serial
peer = FakeSerial((b"*" + POWER_REPLY) * 2)
serial.Serial = lambda *a, **kw: peer
assert execute_power(port="fake").vsense_code == 69
assert peer.closed
peer.closed = False
readings = iter_power(port="fake")
assert next(readings).voltage_v == 1.171875
readings.close()
assert peer.closed
assert "typer" not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout + result.stderr, "")
