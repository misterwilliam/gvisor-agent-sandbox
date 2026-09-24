"""A scripted walkthrough of the sandbox.

It runs the classic agent coding loop (write code, run it, see it fail, fix it,
run it again) and then shows the timeout/wait, kill, and network-isolation
behaviors.

Requires docker with the gVisor runtime registered as `runsc` (same as the
integration tests). Run it with:

    uv run python examples/demo.py
"""

from gvisor_agent_sandbox import Sandbox


def show(title: str, result: str) -> None:
    print(f"\n=== {title} ===")
    print(result)


def main() -> None:
    with Sandbox() as sbx:
        # The agent coding loop: write a buggy script, run it, fix it, rerun.
        show(
            "write sum.py (with a bug: it sums argv[0] too)",
            sbx.shell_exec(
                "cat > sum.py <<'EOF'\nimport sys\nprint(sum(int(x) for x in sys.argv))\nEOF\n"
            ),
        )
        show("run it -> fails on the script name", sbx.shell_exec("python3 sum.py 2 3 4"))
        show(
            "fix it (skip argv[0])",
            sbx.shell_exec(
                "cat > sum.py <<'EOF'\nimport sys\nprint(sum(int(x) for x in sys.argv[1:]))\nEOF\n"
            ),
        )
        show("run it again -> 9", sbx.shell_exec("python3 sum.py 2 3 4"))

        # Each command is independent, but the container's filesystem persists.
        show("the file written earlier is still there", sbx.shell_exec("ls -l sum.py"))

        # A slow command: the call times out with a snapshot, then we wait it out.
        show(
            "start a 3s command with a 1s budget -> still running",
            sbx.shell_exec("echo working...; sleep 3; echo done", timeout=1),
        )
        show("wait for it to finish", sbx.shell_wait(timeout=10))

        # A stuck command: start it, then stop it.
        show("start a stuck command", sbx.shell_exec("sleep 300", timeout=1))
        show("kill it", sbx.shell_kill())

        # Isolation: the container has no network.
        show(
            "try to reach the network -> fails (no egress)",
            sbx.shell_exec(
                "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=5)\"",
                timeout=15,
            ),
        )


if __name__ == "__main__":
    main()
