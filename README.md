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

## FlashFreeze

```bash
hello-fpga freeze enter
hello-fpga freeze exit -v
```

Enter sends '$' and checks its echo. Exit sends '@', checks its echo, and reads
an unsigned little-endian tick count; the time scale is unknown. Python callers
use FlashFreezeController or execute_freeze_enter/execute_freeze_exit. Timeout
defaults to two seconds. Optional transport TX/RX callbacks report traffic
synchronously; verbose CLI tracing goes to stderr.

## FPGA UART bridge API

```python
from hello_fpga.operations import open_bridge

with open_bridge(port='/dev/ttyACM0') as bridge:
    bridge.write(b'h')
    reply = bridge.read_exact(1, 'FPGA application handshake')
```

The example handshake belongs to the FPGA application. Entry sends '&' and
requires its echo; bridge payload is forwarded unchanged. The host-to-FPGA
pattern '%Ex' followed by any three bytes is reserved, even across writes.
Canonical exit sends '%ExIt3' with no acknowledgement. The context manager
automatically exits and closes on completion, errors, and interruption, with
auto_exit=False available for explicit control. Failed exits are not retried;
cleanup failures preserve a pending application exception. The active property
records local session belief, not physical device-state discovery.

SerialTransport.read_some and bridge.read return up to the requested size,
using the serial 0.25-second timeout. Empty reads return b''; read_exact uses
the configured idle timeout instead. The alternate '!' mode is unsupported.

## Bridge console

```bash
hello-fpga bridge
hello-fpga bridge --port /dev/ttyACM1 -v
producer | hello-fpga bridge > response.bin
```

The console forwards stdin/stdout as binary streams. After stdin EOF, it keeps
receiving until Ctrl-C. Terminal input is immediate, with local echo disabled;
terminal settings are restored on exit. Stdout contains FPGA bytes only;
guidance, errors, and verbose tracing go to stderr. Ctrl-C exits 130 and a
closed stdout pipe exits 141, after canonical bridge cleanup.

## Additional DirectC verification

```bash
hello-fpga authenticate secured.dat
hello-fpga digest design.dat
```

Authentication uses host selector '6' (DirectC action 7) to validate encrypted
programming data; ordinary unencrypted DAT files may not be applicable.
Digest uses selector '7' (action 28) to verify programmed-device digests.
Checked regions depend on DAT/device configuration. Both require DAT files,
use the ordinary DirectC options and error handling, and need no confirmation.
Python action constants are ACTION_AUTHENTICATE and ACTION_VERIFY_DIGEST.
