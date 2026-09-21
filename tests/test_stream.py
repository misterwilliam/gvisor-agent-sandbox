"""Unit tests for stream.py."""

import os
import time

from gvisor_agent_sandbox.stream import FdDrainer


def _pipe():
    """A (read_stream, write_stream) pair of unbuffered binary files."""
    r, w = os.pipe()
    return os.fdopen(r, "rb", buffering=0), os.fdopen(w, "wb", buffering=0)


def test_drains_all_bytes_written_before_eof():
    read, write = _pipe()
    drainer = FdDrainer(read)
    write.write(b"hello ")
    write.write(b"world")
    write.close()
    assert drainer.join(timeout_sec=2) is True
    assert drainer.snapshot() == b"hello world"


def test_snapshot_grows_as_more_arrives():
    read, write = _pipe()
    drainer = FdDrainer(read)

    write.write(b"part1")
    write.flush()
    _until(lambda: drainer.snapshot() == b"part1")

    write.write(b"part2")
    write.flush()
    _until(lambda: drainer.snapshot() == b"part1part2")

    write.close()
    drainer.close()


def test_join_reports_whether_eof_was_reached():
    read, write = _pipe()
    drainer = FdDrainer(read)
    assert drainer.join(timeout_sec=0.1) is False  # writer still open -> still draining
    write.close()
    assert drainer.join(timeout_sec=2) is True  # EOF -> thread finished


def test_idle_grows_while_the_stream_is_quiet():
    read, write = _pipe()
    drainer = FdDrainer(read)
    write.write(b"x")
    write.flush()
    _until(lambda: drainer.snapshot() == b"x")

    first = drainer.idle
    time.sleep(0.05)
    assert drainer.idle > first  # no new bytes, so idle time keeps climbing

    write.close()
    drainer.close()


def test_idle_resets_when_new_output_arrives():
    read, write = _pipe()
    drainer = FdDrainer(read)
    write.write(b"a")
    write.flush()
    _until(lambda: drainer.snapshot() == b"a")
    time.sleep(0.05)

    before = drainer.idle
    write.write(b"b")
    write.flush()
    _until(lambda: drainer.snapshot() == b"ab")
    assert drainer.idle < before  # fresh output pushed idle back down

    write.close()
    drainer.close()


def test_close_is_idempotent():
    read, write = _pipe()
    drainer = FdDrainer(read)
    write.write(b"data")
    write.close()
    drainer.close()
    drainer.close()  # second call must not raise


def _until(predicate, timeout_sec: float = 2.0) -> None:
    """Spin until the background thread has caught up, or fail the test."""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("condition not met within timeout")
