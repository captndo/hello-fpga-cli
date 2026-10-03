"""Programming, power, FlashFreeze, and bridge lifetimes without a terminal."""

import math
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator, Optional

from .bridge import DEFAULT_BRIDGE_TIMEOUT, HelloFpgaBridge
from .errors import DatFileError
from .freeze import DEFAULT_FREEZE_TIMEOUT, FlashFreezeController
from .power import (
    DEFAULT_POWER_INTERVAL,
    DEFAULT_POWER_TIMEOUT,
    PowerReading,
    read_power,
)
from .protocol import DirectCHost, ProtocolObserver, RunResult
from .transport import DEFAULT_TIMEOUT, SerialTransport, TrafficCallback


@contextmanager
def open_bridge(
    *,
    port: str,
    timeout: float = DEFAULT_BRIDGE_TIMEOUT,
    on_tx: Optional[TrafficCallback] = None,
    on_rx: Optional[TrafficCallback] = None,
    auto_exit: bool = True,
) -> Generator[HelloFpgaBridge, None, None]:
    """Own one '&' bridge connection; optionally exit before closing.

    Entry and exact reads use timeout (finite, at least one second). Stream
    reads use the serial read timeout. Automatic exit is attempted on normal
    completion, errors, and interruption, unless manually exited or already
    attempted. With auto_exit=False, the caller must arrange device recovery.
    The body exception is preserved if exit also fails, with failure chained.
    """
    if not math.isfinite(timeout) or timeout < 1:
        raise ValueError("bridge timeout must be finite and at least 1 second")
    with SerialTransport(port, timeout=timeout, on_tx=on_tx, on_rx=on_rx) as transport:
        bridge = HelloFpgaBridge(transport)
        bridge.enter()
        try:
            yield bridge
        except BaseException as primary:
            try:
                if auto_exit:
                    bridge._exit_if_active()
            except BaseException as cleanup_error:
                raise primary from cleanup_error
            raise
        else:
            if auto_exit:
                bridge._exit_if_active()


def _validate_freeze_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout < 1:
        raise ValueError("FlashFreeze timeout must be finite and at least 1 second")


def execute_freeze_enter(
    *,
    port: str,
    timeout: float = DEFAULT_FREEZE_TIMEOUT,
    on_tx: Optional[TrafficCallback] = None,
    on_rx: Optional[TrafficCallback] = None,
) -> None:
    """Enter FlashFreeze, closing the connection on success or failure."""
    _validate_freeze_timeout(timeout)
    with SerialTransport(port, timeout=timeout, on_tx=on_tx, on_rx=on_rx) as transport:
        FlashFreezeController(transport).flashfreeze_enter()


def execute_freeze_exit(
    *,
    port: str,
    timeout: float = DEFAULT_FREEZE_TIMEOUT,
    on_tx: Optional[TrafficCallback] = None,
    on_rx: Optional[TrafficCallback] = None,
) -> int:
    """Exit FlashFreeze and return raw ticks, always closing the connection."""
    _validate_freeze_timeout(timeout)
    with SerialTransport(port, timeout=timeout, on_tx=on_tx, on_rx=on_rx) as transport:
        return FlashFreezeController(transport).flashfreeze_exit()


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


def _validate_power_options(timeout: float, interval: float = DEFAULT_POWER_INTERVAL) -> None:
    if not math.isfinite(timeout) or timeout < 1:
        raise ValueError("power timeout must be finite and at least 1 second")
    if not math.isfinite(interval) or interval <= 0:
        raise ValueError("power interval must be finite and positive")


def execute_power(
    *, port: str, timeout: float = DEFAULT_POWER_TIMEOUT
) -> PowerReading:
    """Read one sample, closing the connection on success or failure."""
    _validate_power_options(timeout)
    with SerialTransport(port, timeout=timeout) as transport:
        return read_power(transport)


def iter_power(
    *,
    port: str,
    timeout: float = DEFAULT_POWER_TIMEOUT,
    interval: float = DEFAULT_POWER_INTERVAL,
) -> Generator[PowerReading, None, None]:
    """Yield samples using one connection, starting immediately.

    The interval includes I/O and consumer time. Overruns do not accumulate
    catch-up requests. Close the iterator when stopping early, for example
    using contextlib.closing, to release its serial connection.
    """
    _validate_power_options(timeout, interval)
    with SerialTransport(port, timeout=timeout) as transport:
        while True:
            started = time.monotonic()
            yield read_power(transport)
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                time.sleep(remaining)
