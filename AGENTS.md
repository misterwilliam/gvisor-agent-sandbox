# Agents.md

## Objective

The objective of the project is to produce a security sandbox for containing AI agents
assuming they are highly capable and malicious. Using the `runsc` runtime (i.e. gVisor) and
disabling external networking access are fundamental to this strategy and should not be
changed.

## Coding Style

Code should be easy to read from top to bottom. Therefore the primary API surface exposed
by a module should in general appear before less frequently used parts of the API. The
exception to this is that constants should appear near the beginning of the module.

## Dev workflow

Run tests with:

```sh
# All tests
$ uv run pytest
# Tests that do not depend on docker and gVisor.
$ uv run pytest -m "not docker"
```
