"""Stock PIC32 FlashFreeze commands; the returned timer scale is unknown."""

from .errors import ProtocolError
from .transport import SerialTransport

DEFAULT_FREEZE_TIMEOUT = 2.0


class FlashFreezeController:
    """Perform FlashFreeze exchanges using an already-open transport.

    Calls do not track board state, retry, or convert raw ticks to time units.
    """

    def __init__(self, transport: SerialTransport) -> None:
        self.transport = transport

    def _command(self, command: bytes, operation: str) -> None:
        self.transport.write(command)
        echo = self.transport.read_exact(1, f"FlashFreeze {operation} echo")
        if echo != command:
            raise ProtocolError(
                f"FlashFreeze {operation} echo: expected 0x{command[0]:02X}, "
                f"received 0x{echo[0]:02X}"
            )

    def flashfreeze_enter(self) -> None:
        """Send '$' and require its echo."""
        self._command(b"$", "enter")

    def flashfreeze_exit(self) -> int:
        """Send '@', require its echo, and return unsigned little-endian ticks."""
        self._command(b"@", "exit")
        raw = self.transport.read_exact(4, "FlashFreeze exit ticks")
        return int.from_bytes(raw, "little")
