"""Agentic sandbox: a gVisor-isolated container an agent drives through tools.

- One container per Sandbox instance (not per command), so installed
  packages, files, and background processes survive across calls.
- One long-lived bash process per Sandbox, so cwd, environment variables,
  shell functions, and aliases survive too. `cd /workspace/build` in one call
  is still in effect on the next, and `source venv/bin/activate` actually
  sticks.

    with Sandbox("/path/to/empty/workspace") as sbx:
        sbx.shell_exec("cd /workspace && python3 -m venv venv")
        sbx.shell_exec("source venv/bin/activate")
        sbx.shell_exec("pip install pytest")   # goes into the venv

A command that outruns its timeout is not an error. It stays running and the
agent gets the output so far, then decides whether to keep waiting
(`shell_wait`) or stop it (`shell_kill`). A slow test suite and a hung
process look identical to a fixed timeout but not to an agent holding the
partial output, so the judgment belongs there.
"""

import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

# Full python image (not -slim) is based on buildpack-deps, so it ships gcc,
# make, and friends - enough for "write a C compiler"-shaped tasks without a
# custom image.
DEFAULT_IMAGE = "python:3.12"
DEFAULT_RUNTIME = "runsc"

# Where the harness scratch mount lands inside the container. Deliberately
# NOT under /workspace: the workspace is the measured artifact and shouldn't
# be polluted with command text and captured output.
SCRATCH_GUEST = "/harness"

# Cap on how much output is handed back in a single result. Build and test
# logs would otherwise dominate the context window over a long run.
MAX_OUTPUT_BYTES = 30_000


class SandboxViolation(Exception):
    """Raised when a tool call tries to escape the sandbox workspace."""


class SandboxError(Exception):
    """Raised when the container or shell fails to start."""


def _resolve_in_workspace(root: Path, raw_path: str) -> Path:
    """Reinterpret raw_path (which may look absolute) as rooted at `root`.

    Rejects any path containing '..'. There is no other POSIX path syntax
    meaning "go up a directory", so this is complete against traversal-by-
    syntax; the remaining symlink-escape route is closed by requiring the
    workspace to start empty and by the container boundary itself.
    """
    if ".." in Path(raw_path).parts:
        raise SandboxViolation(f"path contains '..': {raw_path!r}")

    # Strip a leading '/' so an "absolute-looking" agent path is reinterpreted
    # relative to the workspace, not the host's real root.
    candidate = (root / raw_path.lstrip("/")).resolve()

    root_resolved = root.resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise SandboxViolation(f"path resolves outside workspace: {raw_path!r}")
    return candidate


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

    `output` is stdout and stderr interleaved - see the note on 2>&1 in
    PersistentShell for why they aren't kept apart. It contains only output
    not already returned by an earlier call, so repeated `shell_wait`s read
    like `tail -f` rather than re-sending the whole log each time.

    status:
      completed    - finished on its own; exit_code is meaningful
      running      - still going; the agent chooses wait or kill
      killed       - terminated on request; exit_code is the signal's
      session_lost - the shell died; container state is still intact
      rejected     - the call didn't make sense in the current state
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
        elif self.status == "session_lost":
            parts.append("session lost")
        else:
            parts.append(f"exit={self.exit_code}")
        if self.note:
            parts.append(f"[{self.note}]")
        parts.append(f"output:\n{_truncate(self.output)}")
        return "\n".join(parts)


class _Pending:
    """A command that was started and hasn't reported completion yet."""

    def __init__(self, token: str, marker: str, cmd_file: Path, out_file: Path):
        self.token = token
        self.marker = marker
        self.cmd_file = cmd_file
        self.out_file = out_file
        self.started_at = time.monotonic()
        self.last_output_at = time.monotonic()
        self.shown = 0  # characters of out_file already returned to the agent

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    @property
    def idle(self) -> float:
        return time.monotonic() - self.last_output_at


class PersistentShell:
    """A single long-lived `bash` inside the container, driven over stdin.

    Each command is executed as:

        { builtin eval "$(</harness/cmd_TOK)" ; } </dev/null >/harness/out_TOK 2>&1
        builtin printf '__DONE_TOK__%d\\n' $?

    Design notes, each of which is load-bearing:

    - The command text is passed through a *file*, so there is no shell
      escaping to get wrong - arbitrary quotes, newlines, and backslashes in
      the agent's command are handled verbatim.
    - Every name in the wrapper is protected from function shadowing, because
      an agent that defines `printf`, `eval`, or `cat` as a function would
      otherwise break the harness silently rather than loudly:
        * `builtin printf` - a shadowed `printf` would swallow the sentinel,
          making every subsequent command look like a hang.
        * `builtin eval` - a shadowed `eval` is worse: commands would appear
          to succeed while doing nothing at all.
        * `$(<file)` instead of `$(cat file)` - reads the file with bash's own
          redirection, so there is no external `cat` to shadow (and no
          process spawned).
      Aliases are not a threat here; non-interactive bash has `expand_aliases`
      off. Functions are.
    - `eval` contains syntax errors. Handing malformed source straight to the
      shell would abort the line before the sentinel printed, hanging the
      session over a typo. Under eval it's an ordinary non-zero exit.
    - A brace group `{ ...; }` runs in the *current* shell, so `cd` and
      `export` persist. A subshell `( ... )` or a background `&` would
      silently break that - this is the whole point of the class.
    - Output is redirected to a file rather than flowing through the pipe, so
      the pipe only ever carries a short sentinel: no large-output deadlock,
      and no chance of command output being mistaken for the sentinel. It
      also means a still-running command's output can be read at any moment,
      which is what makes shell_wait possible.
    - `2>&1` merges stderr into stdout deliberately. Relative ordering between
      the two streams is destroyed the moment they are captured separately, so
      this has to be decided here rather than at render time. Interleaved
      output preserves which diagnostic belongs to which step, matches what a
      human sees in a terminal, and avoids implying "stderr means error" -
      plenty of tools write progress there. The exit code carries success or
      failure.
    - stdin is /dev/null so a command that reads stdin gets EOF instead of
      eating the next command the harness writes.

    Sessions die for reasons that are ordinary agent behavior, not pathology:
    `set -e` followed by a failing command, or `set -o posix` followed by a
    syntax error, both terminate bash outright (verified). Rather than trying
    to enumerate and prevent these, the shell is simply restarted and the
    result says so. Only session-layer state (cwd, env, functions) is lost;
    files, packages, and background processes live in the container.
    """

    def __init__(self, container_id: str, scratch_host: Path, default_timeout: int = 60):
        self.container_id = container_id
        self.scratch_host = scratch_host
        self.default_timeout = default_timeout
        self._proc: subprocess.Popen | None = None
        self._queue: queue.Queue = queue.Queue()
        self._shell_pid: int | None = None
        self._pending: _Pending | None = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._proc = subprocess.Popen(
            ["docker", "exec", "-i", self.container_id, "bash"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            # bash's own stderr (as opposed to the command's, which is
            # redirected to a file) would only carry harness-level bugs.
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._queue = queue.Queue()
        # The proc and queue are passed explicitly rather than read off self:
        # after a restart, a lingering reader from the *previous* shell would
        # otherwise push its EOF sentinel into the *new* session's queue and
        # make a healthy shell look dead.
        threading.Thread(
            target=self._read_loop, args=(self._proc, self._queue), daemon=True
        ).start()
        self._shell_pid = self._probe_pid()

    @staticmethod
    def _read_loop(proc: subprocess.Popen, q: queue.Queue) -> None:
        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    q.put(line)
        except Exception:
            pass
        finally:
            q.put(None)  # EOF sentinel

    def alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None

    def _probe_pid(self) -> int | None:
        """Ask the shell for its own PID, needed to target its children when
        killing a running command."""
        token = uuid.uuid4().hex[:8]
        marker = f"__PID_{token}__"
        try:
            self._write(f"builtin printf '{marker}%d\\n' $$\n")
        except Exception:
            return None
        deadline = time.monotonic() + 10
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            if line.startswith(marker):
                try:
                    return int(line[len(marker) :].strip())
                except ValueError:
                    return None

    def _write(self, text: str) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise SandboxError("shell is not running")
        self._proc.stdin.write(text)
        self._proc.stdin.flush()

    # ---- the three operations -------------------------------------------

    def run(self, command: str, timeout: int | None = None) -> ShellResult:
        timeout = timeout if timeout is not None else self.default_timeout

        if self._pending is not None:
            return ShellResult(
                status="rejected",
                note=(
                    f"a command has been running for {self._pending.elapsed:.0f}s. "
                    "Use shell_wait to keep waiting or shell_kill to stop it before "
                    "running something else."
                ),
            )

        if not self.alive():
            self.start()

        token = uuid.uuid4().hex[:12]
        cmd_file = self.scratch_host / f"cmd_{token}"
        out_file = self.scratch_host / f"out_{token}"
        cmd_file.write_text(command)
        out_file.touch()

        marker = f"__DONE_{token}__"
        script = (
            f'{{ builtin eval "$(<{SCRATCH_GUEST}/cmd_{token})" ; }} '
            f"</dev/null >{SCRATCH_GUEST}/out_{token} 2>&1\n"
            f"builtin printf '{marker}%d\\n' $?\n"
        )

        try:
            self._write(script)
        except (BrokenPipeError, SandboxError):
            self.start()
            return ShellResult(
                status="session_lost",
                note="the shell session had ended and was restarted; cwd and environment were reset",
            )

        self._pending = _Pending(token, marker, cmd_file, out_file)
        return self._settle(timeout)

    def wait(self, timeout: int | None = None) -> ShellResult:
        timeout = timeout if timeout is not None else self.default_timeout
        if self._pending is None:
            return ShellResult(status="rejected", note="no command is currently running")
        return self._settle(timeout)

    def kill(self) -> ShellResult:
        if self._pending is None:
            return ShellResult(status="rejected", note="no command is currently running")

        pending = self._pending
        elapsed = pending.elapsed

        # TERM first, then KILL. Only direct children of the shell are
        # targeted; grandchildren (make -> gcc) can survive as orphans, which
        # is tolerable because the container is the real boundary and is
        # disposable.
        for signal, grace in (("TERM", 5), ("KILL", 5)):
            self._kill_children(signal)
            code = self._poll_for(pending.marker, grace)
            if code is not None:
                output = self._drain(pending)
                self._finish(pending)
                return ShellResult(
                    output,
                    exit_code=code,
                    status="killed",
                    note=f"SIG{signal} after {elapsed:.0f}s",
                )

        # Neither signal produced a sentinel: the session itself is wedged.
        output = self._drain(pending)
        self._finish(pending)
        self.close()
        self.start()
        return ShellResult(
            output,
            status="session_lost",
            note=(
                f"the command could not be killed after {elapsed:.0f}s, so the shell "
                "session was restarted; cwd and environment were reset, but files, "
                "packages, and background processes are intact"
            ),
        )

    # ---- internals -------------------------------------------------------

    def _settle(self, timeout: int) -> ShellResult:
        """Poll the pending command; either it completes, the session dies, or
        it's still running and the agent gets a snapshot."""
        assert self._pending is not None
        pending = self._pending

        code = self._poll_for(pending.marker, timeout)
        if code is not None:
            output = self._drain(pending)
            self._finish(pending)
            return ShellResult(output, exit_code=code, status="completed")

        if not self.alive():
            output = self._drain(pending)
            self._finish(pending)
            self.start()
            return ShellResult(
                output,
                status="session_lost",
                note=(
                    "the shell session ended while this command was running (a shell-fatal "
                    "setting such as `set -e` will do this) and was restarted; cwd and "
                    "environment were reset, but files, packages, and background processes "
                    "are intact"
                ),
            )

        return ShellResult(
            self._drain(pending),
            status="running",
            elapsed=pending.elapsed,
            idle=pending.idle,
        )

    def _drain(self, pending: _Pending) -> str:
        """Return output not yet shown to the agent, advancing the cursor."""
        if not pending.out_file.exists():
            return ""
        try:
            data = pending.out_file.read_text(errors="replace")
        except OSError:
            return ""
        new = data[pending.shown :]
        if new:
            pending.shown = len(data)
            pending.last_output_at = time.monotonic()
        return new

    def _finish(self, pending: _Pending) -> None:
        for f in (pending.cmd_file, pending.out_file):
            f.unlink(missing_ok=True)
        self._pending = None

    def _kill_children(self, signal: str) -> bool:
        if self._shell_pid is None:
            return False
        result = subprocess.run(
            [
                "docker", "exec", self.container_id,
                "pkill", f"-{signal}", "-P", str(self._shell_pid),
            ],
            capture_output=True,
            text=True,
        )
        # pkill exits 1 when nothing matched, 127 if it isn't installed.
        return result.returncode in (0, 1)

    def _poll_for(self, marker: str, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._queue.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:  # shell EOF
                return None
            if line.startswith(marker):
                try:
                    return int(line[len(marker) :].strip())
                except ValueError:
                    return -1
            # Any other line is a harness-level bug (bash complaining about
            # our wrapper); ignore it rather than corrupting the result.


class Sandbox:
    """A running gVisor container with a bind-mounted workspace and a
    persistent shell.

    The workspace directory is the artifact - it lives on the host and
    survives after the container is removed.
    """

    _SESSION_NOTE = (
        "Your working directory, environment variables, and shell functions persist "
        "between calls. Files, installed packages, and background processes live in the "
        "container and survive even if the shell session restarts. If the session ends "
        "unexpectedly (for example, a command runs `set -e` and then fails), it restarts "
        "automatically and the result will say so - your files and packages are "
        "unaffected, but you may need to `cd` again."
    )

    TOOLS = [
        {
            "name": "file_write",
            "description": (
                "Write text content to a file in your workspace, overwriting it if it "
                "already exists. Creates parent directories as needed."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path, resolved inside your workspace.",
                    },
                    "content": {"type": "string", "description": "Full text content to write."},
                },
                "required": ["path", "content"],
            },
        },
        {
            "name": "shell_exec",
            "description": (
                "Run a command in a persistent bash session inside your container. Returns "
                "the exit code and the command's output, with stdout and stderr interleaved "
                "as they would appear in a terminal. Your workspace is at /workspace.\n\n"
                + _SESSION_NOTE
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
                "status. The shell session itself survives, so your working directory and "
                "environment are preserved."
            ),
            "input_schema": {"type": "object", "properties": {}, "required": []},
        },
    ]

    def __init__(
        self,
        workspace: str | Path,
        image: str = DEFAULT_IMAGE,
        runtime: str = DEFAULT_RUNTIME,
        network: str = "none",
        memory: str = "2g",
        cpus: str = "2",
        pids_limit: int = 512,
        exec_timeout: int = 60,
        run_as_host_user: bool = True,
    ):
        self.workspace = Path(workspace).resolve()
        self.image = image
        self.runtime = runtime
        self.network = network
        self.memory = memory
        self.cpus = cpus
        self.pids_limit = pids_limit
        self.exec_timeout = exec_timeout
        self.run_as_host_user = run_as_host_user
        self.container_id: str | None = None
        self.scratch_host: Path | None = None
        self.shell: PersistentShell | None = None

    def start(self) -> None:
        if not self.workspace.is_dir():
            raise SandboxError(f"{self.workspace} is not a directory")
        if any(self.workspace.iterdir()):
            raise SandboxError(f"{self.workspace} is not empty - workspace must start clean")

        self.scratch_host = Path(tempfile.mkdtemp(prefix="sandbox-scratch-"))

        cmd = [
            "docker", "run", "-d", "--rm",
            "--runtime", self.runtime,
            "--network", self.network,
            "--memory", self.memory,
            "--cpus", self.cpus,
            "--pids-limit", str(self.pids_limit),
            "-v", f"{self.workspace}:/workspace",
            "-v", f"{self.scratch_host}:{SCRATCH_GUEST}",
            "-w", "/workspace",
        ]
        if self.run_as_host_user:
            # Files the agent creates end up owned by the host user rather than
            # root, so the workspace can be archived/deleted without sudo. Cost:
            # no apt-get inside the container (irrelevant under network=none).
            cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
        cmd += [self.image, "sleep", "infinity"]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            shutil.rmtree(self.scratch_host, ignore_errors=True)
            raise SandboxError(f"failed to start container: {result.stderr.strip()}")
        self.container_id = result.stdout.strip()

        self.shell = PersistentShell(self.container_id, self.scratch_host, self.exec_timeout)
        self.shell.start()

    def stop(self) -> None:
        if self.shell is not None:
            self.shell.close()
            self.shell = None
        if self.container_id is not None:
            subprocess.run(["docker", "rm", "-f", self.container_id], capture_output=True, text=True)
            self.container_id = None
        if self.scratch_host is not None:
            shutil.rmtree(self.scratch_host, ignore_errors=True)
            self.scratch_host = None

    def __enter__(self) -> "Sandbox":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    # ---- tool implementations -------------------------------------------

    def shell_exec(self, command: str, timeout: int | None = None) -> str:
        if self.shell is None:
            return "ERROR: sandbox is not running"
        return self.shell.run(command, timeout).render()

    def shell_wait(self, timeout: int | None = None) -> str:
        if self.shell is None:
            return "ERROR: sandbox is not running"
        return self.shell.wait(timeout).render()

    def shell_kill(self) -> str:
        if self.shell is None:
            return "ERROR: sandbox is not running"
        return self.shell.kill().render()

    def file_write(self, path: str, content: str) -> str:
        """Write a file into the workspace from the host side.

        Goes through the bind mount rather than the shell, which avoids having
        to get arbitrary file content through a command line at all - and works
        even while a long-running command holds the shell.
        """
        try:
            target = _resolve_in_workspace(self.workspace, path)
        except SandboxViolation as e:
            return f"ERROR: {e}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
            return f"wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"ERROR: {e}"

    def dispatch(self, tool_name: str, tool_input: dict) -> str:
        """Route a tool_use block to the matching method."""
        if tool_name == "file_write":
            return self.file_write(tool_input["path"], tool_input["content"])
        if tool_name == "shell_exec":
            return self.shell_exec(tool_input["command"], tool_input.get("timeout"))
        if tool_name == "shell_wait":
            return self.shell_wait(tool_input.get("timeout"))
        if tool_name == "shell_kill":
            return self.shell_kill()
        return f"ERROR: unknown tool {tool_name!r}"
