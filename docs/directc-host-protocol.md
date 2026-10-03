# DirectC host protocol

This document specifies the host-side protocol used by the Hello FPGA kit's stock PIC32 firmware to run Microchip DirectC operations.

The protocol was reconstructed from official-GUI traffic and confirmed against the factory PIC32 firmware.

## Entry

The PIC32 top-level dispatcher uses `#` to enter DirectC.

```text
host -> '#'
PIC  -> '#'
```

The PIC echo must be consumed before the DirectC handshake begins.

## Handshake

The firmware performs the following three exchanges:

```text
host -> 's'
PIC  -> 'h'

host -> 'a'
PIC  -> 'k'

host -> 'e'
PIC  -> 'r'
```

The firmware code waits until it receives `s`; subsequent `a` and `e` responses are conditionally acknowledged.

## Action selection

After the handshake:

```text
PIC  -> 'A'
host -> ASCII selector
```

Static analysis recovered the selector lookup table and the action dispatcher.

| Host selector | DirectC action code | Meaning |
| :---: | ---: | --- |
| `'1'` | `1` | device information |
| `'2'` | `2` | read IDCODE |
| `'3'` | `3` | erase |
| `'4'` | `5` | program |
| `'5'` | `6` | verify |
| `'6'` | `7` | encrypted-data authentication |
| `'7'` | `28` (`0x1C`) | verify programmed-device digest |

The CLI exposes all seven selectors. Selectors `'6'` and `'7'` are available as `authenticate` and `digest`, respectively.

## Family selection

The firmware then sends:

```text
PIC  -> 'F'
host -> '5'
```

The firmware converts the ASCII digit to numeric value `5`. The subsequent identification routine uses that value for the SmartFusion2 / IGLOO2 (`M2S/M2GL`) path.

For this project, the host should send ASCII `'5'` exactly, matching the official GUI and successful captures.

## Device identification

After selection, the PIC runs its DirectC identification/setup routine and emits text such as:

```text
Identifying device...
Looking for M2S/M2GL device...
```

The internal status byte later becomes the printed DirectC `Exit code`.

## Runtime record stream

After setup, the PIC drives the session with single-byte record identifiers.

Supported record types observed and implemented by the host are:

```text
'D'  DAT file request
'P'  text/status payload
'R'  progress
'T'  transition marker
```

Any unknown record byte is treated as a protocol error. The host deliberately stops rather than guessing.

## `D` — DAT request

Wire format from PIC:

```text
'D'
<offset:u32le>
<requested_length:u32le>
```

Host response:

```text
<actual_length:u32le>
<data:actual_length bytes>
```

Behavior established from captures:

- requested chunks are normally at most 1024 bytes;
- the host returns the exact slice of the `.dat` file beginning at `offset`;
- at end-of-file the host sends a genuinely short final chunk;
- no zero padding is added.

The current implementation rejects requests larger than 1024 bytes and rejects offsets beyond the end of the loaded DAT file.

Example:

```text
PIC requests: offset=0x00001000, requested=1024
host sends:   actual=1024 + DAT[0x1000:0x1400]
```

At EOF:

```text
PIC requests: requested=1024
remaining:    914
host sends:   actual=914 + 914 bytes
```

Some operations that sound device-only, including IDCODE workflows, may still request DAT data during identification/setup. Hosts should not assume that an action can run without a compatible DAT unless that has been explicitly validated.

## `P` — text payload

Wire format:

```text
'P'
<length:u32le>
<payload:length bytes>
```

The payload contains DirectC status text. Logical strings can be split across multiple `P` records, so parsers must keep a rolling text buffer.

The current host extracts:

```text
Exit code = <integer>
```

and detects final completion by finding:

```text
End DirectC Demo
```

The firmware prints the exit code before the final end marker. Reaching the end marker without having observed an exit code is treated as a protocol failure.

## `R` — progress

Wire format:

```text
'R'
<progress:u8>
```

Observed progress values use the range `0..100`. Repeated values are legal.

Do not use `R=100` as session completion. Successful programming captures have shown substantial silence after 100% before final DirectC text appears.

## `T` — transition marker

Wire format:

```text
'T'
```

No host response is sent.

The exact DirectC-internal semantic meaning of this marker has not been needed by the host implementation. It is surfaced to observers for diagnostics.

## Completion

A successful logical session ends when the rolling `P` text contains `End DirectC Demo` and an exit code has already been parsed.

Typical final text includes:

```text
Exit code = 0
...
End DirectC Demo...
```

Nonzero DirectC exit codes are DirectC results, not serial framing failures.

## Timeout behavior

DirectC should be treated with an **idle timeout**, not a fixed total transaction timeout.

The current transport resets its idle deadline whenever another byte chunk is received. The default DirectC idle timeout is 60 seconds.

This is intentional: successful programming has been observed to remain silent for more than ten seconds after reporting 100% progress.

## Action semantics

### Action 1 — device information

The handler performs a sequence of DirectC device-information/security-query routines, checking the DirectC status after each stage.

### Action 2 — read IDCODE

The top-level action handler is effectively empty. Device identification already occurs during the common setup path, so the relevant ID information is produced by that path rather than a large action-specific body.

### Action 3 — erase

The handler prepares the device, derives operation ranges from DAT records, and executes the erase path. It is destructive.

### Action 5 — program

The handler prepares the device, obtains programming ranges from DAT records, performs programming operations, and finalizes the programming sequence.

### Action 6 — verify

The handler compares programmed device content against the expected programming data using DirectC verification operations.

### Action 7 — encrypted-data authentication

This action authenticates encrypted programming data for the secure-programming flow. It is mainly relevant when using encrypted/secured programming images rather than ordinary unencrypted development images.

Run this action with:

```text
hello-fpga authenticate design.dat
```

### Action 28 — verify digest

This action verifies programmed-device digests. Static firmware analysis found result text for regions including:

- FPGA fabric
- fabric configuration segment
- eNVM 0
- eNVM 1
- user security policies segment
- user key set 1
- user key set 2
- factory row / factory key segment

The exact subset checked depends on device/DAT/security configuration.

Run this action with:

```text
hello-fpga digest design.dat
```

## Safety guidance

- `erase` and `program` can alter device contents.
- Do not probe undocumented/unknown action codes blindly; security-related DirectC actions may have irreversible consequences on some devices.
- If DirectC is interrupted, closing the serial port does not guarantee that the PIC returned to its idle dispatcher.
- If programming is interrupted, assume the FPGA configuration may be invalid until a known-good image is programmed again.

## Host implementation requirements

A robust host should:

1. validate every expected handshake byte;
2. parse USB/serial as a byte stream rather than relying on packet boundaries;
3. bound DAT requests;
4. return short EOF chunks without padding;
5. parse DirectC text across `P` record boundaries;
6. use an idle timeout long enough for late programming stages;
7. stop on unknown record bytes;
8. require both an exit code and the end marker for normal completion.
