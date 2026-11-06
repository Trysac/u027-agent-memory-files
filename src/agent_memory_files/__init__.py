"""Agent memory as Markdown files with JSON-valued frontmatter, search and compaction."""

from __future__ import annotations

import logging

from .compaction import CompactionReport, ConcatSummarizer, Summarizer, compact
from .errors import AgentMemoryError, ErrorCode
from .store import Memory, MemoryEvent, MemoryStore, ScanError

__all__ = [
    "AgentMemoryError",
    "CompactionReport",
    "ConcatSummarizer",
    "ErrorCode",
    "Memory",
    "MemoryEvent",
    "MemoryStore",
    "ScanError",
    "Summarizer",
    "__version__",
    "compact",
]

__version__ = "0.1.0"

logging.getLogger(__name__).addHandler(logging.NullHandler())
