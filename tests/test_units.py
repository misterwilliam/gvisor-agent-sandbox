"""Tests for the logic that needs no container.

Worth keeping separate from the integration tests: this file runs in
milliseconds anywhere Python does, which makes it the half that can gate a
pull request.
"""

import gvisor_agent_sandbox
from gvisor_agent_sandbox import Sandbox, ShellResult

# Private helpers aren't re-exported from the package root, so they come from
# the module directly.
from gvisor_agent_sandbox.sandbox import _truncate

# ---- output truncation --------------------------------------------------


def test_short_output_is_untouched():
    assert _truncate("hello", limit=100) == "hello"


def test_output_exactly_at_the_limit_is_untouched():
    assert _truncate("x" * 100, limit=100) == "x" * 100


def test_oversized_output_keeps_head_and_tail():
    # Errors usually live at the end, context at the start; the middle is what
    # gets dropped.
    text = "HEAD" + "x" * 500 + "TAIL"
    truncated = _truncate(text, limit=100)
    assert truncated.startswith("HEAD")
    assert truncated.endswith("TAIL")
    assert len(truncated) < len(text)


def test_truncation_reports_how_much_it_dropped():
    assert "[900 characters truncated]" in _truncate("x" * 1000, limit=100)


# ---- rendering (what the model actually sees) ---------------------------


def test_rejected_renders_as_the_reason_alone():
    result = ShellResult(status="rejected", note="no running command 9")
    assert str(result) == "ERROR: no running command 9"


def test_failed_renders_as_an_error():
    result = ShellResult(status="failed", note="command 5 could not be run", command_id=5)
    assert str(result) == "ERROR: command 5 could not be run"


def test_completed_renders_exit_code_and_output():
    rendered = str(ShellResult("hello\n", exit_code=0))
    assert "exit=0" in rendered
    assert "hello" in rendered


def test_killed_names_the_signal():
    rendered = str(ShellResult("", exit_code=143, status="killed", note="SIGTERM after 2s"))
    assert "killed (exit=143)" in rendered
    assert "[SIGTERM after 2s]" in rendered


def test_results_name_their_command():
    # With several commands running, the agent needs to know which one a
    # result belongs to.
    assert str(ShellResult("hi\n", exit_code=0, command_id=4)).startswith("command 4: exit=0")


def test_running_with_output_reports_progress():
    rendered = str(ShellResult("tick\n", status="running", elapsed=3.0, idle=0.1, command_id=3))
    assert "still running after 3s" in rendered
    assert "produced 5 new characters" in rendered
    # The agent has to be told what its two options are, and which id to pass, or
    # it can't choose.
    assert "shell_wait" in rendered and "shell_kill" in rendered
    assert "command_id 3" in rendered


def test_running_without_output_reports_silence():
    # Progress vs. silence is the distinction that separates a slow build from
    # a hung process, and it's the whole reason the agent is being asked.
    rendered = str(ShellResult("", status="running", elapsed=10.0, idle=10.0, command_id=3))
    assert "no new output for 10s" in rendered
    assert "(nothing new)" in rendered


# ---- tool surface -------------------------------------------------------


def test_tools_report_clearly_when_the_sandbox_is_not_running():
    sandbox = Sandbox()
    assert str(sandbox.shell_exec("echo hi")) == "ERROR: sandbox is not running"
    assert str(sandbox.shell_wait(1)) == "ERROR: sandbox is not running"
    assert str(sandbox.shell_kill(1)) == "ERROR: sandbox is not running"


def test_dispatch_rejects_an_unknown_tool():
    assert Sandbox().dispatch("rm_rf", {}).startswith("ERROR: unknown tool")


def test_public_api_is_importable_from_the_package_root():
    # `from gvisor_agent_sandbox import Sandbox` is the documented entry point;
    # __all__ drifting from what __init__ actually re-exports would break it
    # for everyone but the tests, which import the module directly.
    for name in gvisor_agent_sandbox.__all__:
        assert hasattr(gvisor_agent_sandbox, name), f"{name} is in __all__ but not exported"


def test_every_advertised_tool_is_dispatchable():
    # Catches a tool being added to TOOLS - and so to the schema the model is
    # given - without being wired into dispatch, which would surface to the
    # agent as an unexplained error mid-task.
    minimal_input = {
        "shell_exec": {"command": "true"},
        "shell_wait": {"command_id": 1},
        "shell_kill": {"command_id": 1},
    }
    sandbox = Sandbox()
    for tool in Sandbox.TOOLS:
        assert tool["name"] in minimal_input, f"no test input for new tool {tool['name']!r}"
        assert "unknown tool" not in sandbox.dispatch(tool["name"], minimal_input[tool["name"]])
