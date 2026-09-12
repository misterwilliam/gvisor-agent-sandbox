"""Agentic sandbox: a gVisor-isolated container an agent drives through tools.

The container is durable - one per Sandbox, so the filesystem, installed
packages, and any backgrounded processes persist across calls. Each command,
though, runs as its own `docker exec`; there is no persistent shell holding
working-directory or environment state between calls.

    with Sandbox("/path/to/empty/workspace") as sbx:
        sbx.shell_exec("cd /workspace && python3 -m venv venv")
        sbx.shell_exec("/workspace/venv/bin/python -c 'import sys; print(sys.prefix)'")

That trade is deliberate. A human leans hard on a persistent shell (cwd,
`export`, `source venv/bin/activate` all sticking); an agent does not need it -
it can emit absolute paths and chain state within one command - and dropping
the persistent shell removes the whole completion-detection problem it created.
Because each command is its own process, "done" is just the process exiting,
output comes straight off that process's pipe, and there is no host-side file
channel for command text or output (and so none of the attack surface one
brings).

A command that outruns its timeout is not an error. It stays running and the
agent gets the output so far, then decides whether to keep waiting
(`shell_wait`) or stop it (`shell_kill`). A slow test suite and a hung process
look identical to a fixed timeout but not to an agent holding the partial
output, so the judgment belongs there.

Requires: docker, and the gVisor runtime registered as `runsc`. The invoking
user must be in the `docker` group (no sudo needed - if you just added
yourself, start a new shell for it to take effect).
"""

import os
import subprocess
import threading
import time
from pathlib import Path

# Full python image (not -slim) is based on buildpack-deps, so it ships gcc,
# make, and friends - enough for "write a C compiler"-shaped tasks without a
# custom image.
DEFAULT_IMAGE = "python:3.12"

# The container's working directory, and where each command starts. The
# workspace bind mount lands here; it is the measured artifact.
WORKSPACE_GUEST = "/workspace"

# Cap on how much output is handed back in a single result. Build and test
# logs would otherwise dominate the context window over a long run.
MAX_OUTPUT_BYTES = 30_000



class Sandbox:
    """A running gVisor container with a bind-mounted workspace.

    The workspace directory is the artifact - it lives on the host and survives
    after the container is removed. Commands run one at a time via CommandRunner.
    """

    _EXEC_NOTE = (
        "Each call runs independently: the working directory resets to /workspace and "
        "environment variables set in one call do not carry over to the next. The "
        "filesystem, installed packages, and backgrounded processes do persist, since the "
        "container is durable. To use state within a single command, chain it - e.g. "
        "'cd src && make' - or use absolute paths (e.g. /workspace/venv/bin/python)."
    )

    TOOLS = [
        {
            "name": "shell_exec",
            "description": (
                "Run a command in your container. Returns the exit code and the command's "
                "output, with stdout and stderr interleaved as they would appear in a "
                "terminal. Your workspace is at /workspace.\n\n"
                + _EXEC_NOTE
                + "\n\nIf the command is still running when the timeout expires, you get the "
                "output so far instead of an error - the command keeps running. Use "
                "shell_wait to keep waiting or shell_kill to stop it. You cannot start "
                "another command until the running one finishes or is killed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Command to run, e.g. 'make test' or 'cd src && ls'.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds to wait before reporting back. Default 60.",
                    },
                },
                "required": ["command"],
            },
        },
        {
            "name": "shell_wait",
            "description": (
                "Keep waiting for the command that is currently running. Returns any output "
                "produced since the last call, plus how long the command has been running "
                "and whether it is still producing output - a build that is still printing "
                "is progressing, while one that has been silent for a long time may be "
                "stuck. If the command finishes, you get its exit code."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "timeout": {
                        "type": "integer",
                        "description": "Additional seconds to wait. Default 60.",
                    },
                },
                "required": [],
            },
        },
        {
            "name": "shell_kill",
            "description": (
                "Terminate the command that is currently running. Sends SIGTERM, then "
                "SIGKILL if that does not work. Returns any remaining output and the exit "
                "status. The container and its filesystem are unaffected."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
    ]

    def __init__(
        self,
        workspace: str | Path,
        image: str = DEFAULT_IMAGE,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 512,
        exec_timeout: int = 60,
        run_as_host_user: bool = True,
    ):
        self.workspace = Path(workspace).resolve()
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.exec_timeout = exec_timeout
        self.run_as_host_user = run_as_host_user
        self.container_id: str | None = None
        self.runner: CommandRunner | None = None

    def start(self) -> None:
        if not self.workspace.is_dir():
            raise SandboxError(f"{self.workspace} is not a directory")
        if any(self.workspace.iterdir()):
            raise SandboxError(f"{self.workspace} is not empty - workspace must start clean")

        cmd = [
            "docker", "run", "-d", "--rm",
            "--runtime", "runsc",
            # Not configurable. The threat model is a capable, possibly hostile
            # agent; egress would let it exfiltrate data, reach a C2 host, or
            # attack third parties from this machine - risks the syscall and
            # filesystem boundaries do nothing about. `none` is the whole point.
            "--network", "none",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "-v", f"{self.workspace}:{WORKSPACE_GUEST}",
            "-w", WORKSPACE_GUEST,
        ]
        if self.run_as_host_user:
            # Files the agent creates end up owned by the host user rather than
            # root, so the workspace can be archived/deleted without sudo. Cost:
            # no apt-get inside the container (irrelevant under network=none).
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
        cmd += [self.image, "sleep", "infinity"]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise SandboxError(f"failed to start container: {result.stderr.strip()}")
        self.container_id = result.stdout.strip()
        self.runner = CommandRunner(self.container_id, WORKSPACE_GUEST, self.exec_timeout)

    def stop(self) -> None:
        if self.runner is not None:
            self.runner.close()
            self.runner = None
        if self.container_id is not None:
            subprocess.run(["docker", "rm", "-f", self.container_id], capture_output=True, text=True)
            self.container_id = None

    def __enter__(self) -> "Sandbox":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---- tool implementations -------------------------------------------

    def shell_exec(self, command: str, timeout: int | None = None) -> str:
        if self.runner is None:
            return "ERROR: sandbox is not running"
        return self.runner.run(command, timeout).render()

    def shell_wait(self, timeout: int | None = None) -> str:
        if self.runner is None:
            return "ERROR: sandbox is not running"
        return self.runner.wait(timeout).render()

    def shell_kill(self) -> str:
        if self.runner is None:
            return "ERROR: sandbox is not running"
        return self.runner.kill().render()

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Route a tool_use block to the matching method."""
        if tool_name == "shell_exec":
            return self.shell_exec(tool_input["command"], tool_input.get("timeout"))
        if tool_name == "shell_wait":
            return self.shell_wait(tool_input.get("timeout"))
        if tool_name == "shell_kill":
            return self.shell_kill()
        return f"ERROR: unknown tool {tool_name!r}"


class SandboxError(Exception):
    """Raised when the container fails to start."""


class CommandRunner:
    """Runs one command at a time in the container, tracking the running one so
    it can be waited on or killed. Holds no shell state - each command is an
    independent `docker exec`."""

    def __init__(self, container_id: str, cwd: str = WORKSPACE_GUEST, default_timeout: int = 60):
        self.container_id = container_id
        self.cwd = cwd
        self.default_timeout = default_timeout
        self._running: _RunningCommand | None = None

    def run(self, command: str, timeout: int | None = None) -> ShellResult:
        timeout = timeout if timeout is not None else self.default_timeout
        if self._running is not None:
            return ShellResult(
                status="rejected",
                note=(
                    f"a command has been running for {self._running.elapsed:.0f}s. "
                    "Use shell_wait to keep waiting or shell_kill to stop it before "
                    "running something else."
                ),
            )
        self._running = _RunningCommand.start(self.container_id, self.cwd, command)
        return self._settle(timeout)

    def wait(self, timeout: int | None = None) -> ShellResult:
        timeout = timeout if timeout is not None else self.default_timeout
        if self._running is None:
            return ShellResult(status="rejected", note="no command is currently running")
        return self._settle(timeout)

    def kill(self) -> ShellResult:
        if self._running is None:
            return ShellResult(status="rejected", note="no command is currently running")

        rc = self._running
        elapsed = rc.elapsed
        # TERM first, then KILL. Grandchildren (make -> gcc) can survive as
        # orphans, which is tolerable because the container is the real boundary
        # and is disposable.
        for sig, grace in (("TERM", 5), ("KILL", 5)):
            rc.signal(sig)
            code = rc.wait_exit(grace)
            if code is not None:
                output = rc.drain()
                self._finish()
                return ShellResult(
                    output, exit_code=code, status="killed", note=f"SIG{sig} after {elapsed:.0f}s"
                )

        # The in-container process is unkillable via signals (should not happen
        # under gVisor); drop the exec client and move on.
        output = rc.drain()
        rc.close()
        self._running = None
        return ShellResult(
            output, status="killed", note=f"could not confirm exit after {elapsed:.0f}s"
        )

    def close(self) -> None:
        """Kill any in-flight command; called on sandbox teardown."""
        if self._running is not None:
            self._running.signal("KILL")
            self._running.close()
            self._running = None

    # ---- internals -------------------------------------------------------

    def _settle(self, timeout: int) -> ShellResult:
        rc = self._running
        assert rc is not None
        code = rc.wait_exit(timeout)
        if code is not None:
            output = rc.drain()
            self._finish()
            return ShellResult(output, exit_code=code, status="completed")
        return ShellResult(rc.drain(), status="running", elapsed=rc.elapsed, idle=rc.idle)

    def _finish(self) -> None:
        if self._running is not None:
            self._running.close()
            self._running = None


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Keep the head and tail of oversized output; errors usually live at the
    end, context usually at the start."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2) :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n... [{dropped} characters truncated] ...\n{tail}"


class ShellResult:
    """One command's outcome, in whatever state it's currently in.

    `output` is stdout and stderr interleaved - captured with stderr merged
    into stdout so their relative ordering (destroyed the moment the two are
    read separately) is preserved. It contains only output not already returned
    by an earlier call, so repeated `shell_wait`s read like `tail -f` rather
    than re-sending the whole log each time.

    status:
      completed - finished on its own; exit_code is meaningful
      running   - still going; the agent chooses wait or kill
      killed    - terminated on request; exit_code is the signal's, if known
      rejected  - the call didn't make sense in the current state
    """

    def __init__(
        self,
        output: str = "",
        exit_code: int | None = None,
        status: str = "completed",
        note: str = "",
        elapsed: float | None = None,
        idle: float | None = None,
    ):
        self.output = output
        self.exit_code = exit_code
        self.status = status
        self.note = note
        self.elapsed = elapsed
        self.idle = idle

    def render(self) -> str:
        if self.status == "rejected":
            return f"ERROR: {self.note}"

        if self.status == "running":
            head = f"still running after {self.elapsed:.0f}s"
            # Distinguishing a slow build from a hung process is the whole
            # reason the agent is being asked - give it the signal that
            # actually separates them.
            if self.output:
                head += f"; produced {len(self.output)} new characters of output"
            else:
                head += f"; no new output for {self.idle:.0f}s"
            body = _truncate(self.output) if self.output else "(nothing new)"
            return (
                f"{head}\n"
                f"output so far:\n{body}\n"
                f"The command is still running. Use shell_wait to keep waiting, "
                f"or shell_kill to stop it."
            )

        parts = []
        if self.status == "killed":
            parts.append(f"killed (exit={self.exit_code})")
        else:
            parts.append(f"exit={self.exit_code}")
        if self.note:
            parts.append(f"[{self.note}]")
        parts.append(f"output:\n{_truncate(self.output)}")
        return "\n".join(parts)


class _RunningCommand:
    """One `docker exec` in flight, with a thread draining its output.

    The command is launched as:

        docker exec -w <cwd> <cid> bash -c 'echo "__PID__$$"; exec bash -c "$1" 2>&1' bash <command>

    Three things earn that wrapper:

    - The command is passed as its own argv element (`$1`), never spliced into
      a shell string, so arbitrary quotes, newlines, and backslashes need no
      escaping - the same guarantee the old file-passing gave, without a file.
    - `echo "__PID__$$"` then `exec` prints the pid of the bash that (after the
      exec, which preserves the pid) runs the command. That pid is what
      `shell_kill` signals; it travels as the guaranteed-first output line,
      which the reader strips before any command output can appear, so nothing
      the command prints can be mistaken for it.
    - `2>&1` merges stderr into stdout *inside the container*. `docker exec`
      transports stdout and stderr as separate streams whose relative ordering
      is lost in transit, so the merge has to happen at the source, before the
      command's output leaves the container.

    stdin is /dev/null: a command that reads stdin gets EOF rather than hanging.
    A syntax error or a shell-fatal setting (`set -e` then a failure) just makes
    this one bash exit non-zero - there is no shared session to wedge.
    """

    def __init__(self, container_id: str, proc: subprocess.Popen):
        self.container_id = container_id
        self.proc = proc
        self.pid: int | None = None  # in-container pid of the command's bash
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self.started_at = time.monotonic()
        self.last_output_at = time.monotonic()
        self.shown = 0  # characters already returned to the agent
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    @classmethod
    def start(cls, container_id: str, cwd: str, command: str) -> "_RunningCommand":
        proc = subprocess.Popen(
            [
                "docker", "exec", "-w", cwd, container_id,
                "bash", "-c", 'echo "__PID__$$"; exec bash -c "$1" 2>&1', "bash", command,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # The command's stderr is already merged into stdout inside the
            # container (2>&1 above); this only folds in docker exec's own
            # client-side diagnostics.
            stderr=subprocess.STDOUT,
        )
        return cls(container_id, proc)

    # ---- output ----------------------------------------------------------

    def _reader(self) -> None:
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        header = b""
        pid_done = False
        try:
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                if not pid_done:
                    header += chunk
                    nl = header.find(b"\n")
                    if nl == -1:
                        continue
                    line, rest = header[:nl], header[nl + 1 :]
                    if line.startswith(b"__PID__"):
                        try:
                            self.pid = int(line[len(b"__PID__") :].strip())
                        except ValueError:
                            self.pid = -1
                    else:
                        # No pid line (e.g. docker exec itself errored); treat
                        # everything as output so the error still surfaces.
                        self.pid = -1
                        rest = header
                    pid_done = True
                    if rest:
                        self._append(rest)
                else:
                    self._append(chunk)
        except OSError:
            pass
        finally:
            try:
                self.proc.stdout.close()
            except OSError:
                pass

    def _append(self, data: bytes) -> None:
        with self._lock:
            self._buffer += data
            self.last_output_at = time.monotonic()

    def drain(self) -> str:
        """Return output not yet shown to the agent, advancing the cursor.

        Decodes the whole buffer and slices by character so a multi-byte
        sequence is never split at the cursor boundary.
        """
        with self._lock:
            data = bytes(self._buffer).decode(errors="replace")
        new = data[self.shown :]
        if new:
            self.shown = len(data)
        return new

    # ---- lifecycle -------------------------------------------------------

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def idle(self) -> float:
        return time.monotonic() - self.last_output_at

    def wait_exit(self, timeout: float) -> int | None:
        """Return the command's exit code if it finishes within `timeout`, else
        None. `docker exec` propagates the exec'd process's exit code."""
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        self._thread.join(timeout=2)  # let the reader capture the last bytes
        return self.proc.returncode

    def _await_pid(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self.pid is None and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.pid

    def signal(self, sig: str) -> None:
        """Signal the in-container command by pid: its children (pkill -P) and
        the command's bash itself. Falls back to killing the `docker exec`
        client if the pid never arrived."""
        pid = self._await_pid(2.0)
        if pid and pid > 0:
            subprocess.run(
                [
                    "docker", "exec", self.container_id, "bash", "-c",
                    f"pkill -{sig} -P {pid} 2>/dev/null; kill -{sig} {pid} 2>/dev/null; true",
                ],
                capture_output=True,
            )
        else:
            try:
                self.proc.terminate() if sig == "TERM" else self.proc.kill()
            except OSError:
                pass

    def close(self) -> None:
        """Make sure the exec client and its reader are gone."""
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except OSError:
                pass
        self._thread.join(timeout=2)

