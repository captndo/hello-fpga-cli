import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import serial

from hello_fpga.errors import DatFileError, DeviceTimeout, ProtocolError
from hello_fpga.operations import execute_action, load_dat
from hello_fpga.protocol import ACTION_INFO
from helpers import FakeSerial, HANDSHAKE, completion, dat_request


class OperationsTests(unittest.TestCase):
    def test_dat_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "design.dat"
            for missing in (path, Path(directory)):
                with self.assertRaisesRegex(DatFileError, "DAT file does not exist"):
                    load_dat(missing)
            path.write_bytes(b"")
            with self.assertRaisesRegex(DatFileError, "DAT file is empty"):
                load_dat(path)
            path.write_bytes(b"abc")
            self.assertEqual(load_dat(path), b"abc")

    def test_dat_read_error_propagates(self):
        with patch.object(Path, "is_file", return_value=True):
            with patch.object(Path, "read_bytes", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    load_dat(Path("design.dat"))

    def test_execution_is_silent_and_returns_failures(self):
        for code in (0, -12, 125, 255):
            with self.subTest(code=code):
                peer = FakeSerial(HANDSHAKE + dat_request(2, 4) + b"R\x0aR\x0aT" + completion(code))
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("serial.Serial", return_value=peer):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        result = execute_action(port="fake", dat=b"abc", action=ACTION_INFO)
                self.assertEqual(result.exit_code, code)
                self.assertTrue(result.saw_end)
                self.assertTrue(peer.closed)
                self.assertEqual(stdout.getvalue(), "")
                self.assertEqual(stderr.getvalue(), "")

    def test_protocol_failure_propagates_and_closes(self):
        peer = FakeSerial(HANDSHAKE + b"?")
        with patch("serial.Serial", return_value=peer):
            with self.assertRaises(ProtocolError):
                execute_action(port="fake", dat=b"abc", action=ACTION_INFO)
        self.assertTrue(peer.closed)

    def test_timeout_propagates_and_closes(self):
        peer = FakeSerial(HANDSHAKE)
        with patch("serial.Serial", return_value=peer):
            with patch("hello_fpga.transport.time.monotonic", side_effect=[0] * 13 + [61]):
                with self.assertRaisesRegex(DeviceTimeout, "Timed out after 60s"):
                    execute_action(port="fake", dat=b"abc", action=ACTION_INFO)
        self.assertTrue(peer.closed)

    def test_serial_failure_propagates(self):
        with patch("serial.Serial", side_effect=serial.SerialException("offline")):
            with self.assertRaisesRegex(serial.SerialException, "offline"):
                execute_action(port="fake", dat=b"abc", action=ACTION_INFO)

    def test_read_and_write_errors_close_connection(self):
        for method in ("read", "write"):
            with self.subTest(method=method):
                peer = FakeSerial(HANDSHAKE)
                with patch.object(peer, method, side_effect=serial.SerialException("offline")):
                    with patch("serial.Serial", return_value=peer):
                        with self.assertRaises(serial.SerialException):
                            execute_action(port="fake", dat=b"abc", action=ACTION_INFO)
                self.assertTrue(peer.closed)

    def test_core_runs_without_importing_typer(self):
        project = Path(__file__).resolve().parents[1]
        script = '''
import importlib.abc
import sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + "/tests"]
class BlockTyper(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == "typer" or fullname.startswith("typer."):
            raise ImportError("The core must not import Typer")
sys.meta_path.insert(0, BlockTyper())
from hello_fpga.operations import execute_action
from hello_fpga.protocol import ACTION_INFO
from helpers import FakeSerial, HANDSHAKE, completion
import serial
peer = FakeSerial(HANDSHAKE + completion())
serial.Serial = lambda *args, **kwargs: peer
assert execute_action(port="fake", dat=b"abc", action=ACTION_INFO).exit_code == 0
assert peer.closed
assert "typer" not in sys.modules
'''
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(project)],
            capture_output=True, text=True, check=False, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
