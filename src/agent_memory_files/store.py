"""File-backed memory store: Markdown files with JSON-valued frontmatter."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import stat
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from .errors import AgentMemoryError, ErrorCode

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

INDEX_NAME = "INDEX.md"
MAX_QUERY_CHARS = 1000
MAX_TITLE_CHARS = 200
MAX_TAGS = 20

_ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TAG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_TOKEN_RE = re.compile(r"\w+")
_KEYS = ("id", "title", "tags", "created", "updated")
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_BACKSLASH = chr(92)


@dataclass(frozen=True)
class Memory:
    """One memory. Tags are casefolded and deduplicated when the memory is saved."""

    id: str
    title: str
    body: str
    tags: tuple[str, ...]
    created: datetime
    updated: datetime


@dataclass(frozen=True)
class MemoryEvent:
    """A store event. It carries ids, paths and codes only, never titles or bodies."""

    kind: Literal["saved", "deleted", "compacted", "skipped"]
    memory_id: str | None = None
    path: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class ScanError:
    """A file that listing skipped, with the reason as a short code."""

    path: str
    code: str


_Memories = list[Memory]
_Scored = list[tuple[Memory, int]]
_Errors = list[ScanError]


class _Skip(Exception):
    """Internal: a file cannot be used; ``code`` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _clip(text: str, limit: int = 40) -> str:
    return repr(text[:limit])


def _check_id(value: object) -> str:
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        shown = _clip(value) if isinstance(value, str) else type(value).__name__
        raise AgentMemoryError("invalid_id", f"invalid memory id: {shown}")
    return value


def _check_title(title: object) -> str:
    if (
        not isinstance(title, str)
        or len(title) > MAX_TITLE_CHARS
        or "\n" in title
        or "\r" in title
    ):
        raise AgentMemoryError(
            "invalid_memory",
            "title must be one line of at most 200 characters",
        )
    return title


def _normalize_tags(tags: object) -> tuple[str, ...]:
    if isinstance(tags, str) or not isinstance(tags, list | tuple):
        raise AgentMemoryError("invalid_memory", "tags must be a sequence of strings")
    seen: dict[str, None] = {}
    for raw in tags:
        if not isinstance(raw, str) or len(raw) > 4 * 40:
            raise AgentMemoryError("invalid_memory", "each tag must be a short string")
        tag = raw.casefold()
        if _TAG_RE.fullmatch(tag) is None:
            raise AgentMemoryError("invalid_memory", f"invalid tag: {_clip(raw)}")
        seen.setdefault(tag, None)
        if len(seen) > MAX_TAGS:
            raise AgentMemoryError("invalid_memory", "at most 20 tags are allowed")
    return tuple(seen)


def _to_utc(value: object, code: ErrorCode, what: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AgentMemoryError(code, f"{what} must be a timezone-aware datetime")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError) as exc:
        raise AgentMemoryError(code, f"{what} is out of range") from exc


def _format_ts(value: datetime) -> str:
    return (
        f"{value.year:04d}-{value.month:02d}-{value.day:02d}"
        f"T{value.hour:02d}:{value.minute:02d}:{value.second:02d}"
        f".{value.microsecond:06d}Z"
    )


def _parse_ts(raw: str) -> datetime:
    try:
        parsed = datetime.strptime(raw, _TS_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise _Skip("frontmatter_invalid") from exc
    if _format_ts(parsed) != raw:
        raise _Skip("frontmatter_invalid")
    return parsed


def _encode(memory: Memory) -> bytes:
    def dump(value: object) -> str:
        return json.dumps(value, ensure_ascii=False)

    head = "\n".join(
        [
            "---",
            f"id: {dump(memory.id)}",
            f"title: {dump(memory.title)}",
            f"tags: {dump(list(memory.tags))}",
            f"created: {dump(_format_ts(memory.created))}",
            f"updated: {dump(_format_ts(memory.updated))}",
            "---",
        ]
    )
    try:
        return f"{head}\n\n{memory.body}".encode()
    except UnicodeEncodeError as exc:
        raise AgentMemoryError(
            "invalid_memory",
            f"memory {memory.id} is not valid UTF-8 text",
        ) from exc


def _parse(text: str) -> Memory:
    lines = text.split("\n", 8)
    if len(lines) < 8 or lines[0] != "---" or lines[6] != "---" or lines[7] != "":
        raise _Skip("frontmatter_invalid")
    values: dict[str, object] = {}
    for key, line in zip(_KEYS, lines[1:6], strict=True):
        prefix = f"{key}: "
        if not line.startswith(prefix):
            raise _Skip("frontmatter_invalid")
        try:
            values[key] = json.loads(line[len(prefix) :])
        except (ValueError, RecursionError) as exc:
            raise _Skip("frontmatter_invalid") from exc
    body = lines[8] if len(lines) > 8 else ""
    raw_id, raw_title, raw_tags = values["id"], values["title"], values["tags"]
    raw_created, raw_updated = values["created"], values["updated"]
    if not (
        isinstance(raw_id, str)
        and isinstance(raw_title, str)
        and isinstance(raw_tags, list)
        and isinstance(raw_created, str)
        and isinstance(raw_updated, str)
    ):
        raise _Skip("frontmatter_invalid")
    try:
        memory_id = _check_id(raw_id)
        title = _check_title(raw_title)
        tags = _normalize_tags(raw_tags)
    except AgentMemoryError as exc:
        raise _Skip("frontmatter_invalid") from exc
    return Memory(
        id=memory_id,
        title=title,
        body=body,
        tags=tags,
        created=_parse_ts(raw_created),
        updated=_parse_ts(raw_updated),
    )


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _lstat(path: Path) -> os.stat_result | None:
    try:
        return os.lstat(path)
    except FileNotFoundError:
        return None


def _score(memory: Memory, wanted: set[str]) -> int:
    title_tokens = set(_TOKEN_RE.findall(memory.title.casefold()))
    body_tokens = set(_TOKEN_RE.findall(memory.body.casefold()))
    score = 0
    for token in wanted:
        if token in title_tokens:
            score += 3
        if token in memory.tags:
            score += 2
        if token in body_tokens:
            score += 1
    return score


def _escape(text: str) -> str:
    for char in (_BACKSLASH, "[", "]"):
        text = text.replace(char, _BACKSLASH + char)
    return text


class MemoryStore:
    """A folder of Markdown memory files. One writer per folder; no cross-process locks.

    Every memory is one ``<id>.md`` file directly under ``root``. Symlinks are never
    followed, writes are atomic (temporary file plus ``os.replace``) and every input is
    bounded by ``max_file_bytes``, ``max_body_chars`` and ``max_files``.
    """

    def __init__(
        self,
        root: Path,
        clock: Callable[[], datetime],
        max_file_bytes: int = 262_144,
        max_body_chars: int = 100_000,
        max_files: int = 10_000,
        on_event: Callable[[MemoryEvent], None] | None = None,
    ) -> None:
        limits = {
            "max_file_bytes": max_file_bytes,
            "max_body_chars": max_body_chars,
            "max_files": max_files,
        }
        for name, value in limits.items():
            if isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        self._root = Path(root)
        self._clock = clock
        self._max_file_bytes = max_file_bytes
        self._max_body_chars = max_body_chars
        self._max_files = max_files
        self._on_event = on_event

    def now(self) -> datetime:
        """Return the injected clock value as an aware UTC datetime.

        Raises:
            AgentMemoryError: ``invalid_clock`` if the clock is naive or not a datetime.
        """
        return _to_utc(self._clock(), "invalid_clock", "clock value")

    def path_for(self, memory_id: str) -> Path:
        """Return the file path of ``memory_id`` after validating it.

        Raises:
            AgentMemoryError: ``invalid_id`` if the id is not ``[a-z0-9][a-z0-9-]{0,63}``.
        """
        mid = _check_id(memory_id)
        root = self._root.resolve()
        path = root / f"{mid}.md"
        if path.parent != root:
            raise AgentMemoryError("invalid_id", f"id escapes the store: {_clip(mid)}")
        return path

    def save(self, memory: Memory) -> Path:
        """Write ``memory`` atomically and return its path.

        A new id is stored with the memory's own timestamps. An existing id keeps its
        stored ``created`` and gets ``updated`` set to the clock value.

        Raises:
            AgentMemoryError: ``invalid_id``, ``invalid_memory``, ``body_too_large``,
                ``invalid_clock`` or ``too_many_files``.
        """
        prepared = self._prepare(memory)
        now = self.now()
        path = self.path_for(prepared.id)
        existing = _lstat(path)
        if existing is None:
            if len(self._md_names(path.parent)) >= self._max_files:
                raise AgentMemoryError(
                    "too_many_files",
                    f"store already holds {self._max_files} files",
                )
            record = prepared
        else:
            created = prepared.created
            try:
                created = self._read_memory(path, prepared.id).created
            except _Skip as skip:
                if skip.code in ("symlink", "not_regular"):
                    raise AgentMemoryError(
                        "invalid_memory",
                        f"memory {prepared.id} is not a regular file",
                    ) from skip
            record = replace(prepared, created=created, updated=now)
        data = _encode(record)
        if len(data) > self._max_file_bytes:
            raise AgentMemoryError(
                "body_too_large",
                f"memory {record.id} exceeds {self._max_file_bytes} bytes",
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, data)
        self._emit(MemoryEvent("saved", record.id, str(path)))
        return path

    def load(self, memory_id: str) -> Memory:
        """Read one memory.

        Raises:
            AgentMemoryError: ``invalid_id``, ``not_found`` or ``invalid_memory`` (the file
                is a symlink, too large or malformed).
        """
        path = self.path_for(memory_id)
        try:
            return self._read_memory(path, memory_id)
        except _Skip as skip:
            if skip.code == "not_found":
                raise AgentMemoryError("not_found", f"memory not found: {memory_id}") from skip
            raise AgentMemoryError(
                "invalid_memory",
                f"memory {memory_id} is unusable: {skip.code}",
            ) from skip

    def delete(self, memory_id: str) -> None:
        """Delete one memory file.

        Raises:
            AgentMemoryError: ``invalid_id``, ``not_found`` or ``invalid_memory``.
        """
        path = self.path_for(memory_id)
        info = _lstat(path)
        if info is None:
            raise AgentMemoryError("not_found", f"memory not found: {memory_id}")
        if not stat.S_ISREG(info.st_mode):
            raise AgentMemoryError(
                "invalid_memory",
                f"memory {memory_id} is not a regular file",
            )
        path.unlink()
        self._emit(MemoryEvent("deleted", memory_id, str(path)))

    def list(self) -> _Memories:
        """Return every readable memory sorted by id; unusable files are skipped.

        Raises:
            AgentMemoryError: ``too_many_files`` if the folder holds more than ``max_files``.
        """
        memories, _ = self._scan(emit=True)
        return sorted(memories, key=lambda memory: memory.id)

    def scan_errors(self) -> _Errors:
        """Scan the folder and report every file that ``list()`` skips."""
        _, errors = self._scan(emit=False)
        return errors

    def search(
        self,
        query: str,
        tags: Sequence[str] | None = None,
        limit: int = 10,
    ) -> _Scored:
        """Rank memories against ``query``.

        Each distinct query token adds 3 when it is a title token, 2 when it equals a tag
        and 1 when it is a body token. Zero-score memories are omitted. An empty query
        returns every memory with score 0, newest first. ``tags`` requires every tag.

        Raises:
            AgentMemoryError: ``query_too_long`` or ``invalid_memory`` (bad tag filter).
        """
        if not isinstance(query, str):
            raise AgentMemoryError("invalid_memory", "query must be a string")
        if len(query) > MAX_QUERY_CHARS:
            raise AgentMemoryError(
                "query_too_long",
                f"query exceeds {MAX_QUERY_CHARS} characters",
            )
        if isinstance(tags, str) or not all(isinstance(tag, str) for tag in tags or ()):
            raise AgentMemoryError("invalid_memory", "tags must be a sequence of strings")
        required = {tag.casefold() for tag in tags or ()}
        wanted = set(_TOKEN_RE.findall(query.casefold()))
        scored: _Scored = []
        for memory in self.list():
            if not required.issubset(memory.tags):
                continue
            score = _score(memory, wanted)
            if wanted and score == 0:
                continue
            scored.append((memory, score))
        scored.sort(key=lambda item: item[0].id)
        scored.sort(key=lambda item: (item[1], item[0].updated), reverse=True)
        return scored[: max(limit, 0)]

    def write_index(self) -> Path:
        """Write ``INDEX.md`` (sorted by id) and return its path."""
        lines = ["# Memory index", ""]
        for memory in self.list():
            entry = f"- [{_escape(memory.title)}]({memory.id}.md)"
            if memory.tags:
                entry = f"{entry} {', '.join(memory.tags)}"
            lines.append(entry)
        text = "\n".join(lines) + "\n"
        root = self._root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        path = root / INDEX_NAME
        _atomic_write(path, text.encode())
        return path

    def _prepare(self, memory: Memory) -> Memory:
        if not isinstance(memory, Memory):
            raise AgentMemoryError("invalid_memory", "memory must be a Memory instance")
        mid = _check_id(memory.id)
        title = _check_title(memory.title)
        tags = _normalize_tags(memory.tags)
        body = memory.body
        if not isinstance(body, str):
            raise AgentMemoryError("invalid_memory", f"body of {mid} must be a string")
        if len(body) > self._max_body_chars:
            raise AgentMemoryError(
                "body_too_large",
                f"body of {mid} exceeds {self._max_body_chars} characters",
            )
        return Memory(
            id=mid,
            title=title,
            body=body.replace("\r\n", "\n"),
            tags=tags,
            created=_to_utc(memory.created, "invalid_memory", "created"),
            updated=_to_utc(memory.updated, "invalid_memory", "updated"),
        )

    def _emit(self, event: MemoryEvent) -> None:
        if self._on_event is None:
            return
        try:
            self._on_event(event)
        except Exception:
            logger.warning("on_event callback failed for kind=%s", event.kind)

    def _md_names(self, root: Path) -> list[str]:
        names: list[str] = []
        try:
            iterator = os.scandir(root)
        except FileNotFoundError:
            return names
        with iterator:
            for entry in iterator:
                if entry.name.endswith(".md") and entry.name != INDEX_NAME:
                    names.append(entry.name)
                    if len(names) > self._max_files:
                        raise AgentMemoryError(
                            "too_many_files",
                            f"store holds more than {self._max_files} files",
                        )
        return sorted(names)

    def _read_text(self, path: Path) -> str:
        try:
            info = os.lstat(path)
        except FileNotFoundError as exc:
            raise _Skip("not_found") from exc
        except OSError as exc:
            raise _Skip("read_error") from exc
        if stat.S_ISLNK(info.st_mode):
            raise _Skip("symlink")
        if not stat.S_ISREG(info.st_mode):
            raise _Skip("not_regular")
        if info.st_size > self._max_file_bytes:
            raise _Skip("file_too_large")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as handle:
                data = handle.read(self._max_file_bytes + 1)
        except OSError as exc:
            raise _Skip("read_error") from exc
        if len(data) > self._max_file_bytes:
            raise _Skip("file_too_large")
        try:
            return data.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError as exc:
            raise _Skip("encoding_invalid") from exc

    def _read_memory(self, path: Path, stem: str) -> Memory:
        memory = _parse(self._read_text(path))
        if memory.id != stem:
            raise _Skip("frontmatter_invalid")
        return memory

    def _scan(self, emit: bool) -> tuple[_Memories, _Errors]:
        root = self._root.resolve()
        memories: _Memories = []
        errors: _Errors = []
        for name in self._md_names(root):
            path = root / name
            stem = name[:-3]
            try:
                if _ID_RE.fullmatch(stem) is None:
                    raise _Skip("invalid_id")
                memories.append(self._read_memory(path, stem))
            except _Skip as skip:
                errors.append(ScanError(str(path), skip.code))
                if emit:
                    logger.warning("skipped a memory file: code=%s", skip.code)
                    known = stem if _ID_RE.fullmatch(stem) else None
                    self._emit(MemoryEvent("skipped", known, str(path), skip.code))
        return memories, errors[: self._max_files]
