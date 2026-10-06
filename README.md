# gvisor-agent-sandbox

A security sandbox suitable for hosting an AI agent. The AI agent accesses the sandbox as
a tool. Sandbox is a wrapper around docker so it fits with container-based workflows and
uses the docker `runsc` runtime (which uses [gVisor](https://gvisor.dev/)) for the
security sandbox.

Simple usage example:

```python
from gvisor_agent_sandbox import Sandbox

with Sandbox() as sbx:
    sbx.shell_exec("ls")
```

The threat model is a sophisticated and malicious agent. The agent is hosted inside the
sandbox with the following security boundaries:

- **gVisor.** Docker `runsc` runtime uses the [gVisor](https://gvisor.dev/) security
  sandbox to give each a VM-like security sandbox. The agent runs inside a gVisor sandbox,
  so the sandbox is still vulnerable to gVisor vulnerabilities.
- **No network.** The container has no network access, so the agent cannot perform attacks
  that require external network access. Libraries and packages the agent needs come from
  provided Docker image (`Sandbox(image=...)`), since nothing can be downloaded at run
  time.
- **Filesystem isolation from host.** No host files are mounted into the container.
- **Resource limits.** Memory, CPU, and process limits cap resource consumption.
- **Harness and keys stay on the host.** The agent acts only through tools that run
  commands inside the sandbox, so the harness and the API key are out of its reach.

Access to the sandbox is provided through tools and therefore the harness runs on the
host, but the agent is given no access to the host. This allows the LLM API keys to stay
on the host accessible to the harness, but inaccessible to the agent which can only access
the sandbox.

Example use case with agentic harness:

```python
import anthropic
from gvisor_agent_sandbox import Sandbox

MODEL = "claude-sonnet-5"
SYSTEM = "..."
TASK = "..."

# Anthropic API key specified through ANTHROPIC_API_KEY environment variable.
client = anthropic.Anthropic()
messages: list[dict] = [{"role": "user", "content": TASK}]

with Sandbox() as sbx:
    for turn in range(10):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM,
            # Provides agent access to sandbox as a tool.
            tools=sbx.TOOLS,
            messages=messages,
            cache_control={"type": "ephemeral"},
        )
        messages.append({"role": "assistant", "content": response.content})

        results = []
        for block in response.content:
            if block.type == "text":
                print(f"\n[{turn}] claude: {block.text}")
            elif block.type == "tool_use":
                print(f"\n[{turn}] {block.name}: {block.input}")
                output = sbx.dispatch(block.name, block.input)
                print("> " + output)
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": output,
                    }
                )

        # No tool calls means the model is done talking to the container.
        if not results:
            print(f"\n=== finished after {turn} turns ({response.stop_reason}) ===")
            break
        messages.append({"role": "user", "content": results})
```

For a full example with agentic loop, see `examples/agent_loop.py` or run with:

```sh
export ANTHROPIC_API_KEY=...
uv run --group examples python examples/agent_loop.py
```

## Installation

Requirements:

- Linux with Docker
- gVisor's `runsc` registered as a Docker runtime
- [uv](https://docs.astral.sh/uv/)

1. Install `runsc` by following
   [gVisor's install guide](https://gvisor.dev/docs/user_guide/install/), then register it
   with Docker:

   ```sh
   sudo runsc install  # adds the runsc runtime to /etc/docker/daemon.json
   sudo systemctl restart docker
   ```

2. Add user to docker group, so that we can run Docker without sudo. The docker group has
   root equivalent capabilities. This just allows us to run docker without sudo:

   ```sh
   sudo usermod -aG docker "$USER"
   newgrp docker
   ```

3. Clone the repository and install its dependencies:

   ```sh
   git clone https://github.com/misterwilliam/gvisor-agent-sandbox.git
   cd gvisor-agent-sandbox
   uv sync  # add --group examples to also install what examples/agent_loop.py needs
   ```

4. Run the tests:

   ```sh
   uv run pytest
   ```

   The first run downloads `python:3.12`, the sandbox's default image (about 1.6 GB), and
   prints nothing while it does, so it can look stuck for a few minutes. To download it
   ahead of time, run `docker pull python:3.12`.

## Sandbox API exposed to the agent

The agent gets three tools:

- `shell_exec(command, timeout)` - run a bash command in the sandbox. Returns the
  command's id, its exit code, and its output, with stdout and stderr interleaved as they
  would appear in a terminal.
- `shell_wait(command_id, timeout)` - keep waiting for a running command and get its new
  output.
- `shell_kill(command_id)` - stop a running command (SIGTERM, then SIGKILL).

Commands run as root, starting in `/root`. Each command is a separate process. A command
that runs past its timeout keeps running; the agent gets the output so far and decides
whether to wait or kill it. Up to 32 commands can run at once, so the agent can start a
server in one command and query it from another. Long output is cut down to its first and
last 15,000 characters.

Sandboxed agent is exposed to the agent access to a docker image where they can run a bash
command with every tool call. Side effects of the each bash command persist across tool
calls, but the sandbox is not given a persistent bash session across tools calls. So
changes to environment variables or current working directory are not persisted. The
rationale for this design choice is because:

1. A persistent shell where `cd` and defining environment variables carry over are what
   make terminal sessions for humans, but they are not necessary for agents which can use
   absolute paths, chain multiple commands within on command, and carry over the necessary
   environment variables across tool calls.
2. With a persistent shell, the harness needs to detect when the output of a command ends.
   Humans watch for the shell prompt to detect when a command returns, but a malicious
   agent can change the prompt or inject fake end of command markers. Adding logic within
   the harness to handle these scenarios adds a complicated potential attack surface.

## Design notes

See [DESIGN.md](DESIGN.md) for why the harness runs outside the container rather than
inside it (key custody, audit-log integrity, artifact purity).

A few non-obvious properties worth knowing before editing
`src/gvisor_agent_sandbox/sandbox.py`:

- Each command is one
  `docker exec ... bash -c 'echo "__PID__$$"; exec bash -c "$1" 2>&1' bash <command>`. The
  command is passed as its own argv element (`$1`), never spliced into a shell string, so
  arbitrary quotes, newlines, and backslashes need no escaping.
- The `echo "__PID__$$"` before the `exec` (which preserves the pid) prints the pid of the
  bash that runs the command. That pid is what `shell_kill` signals; it arrives as the
  guaranteed-first output line, which the reader strips, so no command output can be
  mistaken for it.
- `2>&1` merges stderr into stdout _inside the container_, because `docker exec`
  transports the two as separate streams whose ordering is lost in transit - the merge has
  to happen at the source.
- A syntax error or a shell-fatal setting (`set -e` then a failure) just makes that one
  command's bash exit non-zero; there is no shared session to wedge, and the next command
  is unaffected.

## Presubmits

To run all presubmits run:

```sh
./scripts/check.sh
```

This runs formatting, linting, and tests.

To run the formatting and linting steps individually:

```bash
# Format
uv run ruff format .
# Lint checks
uv run ruff check --fix
```

To run the testing steps individually:

```bash
uv run pytest                    # everything (~18s; needs Docker + gVisor)
```

## License

MIT - see [LICENSE](LICENSE).
