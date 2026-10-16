"""One latest preview frame in local shared memory; JSON carries notifications only."""

import fcntl
import mmap
import os
import struct
import tempfile
from contextlib import contextmanager
from pathlib import Path

HEADER = struct.Struct("<QIIIId")
CAPACITY = HEADER.size + 1280 * 800 * 4


class PreviewBuffer:
    def __init__(self, path=None):
        self.owner = path is None
        if self.owner:
            try:
                self.fd, self.path = tempfile.mkstemp(
                    prefix="glxss-preview-", dir="/dev/shm"
                )
            except OSError:
                self.fd, self.path = tempfile.mkstemp(prefix="glxss-preview-")
            os.ftruncate(self.fd, CAPACITY)
        else:
            self.path = str(path)
            self.fd = os.open(self.path, os.O_RDWR)
            if os.fstat(self.fd).st_size != CAPACITY:
                os.close(self.fd)
                raise ValueError("Invalid preview buffer")
        self.memory = mmap.mmap(self.fd, CAPACITY)
        self.sequence = HEADER.unpack_from(self.memory)[0]

    @contextmanager
    def locked(self, exclusive=False):
        fcntl.flock(self.fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(self.fd, fcntl.LOCK_UN)

    def write(self, pixels, width, height, source_width, source_height, timestamp):
        size = width * height * 4
        if (
            min(width, height) <= 0
            or size != len(pixels)
            or size > CAPACITY - HEADER.size
        ):
            raise ValueError("Invalid preview frame")
        with self.locked(True):
            self.sequence += 1
            self.memory[HEADER.size : HEADER.size + size] = pixels
            HEADER.pack_into(
                self.memory,
                0,
                self.sequence,
                width,
                height,
                source_width,
                source_height,
                timestamp,
            )
        return self.sequence

    def read(self, after=0):
        with self.locked():
            sequence, width, height, sw, sh, stamp = HEADER.unpack_from(self.memory)
            if not sequence or sequence <= after:
                return None
            size = width * height * 4
            if not 0 < size <= CAPACITY - HEADER.size:
                raise ValueError("Invalid preview frame header")
            pixels = self.memory[HEADER.size : HEADER.size + size]
        return {
            "sequence": sequence,
            "width": width,
            "height": height,
            "source_width": sw,
            "source_height": sh,
            "timestamp": stamp,
            "pixels": pixels,
        }

    def close(self):
        if self.memory is None:
            return
        self.memory.close()
        self.memory = None
        os.close(self.fd)
        if self.owner:
            Path(self.path).unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
