"""Typer commands and terminal presentation for Hello FPGA operations."""

import math
import sys
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Optional

import serial
import typer

from .errors import DatFileError, ProtocolError
from .operations import execute_action, execute_power, iter_power, load_dat
from .power import DEFAULT_POWER_INTERVAL, DEFAULT_POWER_TIMEOUT
from .protocol import ACTION_ERASE, ACTION_IDCODE, ACTION_INFO, ACTION_PROGRAM, ACTION_VERIFY
from .transport import BAUD, DEFAULT_TIMEOUT

app = typer.Typer(
    name="hello-fpga",
    no_args_is_help=True,
    add_completion=False,
    help="Native CLI for Hello FPGA DirectC programming and power.",
)


class TerminalObserver:
    """Render protocol notifications with the original stdout/stderr layout."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.verbose = verbose
        self.last_progress: Optional[int] = None

    def on_text(self, text: str) -> None:
        typer.echo(text, nl=False)

    def on_progress(self, value: int) -> None:
        if value != self.last_progress:
            typer.echo(f"\rProgress: {value:3d}%", nl=False, err=True)
            self.last_progress = value

    def on_dat_request(self, offset: int, requested: int, sent: int) -> None:
        if self.verbose:
            typer.echo(
                f"[D] offset=0x{offset:08X} requested={requested} sent={sent}",
                err=True,
            )

    def on_transition(self) -> None:
        if self.verbose:
            typer.echo("\n[T] transition marker", err=True)

    def on_end(self) -> None:
        if self.last_progress is not None:
            typer.echo(err=True)


def run_action(
    *,
    port: str,
    dat_path: Path,
    action: bytes,
    operation: str,
    timeout: float,
    verbose: bool,
) -> None:
    try:
        dat = load_dat(dat_path)
    except DatFileError as exc:
        raise typer.BadParameter(str(exc)) from exc

    typer.echo(f"Port:      {port}", err=True)
    typer.echo(f"Baud:      {BAUD}", err=True)
    typer.echo(f"DAT:       {dat_path}", err=True)
    typer.echo(f"DAT size:  {len(dat)} bytes", err=True)
    typer.echo(f"Operation: {operation}", err=True)
    typer.echo(err=True)

    try:
        result = execute_action(
            port=port,
            dat=dat,
            action=action,
            timeout=timeout,
            observer=TerminalObserver(verbose=verbose),
        )
    except serial.SerialException as exc:
        typer.echo(f"Serial error: {exc}", err=True)
        raise typer.Exit(2)
    except ProtocolError as exc:
        typer.echo(f"\nProtocol error: {exc}", err=True)
        raise typer.Exit(3)

    if result.exit_code != 0:
        typer.echo(
            f"\nDirectC failed with exit code {result.exit_code}.",
            err=True,
        )
        raise typer.Exit(result.exit_code if 1 <= result.exit_code <= 125 else 4)

    typer.secho("\nDirectC completed successfully.", fg=typer.colors.GREEN)


def _positive_finite(value: float) -> float:
    if not math.isfinite(value) or value <= 0:
        raise typer.BadParameter("must be finite and positive")
    return value


def _watch_power(*, port: str, timeout: float, interval: float) -> None:
    live = sys.stdout.isatty()
    active_line = False
    try:
        with closing(iter_power(port=port, timeout=timeout, interval=interval)) as readings:
            for reading in readings:
                stamp = datetime.now().astimezone().isoformat(timespec="seconds")
                line = (
                    f"{stamp}  Vcore: {reading.voltage_v:.3f} V"
                    f"  Icore: {reading.current_a * 1000:.1f} mA"
                    f"  Pcore: {reading.power_w * 1000:.1f} mW"
                )
                if live:
                    active_line = True
                    typer.echo("\r\x1b[K" + line, nl=False, color=True)
                else:
                    typer.echo(line)
    finally:
        if active_line:
            typer.echo()


@app.command()
def power(
    port: str = typer.Option(
        "/dev/ttyACM0", "--port", "-p", help="PIC32 serial device."
    ),
    timeout: float = typer.Option(
        DEFAULT_POWER_TIMEOUT, "--timeout", min=1.0,
        callback=_positive_finite, help="Power reply idle timeout in seconds."
    ),
    watch: bool = typer.Option(
        False, "--watch", help="Continuously monitor with local timestamps."
    ),
    interval: float = typer.Option(
        DEFAULT_POWER_INTERVAL, "--interval", callback=_positive_finite,
        help="Seconds between request starts; used only with --watch."
    ),
) -> None:
    """Read core-rail voltage, current, and power using stock board settings."""
    try:
        if watch:
            _watch_power(port=port, timeout=timeout, interval=interval)
        else:
            reading = execute_power(port=port, timeout=timeout)
            typer.echo(f"Core voltage: {reading.voltage_v:.3f} V")
            typer.echo(f"Core current: {reading.current_a * 1000:.1f} mA")
            typer.echo(f"Core power:   {reading.power_w * 1000:.1f} mW")
    except serial.SerialException as exc:
        typer.echo(f"Serial error: {exc}", err=True)
        raise typer.Exit(2)
    except ProtocolError as exc:
        typer.echo(f"Protocol error: {exc}", err=True)
        raise typer.Exit(3)
    except KeyboardInterrupt:
        raise typer.Exit(130)


@app.command()
def info(
    dat: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="A compatible Libero/Hello FPGA .dat file.",
    ),
    port: str = typer.Option(
        "/dev/ttyACM0", "--port", "-p", help="PIC32 serial device."
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT,
        "--timeout",
        min=1.0,
        help="Idle timeout in seconds. DirectC can be silent for >10 s.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show DAT requests and protocol markers."
    ),
) -> None:
    """Read SmartFusion2 device/design information without programming it."""
    run_action(
        port=port,
        dat_path=dat,
        action=ACTION_INFO,
        operation="info",
        timeout=timeout,
        verbose=verbose,
    )


@app.command("idcode")
def idcode(
    dat: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Compatible Libero/Hello FPGA .dat file required by stock DirectC.",
    ),
    port: str = typer.Option(
        "/dev/ttyACM0", "--port", "-p", help="PIC32 serial device."
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", min=1.0, help="Idle timeout in seconds."
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show individual DAT requests."
    ),
) -> None:
    """Run the stock DirectC IDCODE/device-identification action."""
    run_action(
        port=port,
        dat_path=dat,
        action=ACTION_IDCODE,
        operation="idcode",
        timeout=timeout,
        verbose=verbose,
    )


@app.command()
def program(
    dat: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Libero .dat programming file.",
    ),
    port: str = typer.Option(
        "/dev/ttyACM0", "--port", "-p", help="PIC32 serial device."
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT,
        "--timeout",
        min=1.0,
        help="Idle timeout in seconds. 60 s is intentionally conservative.",
    ),
    verbose: bool = typer.Option(
        False, "--verbose", "-v", help="Show individual DAT requests."
    ),
) -> None:
    """Program the SmartFusion2 using the board's stock PIC32 DirectC firmware."""
    run_action(
        port=port,
        dat_path=dat,
        action=ACTION_PROGRAM,
        operation="program",
        timeout=timeout,
        verbose=verbose,
    )


@app.command()
def verify(
    dat: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Libero .dat file to verify against the FPGA.",
    ),
    port: str = typer.Option(
        "/dev/ttyACM0", "--port", "-p", help="PIC32 serial device."
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", min=1.0, help="Idle timeout in seconds."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run the stock DirectC VERIFY action."""
    run_action(
        port=port,
        dat_path=dat,
        action=ACTION_VERIFY,
        operation="verify",
        timeout=timeout,
        verbose=verbose,
    )


@app.command()
def erase(
    dat: Path = typer.Argument(
        ...,
        exists=True,
        file_okay=True,
        dir_okay=False,
        readable=True,
        help="Compatible .dat file used by DirectC while erasing.",
    ),
    port: str = typer.Option(
        "/dev/ttyACM0", "--port", "-p", help="PIC32 serial device."
    ),
    timeout: float = typer.Option(
        DEFAULT_TIMEOUT, "--timeout", min=1.0, help="Idle timeout in seconds."
    ),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="Skip the destructive-operation confirmation.",
    ),
) -> None:
    """Erase the FPGA programming through the stock DirectC action."""
    if not yes:
        confirmed = typer.confirm(
            "This will erase FPGA programming and may leave the FPGA array "
            "disabled until a successful re-program. Continue?"
        )
        if not confirmed:
            raise typer.Abort()

    run_action(
        port=port,
        dat_path=dat,
        action=ACTION_ERASE,
        operation="erase",
        timeout=timeout,
        verbose=verbose,
    )
