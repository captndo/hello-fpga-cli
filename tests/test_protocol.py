import io
import struct
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import Mock, patch

from hello_fpga.errors import ProtocolError
from hello_fpga.operations import execute_action
from hello_fpga.protocol import DirectCHost
from hello_fpga.transport import SerialTransport
from helpers import FakeSerial, HANDSHAKE, completion, dat_request, text_packet


class ProtocolTests(unittest.TestCase):
    def host(self, incoming):
        peer = FakeSerial(incoming)
        transport = SerialTransport("fake")
        transport.ser = peer
        host = DirectCHost(transport)
        return host, peer

    def service(self, host, dat=b"abc"):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return host.service(dat)

    def test_all_action_handshakes(self):
        for action in (b"1", b"2", b"3", b"4", b"5", b"6", b"7"):
            with self.subTest(action=action):
                host, peer = self.host(HANDSHAKE)
                host.start_directc(action)
                self.assertEqual(peer.writes, [b"#", b"s", b"a", b"e", action, b"5"])
                self.assertEqual(peer.events, [item for data in peer.writes for item in (("write", data), ("flush",))])

    def test_mismatch_at_each_handshake_step(self):
        for index in range(len(HANDSHAKE)):
            with self.subTest(index=index):
                host, _ = self.host(HANDSHAKE[:index] + b"!" + HANDSHAKE[index + 1:])
                with self.assertRaisesRegex(ProtocolError, "expected .*received"):
                    host.start_directc(b"1")

    def test_dat_responses(self):
        for offset, requested, chunk in [(0, 2, b"ab"), (2, 1024, b"c"), (3, 5, b""), (1, 0, b"")]:
            with self.subTest(offset=offset, requested=requested):
                host, peer = self.host(dat_request(offset, requested) + completion())
                result = self.service(host)
                self.assertEqual(result.exit_code, 0)
                self.assertTrue(result.saw_end)
                self.assertEqual(peer.writes, [struct.pack("<I", len(chunk))] + ([chunk] if chunk else []))

    def test_invalid_dat_requests(self):
        for dat, offset, size, message in [(None, 0, 1, "without a DAT file"), (b"abc", 0, 1025, "unexpected chunk size 1025"), (b"abc", 4, 1, "DAT is only 3 bytes")]:
            with self.subTest(message=message):
                host, peer = self.host(dat_request(offset, size))
                with self.assertRaisesRegex(ProtocolError, message):
                    self.service(host, dat)
                self.assertEqual(peer.writes, [])

    def test_split_status_and_end_markers(self):
        stream = text_packet(b"Exit co") + text_packet(b"de = -12\nEnd Dire") + text_packet(b"ctC Demo\n\x00\x00")
        host, _ = self.host(stream)
        self.assertEqual(self.service(host).exit_code, -12)

    def test_empty_and_invalid_utf8_text(self):
        host, _ = self.host(text_packet(b"") + text_packet(b"\xff\x00") + completion())
        self.service(host)
        self.assertTrue(host.text_buffer.startswith("\ufffd"))

    def test_rolling_buffer_limit(self):
        host, _ = self.host(text_packet(b"x" * 5000) + completion())
        self.service(host)
        self.assertEqual(len(host.text_buffer), 4096)

    def test_unknown_byte(self):
        host, _ = self.host(b"?")
        with self.assertRaisesRegex(ProtocolError, "Unknown PIC protocol byte 0x3F"):
            self.service(host)

    def test_end_without_exit_code(self):
        host, _ = self.host(text_packet(b"End DirectC Demo"))
        with self.assertRaisesRegex(ProtocolError, "without reporting an exit code"):
            self.service(host)

    def test_observer_order_and_raw_progress(self):
        stream = text_packet(b"hello\xff\x00") + dat_request(2, 4) + b"R\x0aR\x0aT" + completion()
        host, peer = self.host(stream)

        class Observer:
            def on_text(self, text):
                peer.events.append(("text", text))

            def on_progress(self, value):
                peer.events.append(("progress", value))

            def on_dat_request(self, offset, requested, sent):
                peer.events.append(("dat", offset, requested, sent))

            def on_transition(self):
                peer.events.append(("transition",))

            def on_end(self):
                peer.events.append(("end",))

        host.observer = Observer()
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            host.service(b"abc")
        self.assertEqual(peer.events, [
            ("text", "hello\ufffd"),
            ("write", struct.pack("<I", 1)), ("flush",),
            ("write", b"c"), ("flush",), ("dat", 2, 4, 1),
            ("progress", 10), ("progress", 10), ("transition",),
            ("text", "Exit code = 0\nEnd DirectC Demo\n"), ("end",),
        ])
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")

    def test_observer_exception_propagates_and_closes(self):
        class BrokenObserver:
            def on_text(self, text):
                raise ValueError("observer failed")

        peer = FakeSerial(HANDSHAKE + completion())
        with patch("serial.Serial", return_value=peer):
            with self.assertRaisesRegex(ValueError, "observer failed"):
                execute_action(port="fake", dat=b"abc", action=b"1", observer=BrokenObserver())
        self.assertTrue(peer.closed)

    def test_end_notification_precedes_missing_exit_error(self):
        observer = Mock()
        host, _ = self.host(text_packet(b"End DirectC Demo"))
        host.observer = observer
        with self.assertRaises(ProtocolError):
            host.service()
        observer.on_end.assert_called_once_with()
