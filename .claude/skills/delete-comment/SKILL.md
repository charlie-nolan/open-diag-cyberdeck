---
name: delete-comment
description: Find the code comments left in this repo (or new ones), sort each into tautology / stale / excuse / decision record, delete them, file issues for hacks, move decisions into docs/adr/, and lower the comment ratchet baseline.
---

# /delete-comment

Comments are banned here (docs/adr/0001-no-code-comments.md). `tools/comment_ratchet.py` fails CI when a file has
more comments than `.comment-baseline.json` allows. This skill removes the ones that are left, a file or a folder
at a time.

## 1. Find them

```bash
python3 tools/comment_ratchet.py --list                 # every counted comment in the repo
python3 tools/comment_ratchet.py --list path/to/file.py # one file
```

Not comments (never touch): shebangs, `# -*- coding` lines, SPDX/license tags, `noqa`, `type: ignore`, `pragma`,
`pylint:`, `fmt:`, `isort:`, `shellcheck` directives. Docstrings are not comments: leave them unless one only
restates the function name.

## 2. Sort each comment (read the code around it first)

| Kind | Test | Action |
|---|---|---|
| Tautology | The code already says it (names, types, the line itself). Section dividers too. | Delete. |
| Stale | It describes code, files, flags or behaviour that no longer exist (check with grep). | Delete. |
| Excuse | It explains why a hack, workaround, hard-coded value, fixed sleep, `sys.path` edit, special case for one car/make, or ignored argument is "fine". | Delete, and open an issue that names the file and function, says why it's a hack and how to fix it properly. |
| Decision record | It records a non-obvious choice someone would otherwise undo: a measured number, a hardware/protocol fact, a safety rule, a rejected alternative. | Move it into the ADR for that area in docs/adr/ (see docs/adr/README.md; add an entry under the file, naming the function or class), then delete it. |

When in doubt between tautology and decision record, ask: would a careful reader change this code wrongly without
it? Yes = decision record.

## 3. Rules for anything that leaves the code

The repo is public. In issues and ADRs:
- No VINs, customer names, IPs, hostnames, prices, emails, or links to chat sessions.
- No text copied from service manuals; describe the decision instead of quoting the page.
- Never name one owner's car as the reason for a rule. If an excuse hides a fix made for one car, say so generically
  ("tuned to one tested ECU") and propose the general fix.

## 4. Delete and check

- Delete whole comment lines; strip inline comments back to the code (`x = 1  # ...` -> `x = 1`). Don't leave
  three blank lines in a row.
- A Python file that only had a header comment gets it as a module docstring instead.
- Check: `python3 -m py_compile` on changed .py files, `bash -n` on changed .sh files, then the repo's tests.

## 5. Lock it in

```bash
python3 tools/comment_ratchet.py --update   # lowers the baseline, never raises it
python3 tools/comment_ratchet.py            # must pass
```

Commit the code, `.comment-baseline.json`, the ADR changes, and list the new issues in the PR description.
`--update --allow-increase` exists only for adding a file with an allowed header; don't use it to let comments back in.

## 6. Estimate (if asked)

Lines shed = full-line comments (each is a line) plus nothing for inline comments (the line stays). Count with:

```bash
python3 tools/comment_ratchet.py --list | wc -l
```
