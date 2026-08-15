"""Self-test: verifies the properties `sandbox.py` claims.

The persistence and long-running-command checks are the point - they're what
distinguish this from a `docker exec` per command.

    python3 self_test.py
"""

import tempfile
from pathlib import Path

from sandbox import Sandbox

if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as tmp:
        print(f"workspace: {tmp}")
        with Sandbox(tmp) as sbx:
            assert sbx.shell is not None and sbx.container_id is not None
            print(f"container: {sbx.container_id[:12]}  shell pid: {sbx.shell._shell_pid}")

            def check(label, command, timeout=None):
                print(f"\n-- {label} --")
                print(sbx.shell_exec(command, timeout))

            check("basic exec", "echo hello && python3 --version")
            check("shell features (pipe)", "printf 'b\\na\\nc\\n' | sort | tr '\\n' ' '")
            check("stdout/stderr interleaved in order", "echo one; echo two >&2; echo three; exit 3")

            check("cwd persistence (1/2): cd", "mkdir -p /workspace/sub && cd /workspace/sub && pwd")
            check("cwd persistence (2/2): still there?", "pwd")

            check("env persistence (1/2): export", "export MARKER=persisted && echo set")
            check("env persistence (2/2): still set?", "echo MARKER=$MARKER")

            check("quoting survives the file round-trip", """python3 -c 'print("quotes: \\"a\\" '"'"'b'"'"'")'""")
            check("syntax error is survivable", "if [ ; then")
            check("session still alive after syntax error", "echo still-here")
            check("shadowed printf/eval/cat don't break the harness",
                  "printf() { :; }; eval() { :; }; cat() { :; }; builtin echo shadowed-ok")
            check("...and the next command still works", "echo recovered")

            print("\n-- long-running command: exec times out, stays running --")
            print(sbx.shell_exec("for i in 1 2 3 4 5 6 7 8; do echo tick-$i; sleep 1; done", timeout=3))

            print("\n-- shell_wait: only NEW output, not a repeat --")
            print(sbx.shell_wait(timeout=3))

            print("\n-- starting another command while one runs is rejected --")
            print(sbx.shell_exec("echo nope"))

            print("\n-- shell_wait until completion --")
            print(sbx.shell_wait(timeout=15))

            print("\n-- shell_kill on a genuinely stuck command --")
            print(sbx.shell_exec("sleep 300", timeout=2))
            print(sbx.shell_kill())

            check("cwd survived the kill", "pwd")
            print("\n-- shell_wait with nothing running is rejected --")
            print(sbx.shell_wait())

            print("\n-- file_write, visible to the shell --")
            print(sbx.file_write("src/hi.py", "print('from file_write')\n"))
            print(sbx.shell_exec("python3 /workspace/src/hi.py"))

            print("\n-- path traversal rejected --")
            print(sbx.file_write("../escape.txt", "nope"))

            check("network isolation", "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 80), timeout=3)\"")

        print("\n-- artifact survives container removal --")
        print(sorted(p.relative_to(tmp).as_posix() for p in Path(tmp).rglob("*")))
