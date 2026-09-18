# AI agentic sandbox

## Objective

Create an agentic sandbox. Agents access the sandbox through tools.

## Design

### Agent outside vs. agent inside the sandbox

Sandbox is accessed through tools, so the AI agent is not an installed process that runs
within the sandbox. It is outside the sandbox and accesses the sandbox through tool calls.

The primary motivation for putting the agent outside the sandbox is for security reasons.
If the AI agent were inside the sandbox the agent would be making API calls to the LLM
backend from within the sandbox. This would require that:

1. The sandbox be given access to the external networks
2. The LLM API key would need to be in the sandbox

Both of these are security concerns. Since the AI agent is outside, when we give it
permission to run arbitrary (potentially malicious commands) inside the sandbox, this does
not give it access to the API key (which is outside the sandbox) and this does not require
us to give the AI agent access to the external internet.

The AI agent runs on the host computer, but has no access to the host computer because the
tools provided to it only execute commands inside the sandbox.

### Persistent container, stateless commands

State persists at two very different levels, and only the durable one is kept:

- **Container (durable).** Files, installed packages, background processes. Everything
  expensive - the actual work product - lives here, for the life of the sandbox.
- **Shell session (not kept).** cwd, exported environment, shell functions. Each command
  runs as its own `docker exec`, so none of this carries from one command to the next.

The reasoning is in the README ("Why stateless commands instead of a persistent shell"): a
human needs shell-session state, an agent does not (absolute paths, chaining within one
command), and dropping the persistent shell removes the completion-detection problem that
a shared, long-lived stream forces. Detecting when a command has finished on a shared bash
stream is impossible without either reinterpreting the command or injecting a sentinel and
scanning output for it; process-per-command makes "done" simply the process exiting.

**Starting the container**

```sh
docker run --detach --rm --runtime runsc --network none -w /root <image> sleep infinity
```

Nothing from the host is mounted in, and there is no `--user`: the agent runs as root, and
gVisor - whose sentry is itself deprivileged on the host - is the boundary. Root's home
`/root` doubles as the workspace (it already exists and is writable), so no directory
needs creating or chowning. The container just idles on `sleep infinity`; it is the
durable layer, running as PID 1 inside. The work stays in the container's own filesystem
and is extracted separately (e.g. `docker cp`).

**Executing a command**

Each command is one exec:

```sh
docker exec -w /root <cid> bash -c 'echo "__PID__$$"; exec bash -c "$1" 2>&1' bash <command>
```

- **Command as argv, not string.** `<command>` is passed as its own argument (`$1`), never
  interpolated into a shell string, so arbitrary quotes, newlines, and backslashes need no
  escaping.
- **Pid line.** `echo "__PID__$$"` runs before the `exec` (which preserves the pid), so it
  prints the pid of the bash that goes on to run the command. That pid is what
  `shell_kill` targets (`pkill -P <pid>` for its children, `kill <pid>` for the bash). It
  is the guaranteed-first line of output, stripped by the reader, so no command output can
  be mistaken for it.
- **`2>&1` inside the container.** `docker exec` carries stdout and stderr as separate
  streams whose relative ordering is lost in transit, so stderr is merged into stdout at
  the source. The agent sees interleaved output as it would in a terminal.
- **stdin is /dev/null.** A command that reads stdin gets EOF rather than hanging.

Completion is the exec process exiting; `docker exec` propagates the command's exit code.
A reader thread drains the merged output so a still-running command's partial output can
be returned on a timeout, at which point the agent chooses `shell_wait` or `shell_kill`. A
syntax error or a shell-fatal setting (`set -e` then a failure) just makes that one
command's bash exit non-zero - there is no shared session to wedge.
