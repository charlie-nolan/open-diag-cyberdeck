# ADR-0001: No code comments; a ratchet keeps the count going down

Status: Accepted (2026-11-20)

## Context

Comments in this codebase had drifted into four kinds: ones that repeat the code (tautology), ones that describe
code that has since changed (stale), ones that excuse a hack so it gets copied elsewhere (excuse), and ones that
record a real, non-obvious decision (decision record). Only the last kind is worth keeping, and a comment is the
wrong home for it: it is scattered, it goes stale with the line beside it, and nothing reviews it.

## Decision

- Code comments (`#` in .py and .sh) are banned. Shebangs, encoding lines, SPDX/license tags and tool pragmas
  (`noqa`, `type: ignore`, `pragma`, `shellcheck` ...) are not comments. Docstrings are not comments either.
- Tautology and stale comments are deleted. Excuse comments are deleted and get an issue
  to fix the hack. Decision records move into an ADR in this folder, grouped by area.
- `tools/comment_ratchet.py` counts comments per file against `.comment-baseline.json` and fails CI when any file
  goes up. A new file starts at 0. After deleting comments, run `python3 tools/comment_ratchet.py --update`.
- What is left in the baseline after the first pass is mostly usage headers at the top of shell scripts; they go
  into `--help` text or docs over time.
- `.claude/skills/delete-comment/SKILL.md` repeats the process.

## Consequences

Why something is the way it is lives in docs/adr/ (decisions), in issues (hacks to fix), or in names, tests and
docstrings. A reviewer sees new comments as a CI failure instead of having to spot them.
