# Stock `&` UART bridge protocol

This document specifies the supported UART bridge implemented by the stock PIC32 firmware on the Hello FPGA kit.

The bridge is intentionally treated as a transport layer. FPGA-specific handshakes, packets, filters, audio samples, commands, or other application semantics are **not** part of this protocol.

## Entry

From the PIC32 idle dispatcher:

```text
host -> '&'  (0x26)
PIC  -> '&'  echo
```

After the echo is returned, the PIC enters its common bridge state.

The host should not consider the bridge active until the echo has been validated.

## Active data path

While active, the PIC forwards bytes bidirectionally between the host-facing serial channel and the FPGA-facing serial channel.

At the bridge layer there is:

- no packet header;
- no length field;
- no checksum;
- no application ACK;
- no interpretation of ordinary top-level PIC command bytes such as `*`, `$`, `#`, or `@`.

Those bytes are just application payload while bridge mode is active.

Conceptually:

```text
host application bytes
        |
        v
PIC32 common bridge
        |
        v
FPGA application protocol
```

The FPGA design decides what the data means.

## Exit sequence

The vendor GUI was captured sending:

```text
%ExIt3
```

Hex:

```text
25 45 78 49 74 33
```

Static analysis recovered the escape parser constants directly:

```text
'%' = 0x25
'E' = 0x45
'x' = 0x78
```

After matching `%Ex`, the firmware reads and consumes **three additional host bytes**. The helper used for those three bytes simply reads them into a buffer; no comparison against `I`, `t`, `3` was found.

Therefore the effective firmware condition is:

```text
%Ex + any three following bytes
```

For compatibility, this project always sends the canonical sequence:

```text
%ExIt3
```

## No exit acknowledgement

After consuming the three post-prefix bytes, the firmware changes control state and returns to the normal top-level state. No separate bridge-exit acknowledgement is expected by the host.

The public bridge API therefore sends `%ExIt3` once and marks the local bridge object inactive without waiting for another reply.

## Reserved stream pattern

This is the most important constraint of the stock `&` bridge:

> `%Ex` followed by any three bytes is reserved in the **host-to-FPGA** byte stream and can terminate bridge mode.

This applies even when the bytes are split across multiple host writes because the PIC escape state persists while reading the stream.

Consequences for arbitrary binary protocols:

- avoid generating `%Ex...` in host-to-FPGA payloads; or
- use an application-level encoding/escaping scheme; or
- design a packet format that cannot contain the reserved prefix unescaped.

The bridge library deliberately does **not** transform payload data because doing so would silently invent an FPGA application protocol.

## Partial/mismatched escape prefixes

The firmware clearly implements state for `%`, then `%E`, then `%Ex`.

The exact byte-for-byte forwarding behavior for every malformed or partial prefix (for example a lone `%` followed by long delay, or `%Q`) has not been exhaustively replay-tested. Applications should therefore avoid relying on subtle behavior around the reserved prefix.

## Host API model

The project models bridge state locally:

```python
bridge.enter()
bridge.write(...)
bridge.read(...)
bridge.read_exact(...)
bridge.exit()
```

`active` means only that the current host object believes it entered the bridge successfully and has not exited it. It is not physical board-state discovery.

The implementation rejects obvious local misuse such as:

- write/read before entry;
- double entry;
- double exit.

If the serial session becomes corrupted, callers should discard it rather than attempt speculative resynchronization.

## Context-managed lifetime

The high-level API can own the transport and perform canonical cleanup:

```python
from hello_fpga.operations import open_bridge

with open_bridge(port="/dev/ttyACM0", timeout=2.0) as bridge:
    bridge.write(b"request")
    reply = bridge.read_exact(4)
```

By default, normal completion and exceptions cause the context manager to attempt `%ExIt3` once before closing the serial port.

A failed manual exit is not retried automatically.

## CLI stream behavior

`hello-fpga bridge` connects stdin/stdout to the bridge.

Important properties of the current Linux console implementation:

- stdin payload is forwarded as raw bytes;
- stdout contains only FPGA reply bytes, not the PIC's entry echo;
- human guidance and verbose hex logs go to stderr;
- terminal input uses cbreak mode so input is immediate and Ctrl-C remains local;
- redirected/file input remains binary;
- after stdin EOF, the command continues receiving until interrupted;
- Ctrl-C triggers bridge cleanup and exits with code 130;
- a broken stdout pipe triggers cleanup and exits 141.

## Recovery

Closing the host serial port does **not** itself tell the PIC to leave bridge mode.

If an application dies before the escape is sent, the PIC may remain in the bridge state. Board reset or power cycle is the reliable recovery when the current state is uncertain.

## Alternate `!` bridge mode

The top-level PIC dispatcher also has a `!` command that enters the common bridge after changing control GPIOs and setting an internal flag.

Inside the common bridge loop, that flag bypasses the `%Ex` escape parser. That makes `!` interesting for fully transparent binary traffic, but its physical routing and exit/recovery behavior are not sufficiently characterized for a supported host API.

For that reason this project currently supports **only `&` bridge mode**.
