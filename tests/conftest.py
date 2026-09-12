"""Shared fixtures.

The container-backed tests are marked `docker` and skipped with a reason when
the runtime isn't there, so the pure-function tests still run anywhere -
including CI, which has no gVisor.
"""

import functools
import shutil
import subprocess

import pytest

from gvisor_agent_sandbox import Sandbox


@functools.cache
def _why_docker_unavailable() -> str | None:
    """None if the container tests can run, otherwise the reason they can't.

    Cached: this shells out, and the answer can't change mid-run.
    """
    if shutil.which("docker") is None:
        return "docker is not installed"
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{json .Runtimes}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"could not query docker ({e})"
    if result.returncode != 0:
        return f"docker daemon unreachable ({result.stderr.strip()[:120]})"
    if "runsc" not in result.stdout:
        return "the gVisor runtime is not registered as `runsc`"
    return None


def pytest_collection_modifyitems(config, items):
    reason = _why_docker_unavailable()
    if reason is None:
        return
    skip = pytest.mark.skip(reason=f"needs Docker + gVisor: {reason}")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def sbx():
    """One container for the whole run.

    A fresh container per test would be cleaner but takes seconds each. Commands
    carry no session state between calls, so the only thing that can leak across
    tests is a command left running; `_free_runner` clears that. (The workspace
    filesystem is shared, so tests use distinct paths under /root.)
    """
    with Sandbox() as sandbox:
        yield sandbox


@pytest.fixture(autouse=True)
def _free_runner(request):
    """Kill any command a test left running, so the next test isn't rejected.

    There is no shell session to reset - each command is independent - so this
    only has to clear a still-running command (e.g. a test that returned without
    killing one, or failed mid-command).
    """
    yield

    if "sbx" not in request.fixturenames:
        return  # a test that never touched the container
    runner = request.getfixturevalue("sbx").runner
    if runner is not None and runner._running is not None:
        runner.kill()
