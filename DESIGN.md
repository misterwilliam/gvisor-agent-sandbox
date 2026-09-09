# AI agentic sandbox

## Objective

Create an agentic sandbox. Agents access the sandbox through tools.

## Design

### Agent outside vs. agent inside the sandbox

Two architectures are possible:

- **Agent outside** (chosen): the harness runs on the host and reaches into the container
  through tools (`shell_exec`, `shell_wait`, `shell_kill`). The container never sees an API
  key and runs airgapped with `--network=none` (not configurable).
- **Agent inside**: package the harness, its dependencies, and the task into the image and
  run the whole loop in the container. The agent gets direct filesystem/shell access with
  no tool plumbing at all.

Reasons for choosing agent-outside:

1. **Key custody.** An inside agent needs to reach api.anthropic.com, so the container
   needs network egress and the API key has to live inside it. An outside agent keeps the
   key on the host and leaves the container airgapped - it can't exfiltrate a key it never
   had. Egress can be constrained (DNS pinning + NAT, or simpler, an HTTP proxy with a
   host allowlist via `HTTPS_PROXY` - the same idea as `allowed_hosts` in Anthropic's
   managed agents), but it's real work and a strictly weaker position than no network.
2. **Audit-log integrity.** With the agent outside, the transcript lives in a filesystem
   namespace the agent cannot reach. Inside, the agent can read and in principle edit its
   own audit log.
3. **Artifact purity.** The measured artifact is the final state of the codebase. Inside,
   the harness, its venv, and its logs share the container, so separating agent output
   from scaffolding takes care.
4. **Dev-loop friction.** Iterating on harness code means rebuilding an image rather than
   rerunning a script.

### Runtime

Conceptually there are two layers, and the distinction should be visible to the agent:

- **Container (durable).** Files, installed packages, background processes. Everything
  expensive - the actual work product - lives here.
- **Shell session (cheap, recoverable).** cwd, exported environment, shell functions,
  aliases, shell options. All of it re-derivable in a command or two.

When the agent makes multiple `shell_exec` calls, they normally will hit the same bash
sessions so all state specific to that session will be preserved. However if that bash
session terminates the agent will be informed by an error message that current shell
session terminated and the next shell_exec will be started in a new shell session, but in
the same container.

To make interaction with agent more natural in a text based manner we will not be using
TTY but instead using pipes for stdin, stdout, stderr. Response seen by agent will be
stdout and stderr interleaved. This allows the agent to see the timing of the output and
since this is how humans typically have their shell sessions this should be fairly
intuitive for the agent.

**Starting container**

```sh
# Start container
CONTAINER_ID=$(docker run -d --rm --runtime runsc python:3.12 sleep infinity)
# Start session
docker exec -i ${CONTAINER_ID} bash
```

**Execute command**

Then to execute a command, we write the following to stdin to the process
`<command>\nbuiltin printf '__DONE__:%d' $?`. We use `builtin printf` to emit a marker
that shows when the command terminates. This is similar to how a human uses `$PS1` being
emitted to see when a command is finished. While `docker exec -i` is called interactive
mode it just pipes stdin. It doesn't actually trigger bash interactive mode so `$PS1` is
not used by bash. To protect against the agent defining a `printf` function which causes
shadowing of the printf function we call `builting printf`.

Command will actually be written to a file, and executed using `eval $(</my_cmd_file)`.
This protects against malformed bash commands that aren't terminated causing the bash
session to hang.

The harness will scan for the `__DONE__` end marker until a timeout, and return the
interleaved stdout and stderr back to the agent. The returned output will include the
`__DONE__` marker with return code of the command. If the timeout is hit the interleaved
stdout and stderr is returned immediately (without the `__DONE__` marker).
