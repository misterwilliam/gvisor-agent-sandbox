# gvisor-agent-sandbox

A security sandbox suitable for hosting an AI agent. The AI agent accesses the sandbox as a
tool. Sandbox is a wrapper around docker so it fits with container-based workflows and
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
the sandbox. Through the tools, the agent is allowed to:

- `shell_exec(command, timeout)` - Run arbitrary bash commands within the sandbox
- `shell_wait(timeout)` - Wait for running commands to finish
- `shell_kill()` - Send SIGTERM then SIGKILL to currently executing command

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

## Why stateless commands instead of a persistent shell

State persists at two very different levels, and only the durable one is kept:

- **The container** (durable, kept): installed packages, files, and backgrounded processes
  live for the life of the sandbox.
- **The shell session** (deliberately not kept): cwd, exported environment, and shell
  functions do _not_ carry from one command to the next. Each command is an independent
  `docker exec`.

A human leans hard on shell-session state; an agent does not need it - it can emit
absolute paths and chain state within a single command (`cd src && make`). Dropping the
persistent shell removes the whole problem of detecting when a command has finished on a
shared stream: with one process per command, "done" is just the process exiting, output
comes straight off that process's pipe, and there is no host-side file for command text or
output - and so none of the attack surface one brings.

A command that outruns its timeout is not treated as an error - it keeps running, and the
agent gets the output so far plus the choice to keep waiting (`shell_wait`) or stop it
(`shell_kill`). A slow test suite and a hung process look identical to a fixed timeout but
not to an agent holding the partial output, so the judgment call belongs there, not in the
harness.

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
