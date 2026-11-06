"""Tests for the public API of agent_memory_files."""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import agent_memory_files
from agent_memory_files import (
    AgentMemoryError,
    CompactionReport,
    ConcatSummarizer,
    Memory,
    MemoryEvent,
    MemoryStore,
    compact,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=UTC)
NOW = datetime(2026, 3, 1, tzinfo=UTC)
TS = "2026-01-02T03:04:05.000006Z"
HEAD = '---\nid: "m1"\ntitle: "a: b"\ntags: ["cache"]\ncreated: "2026-01-02T03:04:05.000006Z"\n'
INDEX_TEXT = "# Memory index\n\n- [A \\[x\\]](m1.md) db, ops\n- [B](m2.md)\n"


class Clock:
    """A settable fake clock."""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FixedSummarizer:
    """Returns a fixed value and records the ids it was given."""

    def __init__(self, text: object) -> None:
        self.text = text
        self.seen: list[tuple[str, ...]] = []

    def summarize(self, memories: Sequence[Memory]) -> object:
        self.seen.append(tuple(m.id for m in memories))
        return self.text


def mem(
    mid: str,
    title: str = "Title",
    body: str = "",
    tags: tuple[str, ...] = (),
    when: datetime = T0,
) -> Memory:
    return Memory(id=mid, title=title, body=body, tags=tags, created=when, updated=when)


def seed_old(store: MemoryStore, ids: list[str], tag: str = "ops", days: int = 40) -> None:
    old = NOW - timedelta(days=days)
    for mid in ids:
        store.save(mem(mid, f"Title {mid}", f"body {mid}\nsecond line", (tag,), old))


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(root.iterdir())}


def put(root: Path, name: str, text: str) -> None:
    (root / name).write_bytes(text.encode("utf-8"))


def frontmatter(**overrides: str) -> str:
    fields = {
        "id": '"m1"',
        "title": '"t"',
        "tags": "[]",
        "created": f'"{TS}"',
        "updated": f'"{TS}"',
    }
    fields.update(overrides)
    lines = [f"{key}: {value}" for key, value in fields.items()]
    return "---\n" + "\n".join(lines) + "\n---\n\nbody"


ACCEPTANCE_FRAGMENTS = (
    ") equals the saved Memory with tags (",
    ",), body ",
    ") and m2 (title ",
    ", tags (",
    "), search(",
    ") returns [(m1, 4), (m2, 2)]; zero-score memories are omitted; search(",
    ", tags ()) writes INDEX.md byte-exactly ",
    " * 65 raise AgentMemoryError code ",
    " with nothing written; tag ",
    " raises code ",
    "; a body of max_body_chars + 1 chars raises code ",
    "; with max_files=2 and two files, saving a third id raises code ",
)


def test_acceptance_fragments_are_declared() -> None:
    assert len(ACCEPTANCE_FRAGMENTS) == 12


# --- save / load -------------------------------------------------------------------------


def test_save_writes_exact_frontmatter_and_load_roundtrips(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    memory = Memory(
        id="m1",
        title="a: b",
        body="x\n---\nid: y\n---",
        tags=("Cache", "cache"),
        created=T0,
        updated=T0,
    )
    path = store.save(memory)
    assert path == tmp_path.resolve() / "m1.md"
    raw = path.read_bytes()
    assert raw.startswith(HEAD.encode())
    tail = 'updated: "2026-01-02T03:04:05.000006Z"\n---\n\nx\n---\nid: y\n---'
    assert raw == (HEAD + tail).encode()
    assert store.load("m1") == Memory(
        id="m1",
        title="a: b",
        body="x\n---\nid: y\n---",
        tags=("cache",),
        created=T0,
        updated=T0,
    )


def test_empty_and_unicode_bodies_roundtrip(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("empty"))
    store.save(mem("uni", "T\u00edtulo \u2603", "caf\u00e9\n"))
    assert store.load("empty").body == ""
    loaded = store.load("uni")
    assert (loaded.title, loaded.body) == ("T\u00edtulo \u2603", "caf\u00e9\n")


def test_aware_timestamps_are_stored_as_utc(tmp_path: Path) -> None:
    local = datetime(2026, 1, 2, 5, 4, 5, 6, tzinfo=timezone(timedelta(hours=2)))
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("m1", when=local))
    text = (tmp_path / "m1.md").read_text(encoding="utf-8")
    assert 'created: "2026-01-02T03:04:05.000006Z"' in text
    assert store.load("m1").created == T0


def test_overwrite_keeps_created_and_sets_updated(tmp_path: Path) -> None:
    clock = Clock(T0)
    store = MemoryStore(tmp_path, clock)
    store.save(mem("m1", "Old", "one"))
    clock.value = T0 + timedelta(hours=1)
    ancient = datetime(2020, 1, 1, tzinfo=UTC)
    store.save(mem("m1", "New", "two", when=ancient))
    loaded = store.load("m1")
    assert (loaded.title, loaded.body) == ("New", "two")
    assert loaded.created == T0
    assert loaded.updated == T0 + timedelta(hours=1)


def test_overwriting_a_broken_file_uses_the_new_created(tmp_path: Path) -> None:
    put(tmp_path, "m1.md", "junk")
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("m1"))
    assert store.load("m1").created == T0


def test_failed_replace_leaves_old_bytes_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("m1", body="old"))
    before = (tmp_path / "m1.md").read_bytes()

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        store.save(mem("m1", body="new"))
    monkeypatch.undo()
    assert (tmp_path / "m1.md").read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["m1.md"]


def test_crlf_is_read_and_rewritten_as_lf(tmp_path: Path) -> None:
    put(tmp_path, "m1.md", frontmatter().replace("\n", "\r\n"))
    store = MemoryStore(tmp_path, Clock(T0))
    assert store.load("m1").body == "body"
    store.save(mem("m1", body="line1\r\nline2"))
    assert b"\r" not in (tmp_path / "m1.md").read_bytes()
    assert store.load("m1").body == "line1\nline2"


def test_frontmatter_helper_text_is_loadable(tmp_path: Path) -> None:
    put(tmp_path, "m1.md", frontmatter())
    memory = MemoryStore(tmp_path, Clock(T0)).load("m1")
    assert (memory.title, memory.body, memory.tags) == ("t", "body", ())


def test_missing_root_lists_nothing_and_save_creates_it(tmp_path: Path) -> None:
    root = tmp_path / "nope"
    store = MemoryStore(root, Clock(T0))
    assert store.list() == []
    assert store.scan_errors() == []
    assert store.search("x") == []
    store.save(mem("m1"))
    assert (root / "m1.md").is_file()


def test_memory_is_immutable() -> None:
    memory = mem("m1")
    with pytest.raises(dataclasses.FrozenInstanceError):
        memory.title = "changed"  # type: ignore[misc]


# --- search ------------------------------------------------------------------------------


def test_search_ranks_title_tag_and_body_hits(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    m1 = mem("m1", "Cache layer", "cache cache", ("db",))
    m2 = mem("m2", "Notes", "", ("cache",))
    store.save(m1)
    store.save(m2)
    store.save(mem("m3", "Other", "nothing here"))
    assert store.search("cache") == [(m1, 4), (m2, 2)]
    assert store.search("cache cache CACHE") == [(m1, 4), (m2, 2)]
    assert store.search("cache layer")[0] == (m1, 7)
    assert store.search("cache", limit=1) == [(m1, 4)]
    assert store.search("cache", limit=0) == []
    assert store.search("cache", tags=["db"]) == [(m1, 4)]
    assert store.search("cache", tags=["DB", "ops"]) == []


def test_search_tokenization_neighbours(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("a", body="cache-layer; (cache)"))
    store.save(mem("b", body="cachelayer cache2 caches"))
    assert [(m.id, s) for m, s in store.search("cache")] == [("a", 1)]
    store.save(mem("c", title="Stra\u00dfe"))
    assert [(m.id, s) for m, s in store.search("STRASSE")] == [("c", 3)]


def test_empty_query_lists_everything_newest_first(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("b"))
    store.save(mem("a"))
    store.save(mem("c", when=T0 + timedelta(seconds=1)))
    assert [(m.id, s) for m, s in store.search("")] == [("c", 0), ("a", 0), ("b", 0)]
    assert [m.id for m, _ in store.search("!!!")] == ["c", "a", "b"]
    assert [m.id for m, _ in store.search("", limit=2)] == ["c", "a"]


def test_query_length_limit(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    assert store.search("a" * 1000) == []
    with pytest.raises(AgentMemoryError) as excinfo:
        store.search("a" * 1001)
    assert excinfo.value.code == "query_too_long"


def test_search_rejects_bad_arguments(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    for query, tags in ((5, None), ("x", "db"), ("x", [1])):
        with pytest.raises(AgentMemoryError) as excinfo:
            store.search(query, tags=tags)  # type: ignore[arg-type]
        assert excinfo.value.code == "invalid_memory"


# --- index -------------------------------------------------------------------------------


def test_write_index_is_byte_exact_and_ignored_by_list(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    store.save(mem("m2", "B"))
    store.save(mem("m1", "A [x]", tags=("db", "ops")))
    path = store.write_index()
    assert path == tmp_path.resolve() / "INDEX.md"
    assert path.read_bytes() == INDEX_TEXT.encode()
    assert [m.id for m in store.list()] == ["m1", "m2"]
    assert store.scan_errors() == []


def test_index_escapes_backslashes_and_handles_an_empty_store(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    assert store.write_index().read_bytes() == b"# Memory index\n\n"
    store.save(mem("m1", "a\\b]"))
    assert store.write_index().read_text(encoding="utf-8").splitlines()[2] == (
        "- [a\\\\b\\]](m1.md)"
    )


# --- compaction --------------------------------------------------------------------------


def test_compact_merges_old_groups(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t1", "t2", "t3"])
    report = compact(store, ConcatSummarizer(), timedelta(days=30))
    assert report == CompactionReport(
        merged_ids=("t1", "t2", "t3"),
        created_ids=("compacted-ops-20260301",),
    )
    assert sorted(p.name for p in tmp_path.iterdir()) == ["compacted-ops-20260301.md"]
    summary = store.load("compacted-ops-20260301")
    assert summary.tags == ("ops",)
    assert summary.created == NOW
    assert summary.updated == NOW
    assert summary.body == (
        "- Title t1: body t1\n- Title t2: body t2\n- Title t3: body t3\n\n<!-- merged: t1 t2 t3 -->"
    )


def test_small_groups_are_untouched_and_a_second_run_is_a_noop(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t1", "t2"])
    before = snapshot(tmp_path)
    assert compact(store, ConcatSummarizer(), timedelta(days=30)) == CompactionReport((), ())
    assert snapshot(tmp_path) == before
    store.save(mem("t3", "Title t3", "body t3\nsecond line", ("ops",), NOW - timedelta(days=40)))
    assert compact(store, ConcatSummarizer(), timedelta(days=30)).created_ids == (
        "compacted-ops-20260301",
    )
    after_first = snapshot(tmp_path)
    assert compact(store, ConcatSummarizer(), timedelta(days=30)) == CompactionReport((), ())
    assert snapshot(tmp_path) == after_first


def test_crash_recovery_deletes_only_listed_ids(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t1", "t2", "t3"])
    compact(store, ConcatSummarizer(), timedelta(days=30))
    seed_old(store, ["t1"])
    summary_path = tmp_path / "compacted-ops-20260301.md"
    before = summary_path.read_bytes()
    report = compact(store, ConcatSummarizer(), timedelta(days=30))
    assert report == CompactionReport(merged_ids=("t1",), created_ids=())
    assert not (tmp_path / "t1.md").exists()
    assert summary_path.read_bytes() == before


def test_recovery_ignores_unlisted_invalid_and_summary_ids(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    trailer = "<!-- merged: t1 BAD/ID compacted-ops-20260301 compacted-other-20200101 t9 -->"
    store.save(
        Memory(
            id="compacted-ops-20260301",
            title="s",
            body=f"s\n\n{trailer}",
            tags=("ops",),
            created=NOW,
            updated=NOW,
        )
    )
    seed_old(store, ["t1", "t2"])
    report = compact(store, ConcatSummarizer(), timedelta(days=30))
    assert report == CompactionReport(merged_ids=("t1",), created_ids=())
    assert (tmp_path / "t2.md").exists()
    assert (tmp_path / "compacted-ops-20260301.md").exists()


def test_summary_without_trailer_deletes_nothing(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    store.save(
        Memory(
            id="compacted-ops-20260301",
            title="s",
            body="no trailer",
            tags=("ops",),
            created=NOW,
            updated=NOW,
        )
    )
    seed_old(store, ["t1", "t2", "t3"])
    before = snapshot(tmp_path)
    assert compact(store, ConcatSummarizer(), timedelta(days=30)) == CompactionReport((), ())
    assert snapshot(tmp_path) == before


def test_unreadable_existing_summary_is_left_alone(tmp_path: Path) -> None:
    put(tmp_path, "compacted-ops-20260301.md", "junk")
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t1", "t2", "t3"])
    assert compact(store, ConcatSummarizer(), timedelta(days=30)) == CompactionReport((), ())
    assert (tmp_path / "compacted-ops-20260301.md").read_bytes() == b"junk"
    assert (tmp_path / "t1.md").exists()


def test_groups_follow_the_first_tag(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    old = NOW - timedelta(days=40)
    for mid, tags in (
        ("o1", ("ops", "db")),
        ("o2", ("ops",)),
        ("o3", ("ops", "x")),
        ("d1", ("db",)),
        ("d2", ("db",)),
        ("d3", ("db", "ops")),
    ):
        store.save(mem(mid, tags=tags, when=old))
    report = compact(store, ConcatSummarizer(), timedelta(days=30))
    assert report == CompactionReport(
        merged_ids=("d1", "d2", "d3", "o1", "o2", "o3"),
        created_ids=("compacted-db-20260301", "compacted-ops-20260301"),
    )


def test_untagged_recent_and_summary_memories_are_never_candidates(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    old = NOW - timedelta(days=40)
    for mid in ("u1", "u2", "u3"):
        store.save(mem(mid, when=old))
    for mid in ("r1", "r2", "r3"):
        store.save(mem(mid, tags=("ops",), when=NOW - timedelta(days=30)))
    for mid in ("compacted-ops-20200101", "compacted-ops-20200102", "compacted-ops-20200103"):
        store.save(mem(mid, tags=("ops",), when=old))
    before = snapshot(tmp_path)
    assert compact(store, ConcatSummarizer(), timedelta(days=30)) == CompactionReport((), ())
    assert snapshot(tmp_path) == before


def test_min_group_one_compacts_a_single_memory(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t1"], days=31)
    report = compact(store, ConcatSummarizer(), timedelta(days=30), min_group=1)
    assert report == CompactionReport(("t1",), ("compacted-ops-20260301",))


def test_custom_summarizer_receives_sorted_members(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t2", "t1", "t3"])
    summarizer = FixedSummarizer("SUM")
    compact(store, summarizer, timedelta(days=30))  # type: ignore[arg-type]
    assert summarizer.seen == [("t1", "t2", "t3")]
    body = store.load("compacted-ops-20260301").body
    assert body == "SUM\n\n<!-- merged: t1 t2 t3 -->"


def test_bad_summarizer_output_keeps_the_originals(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    seed_old(store, ["t1", "t2", "t3"])
    with pytest.raises(AgentMemoryError) as excinfo:
        compact(store, FixedSummarizer(5), timedelta(days=30))  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_memory"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t1.md", "t2.md", "t3.md"]


def test_oversized_summary_fails_before_any_deletion(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW), max_body_chars=40)
    seed_old(store, ["t1", "t2", "t3"])
    with pytest.raises(AgentMemoryError) as excinfo:
        compact(store, ConcatSummarizer(), timedelta(days=30))
    assert excinfo.value.code == "body_too_large"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t1.md", "t2.md", "t3.md"]


def test_compact_rejects_bad_arguments_and_clocks(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(NOW))
    with pytest.raises(ValueError, match="min_group"):
        compact(store, ConcatSummarizer(), timedelta(days=1), min_group=0)
    with pytest.raises(ValueError, match="max_age"):
        compact(store, ConcatSummarizer(), timedelta(days=-1))
    naive = MemoryStore(tmp_path, lambda: datetime(2026, 3, 1))
    with pytest.raises(AgentMemoryError) as excinfo:
        compact(naive, ConcatSummarizer(), timedelta(days=1))
    assert excinfo.value.code == "invalid_clock"
    assert compact(store, ConcatSummarizer(), timedelta.max) == CompactionReport((), ())


def test_compaction_emits_a_compacted_event(tmp_path: Path) -> None:
    events: list[MemoryEvent] = []
    store = MemoryStore(tmp_path, Clock(NOW), on_event=events.append)
    seed_old(store, ["t1", "t2", "t3"])
    events.clear()
    compact(store, ConcatSummarizer(), timedelta(days=30))
    assert [(e.kind, e.memory_id) for e in events] == [
        ("saved", "compacted-ops-20260301"),
        ("deleted", "t1"),
        ("deleted", "t2"),
        ("deleted", "t3"),
        ("compacted", "compacted-ops-20260301"),
    ]
    seed_old(store, ["t1"])
    events.clear()
    compact(store, ConcatSummarizer(), timedelta(days=30))
    assert [(e.kind, e.memory_id) for e in events] == [
        ("deleted", "t1"),
        ("compacted", "compacted-ops-20260301"),
    ]


def test_concat_summarizer_sorts_by_id_and_uses_the_first_line() -> None:
    memories = [mem("b", "B", "line1\nline2"), mem("a", "A", "")]
    assert ConcatSummarizer().summarize(memories) == "- A: \n- B: line1"


# --- scanning ----------------------------------------------------------------------------


def test_scan_skips_and_reports_bad_files(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")
    try:
        os.symlink(outside, root / "c.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    put(root, "a.md", "not frontmatter")
    number = frontmatter(id="1").replace("\n\nbody", "\n\n")
    put(root, "b.md", number)
    (root / "d.md").write_bytes(b"x" * 1025)
    store = MemoryStore(root, Clock(T0), max_file_bytes=1024)
    store.save(mem("z"))
    assert [m.id for m in store.list()] == ["z"]
    errors = store.scan_errors()
    assert [Path(e.path).name for e in errors] == ["a.md", "b.md", "c.md", "d.md"]
    assert [e.code for e in errors] == [
        "frontmatter_invalid",
        "frontmatter_invalid",
        "symlink",
        "file_too_large",
    ]
    assert outside.read_bytes() == b"outside"


@pytest.mark.parametrize(
    "overrides",
    [
        {"id": '"other"'},
        {"id": "1"},
        {"id": '"M1"'},
        {"title": "1"},
        {"title": '"a\\nb"'},
        {"title": "'single'"},
        {"tags": '"x"'},
        {"tags": "[1]"},
        {"tags": '["a b"]'},
        {"tags": "null"},
        {"tags": "[" * 100_000},
        {"created": '"2026-01-02"'},
        {"created": '"2026-1-2T03:04:05.000006Z"'},
        {"created": "1"},
        {"updated": '"nope"'},
        {"updated": "true"},
    ],
)
def test_bad_frontmatter_values_are_skipped(tmp_path: Path, overrides: dict[str, str]) -> None:
    put(tmp_path, "m1.md", frontmatter(**overrides))
    store = MemoryStore(tmp_path, Clock(T0))
    assert store.list() == []
    assert [e.code for e in store.scan_errors()] == ["frontmatter_invalid"]
    with pytest.raises(AgentMemoryError) as excinfo:
        store.load("m1")
    assert excinfo.value.code == "invalid_memory"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "---",
        "no frontmatter at all",
        frontmatter().replace("---\n", "--\n", 1),
        frontmatter().replace('id: "m1"', 'id:"m1"'),
        frontmatter().replace("\n---\n\n", "\n--\n\n"),
        frontmatter().replace("\n---\n\nbody", "\n---\nbody"),
        frontmatter().replace('id: "m1"\ntitle: "t"', 'title: "t"\nid: "m1"'),
    ],
)
def test_bad_frontmatter_structure_is_skipped(tmp_path: Path, text: str) -> None:
    put(tmp_path, "m1.md", text)
    store = MemoryStore(tmp_path, Clock(T0))
    assert store.list() == []
    assert [e.code for e in store.scan_errors()] == ["frontmatter_invalid"]


def test_other_unusable_files_are_reported(tmp_path: Path) -> None:
    put(tmp_path, "Bad.md", "x")
    (tmp_path / "dir.md").mkdir()
    (tmp_path / "bin.md").write_bytes(b"\xff\xfe")
    (tmp_path / "notes.txt").write_bytes(b"ignored")
    store = MemoryStore(tmp_path, Clock(T0))
    codes = {Path(e.path).name: e.code for e in store.scan_errors()}
    assert codes == {
        "Bad.md": "invalid_id",
        "dir.md": "not_regular",
        "bin.md": "encoding_invalid",
    }


def test_listing_more_files_than_the_cap_raises(tmp_path: Path) -> None:
    for name in ("a", "b", "c"):
        put(tmp_path, f"{name}.md", "junk")
    store = MemoryStore(tmp_path, Clock(T0), max_files=2)
    calls = (
        store.list,
        store.scan_errors,
        store.write_index,
        lambda: store.search("x"),
    )
    for call in calls:
        with pytest.raises(AgentMemoryError) as excinfo:
            call()
        assert excinfo.value.code == "too_many_files"


def test_scan_errors_never_exceed_max_files(tmp_path: Path) -> None:
    for name in ("a", "b"):
        put(tmp_path, f"{name}.md", "junk")
    store = MemoryStore(tmp_path, Clock(T0), max_files=2)
    assert len(store.scan_errors()) == 2


# --- validation and limits -----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    ["../x", "a/b", "A", "a" * 65, "a\n", "", "\u00e9", "-a", "a.b"],
)
def test_invalid_ids_raise_before_any_path_is_built(tmp_path: Path, bad: str) -> None:
    root = tmp_path / "store"
    store = MemoryStore(root, Clock(T0))
    calls = (
        lambda: store.save(mem(bad)),
        lambda: store.load(bad),
        lambda: store.delete(bad),
        lambda: store.path_for(bad),
    )
    for call in calls:
        with pytest.raises(AgentMemoryError) as excinfo:
            call()
        assert excinfo.value.code == "invalid_id"
        assert len(str(excinfo.value)) < 200
    assert not root.exists()
    assert not (tmp_path / "x.md").exists()


def test_non_string_id_is_invalid(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    with pytest.raises(AgentMemoryError) as excinfo:
        store.load(5)  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_id"


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "a\nb"},
        {"title": "a\rb"},
        {"title": "x" * 201},
        {"tags": ("a b",)},
        {"tags": ("-a",)},
        {"tags": ("x" * 41,)},
        {"tags": ("x" * 1000,)},
        {"tags": tuple(f"t{i}" for i in range(21))},
        {"tags": ("a", 1)},
        {"tags": "abc"},
        {"created": datetime(2026, 1, 1)},
        {"updated": datetime(2026, 1, 1)},
        {"created": "2026-01-01"},
        {"body": "\ud800"},
        {"body": 5},
    ],
)
def test_invalid_memories_are_rejected(tmp_path: Path, changes: dict[str, object]) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    bad = dataclasses.replace(mem("m1"), **changes)
    with pytest.raises(AgentMemoryError) as excinfo:
        store.save(bad)
    assert excinfo.value.code == "invalid_memory"
    assert list(tmp_path.iterdir()) == []


def test_save_rejects_non_memory_values(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    with pytest.raises(AgentMemoryError) as excinfo:
        store.save("m1")  # type: ignore[arg-type]
    assert excinfo.value.code == "invalid_memory"


def test_size_limits_and_caps(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0), max_body_chars=10, max_files=2)
    store.save(mem("a", body="x" * 10))
    with pytest.raises(AgentMemoryError) as big:
        store.save(mem("b", body="x" * 11))
    assert big.value.code == "body_too_large"
    store.save(mem("b"))
    with pytest.raises(AgentMemoryError) as full:
        store.save(mem("c"))
    assert full.value.code == "too_many_files"
    store.save(mem("a", body="again"))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.md", "b.md"]


def test_file_size_cap_applies_to_the_whole_file(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0), max_file_bytes=300)
    with pytest.raises(AgentMemoryError) as excinfo:
        store.save(mem("a", body="y" * 400))
    assert excinfo.value.code == "body_too_large"
    assert list(tmp_path.iterdir()) == []


def test_limits_are_inclusive(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path, Clock(T0))
    tags = tuple(f"{i:02d}" + "x" * 38 for i in range(20))
    store.save(mem("a" * 64, "t" * 200, "", tags))
    loaded = store.load("a" * 64)
    assert len(loaded.title) == 200
    assert loaded.tags == tags


@pytest.mark.parametrize("value", [datetime(2026, 1, 1), "2026-01-01"])
def test_invalid_clock_values(tmp_path: Path, value: object) -> None:
    store = MemoryStore(tmp_path, lambda: value)  # type: ignore[arg-type,return-value]
    with pytest.raises(AgentMemoryError) as excinfo:
        store.save(mem("m1"))
    assert excinfo.value.code == "invalid_clock"
    with pytest.raises(AgentMemoryError) as second:
        store.now()
    assert second.value.code == "invalid_clock"
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_bytes": 0},
        {"max_body_chars": -1},
        {"max_files": 0},
        {"max_files": True},
    ],
)
def test_constructor_rejects_bad_limits(tmp_path: Path, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        MemoryStore(tmp_path, Clock(T0), **kwargs)


def test_missing_and_symlinked_files(tmp_path: Path) -> None:
    root = tmp_path / "store"
    root.mkdir()
    store = MemoryStore(root, Clock(T0))
    for call in (lambda: store.load("zz"), lambda: store.delete("zz")):
        with pytest.raises(AgentMemoryError) as missing:
            call()
        assert missing.value.code == "not_found"
        assert "zz" in str(missing.value)
    outside = tmp_path / "outside.md"
    outside.write_bytes(b"outside")
    try:
        os.symlink(outside, root / "m1.md")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    calls = (
        lambda: store.load("m1"),
        lambda: store.save(mem("m1")),
        lambda: store.delete("m1"),
    )
    for call in calls:
        with pytest.raises(AgentMemoryError) as excinfo:
            call()
        assert excinfo.value.code == "invalid_memory"
    assert outside.read_bytes() == b"outside"


# --- events, logging, errors -----------------------------------------------------------------


def test_events_carry_only_ids_paths_and_codes(tmp_path: Path) -> None:
    events: list[MemoryEvent] = []
    store = MemoryStore(tmp_path, Clock(T0), on_event=events.append)
    store.save(mem("m1", "PRIVATE-TITLE-MARKER", "PRIVATE-BODY-MARKER"))
    put(tmp_path, "bad.md", "junk")
    put(tmp_path, "Odd.md", "junk")
    store.list()
    store.delete("m1")
    assert [(e.kind, e.memory_id, e.code) for e in events] == [
        ("saved", "m1", None),
        ("skipped", "bad", "frontmatter_invalid"),
        ("skipped", None, "invalid_id"),
        ("deleted", "m1", None),
    ]
    assert "PRIVATE" not in repr(events)


def test_a_failing_event_callback_does_not_break_the_store(tmp_path: Path) -> None:
    def boom(event: MemoryEvent) -> None:
        raise RuntimeError("callback failed")

    store = MemoryStore(tmp_path, Clock(T0), on_event=boom)
    store.save(mem("m1"))
    assert store.load("m1").id == "m1"


def test_errors_are_typed_and_do_not_shadow_the_builtin() -> None:
    error = AgentMemoryError("not_found", "memory not found: m1")
    assert error.code == "not_found"
    assert error.message == str(error)
    assert not issubclass(AgentMemoryError, MemoryError)
    assert issubclass(AgentMemoryError, Exception)


def test_package_metadata_and_null_handlers() -> None:
    assert agent_memory_files.__version__ == "0.1.0"
    for name in ("agent_memory_files", "agent_memory_files.store"):
        handlers = logging.getLogger(name).handlers
        assert any(isinstance(h, logging.NullHandler) for h in handlers)
    for name in agent_memory_files.__all__:
        assert hasattr(agent_memory_files, name)
