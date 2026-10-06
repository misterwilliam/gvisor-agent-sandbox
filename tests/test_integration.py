"""Container-backed tests: the properties that need a real gVisor sandbox.

These assert against the `ShellResult` fields the tools return (`status`,
`exit_code`, `output`) rather than the rendered text the model sees. Asserting
on fields instead of substrings means a change to the wording of the output
doesn't break behavioural tests - the wording has its own tests in
test_units.py.

All of these share one container (see the `sbx` fixture).
"""

import pytest

pytestmark = pytest.mark.docker


# ---- the basics ---------------------------------------------------------


def test_command_runs_and_reports_success(sbx):
    result = sbx.shell_exec("echo hello")
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "hello" in result.output


def test_exit_code_is_propagated(sbx):
    # Each command is its own `docker exec`, so a bare `exit 3` just ends that
    # command with code 3 - there is no shared session for it to take down.
    result = sbx.shell_exec("exit 3")
    assert result.status == "completed"
    assert result.exit_code == 3


def test_shell_features_are_available(sbx):
    # `bash -c` gives a real shell, so pipes and redirection work.
    result = sbx.shell_exec("printf 'b\\na\\nc\\n' | sort | tr '\\n' ' '")
    assert result.output.strip() == "a b c"


def test_stdout_and_stderr_interleave_in_order(sbx):
    # stderr is merged into stdout at the source; capturing them separately
    # would destroy the relative ordering irrecoverably.
    result = sbx.shell_exec("echo one; echo two >&2; echo three")
    assert result.output.split() == ["one", "two", "three"]


# ---- statelessness: each command is independent -------------------------


def test_cwd_does_not_persist_between_commands(sbx):
    # No persistent shell: a `cd` in one command is gone by the next, which
    # starts back at /root.
    sbx.shell_exec("cd /tmp")
    assert sbx.shell_exec("pwd").output.strip() == "/root"


def test_environment_does_not_persist_between_commands(sbx):
    # An `export` in one command does not carry into the next.
    sbx.shell_exec("export MARKER=persisted")
    assert "persisted" not in sbx.shell_exec("echo [${MARKER}]").output


def test_the_filesystem_does_persist_between_commands(sbx):
    # The container is durable even though the shell isn't: a file written in
    # one command is there in the next.
    sbx.shell_exec("echo durable > /root/state.txt")
    assert "durable" in sbx.shell_exec("cat /root/state.txt").output


def test_state_can_be_chained_within_one_command(sbx):
    # The documented way to use working-directory state: chain it in a single
    # command rather than relying on it sticking.
    result = sbx.shell_exec("mkdir -p /root/sub && cd /root/sub && pwd")
    assert result.output.strip() == "/root/sub"


# ---- command passing ----------------------------------------------------


def test_quoting_survives_argv_passing(sbx):
    # The command is passed as its own argv element, never spliced into a shell
    # string, so arbitrary quotes and backslashes need no escaping.
    result = sbx.shell_exec("""python3 -c 'print("quotes: \\"a\\" '"'"'b'"'"'")'""")
    assert result.exit_code == 0
    assert "quotes: \"a\" 'b'" in result.output


def test_syntax_error_is_an_ordinary_failure(sbx):
    # A malformed command just makes its own bash exit non-zero; there is no
    # shared session to wedge, and the next command is unaffected.
    broken = sbx.shell_exec("if [ ; then")
    assert broken.status == "completed"
    assert broken.exit_code != 0
    assert sbx.shell_exec("echo still-here").output.strip() == "still-here"


def test_shell_fatal_settings_stay_contained(sbx):
    # `set -e` then a failure ends this command's bash, but nothing else - the
    # next command runs against a fresh process.
    result = sbx.shell_exec("set -e; false; echo unreachable", timeout=30)
    assert result.status == "completed"
    assert result.exit_code != 0
    assert "unreachable" not in result.output
    assert sbx.shell_exec("echo back").output.strip() == "back"


# ---- long-running commands ----------------------------------------------


def test_timeout_leaves_the_command_running(sbx):
    # A timeout is a status, not a failure: the command keeps going and the
    # agent gets a snapshot plus the choice of what to do about it.
    result = sbx.shell_exec("for i in 1 2 3 4 5 6; do echo tick-$i; sleep 1; done", timeout=3)
    assert result.status == "running"
    assert result.exit_code is None
    assert "tick-1" in result.output
    sbx.shell_kill(result.command_id)


def test_wait_returns_only_new_output(sbx):
    # Repeated waits should read like `tail -f`, not re-send the whole log -
    # otherwise a long build would re-fill the context window every check.
    first = sbx.shell_exec("for i in 1 2 3 4 5 6; do echo tick-$i; sleep 1; done", timeout=3)
    assert "tick-1" in first.output
    later = sbx.shell_wait(first.command_id, timeout=2)
    assert "tick-1" not in later.output
    sbx.shell_kill(first.command_id)


def test_wait_returns_the_exit_code_once_the_command_finishes(sbx):
    started = sbx.shell_exec("echo start; sleep 3; echo finished", timeout=1)
    result = sbx.shell_wait(started.command_id, timeout=60)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "finished" in result.output


def test_starting_a_command_over_the_limit_is_rejected(sbx):
    first = sbx.shell_exec("sleep 30", timeout=1)
    rejected = sbx.shell_exec("echo nope")
    assert rejected.status == "rejected"
    # The rejection has to name the way out, and which command is in the way,
    # or the agent is stuck.
    assert "shell_wait" in rejected.note and "shell_kill" in rejected.note
    assert str(first.command_id) in rejected.note
    sbx.shell_kill(first.command_id)


def test_kill_terminates_a_stuck_command(sbx):
    started = sbx.shell_exec("sleep 300", timeout=1)
    result = sbx.shell_kill(started.command_id)
    assert result.status == "killed"
    assert result.exit_code == 143  # 128 + SIGTERM
    assert "SIGTERM" in result.note


def test_a_new_command_works_after_a_kill(sbx):
    # Killing a command frees its place, so the next command runs normally.
    started = sbx.shell_exec("sleep 300", timeout=1)
    sbx.shell_kill(started.command_id)
    assert sbx.shell_exec("echo recovered").output.strip() == "recovered"


# ---- command ids --------------------------------------------------------


def test_wait_on_an_unknown_command_is_rejected(sbx):
    result = sbx.shell_wait(999)
    assert result.status == "rejected"
    assert "no running command 999" in result.note


def test_a_finished_command_is_forgotten(sbx):
    # Once its final result has been returned, the id no longer refers to
    # anything - there is nothing left to wait on or kill.
    finished = sbx.shell_exec("true")
    assert finished.status == "completed"
    assert sbx.shell_wait(finished.command_id).status == "rejected"
    assert sbx.shell_kill(finished.command_id).status == "rejected"


def test_command_ids_are_never_reused(sbx):
    # A reused id would let a stale shell_kill hit a newer, unrelated command.
    first = sbx.shell_exec("true")
    second = sbx.shell_exec("true")
    assert second.command_id > first.command_id


def test_a_missing_command_id_is_reported_not_raised(sbx):
    # The model can omit a required argument; that has to come back to it as an
    # error result rather than crash the harness.
    assert sbx.dispatch("shell_wait", {}).startswith("ERROR: no running command None")
    assert sbx.dispatch("shell_kill", {}).startswith("ERROR: no running command None")


# ---- isolation ----------------------------------------------------------


def test_container_has_no_network(sbx):
    # If this ever passes, the agent can exfiltrate whatever it can read.
    result = sbx.shell_exec(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=5)\"",
        timeout=60,
    )
    assert result.status == "completed"
    assert result.exit_code != 0, "the container reached the network - isolation has regressed"


# ---- writing files ------------------------------------------------------


def test_heredoc_writes_exact_content(sbx):
    # Writing a file goes through the shell; byte fidelity is a guarantee this
    # harness makes. Passing the command as its own argv element is what lets a
    # quoted heredoc carry content that expansion would otherwise mangle. With
    # no host mount, the file is read back through the container, not the host.
    content = '#!/bin/sh\nname="$USER and `whoami`"\necho \'single\' "double" \\back\n'
    write = sbx.shell_exec(f"mkdir -p /root/gen && cat > /root/gen/f.sh <<'XEOF'\n{content}XEOF\n")
    assert write.exit_code == 0
    assert sbx.shell_exec("cat /root/gen/f.sh").output == content


def test_dispatch_returns_the_text_the_agent_receives(sbx):
    # One end-to-end check of the actual contract with the model: dispatch
    # renders the result to text.
    rendered = sbx.dispatch("shell_exec", {"command": "echo hello"})
    assert isinstance(rendered, str)
    assert "exit=0" in rendered
    assert "hello" in rendered
