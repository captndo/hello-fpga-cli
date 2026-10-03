import os
import tempfile
import termios
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import serial
from typer.testing import CliRunner

from hello_fpga_cli import app
from helpers import FakeSerial


class BridgeCliTests(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.source = self.stack.enter_context(tempfile.TemporaryFile())
        self.sink = self.stack.enter_context(tempfile.TemporaryFile())

    def fds(self):
        return self.source.fileno(), self.sink.fileno()

    def test_help_and_invalid_arguments_before_open(self):
        self.assertIn("bridge", self.runner.invoke(app, ["--help"]).stdout)
        result = self.runner.invoke(app, ["bridge", "--help"])
        self.assertEqual(result.exit_code, 0)
        for option in ("--port", "-p", "--timeout", "--verbose", "-v"):
            self.assertIn(option, result.stdout)
        with patch("serial.Serial") as factory, patch("hello_fpga.cli.console_fds") as fds:
            for tail in (["design.dat"], *(["--timeout", value] for value in ("0", "-1", "0.5", "nan", "inf", "-inf"))):
                result = self.runner.invoke(app, ["bridge"] + tail)
                self.assertEqual(result.exit_code, 2)
            factory.assert_not_called()
            fds.assert_not_called()

    def test_binary_output_port_aliases_verbose_and_open_bridge_reuse(self):
        from hello_fpga.operations import open_bridge
        for args, port, verbose in (
            ([], "/dev/ttyACM0", False), (["-p", "fake", "-v"], "fake", True),
            (["--port", "fake", "--verbose"], "fake", True),
        ):
            self.source.seek(0)
            self.source.truncate()
            self.source.write(b"\x00\xffh")
            self.source.seek(0)
            self.sink.seek(0)
            self.sink.truncate()
            peer = FakeSerial(chunks=[b"&", b"", b"\x00\xff", b"a"], fragment_size=100)
            original_read = peer.read
            def read(size):
                if not peer.chunks:
                    raise KeyboardInterrupt()
                return original_read(size)
            with patch("serial.Serial", return_value=peer) as factory, \
                 patch("hello_fpga.cli.console_fds", side_effect=self.fds), \
                 patch("hello_fpga.cli.open_bridge", wraps=open_bridge) as opener, \
                 patch.object(peer, "read", side_effect=read):
                result = self.runner.invoke(app, ["bridge"] + args)
            self.assertEqual(result.exit_code, 130, result.exception)
            self.assertEqual(result.stdout, "")
            self.sink.seek(0)
            self.assertEqual(self.sink.read(), b"\x00\xffa")
            self.assertEqual(result.stderr, (
                "TX: 26\nRX: 26\nTX: 00 ff 68\nRX: 00 ff\nRX: 61\n"
                "TX: 25 45 78 49 74 33\n" if verbose else ""
            ))
            self.assertEqual(factory.call_args.args, (port, 460800))
            self.assertEqual(peer.writes, [b"&", b"\x00\xffh", b"%ExIt3"])
            self.assertTrue(peer.closed)
            opener.assert_called_once()
            self.assertEqual(opener.call_args.kwargs["timeout"], 2)
            self.assertTrue(opener.call_args.kwargs["auto_exit"])

    def test_descriptor_failure_before_serial_open(self):
        with patch("hello_fpga.cli.console_fds", side_effect=OSError("bad console")), \
             patch("serial.Serial") as factory:
            result = self.runner.invoke(app, ["bridge"])
        self.assertEqual(result.exit_code, 1)
        self.assertEqual(result.stderr, "Console I/O error: bad console\n")
        factory.assert_not_called()

    def test_entry_errors_and_timeout_default_override(self):
        with patch("hello_fpga.cli.console_fds", side_effect=self.fds):
            with patch("serial.Serial", side_effect=serial.SerialException("offline")):
                result = self.runner.invoke(app, ["bridge"])
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.stderr, "Serial error: offline\n")
            peer = FakeSerial(b"?")
            with patch("serial.Serial", return_value=peer):
                result = self.runner.invoke(app, ["bridge"])
            self.assertEqual(result.exit_code, 3)
            self.assertIn("bridge-command echo: expected 0x26, received 0x3F", result.stderr)
            self.assertEqual(peer.writes, [b"&"])
            self.assertTrue(peer.closed)
            for args, timeout in (([], 2), (["--timeout", "3"], 3)):
                peer = FakeSerial()
                with patch("serial.Serial", return_value=peer), \
                     patch("hello_fpga.transport.time.monotonic", side_effect=[0, timeout + 1]):
                    result = self.runner.invoke(app, ["bridge"] + args)
                self.assertEqual(result.exit_code, 3)
                self.assertIn(f"Timed out after {timeout}s", result.stderr)
                self.assertTrue(peer.closed)

    def test_forwarding_error_codes_and_cleanup(self):
        for primary, code, message in (
            (serial.SerialException("offline"), 2, "Serial error: offline\n"),
            (OSError("read failed"), 1, "Console I/O error: read failed\n"),
            (termios.error("TTY failed"), 1, "Console I/O error: TTY failed\n"),
            (BrokenPipeError(), 141, ""), (KeyboardInterrupt(), 130, ""),
        ):
            peer = FakeSerial(b"&")
            with patch("hello_fpga.cli.console_fds", side_effect=self.fds), \
                 patch("serial.Serial", return_value=peer), \
                 patch("hello_fpga.cli.forward_streams", side_effect=primary):
                result = self.runner.invoke(app, ["bridge"])
            self.assertEqual(result.exit_code, code, result.exception)
            self.assertEqual(result.stdout, "")
            self.assertEqual(result.stderr, message)
            self.assertEqual(peer.writes, [b"&", b"%ExIt3"])
            self.assertTrue(peer.closed)

    def test_real_closed_stdout_pipe_exits_141(self):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        self.addCleanup(os.close, write_fd)
        peer = FakeSerial(b"&reply")
        with patch("hello_fpga.cli.console_fds", return_value=(self.source.fileno(), write_fd)), \
             patch("serial.Serial", return_value=peer):
            result = self.runner.invoke(app, ["bridge"])
        self.assertEqual(result.exit_code, 141, result.exception)
        self.assertEqual(result.stdout + result.stderr, "")
        self.assertEqual(peer.writes, [b"&", b"%ExIt3"])
        self.assertTrue(peer.closed)

    def test_terminal_guidance_and_restoration_before_exit(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        saved = termios.tcgetattr(slave)
        peer = FakeSerial(b"&")
        original_write = peer.write
        def write(data):
            if data == b"%ExIt3":
                self.assertEqual(termios.tcgetattr(slave), saved)
            return original_write(data)
        def forward(*args, **kwargs):
            mode = termios.tcgetattr(slave)
            self.assertFalse(mode[3] & (termios.ICANON | termios.ECHO))
            self.assertTrue(mode[3] & termios.ISIG)
            raise KeyboardInterrupt()
        with patch("hello_fpga.cli.console_fds", return_value=(slave, self.sink.fileno())), \
             patch("serial.Serial", return_value=peer), \
             patch.object(peer, "write", side_effect=write), \
             patch("hello_fpga.cli.forward_streams", side_effect=forward):
            result = self.runner.invoke(app, ["bridge"])
        self.assertEqual(result.exit_code, 130, result.exception)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "Bridge active. Ctrl-C to exit.\n")
        self.assertEqual(termios.tcgetattr(slave), saved)
        self.assertTrue(peer.closed)

    def test_failed_cleanup_preserves_error_and_attempts_exit_once(self):
        for primary, code in ((KeyboardInterrupt(), 130), (BrokenPipeError(), 141), (None, 2)):
            peer = FakeSerial(b"&")
            original_write = peer.write
            exits = []
            def write(data):
                if data == b"%ExIt3":
                    exits.append(data)
                    raise serial.SerialException("exit failed")
                return original_write(data)
            with patch("hello_fpga.cli.console_fds", side_effect=self.fds), \
                 patch("serial.Serial", return_value=peer), \
                 patch.object(peer, "write", side_effect=write), \
                 patch("hello_fpga.cli.forward_streams", side_effect=primary):
                result = self.runner.invoke(app, ["bridge"])
            self.assertEqual(result.exit_code, code, result.exception)
            self.assertEqual(exits, [b"%ExIt3"])
            self.assertEqual(result.stderr, "Serial error: exit failed\n" if primary is None else "")
            self.assertTrue(peer.closed)
