"""Scripted serial peer used by tests; never opens a hardware port."""

import struct


class FakeSerial:
    def __init__(self, incoming=b"", *, chunks=None, fragment_size=2):
        self.chunks = list(chunks if chunks is not None else [incoming])
        self.fragment_size = fragment_size
        self.events = []
        self.closed = False

    def read(self, size):
        if not self.chunks:
            return b""
        chunk = self.chunks.pop(0)
        count = min(size, self.fragment_size, len(chunk))
        if len(chunk) > count:
            self.chunks.insert(0, chunk[count:])
        return chunk[:count]

    def write(self, data):
        self.events.append(("write", data))
        return len(data)

    def flush(self):
        self.events.append(("flush",))

    def reset_input_buffer(self):
        self.events.append(("reset_input",))

    def reset_output_buffer(self):
        self.events.append(("reset_output",))

    def close(self):
        self.closed = True
        self.events.append(("close",))

    @property
    def writes(self):
        return [event[1] for event in self.events if event[0] == "write"]


def text_packet(payload):
    return b"P" + struct.pack("<I", len(payload)) + payload


def dat_request(offset, requested):
    return b"D" + struct.pack("<II", offset, requested)


def completion(code=0):
    return text_packet(f"Exit code = {code}\nEnd DirectC Demo\n".encode())


HANDSHAKE = b"#hkrAF"
