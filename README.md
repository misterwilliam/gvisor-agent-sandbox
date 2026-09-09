# gvisor-agent-sandbox

A sandbox that gives an LLM agent shell access to an isolated environment: one
[gVisor](https://gvisor.dev/)-isolated Docker container per session, with a single
persistent bash process inside it that the agent drives from the host over stdin/stdout.
The threat model is a capable, possibly hostile agent, so the container has three hard
edges: the agent never holds real root, the harness and any API key stay on the host
rather than inside the container, and the container runs with `--network none`, so it has
no egress to exfiltrate data, reach a command-and-control host, or attack third parties.
Dependencies a task needs are baked into the image, not fetched at run time.

```python
from gvisor_agent_sandbox import Sandbox

with Sandbox("/path/to/empty/workspace") as sbx:
    sbx.shell_exec("cd /workspace && python3 -m venv venv")
    sbx.shell_exec("source venv/bin/activate")            # the activation sticks...
    sbx.shell_exec("python -c 'import sys; print(sys.prefix)'")  # ...still inside the venv
```

## Why a persistent shell instead of one-shot exec

Two layers of state persist across calls, matching how a human's shell session actually
works:

- **The container** (durable): installed packages, files, background processes.
- **The shell session** (cheap, recoverable): cwd, exported environment, shell functions,
  aliases. `cd /workspace/build` in one call is still in effect on the next, and
  `source venv/bin/activate` actually sticks.

A command that outruns its timeout is not treated as an error — it keeps running, and the
agent gets the output so far plus the choice to keep waiting (`shell_wait`) or stop it
(`shell_kill`). A slow test suite and a hung process look identical to a fixed timeout but
not to an agent holding the partial output, so the judgment call belongs there, not in the
harness.

## Tools

- `shell_exec(command, timeout=None)`
- `shell_wait(timeout=None)`
- `shell_kill()`

Every tool runs inside the container, so the container boundary is the only thing that
has to hold: no tool touches the host filesystem at a path the agent chooses. Files are
created through the shell, and a quoted heredoc carries content verbatim because command
text reaches bash through a file rather than a command line.

## Requirements

Docker, with the gVisor runtime registered as `runsc`, and the invoking user in the
`docker` group (no sudo needed — if you just added yourself to the group, start a new
shell for it to take effect).

## Design notes

See [DESIGN.md](DESIGN.md) for why the harness runs outside the container rather than
inside it (key custody, audit-log integrity, artifact purity), and for the wire protocol
between the host and the persistent bash session.

A few non-obvious properties, each verified experimentally, worth knowing before editing
`src/gvisor_agent_sandbox/sandbox.py`:

- The agent's command text is passed through a *file* and run via `builtin eval
  "$(<file)"`, never written to bash's stdin directly — raw text on stdin means an
  unterminated quote swallows the completion marker and wedges the session.
- `builtin printf`, `builtin eval`, and `$(<file)` (not `cat`) protect the wrapper from an
  agent that defines shell functions with those names.
- The command runs in a brace group `{ ...; }`, not a subshell, so `cd` and `export`
  persist across calls. A subshell or `&` would silently break session persistence.
- Sessions die from ordinary agent behavior (`set -e` then a failure; `set -o posix` then a
  syntax error). The design restarts and reports rather than trying to prevent every case.

## Tests

```bash
uv run pytest                    # everything (~18s; needs Docker + gVisor)
uv run pytest -m "not docker"    # pure logic only (~0.1s; runs anywhere)
```

The suite is split so that half of it has no infrastructure requirements. `tests/`
covers output truncation, result rendering and the startup preconditions without a
container at all. The rest is marked `docker` and skipped with a printed reason when the
runtime isn't available, so `uv run pytest` is safe on a machine that can't run
containers.

The container tests share one session-scoped sandbox and reset the shell session between
tests, which keeps the whole suite under twenty seconds.

## Status

Early, but the behaviour above is covered by tests rather than asserted in prose. No CI
yet; `-m "not docker"` is the half that could gate a pull request today.

## License

MIT — see [LICENSE](LICENSE).
