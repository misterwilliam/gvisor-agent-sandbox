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

    def snapshot(self) -> bytes:
        """Every byte drained so far. Cheap to call repeatedly."""
        with self._lock:
            return bytes(self._buffer)

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
