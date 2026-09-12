# gvisor-agent-sandbox

A sandbox that gives an LLM agent shell access to an isolated environment: one
[gVisor](https://gvisor.dev/)-isolated Docker container per session, driven from the host
by running each command as its own `docker exec`. The threat model is a capable, possibly
hostile agent, and the boundary is gVisor plus no network: the agent runs as root *inside*
the container, but gVisor's sentry is itself deprivileged on the host, so container-root is
not host-root; the harness and any API key stay on the host, never inside the container;
and the container runs with `--network none`, so it has no egress to exfiltrate data, reach
a command-and-control host, or attack third parties. Nothing from the host is mounted in —
the agent's home directory `/root` is its workspace, and results are extracted from the
container separately. Dependencies a task needs are baked into the image, not fetched at
run time.

```python
from gvisor_agent_sandbox import Sandbox

with Sandbox() as sbx:
    sbx.shell_exec("python3 -m venv venv")            # runs in /root, the workspace
    sbx.shell_exec("/root/venv/bin/python --version") # use absolute paths across calls
```

## Why stateless commands instead of a persistent shell

State persists at two very different levels, and only the durable one is kept:

- **The container** (durable, kept): installed packages, files, and backgrounded processes
  live for the life of the sandbox.
- **The shell session** (deliberately not kept): cwd, exported environment, and shell
  functions do *not* carry from one command to the next. Each command is an independent
  `docker exec`.

A human leans hard on shell-session state; an agent does not need it — it can emit
absolute paths and chain state within a single command (`cd src && make`). Dropping the
persistent shell removes the whole problem of detecting when a command has finished on a
shared stream: with one process per command, "done" is just the process exiting, output
comes straight off that process's pipe, and there is no host-side file for command text or
output — and so none of the attack surface one brings.

A command that outruns its timeout is not treated as an error — it keeps running, and the
agent gets the output so far plus the choice to keep waiting (`shell_wait`) or stop it
(`shell_kill`). A slow test suite and a hung process look identical to a fixed timeout but
not to an agent holding the partial output, so the judgment call belongs there, not in the
harness.

## Tools

- `shell_exec(command, timeout=None)`
- `shell_wait(timeout=None)`
- `shell_kill()`

Every tool runs inside the container, so the container boundary is the only thing that has
to hold: no tool touches the host filesystem at a path the agent chooses. Files are created
through the shell, and a quoted heredoc carries content verbatim because the command is
passed to bash as its own argv element rather than spliced into a command line.

## Requirements

Docker, with the gVisor runtime registered as `runsc`, and the invoking user in the
`docker` group (no sudo needed — if you just added yourself to the group, start a new
shell for it to take effect).

## Design notes

See [DESIGN.md](DESIGN.md) for why the harness runs outside the container rather than
inside it (key custody, audit-log integrity, artifact purity).

A few non-obvious properties worth knowing before editing
`src/gvisor_agent_sandbox/sandbox.py`:

- Each command is one `docker exec ... bash -c 'echo "__PID__$$"; exec bash -c "$1" 2>&1'
  bash <command>`. The command is passed as its own argv element (`$1`), never spliced into
  a shell string, so arbitrary quotes, newlines, and backslashes need no escaping.
- The `echo "__PID__$$"` before the `exec` (which preserves the pid) prints the pid of the
  bash that runs the command. That pid is what `shell_kill` signals; it arrives as the
  guaranteed-first output line, which the reader strips, so no command output can be
  mistaken for it.
- `2>&1` merges stderr into stdout *inside the container*, because `docker exec` transports
  the two as separate streams whose ordering is lost in transit — the merge has to happen
  at the source.
- A syntax error or a shell-fatal setting (`set -e` then a failure) just makes that one
  command's bash exit non-zero; there is no shared session to wedge, and the next command
  is unaffected.

## Tests

```bash
uv run pytest                    # everything (~18s; needs Docker + gVisor)
uv run pytest -m "not docker"    # pure logic only (~0.1s; runs anywhere)
```

The suite is split so that half of it has no infrastructure requirements. `tests/`
covers output truncation, result rendering, and the tool surface (dispatch and the
not-running guards) without a container at all. The rest is marked `docker` and skipped
with a printed reason when the runtime isn't available, so `uv run pytest` is safe on a
machine that can't run containers.

The container tests share one session-scoped sandbox — since commands carry no state
between calls, the only per-test cleanup needed is killing a command a test left running —
which keeps the whole suite under twenty seconds.

## Status

Early, but the behaviour above is covered by tests rather than asserted in prose. No CI
yet; `-m "not docker"` is the half that could gate a pull request today.

## License

MIT — see [LICENSE](LICENSE).
