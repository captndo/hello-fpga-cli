"""Errors shared by the serial, PIC protocol, and operation layers."""


class ProtocolError(RuntimeError):
    """The PIC32 exchange did not match the expected protocol."""


class DeviceTimeout(ProtocolError):
    """The device exceeded the configured idle timeout."""


class DatFileError(ValueError):
    """The requested DAT file is missing or empty."""
