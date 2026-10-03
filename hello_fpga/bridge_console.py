"""Linux console setup and binary forwarding for the bridge CLI."""

import errno
import fcntl
import os
import select
import sys
import termios
from contextlib import contextmanager
from typing import Generator

from .bridge import HelloFpgaBridge

CHUNK_SIZE = 4096


def console_fds() -> tuple[int, int]:
    """Validate real stdin/stdout descriptors before opening the serial port."""
    try:
        stdin_fd, stdout_fd = sys.stdin.fileno(), sys.stdout.fileno()
        os.fstat(stdin_fd)
        os.fstat(stdout_fd)
        if fcntl.fcntl(stdin_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_WRONLY:
            raise OSError("stdin is not readable")
        if fcntl.fcntl(stdout_fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY:
            raise OSError("stdout is not writable")
        select.select([stdin_fd], [], [], 0)
    except (AttributeError, OSError, ValueError) as exc:
        raise OSError(f"bridge requires readable stdin and writable stdout file descriptors: {exc}") from exc
    return stdin_fd, stdout_fd


@contextmanager
def terminal_input(fd: int) -> Generator[None, None, None]:
    """Use cbreak input on a TTY and restore it even if setup or the body fails."""
    if not os.isatty(fd):
        yield
        return

    saved = termios.tcgetattr(fd)
    mode = saved[:]
    mode[6] = saved[6][:]
    mode[3] = (mode[3] & ~(termios.ECHO | termios.ICANON)) | termios.ISIG
    mode[6][termios.VMIN] = 1
    mode[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        yield
    except BaseException as primary:
        try:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        except BaseException as restore_error:
            raise primary from restore_error
        raise
    else:
        termios.tcsetattr(fd, termios.TCSANOW, saved)


def forward_streams(bridge: HelloFpgaBridge, *, stdin_fd: int, stdout_fd: int) -> None:
    """Forward bytes until interrupted; stdin EOF leaves the receiver running.

    All serial I/O stays in the calling thread. Bridge reads use the transport's
    serial timeout, so idle sessions do not busy-poll stdin.
    """
    input_open = True
    while True:
        if input_open and select.select([stdin_fd], [], [], 0)[0]:
            payload = os.read(stdin_fd, CHUNK_SIZE)
            if payload:
                bridge.write(payload)
            else:
                input_open = False

        reply = bridge.read(CHUNK_SIZE)
        remaining = memoryview(reply)
        while remaining:
            written = os.write(stdout_fd, remaining)
            if written <= 0:
                raise OSError(errno.EIO, "stdout write made no progress")
            remaining = remaining[written:]
