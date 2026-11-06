# File format, search and compaction

This page documents the on-disk format and the behaviour of `agent_memory_files`.

## File format

Every memory is one UTF-8 file `<id>.md` directly under the store folder, written with LF line
endings (CRLF is read and rewritten as LF):

```
---
id: "m1"
title: "a: b"
tags: ["cache"]
created: "2026-01-02T03:04:05.000006Z"
updated: "2026-01-02T03:04:05.000006Z"
---

The body, verbatim.
```

- Exactly five frontmatter lines in this order; each value is `json.dumps(value, ensure_ascii=False)`.
- Values are JSON strings, plus one array of strings for `tags`. Numbers, booleans, null and nested
  structures make the file `frontmatter_invalid`. Parsing uses the standard `json` module only.
- The frontmatter ends at the sixth line, so body lines equal to `---` never end it.
- Timestamps are UTC with the format `%Y-%m-%dT%H:%M:%S.%fZ`; a naive clock value raises `invalid_clock`.
- Ids match `[a-z0-9][a-z0-9-]{0,63}`; tags are casefolded, deduplicated, match
  `[a-z0-9][a-z0-9-]{0,39}` and number at most 20; titles have at most 200 characters and one line.

## Saving

A new id is stored with the memory's own `created` and `updated`. An existing id keeps its stored
`created` and receives the clock value as `updated`. Writes go to a temporary file in the same
folder and are moved with `os.replace`; when the replace fails the old file is untouched and the
temporary file is removed.

## Search

Tokens come from `re.findall(r"\w+", text.casefold())`. Each distinct query token adds 3 when it is a
title token, 2 when it equals a tag and 1 when it is a body token. Zero-score memories are omitted.
An empty query returns every memory with score 0, newest `updated` first and then by id. A tag
filter requires every listed tag. Queries over 1000 characters raise `query_too_long`.

## Index

`write_index()` writes `INDEX.md` with one line per memory sorted by id:
`- [<title>](<id>.md)` followed by a space and the tags joined with `, ` when there are tags.
Backslash, `[` and `]` in titles are backslash-escaped. `list()` ignores `INDEX.md`, and it is not
counted against `max_files`.

## Compaction

`compact(store, summarizer, max_age, min_group=3)` merges memories whose `updated` is older than
`now - max_age`, that have at least one tag and whose id does not start with `compacted-`. They are
grouped by their first tag; every group of at least `min_group` becomes
`compacted-<tag>-<YYYYMMDD of now>` with the tags `(tag,)` and the body
`<summary>` + blank line + `<!-- merged: <space-separated ids> -->`. The summary is written before
the originals are deleted. If today's summary for a group already exists (for example after a crash)
only the ids listed in its trailer that are still present are deleted and the summary is never
rewritten. The save of the summary needs one free slot below `max_files`.

`ConcatSummarizer` writes `- <title>: <first body line>` per memory sorted by id. Any object with a
`summarize(memories) -> str` method can replace it.

## Limits

| Limit | Default | Error |
|---|---|---|
| `max_file_bytes` | 262144 | file skipped as `file_too_large`; saving raises `body_too_large` |
| `max_body_chars` | 100000 | `body_too_large` |
| `max_files` | 10000 | `too_many_files` |
| query length | 1000 | `query_too_long` |

One writer per folder is supported; there are no cross-process locks.

## Threat model

- Ids are validated against a strict pattern before any path is built, and the resulting path must be
  a direct child of the resolved root.
- `os.lstat` runs first and symlinks are never followed when reading, listing, saving or deleting;
  files are opened with `O_NOFOLLOW` where the platform offers it.
- Files larger than `max_file_bytes` are not read; at most `max_file_bytes + 1` bytes are read.
- Malformed JSON, deep nesting (`RecursionError`) and invalid UTF-8 mark a file as skipped instead of
  raising; `scan_errors()` reports the reason.
- Error messages carry the id only (at most 40 characters of caller input). Events carry ids, paths and
  codes, never titles or bodies. The library logs codes only through `logging.getLogger(__name__)`.
- Out of scope: embeddings, an LLM summarizer, file watching and cross-process locking.
