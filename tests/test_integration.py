"""Container-backed tests: the properties that need a real gVisor sandbox.

These assert against `sbx.runner.run(...)`, which returns a `ShellResult`,
rather than `sbx.shell_exec(...)`, which returns rendered text. Asserting on
fields instead of substrings means a change to the wording of the output
doesn't break behavioural tests - the wording has its own tests in
test_units.py.

All of these share one container (see the `sbx` fixture). Skipped
automatically where Docker or gVisor is missing.
"""
import pytest

from gvisor_agent_sandbox import Sandbox

pytestmark = pytest.mark.docker


# ---- the basics ---------------------------------------------------------


def test_command_runs_and_reports_success(sbx):
    result = sbx.runner.run("echo hello")
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "hello" in result.output


def test_exit_code_is_propagated(sbx):
    # Each command is its own `docker exec`, so a bare `exit 3` just ends that
    # command with code 3 - there is no shared session for it to take down.
    result = sbx.runner.run("exit 3")
    assert result.status == "completed"
    assert result.exit_code == 3


def test_shell_features_are_available(sbx):
    # `bash -c` gives a real shell, so pipes and redirection work.
    result = sbx.runner.run("printf 'b\\na\\nc\\n' | sort | tr '\\n' ' '")
    assert result.output.strip() == "a b c"


def test_stdout_and_stderr_interleave_in_order(sbx):
    # stderr is merged into stdout at the source; capturing them separately
    # would destroy the relative ordering irrecoverably.
    result = sbx.runner.run("echo one; echo two >&2; echo three")
    assert result.output.split() == ["one", "two", "three"]


# ---- statelessness: each command is independent -------------------------


def test_cwd_does_not_persist_between_commands(sbx):
    # No persistent shell: a `cd` in one command is gone by the next, which
    # starts back at /workspace.
    sbx.runner.run("cd /tmp")
    assert sbx.runner.run("pwd").output.strip() == "/workspace"


def test_environment_does_not_persist_between_commands(sbx):
    # An `export` in one command does not carry into the next.
    sbx.runner.run("export MARKER=persisted")
    assert "persisted" not in sbx.runner.run("echo [${MARKER}]").output


def test_the_filesystem_does_persist_between_commands(sbx):
    # The container is durable even though the shell isn't: a file written in
    # one command is there in the next.
    sbx.runner.run("echo durable > /workspace/state.txt")
    assert "durable" in sbx.runner.run("cat /workspace/state.txt").output


def test_state_can_be_chained_within_one_command(sbx):
    # The documented way to use working-directory state: chain it in a single
    # command rather than relying on it sticking.
    result = sbx.runner.run("mkdir -p /workspace/sub && cd /workspace/sub && pwd")
    assert result.output.strip() == "/workspace/sub"


# ---- command passing ----------------------------------------------------


def test_quoting_survives_argv_passing(sbx):
    # The command is passed as its own argv element, never spliced into a shell
    # string, so arbitrary quotes and backslashes need no escaping.
    result = sbx.runner.run("""python3 -c 'print("quotes: \\"a\\" '"'"'b'"'"'")'""")
    assert result.exit_code == 0
    assert "quotes: \"a\" 'b'" in result.output


def test_syntax_error_is_an_ordinary_failure(sbx):
    # A malformed command just makes its own bash exit non-zero; there is no
    # shared session to wedge, and the next command is unaffected.
    broken = sbx.runner.run("if [ ; then")
    assert broken.status == "completed"
    assert broken.exit_code != 0
    assert sbx.runner.run("echo still-here").output.strip() == "still-here"


def test_shell_fatal_settings_stay_contained(sbx):
    # `set -e` then a failure ends this command's bash, but nothing else - the
    # next command runs against a fresh process.
    result = sbx.runner.run("set -e; false; echo unreachable", timeout=30)
    assert result.status == "completed"
    assert result.exit_code != 0
    assert "unreachable" not in result.output
    assert sbx.runner.run("echo back").output.strip() == "back"


# ---- long-running commands ----------------------------------------------


def test_timeout_leaves_the_command_running(sbx):
    # A timeout is a status, not a failure: the command keeps going and the
    # agent gets a snapshot plus the choice of what to do about it.
    result = sbx.runner.run("for i in 1 2 3 4 5 6; do echo tick-$i; sleep 1; done", timeout=3)
    assert result.status == "running"
    assert result.exit_code is None
    assert "tick-1" in result.output
    sbx.runner.kill()


def test_wait_returns_only_new_output(sbx):
    # Repeated waits should read like `tail -f`, not re-send the whole log -
    # otherwise a long build would re-fill the context window every check.
    first = sbx.runner.run("for i in 1 2 3 4 5 6; do echo tick-$i; sleep 1; done", timeout=3)
    assert "tick-1" in first.output
    later = sbx.runner.wait(timeout=2)
    assert "tick-1" not in later.output
    sbx.runner.kill()


def test_wait_returns_the_exit_code_once_the_command_finishes(sbx):
    sbx.runner.run("echo start; sleep 3; echo finished", timeout=1)
    result = sbx.runner.wait(timeout=60)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "finished" in result.output


def test_starting_a_second_command_is_rejected(sbx):
    sbx.runner.run("sleep 30", timeout=1)
    rejected = sbx.runner.run("echo nope")
    assert rejected.status == "rejected"
    # The rejection has to name the way out, or the agent is stuck.
    assert "shell_wait" in rejected.note and "shell_kill" in rejected.note
    sbx.runner.kill()


def test_wait_with_nothing_running_is_rejected(sbx):
    result = sbx.runner.wait()
    assert result.status == "rejected"
    assert result.note == "no command is currently running"


def test_kill_terminates_a_stuck_command(sbx):
    sbx.runner.run("sleep 300", timeout=1)
    result = sbx.runner.kill()
    assert result.status == "killed"
    assert result.exit_code == 143  # 128 + SIGTERM
    assert "SIGTERM" in result.note


def test_a_new_command_works_after_a_kill(sbx):
    # Killing frees the runner so the next command runs normally.
    sbx.runner.run("sleep 300", timeout=1)
    sbx.runner.kill()
    assert sbx.runner.run("echo recovered").output.strip() == "recovered"


# ---- isolation ----------------------------------------------------------


def test_container_has_no_network(sbx):
    # If this ever passes, the agent can exfiltrate whatever it can read.
    result = sbx.runner.run(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=5)\"",
        timeout=60,
    )
    assert result.status == "completed"
    assert result.exit_code != 0, "the container reached the network - isolation has regressed"


# ---- writing files ------------------------------------------------------


def test_heredoc_writes_exact_content(sbx):
    # Writing a file goes through the shell; byte fidelity is a guarantee this
    # harness makes. Passing the command as its own argv element is what lets a
    # quoted heredoc carry content that expansion would otherwise mangle.
    content = '#!/bin/sh\nname="$USER and `whoami`"\necho \'single\' "double" \\back\n'
    result = sbx.runner.run(
        f"mkdir -p /workspace/gen && cat > /workspace/gen/f.sh <<'XEOF'\n{content}XEOF\n"
    )
    assert result.exit_code == 0
    assert (sbx.workspace / "gen/f.sh").read_text() == content


# ---- lifecycle ----------------------------------------------------------


def test_workspace_outlives_the_container(tmp_path):
    # The workspace is the measured artifact; the container is disposable.
    # Needs its own sandbox because it asserts on state after teardown.
    with Sandbox(tmp_path) as sandbox:
        sandbox.runner.run("echo by-the-shell > /workspace/generated.txt")
        sandbox.runner.run("mkdir -p /workspace/sub && echo nested > /workspace/sub/deep.txt")

    assert (tmp_path / "generated.txt").read_text().strip() == "by-the-shell"
    assert (tmp_path / "sub/deep.txt").read_text().strip() == "nested"
    assert sandbox.container_id is None


def test_rendered_output_is_what_the_agent_receives(sbx):
    # One end-to-end check that the tool methods render rather than returning
    # objects - this is the actual contract with the model.
    rendered = sbx.shell_exec("echo hello")
    assert isinstance(rendered, str)
    assert "exit=0" in rendered
    assert "hello" in rendered
