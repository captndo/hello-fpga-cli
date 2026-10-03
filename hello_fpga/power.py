"""Stock Hello FPGA PAC1710 power reply and board-specific conversions."""

from dataclasses import dataclass

from .errors import ProtocolError
from .transport import SerialTransport

DEFAULT_POWER_TIMEOUT = 1.0
DEFAULT_POWER_INTERVAL = 2.0
SENSE_RESISTANCE_OHM = 0.025
SENSE_FULL_SCALE_V = 0.080
SENSE_FULL_SCALE_CODE = 2047
SOURCE_LSB_V = 0.01953125


@dataclass(frozen=True)
class PowerReading:
    """Validated reply, with SI-unit properties for the stock board settings.

    Current assumes a 25 mΩ shunt and ±80 mV, 11-bit configuration. The
    seven-byte reply does not include configuration registers.
    """

    raw: bytes

    @property
    def vsense_raw(self) -> int:
        return int.from_bytes(self.raw[:2], "big")

    @property
    def vsource_raw(self) -> int:
        return int.from_bytes(self.raw[2:4], "big")

    @property
    def vsense_code(self) -> int:
        code = self.vsense_raw >> 4
        return code - 0x1000 if code & 0x800 else code

    @property
    def vsource_code(self) -> int:
        return self.vsource_raw >> 5

    @property
    def voltage_v(self) -> float:
        return self.vsource_code * SOURCE_LSB_V

    @property
    def current_a(self) -> float:
        return (
            self.vsense_code * SENSE_FULL_SCALE_V
            / SENSE_FULL_SCALE_CODE / SENSE_RESISTANCE_OHM
        )

    @property
    def power_w(self) -> float:
        return self.voltage_v * self.current_a


def decode_power_reply(raw: bytes) -> PowerReading:
    """Validate and decode seven positional bytes, without serial I/O."""
    if len(raw) != 7:
        raise ProtocolError(
            f"power reply length: expected 7 bytes, received {len(raw)}"
        )
    for index, expected, label in (
        (4, 0x58, "PAC1710 product ID"),
        (5, 0x5D, "Microchip manufacturer ID"),
        (6, 0x61, "power reply terminator"),
    ):
        if raw[index] != expected:
            raise ProtocolError(
                f"{label}: expected 0x{expected:02X}, received 0x{raw[index]:02X}"
            )
    return PowerReading(bytes(raw))


def read_power(transport: SerialTransport) -> PowerReading:
    """Request one measurement on an already-open transport; send no ACK."""
    transport.write(b"*")
    echo = transport.read_exact(1, "power-command echo")
    if echo != b"*":
        raise ProtocolError(
            f"power-command echo: expected 0x2A, received 0x{echo[0]:02X}"
        )
    return decode_power_reply(transport.read_exact(7, "power reply"))
