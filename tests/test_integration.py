"""Container-backed tests: the properties that need a real gVisor sandbox.

These assert against `sbx.shell.run(...)`, which returns a `ShellResult`,
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
    result = sbx.shell.run("echo hello")
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "hello" in result.output


def test_exit_code_is_propagated(sbx):
    # `(exit 3)`, not `exit 3`: the command runs in a brace group in the
    # *current* shell, so a bare `exit` takes the session down with it - see
    # test_exit_ends_the_session_and_says_so.
    result = sbx.shell.run("(exit 3)")
    assert result.status == "completed"
    assert result.exit_code == 3


def test_shell_features_are_available(sbx):
    # A real shell, not exec of a bare argv - pipes have to work.
    result = sbx.shell.run("printf 'b\\na\\nc\\n' | sort | tr '\\n' ' '")
    assert result.output.strip() == "a b c"


def test_stdout_and_stderr_interleave_in_order(sbx):
    # Merging with 2>&1 at capture time is what preserves this. Capturing the
    # two streams separately destroys the relative ordering irrecoverably, so
    # the decision can't be deferred to render time.
    result = sbx.shell.run("echo one; echo two >&2; echo three")
    assert result.output.split() == ["one", "two", "three"]


# ---- session persistence: the reason this isn't `docker exec` per call --


def test_cwd_persists_between_commands(sbx):
    sbx.shell.run("mkdir -p /workspace/sub && cd /workspace/sub")
    assert sbx.shell.run("pwd").output.strip() == "/workspace/sub"


def test_environment_persists_between_commands(sbx):
    sbx.shell.run("export MARKER=persisted")
    assert "persisted" in sbx.shell.run("echo $MARKER").output


# ---- the wrapper's defences ---------------------------------------------


def test_quoting_survives_the_file_round_trip(sbx):
    # The command text is passed through a file precisely so that arbitrary
    # quotes, newlines and backslashes need no escaping.
    result = sbx.shell.run("""python3 -c 'print("quotes: \\"a\\" '"'"'b'"'"'")'""")
    assert result.exit_code == 0
    assert "quotes: \"a\" 'b'" in result.output


def test_syntax_error_does_not_wedge_the_session(sbx):
    # Under `eval` this is an ordinary non-zero exit. Handed straight to bash
    # it would abort the line before the sentinel printed, hanging the session
    # over a typo.
    broken = sbx.shell.run("if [ ; then")
    assert broken.status == "completed"
    assert broken.exit_code != 0
    assert sbx.shell.run("echo still-here").output.strip() == "still-here"


def test_shadowed_builtins_do_not_break_the_harness(sbx):
    # An agent that defines printf/eval/cat as functions would otherwise break
    # the wrapper silently: a shadowed printf swallows the sentinel (every
    # later command looks hung), a shadowed eval makes commands appear to
    # succeed while doing nothing.
    result = sbx.shell.run(
        "printf() { :; }; eval() { :; }; cat() { :; }; builtin echo shadowed-ok"
    )
    assert result.status == "completed"
    assert "shadowed-ok" in result.output
    assert sbx.shell.run("echo recovered").output.strip() == "recovered"


@pytest.mark.parametrize(
    "fatal_command",
    [
        "echo bye; exit 3",  # `exit` runs in the current shell, so it ends it
        "set -e; false; echo unreachable",  # a shell-fatal setting, then a failure
    ],
)
def test_session_fatal_commands_are_reported_not_hidden(sbx, fatal_command):
    # These are ordinary agent behaviour, not pathology, and they genuinely
    # kill bash. The contract is that the harness says so rather than looking
    # like a hang, and that the next command works against a fresh session.
    result = sbx.shell.run(fatal_command, timeout=30)
    assert result.status == "session_lost"
    assert result.exit_code is None
    assert "restarted" in result.note
    assert sbx.shell.run("echo back").output.strip() == "back"


def test_output_produced_before_a_fatal_exit_survives(sbx):
    # Losing the session must not lose what the command already printed - that
    # output is often the only evidence of why it died.
    result = sbx.shell.run("echo printed-before-exit; exit 1", timeout=30)
    assert result.status == "session_lost"
    assert "printed-before-exit" in result.output


# ---- long-running commands ----------------------------------------------


def test_timeout_leaves_the_command_running(sbx):
    # A timeout is a status, not a failure: the command keeps going and the
    # agent gets a snapshot plus the choice of what to do about it.
    result = sbx.shell.run("for i in 1 2 3 4 5 6; do echo tick-$i; sleep 1; done", timeout=3)
    assert result.status == "running"
    assert result.exit_code is None
    assert "tick-1" in result.output
    sbx.shell.kill()


def test_wait_returns_only_new_output(sbx):
    # Repeated waits should read like `tail -f`, not re-send the whole log -
    # otherwise a long build would re-fill the context window every check.
    first = sbx.shell.run("for i in 1 2 3 4 5 6; do echo tick-$i; sleep 1; done", timeout=3)
    assert "tick-1" in first.output
    later = sbx.shell.wait(timeout=2)
    assert "tick-1" not in later.output
    sbx.shell.kill()


def test_wait_returns_the_exit_code_once_the_command_finishes(sbx):
    sbx.shell.run("echo start; sleep 3; echo finished", timeout=1)
    result = sbx.shell.wait(timeout=60)
    assert result.status == "completed"
    assert result.exit_code == 0
    assert "finished" in result.output


def test_starting_a_second_command_is_rejected(sbx):
    sbx.shell.run("sleep 30", timeout=1)
    rejected = sbx.shell.run("echo nope")
    assert rejected.status == "rejected"
    # The rejection has to name the way out, or the agent is stuck.
    assert "shell_wait" in rejected.note and "shell_kill" in rejected.note
    sbx.shell.kill()


def test_wait_with_nothing_running_is_rejected(sbx):
    result = sbx.shell.wait()
    assert result.status == "rejected"
    assert result.note == "no command is currently running"


def test_kill_terminates_a_stuck_command(sbx):
    sbx.shell.run("sleep 300", timeout=1)
    result = sbx.shell.kill()
    assert result.status == "killed"
    assert result.exit_code == 143  # 128 + SIGTERM
    assert "SIGTERM" in result.note


def test_session_state_survives_a_kill(sbx):
    # Killing the command must not cost the agent its shell: only the child is
    # signalled, never the shell itself.
    sbx.shell.run("mkdir -p /workspace/keep && cd /workspace/keep")
    sbx.shell.run("sleep 300", timeout=1)
    sbx.shell.kill()
    assert sbx.shell.run("pwd").output.strip() == "/workspace/keep"


# ---- isolation ----------------------------------------------------------

def test_container_has_no_network(sbx):
    # If this ever passes, the agent can exfiltrate whatever it can read.
    result = sbx.shell.run(
        "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=5)\"",
        timeout=60,
    )
    assert result.status == "completed"
    assert result.exit_code != 0, "the container reached the network - isolation has regressed"


def test_file_write_is_visible_inside_the_container(sbx):
    # file_write goes through the bind mount rather than the shell, so it works
    # even while a long command holds the session - but it has to land where
    # the container can see it.
    sbx.file_write("src/hi.py", "print('from file_write')\n")
    result = sbx.shell.run("python3 /workspace/src/hi.py")
    assert result.exit_code == 0
    assert "from file_write" in result.output


# ---- lifecycle ----------------------------------------------------------


def test_workspace_outlives_the_container(tmp_path):
    # The workspace is the measured artifact; the container is disposable.
    # Needs its own sandbox because it asserts on state after teardown.
    with Sandbox(tmp_path) as sandbox:
        sandbox.file_write("written.txt", "by file_write\n")
        sandbox.shell.run("echo by-the-shell > /workspace/generated.txt")

    assert (tmp_path / "written.txt").read_text() == "by file_write\n"
    assert (tmp_path / "generated.txt").read_text().strip() == "by-the-shell"
    assert sandbox.container_id is None


def test_rendered_output_is_what_the_agent_receives(sbx):
    # One end-to-end check that the tool methods render rather than returning
    # objects - this is the actual contract with the model.
    rendered = sbx.shell_exec("echo hello")
    assert isinstance(rendered, str)
    assert "exit=0" in rendered
    assert "hello" in rendered
