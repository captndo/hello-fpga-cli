"""Stock PIC32 '&' UART bridge, independent of FPGA application protocols."""

from .errors import ProtocolError
from .transport import SerialTransport

BRIDGE_COMMAND = b"&"
BRIDGE_EXIT_SEQUENCE = b"%ExIt3"
DEFAULT_BRIDGE_TIMEOUT = 2.0


class HelloFpgaBridge:
    """Control one bridge session on a caller-owned, already-open transport.

    The host-to-FPGA stream reserves '%Ex' followed by any three bytes, even
    across writes. Payload is neither checked nor escaped. ``active`` is local
    session belief, not physical state discovery. Discard corrupted sessions;
    this API does not retry or resynchronize.
    """

    def __init__(self, transport: SerialTransport) -> None:
        self.transport = transport
        self._active = False
        self._exit_attempted = False

    @property
    def active(self) -> bool:
        return self._active

    def _require_active(self) -> None:
        if not self.active:
            raise ProtocolError("bridge is not active")

    def enter(self) -> None:
        """Send '&' and become active only after validating its echo."""
        if self.active:
            raise ProtocolError("bridge is already active")
        self.transport.write(BRIDGE_COMMAND)
        echo = self.transport.read_exact(1, "bridge-command echo")
        if echo != BRIDGE_COMMAND:
            raise ProtocolError(
                f"bridge-command echo: expected 0x26, received 0x{echo[0]:02X}"
            )
        self._active = True
        self._exit_attempted = False

    def write(self, data: bytes) -> None:
        """Forward payload unchanged; callers must avoid the reserved prefix."""
        self._require_active()
        self.transport.write(data)

    def read(self, size: int = 4096) -> bytes:
        """Read up to size bytes once; b'' indicates no data before timeout."""
        self._require_active()
        return self.transport.read_some(size)

    def read_exact(self, size: int, label: str = "bridge data") -> bytes:
        """Collect exactly size bytes with the transport's idle timeout."""
        self._require_active()
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError("size must be a nonnegative integer")
        return self.transport.read_exact(size, label)

    def exit(self) -> None:
        """Send the canonical escape once; no exit acknowledgement exists."""
        self._require_active()
        if self._exit_attempted:
            raise ProtocolError("bridge exit was already attempted; discard this session")
        self._exit_attempted = True
        self.transport.write(BRIDGE_EXIT_SEQUENCE)
        self._active = False

    def _exit_if_active(self) -> None:
        """Context cleanup must not repeat a failed manual exit attempt."""
        if self.active and not self._exit_attempted:
            self.exit()
