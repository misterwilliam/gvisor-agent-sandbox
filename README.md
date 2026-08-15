# gvisor-agent-sandbox

A sandbox that gives an LLM agent shell access to an isolated environment: one
[gVisor](https://gvisor.dev/)-isolated Docker container per session, with a single
persistent bash process inside it that the agent drives from the host over stdin/stdout.
The agent never holds real root, and — because the harness stays outside the container and
reaches in through tools rather than running inside it — never has a network path to
exfiltrate an API key it was never given.

```python
with Sandbox("/path/to/empty/workspace") as sbx:
    sbx.shell_exec("cd /workspace && python3 -m venv venv")
    sbx.shell_exec("source venv/bin/activate")
    sbx.shell_exec("pip install pytest")   # goes into the venv
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

- `file_write(path, content)`
- `shell_exec(command, timeout=None)`
- `shell_wait(timeout=None)`
- `shell_kill()`

## Requirements

Docker, with the gVisor runtime registered as `runsc`, and the invoking user in the
`docker` group (no sudo needed — if you just added yourself to the group, start a new
shell for it to take effect).

## Design notes

See [DESIGN.md](DESIGN.md) for why the harness runs outside the container rather than
inside it (key custody, audit-log integrity, artifact purity), and for the wire protocol
between the host and the persistent bash session.

A few non-obvious properties, each verified experimentally, worth knowing before editing
`sandbox.py`:

- The agent's command text is passed through a *file* and run via `builtin eval
  "$(<file)"`, never written to bash's stdin directly — raw text on stdin means an
  unterminated quote swallows the completion marker and wedges the session.
- `builtin printf`, `builtin eval`, and `$(<file)` (not `cat`) protect the wrapper from an
  agent that defines shell functions with those names.
- The command runs in a brace group `{ ...; }`, not a subshell, so `cd` and `export`
  persist across calls. A subshell or `&` would silently break session persistence.
- Sessions die from ordinary agent behavior (`set -e` then a failure; `set -o posix` then a
  syntax error). The design restarts and reports rather than trying to prevent every case.

## Status

Early. The self-test built into `sandbox.py` (`python3 sandbox.py`) is the current test
suite and the best executable documentation of what the sandbox guarantees. No CI yet.

## License

MIT — see [LICENSE](LICENSE).
