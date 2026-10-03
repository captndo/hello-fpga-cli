"""Serial connection lifecycle and exact reads with an idle timeout."""

from __future__ import annotations

import time
from typing import Optional

import serial

from .errors import DeviceTimeout

BAUD = 460800
DEFAULT_TIMEOUT = 60.0


class SerialTransport:
    """Own the serial connection and exact reads."""

    def __init__(
        self,
        port: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.port = port
        self.timeout = timeout
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
        self.ser.reset_input_buffer()
        self.ser.reset_output_buffer()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.ser is not None:
            self.ser.close()

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
            elif time.monotonic() > deadline:
                raise DeviceTimeout(
                    f"Timed out after {self.timeout:.0f}s waiting for "
                    f"{label}: received {len(data)}/{n} bytes"
                )

        return bytes(data)

    def write(self, data: bytes) -> None:
        self.serial.write(data)
        self.serial.flush()
