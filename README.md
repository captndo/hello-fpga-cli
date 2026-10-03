# hello-fpga-cli

Native Linux CLI and Python host for the stock Hello FPGA PIC32 firmware.

## Install

```bash
python -m pip install -e .
```

## DirectC programming

```bash
hello-fpga info design.dat
hello-fpga idcode design.dat
hello-fpga program design.dat
hello-fpga verify design.dat
hello-fpga erase design.dat
```

Commands require a readable, nonempty DAT file. Erase asks for confirmation;
use --yes to skip it. The serial link is 460800 baud, 8N1, on /dev/ttyACM0 by
default. Use --port/-p to change it, --timeout for the idle timeout (60 seconds
by default), and --verbose/-v to show DAT requests and protocol markers.

The host services DirectC DAT requests, text, progress, and transition records.
The Python API is execute_action(port=..., dat=..., action=...) in
hello_fpga.operations; action constants live in hello_fpga.protocol.
The core is silent and independent of Typer. Serial and protocol errors
propagate, and operation wrappers close the connection on failure.

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use fake serial peers and deterministic clocks; no board is required.

## Power telemetry

```bash
hello-fpga power
hello-fpga power --watch --interval 0.5
```

The PAC1710 reply is seven positional bytes after the '*' echo. Power readings
preserve signed current and use the stock 25 mOhm shunt, +/-80 mV range, and
11-bit current configuration. The reply cannot verify those settings.
Watch refreshes a timestamped terminal line or appends lines when redirected.
It uses one connection and defaults to two seconds between request starts.
The Python API provides execute_power and iter_power; explicitly close an
iterator when stopping early. Power timeout defaults to one second.
