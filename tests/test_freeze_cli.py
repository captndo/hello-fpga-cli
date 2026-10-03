import unittest
from unittest.mock import patch

import serial
from typer.testing import CliRunner

from hello_fpga_cli import app
from helpers import FakeSerial


class FreezeCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

    def test_help(self):
        self.assertIn("freeze", self.runner.invoke(app, ["--help"]).stdout)
        result = self.runner.invoke(app, ["freeze", "--help"])
        self.assertEqual(result.exit_code, 0)
        for command in ("enter", "exit"):
            self.assertIn(command, result.stdout)
            help_result = self.runner.invoke(app, ["freeze", command, "--help"])
            self.assertEqual(help_result.exit_code, 0)
            for option in ("--port", "-p", "--timeout", "--verbose", "-v"):
                self.assertIn(option, help_result.stdout)
        self.assertEqual(self.runner.invoke(app, ["freeze"]).exit_code, 2)

    def test_output_port_and_verbose_aliases(self):
        for command, reply, output, trace, tx in (
            ("enter", b"$", "FlashFreeze entered\n", "TX: 24\nRX: 24\n", b"$"),
            ("exit", b"@\x0d\x01\x00\x00",
             "FlashFreeze exited\nWake-up time: 269 ticks\n",
             "TX: 40\nRX: 40\nRX: 0d 01 00 00\n", b"@"),
        ):
            for args, port, verbose in (
                ([], "/dev/ttyACM0", False),
                (["-p", "fake", "-v"], "fake", True),
                (["--port", "fake", "--verbose"], "fake", True),
            ):
                with self.subTest(command=command, args=args):
                    peer = FakeSerial(reply, fragment_size=4)
                    with patch("serial.Serial", return_value=peer) as factory:
                        result = self.runner.invoke(app, ["freeze", command] + args)
                    self.assertEqual(result.exit_code, 0, result.exception)
                    self.assertEqual(result.stdout, output)
                    self.assertEqual(result.stderr, trace if verbose else "")
                    self.assertEqual(factory.call_args.args, (port, 460800))
                    self.assertEqual(peer.writes, [tx])
                    self.assertTrue(peer.closed)

    def test_invalid_arguments_before_open(self):
        with patch("serial.Serial") as factory:
            for command in ("enter", "exit"):
                for value in ("0", "-1", "0.5", "nan", "inf", "-inf"):
                    result = self.runner.invoke(app, ["freeze", command, "--timeout", value])
                    self.assertEqual(result.exit_code, 2)
                result = self.runner.invoke(app, ["freeze", command, "design.dat"])
                self.assertEqual(result.exit_code, 2)
            factory.assert_not_called()

    def test_default_timeout_override_and_partial_trace(self):
        for args, timeout in (([], 2), (["--timeout", "3"], 3)):
            for command in ("enter", "exit"):
                peer = FakeSerial()
                with patch("serial.Serial", return_value=peer), \
                     patch("hello_fpga.transport.time.monotonic", side_effect=[0, timeout + 1]):
                    result = self.runner.invoke(app, ["freeze", command] + args)
                self.assertEqual(result.exit_code, 3)
                self.assertIn(f"Timed out after {timeout}s", result.stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(peer.closed)
        peer = FakeSerial(b"@\x0d\x01", fragment_size=1)
        with patch("serial.Serial", return_value=peer), \
             patch("hello_fpga.transport.time.monotonic", side_effect=[0, 0, 0, 0, 0, 3]):
            result = self.runner.invoke(app, ["freeze", "exit", "-v"])
        self.assertEqual(result.exit_code, 3)
        self.assertTrue(result.stderr.startswith("TX: 40\nRX: 40\nRX: 0d\nRX: 01\nProtocol error:"))
        self.assertIn("received 2/4 bytes", result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertTrue(peer.closed)

    def test_error_exit_codes_and_cleanup(self):
        for command in ("enter", "exit"):
            with patch("serial.Serial", side_effect=serial.SerialException("no port")):
                result = self.runner.invoke(app, ["freeze", command])
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.stderr, "Serial error: no port\n")
            for error, code, stderr in (
                (serial.SerialException("offline"), 2, "Serial error: offline\n"),
                (KeyboardInterrupt(), 130, ""),
            ):
                peer = FakeSerial()
                with patch("serial.Serial", return_value=peer), \
                     patch.object(peer, "read", side_effect=error):
                    result = self.runner.invoke(app, ["freeze", command])
                self.assertEqual(result.exit_code, code)
                self.assertEqual(result.stderr, stderr)
                self.assertEqual(result.stdout, "")
                self.assertTrue(peer.closed)
            peer = FakeSerial(b"?")
            with patch("serial.Serial", return_value=peer):
                result = self.runner.invoke(app, ["freeze", command, "-v"])
            self.assertEqual(result.exit_code, 3)
            self.assertIn("RX: 3f\nProtocol error:", result.stderr)
            self.assertIn("received 0x3F", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertTrue(peer.closed)
