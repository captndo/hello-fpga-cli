# hello-fpga-cli

`hello-fpga-cli` is a native Linux command-line and Python interface for the **stock PIC32 firmware on the Microchip Hello FPGA kit**.

It replaces the host-side role of the legacy Hello FPGA GUI for the parts of the board protocol that have been reverse-engineered and validated: DirectC FPGA programming, core-rail power telemetry, FlashFreeze control, and the PIC32-to-FPGA UART bridge.

The tool does **not** replace or modify the PIC32 firmware. It talks to the board using the protocol already implemented by the factory firmware.

> **Project status:** experimental / reverse-engineered. The protocol coverage is substantial, but this project should not yet be treated as a safety-critical or factory-production programmer. See [Support status and limitations](#support-status-and-limitations).

## Why this project exists

The Hello FPGA kit exposes useful functionality through a separate PIC32 board-management MCU, but the original host workflow is tied to an aging vendor GUI. This project makes the stock board functionality usable from Linux, scripts, test rigs, CI jobs, and custom FPGA applications.

The most important capability is the UART bridge: once an FPGA design is programmed, the host can exchange arbitrary application bytes with that design through the stock PIC32 without writing replacement PIC firmware.

## Features

Current CLI support:

- DirectC device information
- DirectC IDCODE read
- DirectC erase
- DirectC program
- DirectC verify
- DirectC encrypted-data authentication
- DirectC programmed-device digest verification
- PAC1710-based core-rail voltage/current/power readout
- continuous power monitoring
- FlashFreeze enter and exit
- raw `&` UART bridge between the host and FPGA-side UART
- Python APIs for DirectC orchestration, power, FlashFreeze, and bridge sessions

Authentication and digest verification are exposed as `authenticate` and `digest` and documented in [`docs/directc-host-protocol.md`](docs/directc-host-protocol.md).

## Requirements

- Linux
- Python 3.10+
- a Microchip/Microsemi Hello FPGA kit using the stock PIC32 firmware studied by this project
- board serial device, typically `/dev/ttyACM0`
- compatible Libero/Hello FPGA `.dat` files for DirectC operations

The host serial link is configured as **460800 baud, 8 data bits, no parity, 1 stop bit**.

## Installation

For a development checkout:

```bash
python -m pip install -e .
```

This installs the `hello-fpga` command.

## Quick start

Show help:

```bash
hello-fpga --help
```

Inspect or program an FPGA image:

```bash
hello-fpga info design.dat
hello-fpga idcode design.dat
hello-fpga program design.dat
hello-fpga verify design.dat
hello-fpga authenticate secured.dat
hello-fpga digest design.dat
hello-fpga erase design.dat
```

`erase` is destructive and asks for confirmation unless `--yes` / `-y` is supplied.

Read the core-rail monitor:

```bash
hello-fpga power
hello-fpga power --watch
hello-fpga power --watch --interval 0.5
```

Control FlashFreeze:

```bash
hello-fpga freeze enter
hello-fpga freeze exit
```

Open the FPGA UART bridge:

```bash
hello-fpga bridge
```

or pipe raw binary data:

```bash
producer | hello-fpga bridge > response.bin
hello-fpga bridge < request.bin > response.bin
```

Use another serial device with `--port` / `-p`:

```bash
hello-fpga program design.dat --port /dev/ttyACM1
```

## UART bridge

The supported bridge is entered with the stock PIC32 `&` command. After the PIC echoes `&`, bytes are forwarded between the host and FPGA-side UART. The PIC does not impose application framing, checksums, packet types, or an FPGA handshake; those are defined by the FPGA design.

The canonical bridge exit sequence used by the vendor GUI is:

```text
%ExIt3
```

Static firmware analysis shows that the PIC actually recognizes the prefix `%Ex`, then consumes the following three bytes without validating them. For compatibility this project always sends the canonical `%ExIt3` sequence.

**Important:** while `&` bridge mode is active, `%Ex` followed by any three bytes is reserved in the host-to-FPGA stream and can terminate the bridge. Arbitrary binary application protocols must avoid that pattern or add their own encoding.

The alternate stock `!` bridge mode is intentionally unsupported because its routing behavior and recovery/exit semantics have not been fully characterized.

See [`docs/bridge-protocol.md`](docs/bridge-protocol.md).

## Python API example

```python
from hello_fpga.operations import open_bridge

with open_bridge(port="/dev/ttyACM0", timeout=2.0) as bridge:
    bridge.write(b"h")
    reply = bridge.read_exact(1, "FPGA application handshake")
```

The `h` / reply exchange above is only an example of an FPGA-side application protocol. It is not part of the PIC bridge protocol.

## DirectC behavior

The DirectC host flow is reverse-engineered from the factory PIC32 firmware and official-GUI traffic. The host enters DirectC with `#`, completes a three-byte handshake, chooses one of the firmware's DirectC actions, selects the SmartFusion2/IGLOO2 family path, and then services PIC requests until DirectC reports completion.

The runtime records are:

- `D`: request a slice of the `.dat` file
- `P`: textual DirectC output
- `R`: raw progress byte
- `T`: transition marker

The implementation refuses unexpected protocol bytes instead of guessing. DirectC can legitimately remain silent for more than ten seconds near the end of a programming operation, so the default DirectC timeout is an **idle timeout of 60 seconds**, reset whenever data arrives.

See [`docs/directc-host-protocol.md`](docs/directc-host-protocol.md).

## Power telemetry

`hello-fpga power` sends the stock PIC32 `*` command, validates its echo, and decodes the seven-byte PAC1710 reply.

The software currently assumes the stock board's 25 mΩ shunt and an ±80 mV, 11-bit sense range when converting current. The seven-byte reply itself does not contain the PAC1710 configuration registers, so the exact sensor configuration remains an implementation assumption to be validated against the firmware/device setup.

See [`docs/pic32-protocol.md`](docs/pic32-protocol.md).

## FlashFreeze

`$` enters FlashFreeze and `@` exits it. Both commands are echoed by the PIC. `@` additionally returns a four-byte unsigned little-endian counter value.

The counter's conversion to real time is not yet known, so the CLI reports raw ticks rather than inventing a microsecond scale.

## Recovery and failure behavior

The stock PIC32 protocol is stateful. Closing the host serial port does not necessarily reset the PIC state.

Important recovery cases:

- **Interrupted DirectC:** the PIC may remain inside the DirectC session. If reconnecting does not recover cleanly, reset or power-cycle the board.
- **Interrupted programming:** the FPGA configuration may be incomplete or invalid until the device is programmed again.
- **Interrupted `&` bridge:** closing the serial port does not itself send the bridge escape. Prefer a clean `%ExIt3` exit; otherwise board reset/power-cycle may be required.
- **Unknown protocol state:** this tool deliberately avoids aggressive automatic resynchronization because sending guesses to a programmer can be more dangerous than failing explicitly.

## Exit behavior

The CLI distinguishes broad failure classes:

- serial/open/I/O errors: exit 2
- protocol errors and protocol timeouts: exit 3
- DirectC nonzero exit codes: returned directly when practical (`1..125`), otherwise mapped to 4
- Ctrl-C: exit 130
- broken stdout pipe in bridge mode: exit 141

## Project architecture

The current package is intentionally split by responsibility:

```text
hello_fpga/
    transport.py       serial lifecycle and exact/stream reads
    protocol.py        DirectC host protocol
    power.py           PAC1710 request/reply and conversions
    freeze.py          FlashFreeze exchanges
    bridge.py          stock '&' bridge control
    bridge_console.py  Linux terminal/pipe forwarding
    operations.py      connection lifetimes and orchestration
    cli.py             Typer CLI and presentation
```

FPGA application protocols should be implemented **above** `HelloFpgaBridge`, not inside it. The bridge is transport; FIR/audio/image/control semantics belong to the FPGA application.

## Protocol documentation

- [`docs/pic32-protocol.md`](docs/pic32-protocol.md) — stock PIC32 host command surface and state model
- [`docs/directc-host-protocol.md`](docs/directc-host-protocol.md) — DirectC handshake, action mapping, `D/P/R/T` records, completion semantics
- [`docs/bridge-protocol.md`](docs/bridge-protocol.md) — `&` bridge entry, raw stream behavior, escape sequence, API implications
- [`docs/reverse-engineering-notes.md`](docs/reverse-engineering-notes.md) — evidence, Ghidra setup, key recovered functions, confidence levels, open questions

## Support status and limitations

The project has a solid practical use case, but a 1.0 / production-quality release should mean more than “the happy path works once.” Before calling it production-ready for its supported board, the project should have:

- repeated real-hardware tests of every advertised operation
- several known-good `.dat` images and at least one deliberate failure case
- validated recovery guidance for interrupted program/bridge sessions
- regression tests for all protocol framing and selector mappings
- explicit version reporting and release metadata
- a chosen open-source license
- documented supported board/firmware revision(s)
- documented public Python API stability policy
- no known data-corruption or unsafe retry behavior in supported workflows

“Production ready” here should mean **reliable for the documented Hello FPGA board and stock firmware under normal development/automation use**, not certification for safety-critical manufacturing.

## Reverse-engineering scope

The project distinguishes four different claims:

1. code exists in the PIC firmware;
2. code is reachable in normal board operation;
3. functionality is host-accessible through the stock dispatcher;
4. functionality is used by the official GUI.

These are not interchangeable. The documentation labels unresolved or inferred behavior rather than silently treating it as confirmed.

## Contributing

Useful contributions include:

- captures from additional stock Hello FPGA boards / firmware revisions
- hardware validation of failure and recovery cases
- exact mapping of PIC32 UARTs and control GPIOs to the schematic
- confirmation of PAC1710 runtime configuration
- tests for additional DirectC actions
- FPGA-side application protocols built on `HelloFpgaBridge`

When documenting reverse-engineered behavior, include how it was established: capture, static firmware analysis, active replay, or inference.
