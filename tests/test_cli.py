import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import serial
from typer.testing import CliRunner

from hello_fpga_cli import app
from helpers import FakeSerial, HANDSHAKE, completion, dat_request, text_packet


class CliTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dat = Path(self.directory.name) / "design.dat"
        self.dat.write_bytes(b"abc")
        self.runner = CliRunner()

    def invoke(self, args, stream=None, **kwargs):
        peer = FakeSerial(HANDSHAKE + (completion() if stream is None else stream))
        with patch("serial.Serial", return_value=peer) as factory:
            result = self.runner.invoke(app, args, **kwargs)
        return result, peer, factory

    def test_help_and_no_args(self):
        result = self.runner.invoke(app, ["--help"])
        self.assertEqual(result.exit_code, 0)
        for name in ("info", "idcode", "program", "verify", "erase", "authenticate", "digest"):
            self.assertIn(name, result.stdout)
            result_command = self.runner.invoke(app, [name, "--help"])
            self.assertEqual(result_command.exit_code, 0)
            for option in ("--port", "-p", "--timeout", "--verbose", "-v"):
                self.assertIn(option, result_command.stdout)
        self.assertEqual(self.runner.invoke(app, []).exit_code, 2)

    def test_commands_defaults_and_output(self):
        for command, selector in [("info", b"1"), ("idcode", b"2"), ("erase", b"3"), ("program", b"4"), ("verify", b"5"), ("authenticate", b"6"), ("digest", b"7")]:
            with self.subTest(command=command):
                args = [command, str(self.dat)] + (["--yes"] if command == "erase" else [])
                result, peer, factory = self.invoke(args)
                self.assertEqual(result.exit_code, 0, result.exception)
                self.assertEqual(peer.writes, [b"#", b"s", b"a", b"e", selector, b"5"])
                self.assertEqual(factory.call_args.args, ("/dev/ttyACM0", 460800))
                self.assertEqual(result.stdout, "Exit code = 0\nEnd DirectC Demo\n\nDirectC completed successfully.\n")
                self.assertEqual(result.stderr, f"Port:      /dev/ttyACM0\nBaud:      460800\nDAT:       {self.dat}\nDAT size:  3 bytes\nOperation: {command}\n\n")
                self.assertTrue(peer.closed)

    def test_option_aliases_verbose_and_progress(self):
        stream = dat_request(2, 4) + b"R\x0aR\x0aR\x64T" + completion()
        for verbose, options in [(False, ["-p", "fake"]), (True, ["-p", "fake", "-v"]), (True, ["--port", "fake", "--verbose"])]:
            with self.subTest(options=options):
                result, peer, factory = self.invoke(["info", str(self.dat), "--timeout", "12"] + options, stream)
                self.assertEqual(result.exit_code, 0, result.exception)
                self.assertEqual(factory.call_args.args[0], "fake")
                preamble = f"Port:      fake\nBaud:      460800\nDAT:       {self.dat}\nDAT size:  3 bytes\nOperation: info\n\n"
                expected = preamble
                if verbose:
                    expected += "[D] offset=0x00000002 requested=4 sent=1\n"
                expected += "\rProgress:  10%\rProgress: 100%"
                if verbose:
                    expected += "\n[T] transition marker\n"
                expected += "\n"
                self.assertEqual(result.stderr, expected)

    def test_invalid_arguments_do_not_open_serial(self):
        for command in ("info", "idcode", "program", "verify", "erase", "authenticate", "digest"):
            for tail in ([], [str(self.dat.parent / "missing.dat")], [str(self.dat.parent)], [str(self.dat), "--timeout", "0.5"]):
                with self.subTest(command=command, tail=tail):
                    result, _, factory = self.invoke([command] + tail)
                    self.assertEqual(result.exit_code, 2)
                    factory.assert_not_called()

    def test_empty_dat_before_preamble(self):
        self.dat.write_bytes(b"")
        for command in ("info", "authenticate", "digest"):
            with self.subTest(command=command):
                result, _, factory = self.invoke([command, str(self.dat)])
                self.assertEqual(result.exit_code, 2)
                self.assertIn("DAT file is empty", result.stderr)
                self.assertNotIn("Port:", result.stderr)
                factory.assert_not_called()

    def test_erase_confirmation(self):
        result, _, factory = self.invoke(["erase", str(self.dat)], input="n\n")
        self.assertEqual(result.exit_code, 1)
        self.assertIn("This will erase FPGA programming", result.stdout)
        factory.assert_not_called()
        result, peer, _ = self.invoke(["erase", str(self.dat)], input="y\n")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(peer.writes[-2:], [b"3", b"5"])
        result, _, _ = self.invoke(["erase", str(self.dat), "-y"])
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn("Continue?", result.stdout)

    def test_serial_error(self):
        with patch("serial.Serial", side_effect=serial.SerialException("offline")):
            for command in ("info", "authenticate", "digest"):
                with self.subTest(command=command):
                    result = self.runner.invoke(app, [command, str(self.dat)])
                    self.assertEqual(result.exit_code, 2)
                    self.assertTrue(result.stderr.endswith("Serial error: offline\n"))

    def test_protocol_errors_close_serial(self):
        for command in ("info", "authenticate", "digest"):
            for stream in (b"?", b"R\x0a" + text_packet(b"End DirectC Demo")):
                with self.subTest(command=command, stream=stream):
                    result, peer, _ = self.invoke([command, str(self.dat)], stream)
                    self.assertEqual(result.exit_code, 3)
                    self.assertIn("\nProtocol error:", result.stderr)
                    if stream.startswith(b"R"):
                        self.assertIn("\rProgress:  10%\n\nProtocol error:", result.stderr)
                    self.assertTrue(peer.closed)

    def test_timeout_option_and_exit_code(self):
        # Six successful handshake reads precede the service-loop timeout.
        for command in ("info", "authenticate", "digest"):
            with self.subTest(command=command), patch("hello_fpga.transport.time.monotonic", side_effect=[0] * 13 + [13]):
                result, peer, _ = self.invoke([command, str(self.dat), "--timeout", "12"], b"")
            self.assertEqual(result.exit_code, 3)
            self.assertIn("Timed out after 12s", result.stderr)
            self.assertTrue(peer.closed)

    def test_default_timeout(self):
        for command in ("info", "authenticate", "digest"):
            with self.subTest(command=command), patch("hello_fpga.transport.time.monotonic", side_effect=[0] * 13 + [61]):
                result, peer, _ = self.invoke([command, str(self.dat)], b"")
            self.assertEqual(result.exit_code, 3)
            self.assertIn("Timed out after 60s", result.stderr)
            self.assertTrue(peer.closed)

    def test_serial_error_during_service_closes(self):
        for command in ("info", "authenticate", "digest"):
            with self.subTest(command=command):
                peer = FakeSerial(HANDSHAKE)
                original_read = peer.read
                def read(size):
                    if not peer.chunks:
                        raise serial.SerialException("offline")
                    return original_read(size)
                with patch.object(peer, "read", side_effect=read), \
                     patch("serial.Serial", return_value=peer):
                    result = self.runner.invoke(app, [command, str(self.dat)])
                self.assertEqual(result.exit_code, 2)
                self.assertTrue(result.stderr.endswith("Serial error: offline\n"))
                self.assertTrue(peer.closed)

    def test_directc_exit_mapping(self):
        for command in ("info", "authenticate", "digest"):
            for code, expected in [(1, 1), (125, 125), (-1, 4), (126, 4), (255, 4)]:
                with self.subTest(command=command, code=code):
                    result, peer, _ = self.invoke([command, str(self.dat)], completion(code))
                    self.assertEqual(result.exit_code, expected)
                    self.assertIn(f"\nDirectC failed with exit code {code}.\n", result.stderr)
                    self.assertNotIn("completed successfully", result.stdout)
                    self.assertTrue(peer.closed)

    def test_new_actions_serve_dat_and_display_fragmented_status(self):
        for command, selector, status in (
            ("authenticate", b"6", b"Encrypted-data authentication: PASS\n"),
            ("digest", b"7", b"FPGA Fabric digest verification: PASS\n"),
        ):
            for options in (["-p", "fake", "-v"], ["--port", "fake", "--verbose"]):
                with self.subTest(command=command, options=options):
                    stream = (
                        dat_request(1, 2) + text_packet(status[:-4])
                        + text_packet(status[-4:]) + b"R\x32T"
                        + text_packet(b"Exit co") + text_packet(b"de = 0\nEnd Dire")
                        + text_packet(b"ctC Demo\n")
                    )
                    with patch("typer.confirm", side_effect=AssertionError("no confirmation")):
                        result, peer, factory = self.invoke([command, str(self.dat)] + options, stream)
                    self.assertEqual(result.exit_code, 0, result.exception)
                    self.assertEqual(peer.writes, [b"#", b"s", b"a", b"e", selector, b"5", b"\x02\0\0\0", b"bc"])
                    self.assertEqual(factory.call_args.args[0], "fake")
                    self.assertEqual(result.stdout, status.decode() + "Exit code = 0\nEnd DirectC Demo\n\nDirectC completed successfully.\n")
                    self.assertIn(f"Operation: {command}\n", result.stderr)
                    self.assertIn("[D] offset=0x00000001 requested=2 sent=2\n", result.stderr)
                    self.assertIn("\rProgress:  50%\n[T] transition marker\n", result.stderr)
                    self.assertTrue(peer.closed)
