"""Serial connection lifecycle, stream reads, and exact idle-timeout reads."""

from __future__ import annotations

import time
from typing import Callable, Optional

import serial

from .errors import DeviceTimeout

BAUD = 460800
DEFAULT_TIMEOUT = 60.0
TrafficCallback = Callable[[bytes], None]


class SerialTransport:
    """Own serial I/O with optional synchronous traffic callbacks.

    TX is notified after write and flush succeed; RX after each nonempty read,
    including partial replies. Callback exceptions propagate to the caller.
    """

    def __init__(
        self,
        port: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        on_tx: Optional[TrafficCallback] = None,
        on_rx: Optional[TrafficCallback] = None,
    ) -> None:
        self.port = port
        self.timeout = timeout
        self.on_tx = on_tx
        self.on_rx = on_rx
        self.ser: Optional[serial.Serial] = None

    def __enter__(self) -> "SerialTransport":
        self.ser = serial.Serial(
            self.port,
            BAUD,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.25,
            write_timeout=5,
        )
        try:
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ser is not None:
            try:
                self.ser.close()
            except BaseException as close_error:
                if exc is not None:
                    raise exc.with_traceback(tb) from close_error
                raise

    @property
    def serial(self) -> serial.Serial:
        if self.ser is None:
            raise RuntimeError("serial port is not open")
        return self.ser

    def read_exact(self, n: int, label: str) -> bytes:
        data = bytearray()
        deadline = time.monotonic() + self.timeout

        while len(data) < n:
            chunk = self.serial.read(n - len(data))
            if chunk:
                data.extend(chunk)
                # DirectC can legitimately go silent for >10 seconds. Each
                # received chunk resets the idle deadline, not a total deadline.
                deadline = time.monotonic() + self.timeout
                if self.on_rx is not None:
                    self.on_rx(chunk)
            elif time.monotonic() > deadline:
                raise DeviceTimeout(
                    f"Timed out after {self.timeout:.0f}s waiting for "
                    f"{label}: received {len(data)}/{n} bytes"
                )

        return bytes(data)

    def read_some(self, max_bytes: int = 4096) -> bytes:
        """Perform one bounded read, returning b'' on the serial read timeout.

        Uses the serial port's 0.25-second timeout, not the exact-read idle
        deadline. Partial data is returned unchanged and notified once.
        """
        if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
            raise ValueError("max_bytes must be a positive integer")
        chunk = self.serial.read(max_bytes)
        if chunk and self.on_rx is not None:
            self.on_rx(chunk)
        return chunk

    def write(self, data: bytes) -> None:
        self.serial.write(data)
        self.serial.flush()
        if self.on_tx is not None:
            self.on_tx(data)
