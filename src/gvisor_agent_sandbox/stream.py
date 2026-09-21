"""Stream related utils."""

import os
import threading
import time
import typing

# How much to request per read. Larger than a typical pipe buffer so a single
# read usually drains what is available.
_READ_CHUNK = 65536


class FdDrainer:
    """Drainer for file-descriptor-backed byte stream."""

    def __init__(self, stream: typing.BinaryIO):
        self._stream = stream
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._last_activity = time.monotonic()
        # Create thread for draining with daemon=True. In Python non-daemon threads prevent the
        # program from exiting.
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def read(self) -> bytes:
        """Return the bytes drained since the last read, advancing past them.

        A consuming read: bytes are handed over once and then dropped from the
        buffer, so memory stays bounded to whatever has not been read yet.
        Returns empty if nothing new has arrived.
        """
        with self._lock:
            block = bytes(self._buffer)
            self._buffer.clear()
            return block

    def finished(self) -> bool:
        """True once the stream has reached EOF and the drain thread has ended -
        no more bytes will ever arrive."""
        return not self._thread.is_alive()

    @property
    def idle(self) -> float:
        """Seconds since the stream last produced output."""
        with self._lock:
            return time.monotonic() - self._last_activity

    def join(self, timeout_sec: float | None = None) -> bool:
        """Wait up to `timeout_sec` for the stream to reach EOF. Returns True if the
        draining thread has finished, i.e. all output has been captured."""
        self._thread.join(timeout_sec)
        return not self._thread.is_alive()

    def close(self) -> None:
        """Stop draining and close stream."""
        try:
            self._stream.close()
        except OSError:
            pass
        # Closing stream unblocks any blocked read that thread was waiting on.
        self._thread.join(timeout=2)

    def _drain(self) -> None:
        fd = self._stream.fileno()
        try:
            while True:
                chunk = os.read(fd, _READ_CHUNK)
                if not chunk:
                    break  # EOF: the writer closed the stream
                with self._lock:
                    self._buffer += chunk
                    self._last_activity = time.monotonic()
        except OSError:
            pass
        finally:
            try:
                self._stream.close()
            except OSError:
                pass


class StreamPrefixError(Exception):
    """Raised when a byte stream does not begin with the expected prefix."""


class AssertAndDiscardStreamPrefix:
    """Requires a byte stream to begin with a fixed prefix, and discards it.

    Fed the stream block by block via `feed`, it checks that the leading bytes
    equal `prefix`, strips them, and passes everything after through unchanged.
    A block that diverges from the prefix raises `StreamPrefixError` as soon as
    the first mismatching byte is seen, and the prefix may be split across any
    number of feeds. At end of stream, check `done`: a stream that ended before
    the whole prefix arrived never satisfied it.
    """

    def __init__(self, prefix: bytes):
        self._prefix = prefix
        self._matched = 0  # bytes of the prefix confirmed so far
        self._done = False

    @property
    def done(self) -> bool:
        """True once the whole prefix has been seen and discarded."""
        return self._done

    def feed(self, block: bytes) -> bytes:
        """Consume a block; return the bytes following the prefix (empty until
        the prefix is fully matched). Raises `StreamPrefixError` on a mismatch."""
        if self._done:
            return block
        remaining = self._prefix[self._matched :]
        n = min(len(remaining), len(block))
        if block[:n] != remaining[:n]:
            raise StreamPrefixError(f"stream does not start with {self._prefix!r}")
        self._matched += n
        if self._matched == len(self._prefix):
            self._done = True
            return block[n:]
        return b""
