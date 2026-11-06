"""Typed errors raised by agent_memory_files."""

from __future__ import annotations

from typing import Literal

ErrorCode = Literal[
    "invalid_id",
    "not_found",
    "body_too_large",
    "invalid_clock",
    "query_too_long",
    "too_many_files",
    "invalid_memory",
]


class AgentMemoryError(Exception):
    """Base error of the library.

    ``code`` is a stable machine-readable identifier. Messages carry the memory id only,
    never titles or bodies, and never echo more than a short prefix of caller input.
    This class deliberately does not shadow the builtin ``MemoryError``.
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
