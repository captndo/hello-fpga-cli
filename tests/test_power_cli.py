import io
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from unittest.mock import patch

import serial
import typer
from typer.testing import CliRunner

from hello_fpga.cli import power
from hello_fpga_cli import app
from helpers import FakeSerial, POWER_REPLY


STAMP = "2026-10-02T12:34:56+02:00"
WATCH_LINE = (
    STAMP + "  Vcore: 1.172 V  Icore: 107.9 mA  Pcore: 126.4 mW"
)


class TerminalStream(io.StringIO):
    def __init__(self):
        super().__init__()
        self.flushes = 0

    def isatty(self):
        return True

    def flush(self):
        self.flushes += 1


class PowerCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_help(self):
        self.assertIn("power", self.runner.invoke(app, ["--help"]).stdout)
        result = self.runner.invoke(app, ["power", "--help"])
        self.assertEqual(result.exit_code, 0)
        for option in ("--port", "-p", "--timeout", "--watch", "--interval"):
            self.assertIn(option, result.stdout)

    def test_single_output_and_port(self):
        for low, current, watts in ((0x40, "106.3", "124.6"), (0x50, "107.9", "126.4")):
            for options, port in (([], "/dev/ttyACM0"), (["-p", "fake"], "fake"), (["--port", "fake"], "fake")):
                with self.subTest(low=low, options=options):
                    peer = FakeSerial(b"*\x04" + bytes([low]) + POWER_REPLY[2:])
                    with patch("serial.Serial", return_value=peer) as factory:
                        result = self.runner.invoke(app, ["power"] + options)
                    self.assertEqual(result.exit_code, 0, result.exception)
                    self.assertEqual(result.stdout, f"Core voltage: 1.172 V\nCore current: {current} mA\nCore power:   {watts} mW\n")
                    self.assertEqual(result.stderr, "")
                    self.assertEqual(factory.call_args.args, (port, 460800))
                    self.assertEqual(peer.writes, [b"*"])
                    self.assertTrue(peer.closed)

    def test_invalid_options_before_open(self):
        args = [["design.dat"]]
        for option in ("--interval", "--timeout"):
            for value in ("0", "-1", "nan", "inf", "-inf"):
                args.append([option, value])
        args.append(["--timeout", "0.5"])
        with patch("serial.Serial") as factory:
            for tail in args:
                with self.subTest(tail=tail):
                    result = self.runner.invoke(app, ["power"] + tail)
                    self.assertEqual(result.exit_code, 2)
            factory.assert_not_called()

    def test_timeout_default_and_override(self):
        for args, seconds in (([], 1), (["--timeout", "3"], 3)):
            peer = FakeSerial()
            with patch("serial.Serial", return_value=peer), \
                 patch("hello_fpga.transport.time.monotonic", side_effect=[0, seconds + 1]):
                result = self.runner.invoke(app, ["power"] + args)
            self.assertEqual(result.exit_code, 3)
            self.assertIn(f"Timed out after {seconds}s", result.stderr)
            self.assertTrue(peer.closed)

    def test_errors_and_read_interrupt(self):
        for error, expected, message in (
            (serial.SerialException("offline"), 2, "Serial error: offline\n"),
            (KeyboardInterrupt(), 130, ""),
        ):
            peer = FakeSerial()
            with patch("serial.Serial", return_value=peer), \
                 patch.object(peer, "read", side_effect=error):
                result = self.runner.invoke(app, ["power"])
            self.assertEqual(result.exit_code, expected)
            self.assertEqual(result.stderr, message)
            self.assertTrue(peer.closed)
        with patch("serial.Serial", side_effect=serial.SerialException("no port")):
            result = self.runner.invoke(app, ["power"])
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.stderr, "Serial error: no port\n")
        peer = FakeSerial(b"?")
        with patch("serial.Serial", return_value=peer):
            result = self.runner.invoke(app, ["power"])
        self.assertEqual(result.exit_code, 3)
        self.assertIn("Protocol error: power-command echo:", result.stderr)
        self.assertTrue(peer.closed)

    def test_redirected_watch_timestamps_interval_and_interrupt(self):
        for args, interval in (([], 2), (["--interval", "0.5"], 0.5)):
            peer = FakeSerial((b"*" + POWER_REPLY) * 2)
            with patch("serial.Serial", return_value=peer) as factory, \
                 patch("hello_fpga.operations.time.monotonic", return_value=0), \
                 patch("hello_fpga.operations.time.sleep", side_effect=[None, KeyboardInterrupt()]) as sleep, \
                 patch("hello_fpga.cli.datetime") as clock:
                clock.now.return_value.astimezone.return_value = datetime.fromisoformat(STAMP)
                result = self.runner.invoke(app, ["power", "--watch", "-p", "fake"] + args)
            self.assertEqual(result.exit_code, 130, result.exception)
            self.assertEqual(result.stdout, (WATCH_LINE + "\n") * 2)
            self.assertEqual(result.stderr, "")
            self.assertEqual([call.args[0] for call in sleep.call_args_list], [interval, interval])
            factory.assert_called_once()
            self.assertTrue(peer.closed)

    def test_tty_refresh_and_cleanup_on_error_or_interrupt(self):
        for bad_reply in (False, True):
            peer = FakeSerial(b"*" + POWER_REPLY + (b"?" if bad_reply else b"*" + POWER_REPLY))
            out, err = TerminalStream(), io.StringIO()
            with patch("serial.Serial", return_value=peer), \
                 patch("hello_fpga.operations.time.monotonic", return_value=0), \
                 patch("hello_fpga.operations.time.sleep", side_effect=[None, KeyboardInterrupt()]), \
                 patch("hello_fpga.cli.datetime") as clock, \
                 redirect_stdout(out), redirect_stderr(err):
                clock.now.return_value.astimezone.return_value = datetime.fromisoformat(STAMP)
                with self.assertRaises(typer.Exit) as exit:
                    power(port="fake", timeout=1, watch=True, interval=2)
            self.assertEqual(exit.exception.exit_code, 3 if bad_reply else 130)
            self.assertEqual(out.getvalue(), ("\r\x1b[K" + WATCH_LINE) * (1 if bad_reply else 2) + "\n")
            self.assertGreaterEqual(out.flushes, 2 if bad_reply else 3)
            self.assertTrue(peer.closed)
            if bad_reply:
                self.assertIn("Protocol error:", err.getvalue())
            else:
                self.assertEqual(err.getvalue(), "")

    def test_render_failure_closes_suspended_iterator(self):
        peer = FakeSerial(b"*" + POWER_REPLY)
        with patch("serial.Serial", return_value=peer), \
             patch("hello_fpga.cli.datetime") as clock:
            clock.now.side_effect = RuntimeError("render failed")
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                power(port="fake", timeout=1, watch=True, interval=2)
        self.assertTrue(peer.closed)
