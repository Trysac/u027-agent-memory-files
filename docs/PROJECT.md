# Project record

## Objective

Agent memory stored as Markdown files with JSON-valued frontmatter, ranked keyword search, an index file and deterministic compaction

## Scope

- In scope: the public API documented in [README.md](../README.md), typed and tested.
- Out of scope: anything that needs runtime dependencies or I/O without an accepted ADR.

## Timeline

| Date | Event |
|---|---|
| 2026 | Repository created from the factory utility template. |

## Effort metrics

Measured values only; a value that has not been measured stays "pending measurement".

| Metric | Value |
|---|---|
| Tokens (input / output) | 495297 / 113892 |
| Hours (human) | pending measurement |
| Cost (USD) | 0.9239 |

## Factory run u027 (measured from API usage)

| Phase | Model | Batch | Input | Output | Cache write | Cache read | Cost (USD) |
|---|---|---|---|---|---|---|---|
| generation | claude-sonnet-5-5 | yes | 2862 | 95139 | 0 | 4246 | 0.4790 |
| fix-1 | claude-sonnet-5-5 | no | 4 | 614 | 6697 | 25002 | 0.0254 |
| fix-2 | claude-sonnet-5-5 | no | 8 | 932 | 7973 | 58098 | 0.0351 |
| fix-3 | claude-sonnet-5-5 | no | 8 | 829 | 8818 | 60560 | 0.0364 |
| fix-4 | claude-sonnet-5-5 | no | 8 | 1532 | 22218 | 70604 | 0.0779 |
| audit | claude-sonnet-5-5 | no | 12 | 5695 | 44983 | 147919 | 0.1842 |
| docs | claude-haiku-5-5 | no | 125966 | 9151 | 0 | 0 | 0.0859 |

Total: $0.9239. Prices: Anthropic list prices.
