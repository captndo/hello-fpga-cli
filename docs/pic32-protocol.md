# Stock PIC32 host protocol

This document describes the host-facing protocol implemented by the stock PIC32 firmware on the Microchip Hello FPGA kit, as reconstructed from USB/serial captures and static analysis of the factory firmware.

It focuses on the PIC32 command surface. DirectC and the UART bridge have dedicated documents.

## Scope

Host path studied by this project:

```text
Linux host
    |
    | USB CDC / serial device (/dev/ttyACM*)
    v
USB-UART bridge
    |
    | 460800 8N1
    v
PIC32 board-management MCU
    |
    +-- DirectC programming path
    +-- power monitor path
    +-- FlashFreeze controls
    +-- UART bridge to SmartFusion2
```

The PIC32 host UART is opened at:

```text
460800 baud
8 data bits
no parity
1 stop bit
```

## Top-level dispatcher

Static firmware analysis recovered the complete switch in the normal idle dispatcher. It reads one host byte, echoes it, and handles exactly these command values:

| Byte | ASCII | State / function | Project support |
| ---: | :---: | --- | --- |
| `0x21` | `!` | alternate bridge setup, then common bridge state | intentionally unsupported |
| `0x23` | `#` | DirectC | supported |
| `0x24` | `$` | FlashFreeze enter | supported |
| `0x26` | `&` | common UART bridge | supported |
| `0x2A` | `*` | PAC1710 power read | supported |
| `0x40` | `@` | FlashFreeze exit + counter | supported |

No additional top-level cases were present in the recovered dispatcher for the studied firmware image.

Unknown input bytes fall through without selecting another state.

## State model

The main firmware state variable was recovered as a global switch with values `0..6`:

```text
state 0  initialization
state 1  idle / host command dispatcher
state 2  DirectC
state 3  common UART bridge
state 4  PAC1710 power read
state 5  FlashFreeze enter
state 6  FlashFreeze exit / timing
```

Most one-shot operations return the state variable to `1` after completion.

### Initialization

The initialization state opens two serial/UART abstractions and configures board peripherals. Exact physical UART mapping is still being investigated.

## Common command echo

The dispatcher echoes the received command byte before transitioning to its operation. Examples:

```text
host -> '*'
PIC  -> '*'
```

```text
host -> '&'
PIC  -> '&'
```

This echo is part of the protocol and is validated by the host implementation.

## `*` — power monitor

Transaction:

```text
host -> 2A                 '*'
PIC  -> 2A                 echo
PIC  -> 7-byte payload
```

Payload layout:

```text
byte 0  VSENSE high
byte 1  VSENSE low
byte 2  VSOURCE high
byte 3  VSOURCE low
byte 4  product ID        expected 0x58
byte 5  manufacturer ID   expected 0x5D
byte 6  terminator        expected 0x61 ('a')
```

Observed example:

```text
04 50 07 80 58 5D 61
```

USB packet boundaries are irrelevant; serial readers must collect the required byte count even if the reply arrives fragmented.

### Current conversion used by the software

The current code assumes:

```text
shunt resistance          0.025 ohm
sense full scale          ±80 mV
sense code width          11-bit signed
source voltage LSB        19.53125 mV
```

This produces plausible measurements for the stock 1.2 V rail. However, the seven-byte transaction does not expose PAC1710 configuration registers, so the exact runtime sensor configuration remains an assumption until the firmware's setup writes are fully verified.

## `$` — FlashFreeze enter

Transaction:

```text
host -> 24  '$'
PIC  -> 24  echo
```

No additional payload has been observed or found in the high-level handler.

The command causes control GPIO changes associated with FlashFreeze entry. Exact pin names / board-net meanings are not yet fully mapped.

## `@` — FlashFreeze exit

Transaction:

```text
host -> 40  '@'
PIC  -> 40  echo
PIC  -> <ticks:u32le>
```

Example:

```text
0D 01 00 00  -> 269 ticks
```

Static analysis confirms that the firmware takes a counter difference and serializes it least-significant byte first.

The counter's physical time scale is not yet confirmed. The CLI therefore reports raw ticks only.

## `#` — DirectC

`#` enters a stateful programming protocol rather than performing a one-shot command.

See [directc-host-protocol.md](directc-host-protocol.md).

## `&` — UART bridge

`&` enters the common PIC32 host-to-FPGA bridge state.

See [bridge-protocol.md](bridge-protocol.md).

## `!` — alternate bridge mode

The stock dispatcher also accepts `!`.

Static analysis shows that it:

- sets a bridge-mode flag;
- changes several board GPIO/control outputs;
- then enters the same common bridge state as `&`.

Inside the bridge loop, the flag causes the `%Ex...` escape-recognition path to be bypassed. Because the physical routing changes and a reliable host exit/recovery mechanism have not been fully characterized, this project does not expose `!` as a supported API.

## Stateful recovery

The PIC32 firmware is stateful. Closing `/dev/ttyACM*` does not imply that the PIC resets or returns to the idle dispatcher.

Consequences:

- an interrupted DirectC exchange may leave the PIC waiting inside DirectC;
- an abandoned `&` bridge may remain active if the escape sequence was not sent;
- an interrupted programming operation may leave the FPGA unconfigured or partially programmed.

The safest recovery when protocol state is unknown is a board reset or power cycle rather than speculative protocol bytes.

## Known internal control-register clues

Static analysis has identified GPIO latch operations in the `0xBF886xxx` SFR region. Examples include writes corresponding to `LATA*` / `LATD*` shadow operations and individual bit masks used around bridge, DirectC, and FlashFreeze transitions.

These are useful for future board-plumbing work, but the exact net-level meaning of every control bit is not required for host protocol operation and is not yet treated as part of the public protocol specification.

## What is considered complete

For the studied factory firmware, confidence is high that the **normal top-level host command alphabet is complete** because it was recovered directly from the dispatch switch rather than inferred from GUI behavior.

What remains incomplete is mostly implementation detail beneath those commands: exact UART peripheral mapping, all control-pin net names, PAC1710 setup details, and timer scaling.
