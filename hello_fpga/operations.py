"""Programming, power, FlashFreeze, and bridge lifetimes without a terminal."""

from pathlib import Path
from typing import Generator, Optional

from .errors import DatFileError
from .protocol import DirectCHost, ProtocolObserver, RunResult
from .transport import DEFAULT_TIMEOUT, SerialTransport


def load_dat(path: Path) -> bytes:
    if not path.is_file():
        raise DatFileError(f"DAT file does not exist: {path}")
    data = path.read_bytes()
    if not data:
        raise DatFileError(f"DAT file is empty: {path}")
    return data


def execute_action(
    *,
    port: str,
    dat: bytes,
    action: bytes,
    timeout: float = DEFAULT_TIMEOUT,
    observer: Optional[ProtocolObserver] = None,
) -> RunResult:
    """Run an action, returning its DirectC result or propagating I/O errors.

    Nonzero DirectC exit codes are results, not Python exceptions. The caller
    chooses how to present them. Observer methods run synchronously.
    """
    with SerialTransport(port, timeout=timeout) as transport:
        host = DirectCHost(transport, observer=observer)
        host.start_directc(action)
        return host.service(dat)
