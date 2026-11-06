# agent-memory-files

[![PyPI version](https://img.shields.io/pypi/v/trysac-agent-memory-files)](https://pypi.org/project/trysac-agent-memory-files/)
[![License: MIT](https://img.shields.io/pypi/l/trysac-agent-memory-files)](LICENSE)
[![CI](https://github.com/Trysac/u027-agent-memory-files/actions/workflows/ci.yml/badge.svg)](https://github.com/Trysac/u027-agent-memory-files/actions/workflows/ci.yml)

Agent memory stored as Markdown files with JSON-valued frontmatter, ranked keyword search, an index file and deterministic compaction

## Problem

Describe in two or three sentences the concrete problem this utility solves, who hits it, and what
they do today without it. Replace this paragraph when the first real feature lands.

## Install

```sh
pip install trysac-agent-memory-files
# or
uv add trysac-agent-memory-files
```

Requires Python 3.10 or newer. No runtime dependencies.

## Usage

```python
import agent_memory_files

# normalize() trims surrounding whitespace and lower-cases the text.
print(agent_memory_files.normalize("  Hello World "))  # -> "hello world"

# Blank input is rejected explicitly instead of returning an empty string.
try:
    agent_memory_files.normalize("   ")
except ValueError as exc:
    print(f"rejected: {exc}")
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) and [decisions](docs/decisions/)
- [Testing](docs/TESTING.md) and [security](docs/SECURITY.md)
- [Project record](docs/PROJECT.md) and [stack](docs/STACK.md)
- [Changelog](CHANGELOG.md) and [contributing guide](CONTRIBUTING.md)

## License

[MIT](LICENSE) (c) 2026 Miguel Gomez
