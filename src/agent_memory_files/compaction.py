"""Deterministic compaction of old memories into per-tag summaries."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from .errors import AgentMemoryError
from .store import _ID_RE, Memory, MemoryEvent, MemoryStore

SUMMARY_PREFIX = "compacted-"
_TRAILER_START = "\n\n<!-- merged: "
_TRAILER_END = " -->"


class Summarizer(Protocol):
    """Turns a group of memories into one summary text."""

    def summarize(self, memories: Sequence[Memory]) -> str:
        """Return the summary text for ``memories``."""


class ConcatSummarizer:
    """Deterministic summarizer: ``- <title>: <first body line>`` per memory, sorted by id."""

    def summarize(self, memories: Sequence[Memory]) -> str:
        """Return one LF-joined line per memory, sorted by id."""
        ordered = sorted(memories, key=lambda memory: memory.id)
        return "\n".join(f"- {m.title}: {m.body.split(chr(10), 1)[0]}" for m in ordered)


@dataclass(frozen=True)
class CompactionReport:
    """Ids deleted because they were merged and ids of the summaries that were created."""

    merged_ids: tuple[str, ...]
    created_ids: tuple[str, ...]


def _trailer_ids(body: str) -> list[str]:
    start = body.rfind(_TRAILER_START)
    if start < 0 or not body.endswith(_TRAILER_END):
        return []
    inner = body[start + len(_TRAILER_START) : len(body) - len(_TRAILER_END)]
    found: dict[str, None] = {}
    for token in inner.split(" "):
        if token and _ID_RE.fullmatch(token):
            found.setdefault(token, None)
    return list(found)


def _recover(store: MemoryStore, summary: Memory, present: set[str]) -> list[str]:
    deleted: list[str] = []
    for mid in _trailer_ids(summary.body):
        if mid == summary.id or mid.startswith(SUMMARY_PREFIX) or mid not in present:
            continue
        try:
            store.delete(mid)
        except AgentMemoryError as exc:
            if exc.code == "not_found":
                continue
            raise
        deleted.append(mid)
    if deleted:
        store._emit(MemoryEvent("compacted", summary.id, str(store.path_for(summary.id))))
    return deleted


def compact(
    store: MemoryStore,
    summarizer: Summarizer,
    max_age: timedelta,
    min_group: int = 3,
) -> CompactionReport:
    """Merge old tagged memories into ``compacted-<tag>-<YYYYMMDD>`` summaries.

    Candidates have ``updated < now - max_age`` and at least one tag; they are grouped by
    their first tag and groups of at least ``min_group`` are summarized. The summary is
    written before the originals are deleted. If today's summary for a group already
    exists (crash recovery), only the ids listed in its trailer that are still present
    are deleted and the summary is never rewritten. Summaries are never candidates.

    Raises:
        ValueError: if ``min_group`` < 1 or ``max_age`` is negative.
        AgentMemoryError: ``invalid_clock``, ``invalid_memory`` (summarizer returned a
            non-string) or any error of the underlying store.
    """
    if min_group < 1:
        raise ValueError("min_group must be at least 1")
    if max_age < timedelta(0):
        raise ValueError("max_age must not be negative")
    now = store.now()
    try:
        cutoff = now - max_age
    except OverflowError:
        return CompactionReport((), ())
    stamp = f"{now.year:04d}{now.month:02d}{now.day:02d}"
    memories = store.list()
    present = {memory.id for memory in memories}
    groups: dict[str, list[Memory]] = {}
    for memory in sorted(memories, key=lambda item: item.id):
        if memory.id.startswith(SUMMARY_PREFIX) or not memory.tags or memory.updated >= cutoff:
            continue
        groups.setdefault(memory.tags[0], []).append(memory)
    merged: list[str] = []
    created: list[str] = []
    for tag in sorted(groups):
        members = groups[tag]
        summary_id = f"{SUMMARY_PREFIX}{tag}-{stamp}"
        try:
            existing: Memory | None = store.load(summary_id)
        except AgentMemoryError as exc:
            if exc.code != "not_found":
                continue
            existing = None
        if existing is not None:
            merged.extend(_recover(store, existing, present))
            continue
        if len(members) < min_group:
            continue
        text = summarizer.summarize(tuple(members))
        if not isinstance(text, str):
            raise AgentMemoryError("invalid_memory", "summarizer must return a string")
        ids = tuple(member.id for member in members)
        body = f"{text}{_TRAILER_START}{' '.join(ids)}{_TRAILER_END}"
        store.save(
            Memory(
                id=summary_id,
                title=f"Compacted {tag} memories",
                body=body,
                tags=(tag,),
                created=now,
                updated=now,
            )
        )
        created.append(summary_id)
        for mid in ids:
            store.delete(mid)
        merged.extend(ids)
        store._emit(MemoryEvent("compacted", summary_id, str(store.path_for(summary_id))))
    return CompactionReport(tuple(merged), tuple(created))
