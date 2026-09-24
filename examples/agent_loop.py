# Exact copy of agent loop example in README.md
# Run with:
# export ANTHROPIC_API_KEY=...
# uv run --group examples python examples/agent_loop.py
import sys

import anthropic

from gvisor_agent_sandbox import Sandbox

MODEL = "claude-sonnet-5"

SYSTEM = """You are working inside a Linux container that you drive with shell commands.

You are root and your commands start in /root. Python 3.12 is available as python3.
The container has no network access, so nothing can be installed - use the standard
library only.

Each shell_exec runs independently: the working directory resets to /root every time
and environment variables do not carry over. The filesystem does persist. Chain state
within a single command, or use absolute paths.

Work until the task is done, then summarize what you built and stop."""

TASK = """In /root, write stats.py containing a function median(values) that returns the
median of a list of numbers and raises ValueError on an empty list. Then write
test_stats.py with unittest tests covering odd-length, even-length, and empty inputs.
Run the tests and fix whatever fails until they all pass."""

client = anthropic.Anthropic()
messages: list[dict] = [{"role": "user", "content": TASK}]


def main() -> int:
    with Sandbox() as sbx:
        for turn in range(10):
            response = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=SYSTEM,
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
                return 0
            messages.append({"role": "user", "content": results})


if __name__ == "__main__":
    sys.exit(main())
