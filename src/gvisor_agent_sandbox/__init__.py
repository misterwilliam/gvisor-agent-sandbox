"""A gVisor-isolated container an LLM agent drives through tools.

The implementation lives in `.sandbox`; this re-exports the public surface so
callers can write `from gvisor_agent_sandbox import Sandbox`. Private helpers
(`_resolve_in_workspace`, `_truncate`) are deliberately not re-exported -
import them from `gvisor_agent_sandbox.sandbox` if you need them, as the tests
do.
"""

from .sandbox import (
    DEFAULT_IMAGE,
    MAX_OUTPUT_BYTES,
    PersistentShell,
    Sandbox,
    SandboxError,
    SandboxViolation,
    ShellResult,
)

__all__ = [
    "DEFAULT_IMAGE",
    "MAX_OUTPUT_BYTES",
    "PersistentShell",
    "Sandbox",
    "SandboxError",
    "SandboxViolation",
    "ShellResult",
]
