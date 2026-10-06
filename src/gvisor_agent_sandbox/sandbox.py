"""Agentic sandbox: a gVisor-isolated container an agent drives through tools.

The container is durable - one per Sandbox, so the filesystem, installed
packages, and any backgrounded processes persist across calls. The agent runs
as root, and its home directory /root doubles as the workspace; nothing from
the host is mounted in, so the work lives inside the container until it is
extracted separately. Each command runs as its own `docker exec`; there is no
persistent shell holding working-directory or environment state between calls.

    with Sandbox() as sbx:
        sbx.shell_exec("python3 -m venv venv")   # runs in /root, the workspace
        sbx.shell_exec("/root/venv/bin/python -c 'import sys; print(sys.prefix)'")

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

import codecs
import logging
import subprocess
import threading
import time
import typing

from .stream import AssertAndDiscardStreamPrefix, FdDrainer, StreamPrefixError

_log = logging.getLogger(__name__)

# Full python image (not -slim) is based on buildpack-deps, so it ships gcc,
# make, and friends - enough for "write a C compiler"-shaped tasks without a
# custom image.
DEFAULT_IMAGE = "python:3.12"

# Cap on how much output is handed back in a single result. Build and test
# logs would otherwise dominate the context window over a long run.
MAX_OUTPUT_BYTES = 30_000

# How many commands can run at once. Each running command holds a `docker exec` process
# and a reader thread on the host, so this also bounds what the agent can use outside the
# container.
MAX_RUNNING_COMMANDS = 64


class Sandbox:
    """A running gVisor container the agent drives through tools.

    The agent runs as root with /root as its home and workspace. Nothing from
    the host is mounted in; the container's own filesystem holds the work, and
    results are extracted from it separately.
    """

    _EXEC_NOTE = (
        "Each call runs independently: the working directory resets to /root and "
        "environment variables set in one call do not carry over to the next. The "
        "filesystem, installed packages, and backgrounded processes do persist, since the "
        "container is durable. To use state within a single command, chain it - e.g. "
        "'cd src && make' - or use absolute paths (e.g. /root/venv/bin/python)."
    )

    TOOLS: typing.ClassVar = [
        {
            "name": "shell_exec",
            "description": (
                "Run a command in your container. Returns the exit code and the command's "
                "output, with stdout and stderr interleaved as they would appear in a "
                "terminal. You run as root; your commands will be executed from your "
                "home directory which is /root.\n\n"
                + _EXEC_NOTE
                + "\n\nIf the command is still running when the timeout expires, you get the "
                "output so far instead of an error - the command keeps running. The result "
                "names the command's id; pass it to shell_wait to keep waiting or to "
                "shell_kill to stop it. You can run other commands while one is running - "
                "for example, start a server and then query it - and each has its own id. "
                f"Up to {MAX_RUNNING_COMMANDS} commands can run at once."
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
                "Keep waiting for a running command, identified by the id shell_exec "
                "reported. Returns any output produced since the last call, plus how long "
                "the command has been running and whether it is still producing output - a "
                "build that is still printing is progressing, while one that has been "
                "silent for a long time may be stuck. If the command finishes, you get its "
                "exit code."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command_id": {
                        "type": "integer",
                        "description": "The id of the command, as reported by shell_exec.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Additional seconds to wait. Default 60.",
                    },
                },
                "required": ["command_id"],
            },
        },
        {
            "name": "shell_kill",
            "description": (
                "Terminate a running command, identified by the id shell_exec reported. "
                "Sends SIGTERM, then SIGKILL if that does not work. Returns any remaining "
                "output and the exit status. The container and its filesystem are "
                "unaffected."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "command_id": {
                        "type": "integer",
                        "description": "The id of the command, as reported by shell_exec.",
                    },
                },
                "required": ["command_id"],
            },
        },
    ]

    # Home directory of root user. This is the directory we will run all commands from.
    ROOT_USER_HOME_DIR = "/root"

    def __init__(
        self,
        image: str = DEFAULT_IMAGE,
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 512,
        exec_timeout: int = 60,
    ):
        self.image = image
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.exec_timeout = exec_timeout
        self.container_id: str | None = None
        # Commands that have started but whose final result hasn't been returned yet.
        self._commands: dict[int, SandboxedShellCommand] = {}
        # Ids increase and are never reused, so a stale id can't refer to a newer command.
        self._next_command_id = 1
        # Guards container_id, _commands, and _next_command_id, so the tools can be
        # called from several threads at once. It is only held for bookkeeping, never
        # while waiting on a command, so a long shell_wait on one thread doesn't block
        # calls on another.
        self._lock = threading.Lock()

    def start(self) -> None:
        # Start container and sleep forever.
        start_cmd = ["sleep", "infinity"]
        # fmt: off
        cmd = [
            "docker", "run",
            # Run container in background and print container ID
            "--detach",
            "--rm",
            "--runtime", "runsc",
            # Block network access.
            "--network", "none",
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            # Run commands from /root. Automatically created by docker if it does not exist, but
            # that is unlikely since images in general do have a /root directory.
            "--workdir", Sandbox.ROOT_USER_HOME_DIR,
            self.image,
        ] + start_cmd
        # fmt: on

        result = subprocess.run(cmd, capture_output=True, check=False, text=True)
        if result.returncode != 0:
            raise SandboxError(f"failed to start container: {result.stderr.strip()}")
        with self._lock:
            self.container_id = result.stdout.strip()

    def stop(self) -> None:
        # Removing the container ends every command running in it, so the commands are
        # only closed afterwards, never signaled: signaling reads a command's output,
        # which can raise, and nothing may stop the container from being removed.
        with self._lock:
            container_id, self.container_id = self.container_id, None
            commands = list(self._commands.values())
            self._commands.clear()
        if container_id is not None:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                check=False,
                text=True,
            )
        for command in commands:
            command.close()

    def __enter__(self) -> typing.Self:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---- tool implementations -------------------------------------------

    def shell_exec(self, command: str, timeout: int | None = None) -> ShellResult:
        """Run `command` and wait up to `timeout` seconds for it to finish."""
        # Checking the limit, starting the exec, and registering it happen under one
        # lock, so threads starting commands at once can't overshoot the limit, and
        # stop() either sees the command or prevents it from starting.
        with self._lock:
            if self.container_id is None:
                return ShellResult(status="rejected", note="sandbox is not running")
            if len(self._commands) >= MAX_RUNNING_COMMANDS:
                return ShellResult(
                    status="rejected",
                    note=(
                        f"{MAX_RUNNING_COMMANDS} commands are already running "
                        f"({self._running_ids()}), which is the limit. Use shell_wait or "
                        "shell_kill to finish one before starting another."
                    ),
                )
            command_id = self._next_command_id
            self._next_command_id += 1
            running = SandboxedShellCommand.start(
                command_id, self.container_id, Sandbox.ROOT_USER_HOME_DIR, command
            )
            self._commands[command_id] = running
        timeout = timeout if timeout is not None else self.exec_timeout
        return self._collect(running, lambda: running.wait(timeout))

    def shell_wait(self, command_id: int, timeout: int | None = None) -> ShellResult:
        """Wait up to `timeout` more seconds for command `command_id` to finish."""
        with self._lock:
            if self.container_id is None:
                return ShellResult(status="rejected", note="sandbox is not running")
            running = self._commands.get(command_id)
            if running is None:
                return self._unknown_command(command_id)
        timeout = timeout if timeout is not None else self.exec_timeout
        return self._collect(running, lambda: running.wait(timeout))

    def shell_kill(self, command_id: int) -> ShellResult:
        """Stop command `command_id`."""
        with self._lock:
            if self.container_id is None:
                return ShellResult(status="rejected", note="sandbox is not running")
            running = self._commands.get(command_id)
            if running is None:
                return self._unknown_command(command_id)
        return self._collect(running, running.kill)

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Route a tool_use block to the matching method and return the text the model
        sees."""
        # A missing or malformed command_id is passed through as-is: it matches no
        # running command, so the model gets an error result rather than the harness
        # raising.
        if tool_name == "shell_exec":
            return str(self.shell_exec(tool_input["command"], tool_input.get("timeout")))
        if tool_name == "shell_wait":
            return str(self.shell_wait(tool_input.get("command_id"), tool_input.get("timeout")))
        if tool_name == "shell_kill":
            return str(self.shell_kill(tool_input.get("command_id")))
        return f"ERROR: unknown tool {tool_name!r}"

    # ---- internals -------------------------------------------------------

    def _collect(
        self, running: SandboxedShellCommand, action: typing.Callable[[], ShellResult]
    ) -> ShellResult:
        """Return the result of `action` on a running command, and forget the command
        once it has finished or failed."""
        try:
            result = action()
        except StreamPrefixError as e:
            # The exec's output did not start with the expected __PID__ line, so it
            # can't be trusted to be the command's own output - for example, when
            # docker exec failed to start the command. None of it reaches the agent.
            _log.warning("command %d: discarding untrusted output: %s", running.id, e)
            result = ShellResult(
                status="failed",
                note=(
                    f"command {running.id} could not be run in the sandbox, so no output "
                    "is available. This can happen when the sandbox is out of resources; "
                    "stopping other commands may help."
                ),
                command_id=running.id,
            )
        # A command stays registered until its final result has been returned, so its
        # last output and exit code can't be lost. Two threads can both see it finish
        # (say, a wait and a kill), or stop() can get there first; only whoever removes
        # it from the registry closes it.
        if result.status != "running":
            with self._lock:
                finished = self._commands.pop(running.id, None)
            if finished is not None:
                finished.close()
        return result

    def _unknown_command(self, command_id: object) -> ShellResult:
        # The caller holds self._lock.
        return ShellResult(
            status="rejected",
            note=f"no running command {command_id!r} ({self._running_ids()})",
        )

    def _running_ids(self) -> str:
        # The caller holds self._lock.
        if not self._commands:
            return "no commands are running"
        return "running: " + ", ".join(str(i) for i in sorted(self._commands))


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
      failed    - the command couldn't be run, or its output couldn't be trusted;
                  no output is returned
    """

    def __init__(
        self,
        output: str = "",
        exit_code: int | None = None,
        status: str = "completed",
        note: str = "",
        elapsed: float | None = None,
        idle: float | None = None,
        command_id: int | None = None,
    ):
        self.output = output
        self.exit_code = exit_code
        self.status = status
        self.note = note
        self.elapsed = elapsed
        self.idle = idle
        self.command_id = command_id

    def __str__(self) -> str:
        """The text the model receives for this result."""
        if self.status in ("rejected", "failed"):
            return f"ERROR: {self.note}"

        label = f"command {self.command_id}: " if self.command_id is not None else ""
        if self.status == "running":
            head = f"{label}still running after {self.elapsed:.0f}s"
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
                f"The command is still running. Use shell_wait to keep waiting, or "
                f"shell_kill to stop it, with command_id {self.command_id}."
            )

        parts = []
        if self.status == "killed":
            parts.append(f"{label}killed (exit={self.exit_code})")
        else:
            parts.append(f"{label}exit={self.exit_code}")
        if self.note:
            parts.append(f"[{self.note}]")
        parts.append(f"output:\n{_truncate(self.output)}")
        return "\n".join(parts)


class SandboxError(Exception):
    """Raised when the container fails to start."""


class SandboxedShellCommand:
    """One `docker exec` in flight, with a `FdDrainer` reading its output.

    The command is launched as:

        docker exec --workdir <cwd> <cid> bash -c 'echo "__PID__$$"; exec bash -c "$1" 2>&1' bash <command>

    Three things earn that wrapper:

    - The command is passed as its own argv element (`$1`), never spliced into
      a shell string, so arbitrary quotes, newlines, and backslashes need no
      escaping.
    - `echo "__PID__$$"` then `exec` prints the pid of the bash that (after the
      exec, which preserves the pid) runs the command. That pid is what
      `shell_kill` signals; it travels as the guaranteed-first output line,
      which `drain` strips before any command output can appear, so nothing
      the command prints can be mistaken for it.
    - `2>&1` merges stderr into stdout *inside the container*. `docker exec`
      transports stdout and stderr as separate streams whose relative ordering
      is lost in transit, so the merge has to happen at the source, before the
      command's output leaves the container.

    stdin is /dev/null: a command that reads stdin gets EOF rather than hanging.
    A syntax error or a shell-fatal setting (`set -e` then a failure) just makes
    this one bash exit non-zero - there is no shared session to wedge.
    """

    def __init__(self, command_id: int, container_id: str, proc: subprocess.Popen):
        assert proc.stdout is not None
        self.id = command_id  # the id the agent uses to refer to this command
        self.container_id = container_id
        self.proc = proc
        self.pid: int | None = None  # in-container pid of the command's bash
        self.started_at = time.monotonic()
        self._drainer = FdDrainer(proc.stdout)
        # Output interpretation, fed block-by-block from the drainer: assert and
        # strip the __PID__ sentinel, read the pid, then decode the rest
        # incrementally so a UTF-8 sequence split across two reads is never
        # mangled at the boundary.
        self._prefix = AssertAndDiscardStreamPrefix(b"__PID__")
        self._pid_parsed = False
        self._pid_line = bytearray()  # the <pid>\n after the sentinel, until the newline
        self._decoder: codecs.IncrementalDecoder | None = codecs.getincrementaldecoder("utf-8")(
            errors="replace"
        )
        self._pending = ""  # decoded output produced but not yet returned by drain()
        # Guards the output state above (prefix check, pid, decoder, pending output):
        # a wait and a kill on different threads can both read this command's output.
        self._output_lock = threading.Lock()

    @classmethod
    def start(
        cls, command_id: int, container_id: str, cwd: str, command: str
    ) -> SandboxedShellCommand:
        # container_cmd is what docker exec runs inside the container.
        # bash -c accepts the following format:
        # bash -c <script> $0 $1
        # The script in our case is 'echo "__PID__$$"; exec bash -c "$1" 2>&1'
        # $0 is set to "bash"
        # $1 is set to <command>
        # The script is what is executed. Within the script $0 (ie the shell name) is set to bash,
        # and $1 is set to <command>. The script first echos the PID of the bash process
        # prefixed by __PID__ then exec replaces the current bash process image with a new bash
        # process so that the PID stays constant. Then script executes $1 (ie <command>). The
        # stdout and stderr of <command> are interleaved into stdout.
        # fmt: off
        container_cmd = [
            "bash", "-c",
            'echo "__PID__$$"; exec bash -c "$1" 2>&1', # script
            "bash",  # $0
            command, # $1
        ]
        # fmt: on
        # Run <container_cmd> inside <container_id>. Stdout of the subprocess will be stdout of
        # docker exec. Key security assumption: we are going to assume that this is purely the
        # stdout of container_cmd and does not contain anything from running docker exec. Docker
        # exec stderr can contain the error message of running docker exec (not the container_cmd)
        # so we are going to discard that. Therefore stdout will be:
        # __PID__<bash PID><interleaved stdout and stderr><stream close>
        # If stdout does not start with __PID__, the stream is not purely
        # container_cmd's stdout; AssertAndDiscardStreamPrefix enforces this and
        # raises StreamPrefixError.
        # fmt: off
        proc = subprocess.Popen(
            [
                "docker",
                "exec",
                "--workdir", cwd,
                container_id,
            ] + container_cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            # TODO: Ignore docker exec's err messages for now. When we add logging to the sandbox
            # redirect this to the sandbox logs. Safe to discard because container_cmd merged stderr
            # into stdout.
            stderr=subprocess.DEVNULL,
        )
        # fmt: on

        return cls(command_id, container_id, proc)

    def wait(self, timeout: float) -> ShellResult:
        """Wait up to `timeout` seconds for the command to exit. Returns its final result
        if it did, otherwise the output produced since the last call."""
        code = self.wait_exit(timeout)
        if code is not None:
            return ShellResult(self.drain(), exit_code=code, status="completed", command_id=self.id)
        return ShellResult(
            self.drain(),
            status="running",
            elapsed=self.elapsed,
            idle=self.idle,
            command_id=self.id,
        )

    def kill(self) -> ShellResult:
        """Stop the command with SIGTERM, then SIGKILL if it is still running."""
        elapsed = self.elapsed
        # TERM first, then KILL. Grandchildren (make -> gcc) can survive as
        # orphans, which is tolerable because the container is the real boundary
        # and is disposable.
        for sig, grace in (("TERM", 5), ("KILL", 5)):
            self.signal(sig)
            code = self.wait_exit(grace)
            if code is not None:
                return ShellResult(
                    self.drain(),
                    exit_code=code,
                    status="killed",
                    note=f"SIG{sig} after {elapsed:.0f}s",
                    command_id=self.id,
                )
        # The in-container process is unkillable via signals (should not happen
        # under gVisor); the caller drops the exec client and moves on.
        return ShellResult(
            self.drain(),
            status="killed",
            note=f"could not confirm exit after {elapsed:.0f}s",
            command_id=self.id,
        )

    def drain(self) -> str:
        """Return output produced since the last call (empty if none yet)."""
        # Pull the next block, strip the __PID__ header once, decode incrementally.
        with self._output_lock:
            self._ingest()
            out = self._pending
            self._pending = ""
            return out

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def idle(self) -> float:
        return self._drainer.idle

    def wait_exit(self, timeout: float) -> int | None:
        """Return the command's exit code if it finishes within `timeout`, else None."""
        try:
            self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            return None
        self._drainer.join(timeout_sec=2)  # let the drainer capture the last bytes
        return self.proc.returncode  # docker exec propagates the command's exit code

    def signal(self, sig: str) -> None:
        """Send signal `sig` to the running command."""
        pid = self._await_pid(2.0)
        if pid and pid > 0:
            # The pid is the bash the wrapper exec'd into; signal it and its
            # children (pkill -P) so the whole command tree gets the signal.
            subprocess.run(
                [
                    "docker",
                    "exec",
                    self.container_id,
                    "bash",
                    "-c",
                    f"pkill -{sig} -P {pid} 2>/dev/null; kill -{sig} {pid} 2>/dev/null; true",
                ],
                capture_output=True,
                check=False,
            )
        else:
            # The pid never arrived (e.g. a very early kill); fall back to
            # killing the docker exec client.
            try:
                self.proc.terminate() if sig == "TERM" else self.proc.kill()
            except OSError:
                pass

    def close(self) -> None:
        """Make sure the exec client and its drainer are gone."""
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                self.proc.kill()
            except OSError:
                pass
        self._drainer.close()

    def _ingest(self) -> None:
        """Pull the next block from the drainer and turn it into pending output:
        assert and strip the __PID__ header the first time, then decode the rest.

        Both `drain` and `_await_pid` call this - draining the drainer parses the
        pid as a side effect - so output pulled while waiting for the pid is held
        in `_pending` for the next `drain`, never lost.
        """
        # The caller holds self._output_lock.
        if self._decoder is None:
            return  # a final decode already flushed the decoder at EOF
        block = self._drainer.read()
        final = self._drainer.finished()
        if not block and not final:
            return
        # _strip_pid raises StreamPrefixError if __PID__ is absent, or (at EOF) truncated.
        output = self._strip_pid(block, final)
        self._pending += self._decoder.decode(output, final=final)
        if final:
            self._decoder = None  # a final decode cannot be reused

    def _strip_pid(self, block: bytes, final: bool) -> bytes:
        r"""Consume the `__PID__<pid>\n` header the wrapper prints first,
        recording the pid, and return the bytes that are real output.

        `AssertAndDiscardStreamPrefix` verifies and removes the `__PID__`
        sentinel (raising `StreamPrefixError` if the stream does not start with
        it - see the security note in `start`); what remains is `<pid>\n` then
        the command's output.
        """
        if self._pid_parsed:
            return block
        after_sentinel = self._prefix.feed(block, end=final)  # raises if __PID__ absent/truncated
        if not after_sentinel:
            return b""  # still matching the sentinel
        self._pid_line += after_sentinel
        nl = self._pid_line.find(b"\n")
        if nl == -1:
            return b""  # pid digits not terminated yet
        line, rest = bytes(self._pid_line[:nl]), bytes(self._pid_line[nl + 1 :])
        try:
            self.pid = int(line.strip())
        except ValueError:
            self.pid = -1
        self._pid_parsed = True
        return rest

    def _await_pid(self, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while self.pid is None and time.monotonic() < deadline:
            with self._output_lock:
                self._ingest()  # parses the pid as a side effect; output goes to _pending
            if self.pid is not None:
                break
            time.sleep(0.02)
        return self.pid


def _truncate(text: str, limit: int = MAX_OUTPUT_BYTES) -> str:
    """Keep the head and tail of oversized output; errors usually live at the
    end, context usually at the start."""
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-(limit // 2) :]
    dropped = len(text) - len(head) - len(tail)
    return f"{head}\n... [{dropped} characters truncated] ...\n{tail}"
