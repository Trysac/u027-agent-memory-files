# Store agent memory as strict Markdown files with JSON-valued frontmatter

Status: accepted (generated, pending human review)

## Context

Coding and research agents need memory that a human can read, diff and edit in git. Vector-database memory libraries hide the data, and a general YAML parser would add a dependency and accept surprising types. The store must also be safe against hostile ids, symlinks and oversized files, and the folder must not grow forever.

## Decision

Each memory is one <id>.md file whose frontmatter is five fixed lines with JSON-encoded string values and one string array, parsed with the standard json module. Ids and tags use strict patterns before any path is built, symlinks are never followed, writes are atomic, and every input has a size cap. Search is a deterministic weighted keyword score. Compaction groups old tagged memories by first tag into a summary memory that lists the merged ids in a trailer comment; it is written before the originals are deleted, which makes crash recovery a pure function of the folder contents. The summarizer is a Protocol and the clock is injected.

## Consequences

The folder is transparent, diffable and dependency-free, and every parse failure is a skipped file with a reported code instead of an exception. Frontmatter is deliberately narrower than YAML, so hand edits must keep the exact line layout. Search is keyword-only, with no semantic ranking. Only one writer per folder is supported. Compaction needs one free file slot under max_files, and a summary that already exists is never rewritten, so memories added to a group after it was summarized wait for the next day's summary.
