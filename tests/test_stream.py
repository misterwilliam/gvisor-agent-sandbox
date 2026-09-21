"""Unit tests for stream.py."""

import os
import time

import pytest

from gvisor_agent_sandbox.stream import (
    AssertAndDiscardStreamPrefix,
    FdDrainer,
    StreamPrefixError,
)


def _pipe():
    """A (read_stream, write_stream) pair of unbuffered binary files."""
    r, w = os.pipe()
    return os.fdopen(r, "rb", buffering=0), os.fdopen(w, "wb", buffering=0)


def _read_until(drainer, expected: bytes, timeout_sec: float = 2.0) -> bytes:
    """Accumulate reads until at least len(expected) bytes arrive (a read can
    chunk arbitrarily), or fail the test."""
    got = b""
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        got += drainer.read()
        if len(got) >= len(expected):
            return got
        time.sleep(0.005)
    raise AssertionError(f"expected {expected!r}, only saw {got!r} within timeout")


def test_read_returns_all_bytes_written_before_eof():
    read, write = _pipe()
    drainer = FdDrainer(read)
    write.write(b"hello ")
    write.write(b"world")
    write.close()
    assert drainer.join(timeout_sec=2) is True
    assert drainer.read() == b"hello world"


def test_read_consumes_so_a_second_read_is_empty():
    read, write = _pipe()
    drainer = FdDrainer(read)
    write.write(b"once")
    write.close()
    drainer.join(timeout_sec=2)
    assert drainer.read() == b"once"
    assert drainer.read() == b""  # already handed over


def test_read_returns_only_new_bytes_each_call():
    read, write = _pipe()
    drainer = FdDrainer(read)

    write.write(b"part1")
    write.flush()
    assert _read_until(drainer, b"part1") == b"part1"  # consumes part1

    write.write(b"part2")
    write.flush()
    assert _read_until(drainer, b"part2") == b"part2"  # only the new bytes

    write.close()
    drainer.close()


def test_finished_reflects_eof():
    read, write = _pipe()
    drainer = FdDrainer(read)
    assert drainer.finished() is False  # writer still open
    write.close()
    drainer.join(timeout_sec=2)
    assert drainer.finished() is True


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
    _read_until(drainer, b"x")  # ensure the byte arrived (idle just reset)

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
    _read_until(drainer, b"a")
    time.sleep(0.05)

    before = drainer.idle
    write.write(b"b")
    write.flush()
    _read_until(drainer, b"b")
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


# ---- AssertAndDiscardStreamPrefix ---------------------------------------


def test_prefix_discarded_when_present_in_one_block():
    f = AssertAndDiscardStreamPrefix(b"__PID__")
    assert f.feed(b"__PID__123\nrest") == b"123\nrest"
    assert f.done is True
    assert f.feed(b"more") == b"more"  # everything after the prefix passes through


def test_prefix_split_across_blocks():
    f = AssertAndDiscardStreamPrefix(b"__PID__")
    assert f.feed(b"__PI") == b""  # partial prefix, nothing to emit yet
    assert f.done is False
    assert f.feed(b"D__tail") == b"tail"
    assert f.done is True


def test_prefix_exactly_then_nothing():
    f = AssertAndDiscardStreamPrefix(b"__PID__")
    assert f.feed(b"__PID__") == b""
    assert f.done is True


def test_mismatch_raises():
    f = AssertAndDiscardStreamPrefix(b"__PID__")
    with pytest.raises(StreamPrefixError):
        f.feed(b"__XID__nope")


def test_mismatch_detected_across_a_split():
    f = AssertAndDiscardStreamPrefix(b"__PID__")
    assert f.feed(b"__") == b""
    with pytest.raises(StreamPrefixError):
        f.feed(b"XID__")  # diverges at the third byte, before the full prefix arrives


def test_empty_feeds_are_harmless():
    f = AssertAndDiscardStreamPrefix(b"__PID__")
    assert f.feed(b"") == b""
    assert f.done is False
    assert f.feed(b"__PID__x") == b"x"
