import io
import os
import select
import tempfile
import termios
import unittest
from contextlib import ExitStack
from unittest.mock import Mock, patch

from hello_fpga.bridge_console import console_fds, forward_streams, terminal_input


class BridgeConsoleTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

    def pipe(self):
        read_fd, write_fd = os.pipe()
        reader = self.stack.enter_context(os.fdopen(read_fd, "rb", buffering=0))
        writer = self.stack.enter_context(os.fdopen(write_fd, "wb", buffering=0))
        return reader, writer

    def terminal(self):
        master, slave = os.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        return master, slave

    def test_binary_forwarding_and_delayed_replies_after_eof(self):
        source, producer = self.pipe()
        sink, output = self.pipe()
        payload = b"\x00\xff*$#&!@%ExXYZ"
        producer.write(payload)
        producer.close()
        bridge = Mock()
        bridge.read.side_effect = [b"", b"\x00\xff", b"a\r\n", KeyboardInterrupt()]
        with patch("hello_fpga.bridge_console.select.select", wraps=select.select) as ready:
            with self.assertRaises(KeyboardInterrupt):
                forward_streams(bridge, stdin_fd=source.fileno(), stdout_fd=output.fileno())
        bridge.write.assert_called_once_with(payload)
        self.assertEqual(ready.call_count, 2)  # Payload, then EOF; never poll EOF again.
        self.assertEqual(bridge.read.call_count, 4)
        self.assertEqual(sink.read(100), b"\x00\xffa\r\n")

    def test_regular_file_input_and_output_with_bounded_chunks(self):
        source = self.stack.enter_context(tempfile.TemporaryFile())
        sink = self.stack.enter_context(tempfile.TemporaryFile())
        payload = bytes(range(256)) * 20
        source.write(payload)
        source.seek(0)
        bridge = Mock()
        bridge.read.side_effect = [b"response", b"", b"", KeyboardInterrupt()]
        with self.assertRaises(KeyboardInterrupt):
            forward_streams(bridge, stdin_fd=source.fileno(), stdout_fd=sink.fileno())
        self.assertEqual([len(call.args[0]) for call in bridge.write.call_args_list], [4096, 1024])
        self.assertEqual(b"".join(call.args[0] for call in bridge.write.call_args_list), payload)
        sink.seek(0)
        self.assertEqual(sink.read(), b"response")

    def test_partial_stdout_writes_are_completed(self):
        source, producer = self.pipe()
        sink, output = self.pipe()
        producer.close()
        bridge = Mock()
        reply = b"\x00\xffabcdef"
        bridge.read.side_effect = [reply, KeyboardInterrupt()]
        original_write = os.write
        def partial(fd, data):
            return original_write(fd, data[:2])
        with patch("hello_fpga.bridge_console.os.write", side_effect=partial) as write:
            with self.assertRaises(KeyboardInterrupt):
                forward_streams(bridge, stdin_fd=source.fileno(), stdout_fd=output.fileno())
        self.assertEqual(write.call_count, 4)
        self.assertEqual(sink.read(100), reply)
        bridge.write.assert_not_called()

    def test_closed_stdout_pipe_and_zero_progress_fail(self):
        source, producer = self.pipe()
        sink, output = self.pipe()
        producer.close()
        sink.close()
        bridge = Mock()
        bridge.read.return_value = b"reply"
        with self.assertRaises(BrokenPipeError):
            forward_streams(bridge, stdin_fd=source.fileno(), stdout_fd=output.fileno())
        with patch("hello_fpga.bridge_console.os.write", return_value=0):
            with self.assertRaisesRegex(OSError, "no progress"):
                forward_streams(bridge, stdin_fd=source.fileno(), stdout_fd=output.fileno())

    def test_descriptor_validation_with_real_pipes_and_files(self):
        source, producer = self.pipe()
        sink, output = self.pipe()
        for stdin, stdout in (
            (source, output),
            (self.stack.enter_context(tempfile.TemporaryFile()),
             self.stack.enter_context(tempfile.TemporaryFile())),
        ):
            with patch("hello_fpga.bridge_console.sys.stdin", stdin), \
                 patch("hello_fpga.bridge_console.sys.stdout", stdout):
                self.assertEqual(console_fds(), (stdin.fileno(), stdout.fileno()))
        for stdin, stdout in ((producer, output), (source, sink), (io.StringIO(), output)):
            with patch("hello_fpga.bridge_console.sys.stdin", stdin), \
                 patch("hello_fpga.bridge_console.sys.stdout", stdout):
                with self.assertRaisesRegex(OSError, "requires readable stdin"):
                    console_fds()
        closed_fd = producer.fileno()
        producer.close()
        with patch("hello_fpga.bridge_console.sys.stdin", Mock(fileno=lambda: closed_fd)), \
             patch("hello_fpga.bridge_console.sys.stdout", output):
            with self.assertRaises(OSError):
                console_fds()

    def test_terminal_cbreak_has_immediate_input_and_restores_settings(self):
        master, slave = self.terminal()
        saved = termios.tcgetattr(slave)
        with terminal_input(slave):
            mode = termios.tcgetattr(slave)
            self.assertFalse(mode[3] & termios.ICANON)
            self.assertFalse(mode[3] & termios.ECHO)
            self.assertTrue(mode[3] & termios.ISIG)
            self.assertEqual(mode[:3], saved[:3])  # Preserve I/O translations.
            os.write(master, b"h")
            self.assertTrue(select.select([slave], [], [], 1)[0])
            self.assertEqual(os.read(slave, 1), b"h")
            self.assertEqual(select.select([master], [], [], 0)[0], [])  # No local echo.
        self.assertEqual(termios.tcgetattr(slave), saved)

    def test_terminal_restores_on_errors_and_interruption(self):
        _, slave = self.terminal()
        saved = termios.tcgetattr(slave)
        for primary in (KeyboardInterrupt(), OSError("I/O failed"), ValueError("body failed")):
            with self.assertRaises(type(primary)) as error:
                with terminal_input(slave):
                    raise primary
            self.assertIs(error.exception, primary)
            self.assertEqual(termios.tcgetattr(slave), saved)

    def test_terminal_setup_failure_restores_already_changed_settings(self):
        _, slave = self.terminal()
        saved = termios.tcgetattr(slave)
        original_set = termios.tcsetattr
        calls = []
        def fail_once(fd, when, attrs):
            original_set(fd, when, attrs)
            calls.append(when)
            if len(calls) == 1:
                raise OSError("setup failed")
        with patch("hello_fpga.bridge_console.termios.tcsetattr", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "setup failed"):
                with terminal_input(slave):
                    self.fail("setup failure must not yield")
        self.assertEqual(calls, [termios.TCSANOW, termios.TCSANOW])
        self.assertEqual(termios.tcgetattr(slave), saved)

    def test_terminal_restore_failure_preserves_primary(self):
        _, slave = self.terminal()
        primary, cleanup = KeyboardInterrupt(), OSError("restore failed")
        with patch("hello_fpga.bridge_console.termios.tcsetattr", side_effect=[None, cleanup]):
            with self.assertRaises(KeyboardInterrupt) as error:
                with terminal_input(slave):
                    raise primary
        self.assertIs(error.exception, primary)
        self.assertIs(error.exception.__cause__, cleanup)

    def test_redirected_stdin_never_changes_terminal_settings(self):
        source, _ = self.pipe()
        with patch("hello_fpga.bridge_console.termios.tcgetattr") as get, \
             patch("hello_fpga.bridge_console.termios.tcsetattr") as set_attrs:
            with terminal_input(source.fileno()):
                pass
        get.assert_not_called()
        set_attrs.assert_not_called()
