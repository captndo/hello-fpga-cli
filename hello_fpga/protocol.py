"""
Host protocol for the Microchip Hello FPGA kit's stock PIC32 DirectC loader.

Reverse-engineered from:
- the factory PIC32 firmware
- USB captures of the official Hello FPGA GUI
- successful device-info and programming runs

Protocol:
    Host UART: 460800 8N1
    '#' enters DirectC
    "shaker" handshake
    PIC 'A' -> host ASCII action selector
    PIC 'F' -> host ASCII '5'
    PIC 'D' + <offset:u32le> + <requested:u32le>
        host responds <actual:u32le> + data
    PIC 'P' + <length:u32le> + payload
        textual DirectC status
    PIC 'R' + <u8>
        progress
    PIC 'T'
        transition marker
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass
from typing import Optional, Protocol

from .errors import ProtocolError
from .transport import SerialTransport

# Host action selector -> DirectC action observed in the factory firmware.
#
# "1" -> DirectC 1  (device info)
# "2" -> DirectC 2  (IDCODE)
# "3" -> DirectC 3  (erase)
# "4" -> DirectC 5  (program)
# "5" -> DirectC 6  (verify)
# "6" -> DirectC 7  (authenticate encrypted programming data)
# "7" -> DirectC 28 (verify programmed-device digests)
ACTION_INFO = b"1"
ACTION_IDCODE = b"2"
ACTION_ERASE = b"3"
ACTION_PROGRAM = b"4"
ACTION_VERIFY = b"5"
ACTION_AUTHENTICATE = b"6"
ACTION_VERIFY_DIGEST = b"7"

EXIT_CODE_RE = re.compile(r"Exit code\s*=\s*(-?\d+)")


class ProtocolObserver(Protocol):
    """Synchronous notifications; omitting the observer keeps the host silent.

    Text is decoded before parsing. DAT notifications follow the response write.
    Progress values are raw (including repeats). End is notified before checking
    for a missing exit code, allowing presenters to finish their progress line.
    """

    def on_text(self, text: str) -> None: ...

    def on_progress(self, value: int) -> None: ...

    def on_dat_request(self, offset: int, requested: int, sent: int) -> None: ...

    def on_transition(self) -> None: ...

    def on_end(self) -> None: ...


@dataclass
class RunResult:
    exit_code: Optional[int] = None
    saw_end: bool = False


class DirectCHost:
    """Interpret one DirectC session using an already-open transport."""

    def __init__(
        self,
        transport: SerialTransport,
        *,
        observer: Optional[ProtocolObserver] = None,
    ) -> None:
        self.transport = transport
        self.observer = observer
        self.result = RunResult()
        self.text_buffer = ""

    def expect(self, expected: bytes, label: str) -> None:
        got = self.transport.read_exact(len(expected), label)
        if got != expected:
            raise ProtocolError(
                f"{label}: expected {expected!r}, received {got!r}"
            )

    def start_directc(self, action: bytes) -> None:
        self.transport.write(b"#")
        self.expect(b"#", "echo of '#'")

        for tx, rx in ((b"s", b"h"), (b"a", b"k"), (b"e", b"r")):
            self.transport.write(tx)
            self.expect(rx, f"handshake after {tx!r}")

        self.expect(b"A", "action request")
        self.transport.write(action)

        self.expect(b"F", "DirectC option request")
        self.transport.write(b"5")

    def _handle_text(self, payload: bytes) -> None:
        text = payload.rstrip(b"\x00").decode("utf-8", errors="replace")
        if self.observer is not None:
            self.observer.on_text(text)

        # DirectC status lines can be split across multiple P packets, so keep
        # a rolling buffer instead of parsing each packet independently.
        self.text_buffer += text
        if len(self.text_buffer) > 4096:
            self.text_buffer = self.text_buffer[-4096:]

        match = EXIT_CODE_RE.search(self.text_buffer)
        if match:
            self.result.exit_code = int(match.group(1))

        if "End DirectC Demo" in self.text_buffer:
            self.result.saw_end = True

    def service(self, dat: Optional[bytes] = None) -> RunResult:
        while not self.result.saw_end:
            cmd = self.transport.read_exact(1, "protocol command")

            if cmd == b"P":
                length = struct.unpack(
                    "<I", self.transport.read_exact(4, "P payload length")
                )[0]
                payload = self.transport.read_exact(length, "P payload")
                self._handle_text(payload)

            elif cmd == b"D":
                offset, requested = struct.unpack(
                    "<II", self.transport.read_exact(8, "D request")
                )

                if dat is None:
                    raise ProtocolError(
                        f"DirectC requested DAT data at offset 0x{offset:08X}, "
                        "but this command was started without a DAT file"
                    )

                if requested > 1024:
                    raise ProtocolError(
                        f"PIC requested unexpected chunk size {requested} "
                        f"at offset 0x{offset:08X}"
                    )

                if offset > len(dat):
                    raise ProtocolError(
                        f"PIC requested offset 0x{offset:08X}, but DAT is only "
                        f"{len(dat)} bytes"
                    )

                end = min(offset + requested, len(dat))
                chunk = dat[offset:end]

                self.transport.write(struct.pack("<I", len(chunk)))
                if chunk:
                    self.transport.write(chunk)

                if self.observer is not None:
                    self.observer.on_dat_request(offset, requested, len(chunk))

            elif cmd == b"R":
                value = self.transport.read_exact(1, "R progress")[0]
                if self.observer is not None:
                    self.observer.on_progress(value)

            elif cmd == b"T":
                if self.observer is not None:
                    self.observer.on_transition()

            else:
                raise ProtocolError(
                    f"Unknown PIC protocol byte 0x{cmd[0]:02X} ({cmd!r}); "
                    "stopping rather than guessing."
                )

        if self.observer is not None:
            self.observer.on_end()

        # The official firmware prints its DirectC result before its final
        # "End DirectC Demo..." message. A missing result is a protocol failure;
        # callers decide how to present nonzero DirectC exit codes.
        if self.result.exit_code is None:
            raise ProtocolError(
                "DirectC ended without reporting an exit code"
            )

        return self.result
