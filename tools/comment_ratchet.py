#!/usr/bin/env python3
"""Comment ratchet: code comments are banned, and the count may only go down.

Counts `#` comments in tracked .py and .sh files. Shebangs, encoding lines, SPDX/license tags and tool pragmas
(noqa, type: ignore, pragma, pylint, fmt, isort, shellcheck) are not counted. Docstrings are not comments.

    python3 tools/comment_ratchet.py            check: fail if any file has more comments than .comment-baseline.json
    python3 tools/comment_ratchet.py --update   write the current counts as the new baseline (only lowers counts
                                                unless --allow-increase is also given)
    python3 tools/comment_ratchet.py --list F   print every counted comment in F, with its line number

A new file starts at 0. Why a decision was made goes in docs/adr/, a hack to fix goes in an issue, and the rest
is deleted (see .claude/skills/delete-comment/SKILL.md).
"""
import argparse
import io
import json
import os
import re
import subprocess
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE = os.path.join(ROOT, ".comment-baseline.json")
ALLOWED = re.compile(r"^#!|-\*-|coding[:=]|type:\s*ignore|noqa|pragma|SPDX|pylint:|fmt:\s*(on|off)|isort:|shellcheck")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def python_comments(text):
    out = []
    try:
        for t in tokenize.generate_tokens(io.StringIO(text).readline):
            if t.type == tokenize.COMMENT:
                out.append((t.start[0], t.string))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return out


def shell_comments(text):
    out, heredoc = [], None
    for n, line in enumerate(text.split("\n"), 1):
        s = line.strip()
        if heredoc:
            if s == heredoc:
                heredoc = None
            continue
        if s.startswith("#"):
            out.append((n, s))
        else:
            m = re.search(r"\s#\s.*$", line)
            if m and not re.search(r"['\"][^'\"]*\s#", line[:m.start() + 2]):
                out.append((n, m.group(0).strip()))
            h = HEREDOC.search(line)
            if h:
                heredoc = h.group(1)
    return out


def comments(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    found = python_comments(text) if path.endswith(".py") else shell_comments(text)
    return [(n, c) for n, c in found if not ALLOWED.search(c)]


def tracked():
    try:
        files = subprocess.check_output(["git", "-C", ROOT, "ls-files"], text=True).split("\n")
    except (OSError, subprocess.CalledProcessError):
        files = [os.path.relpath(os.path.join(d, f), ROOT) for d, _, fs in os.walk(ROOT) for f in fs]
    return sorted(f for f in files if f.endswith((".py", ".sh")) and os.path.isfile(os.path.join(ROOT, f)))


def counts():
    return {f: n for f in tracked() if (n := len(comments(os.path.join(ROOT, f))))}


def load_baseline():
    if not os.path.exists(BASELINE):
        return {}
    with open(BASELINE) as f:
        return json.load(f)


def check():
    base, now = load_baseline(), counts()
    worse = {f: (base.get(f, 0), n) for f, n in now.items() if n > base.get(f, 0)}
    better = sum(base.get(f, 0) - now.get(f, 0) for f in base if now.get(f, 0) < base[f])
    for f, (was, n) in sorted(worse.items()):
        print(f"{f}: {n} comments, baseline {was}. Delete the new ones (see docs/adr/ and the delete-comment skill).")
    if better:
        print(f"{better} comments fewer than the baseline: run tools/comment_ratchet.py --update to lock that in.")
    print(f"{sum(now.values())} comments left in {len(now)} files (baseline {sum(base.values())}).")
    return 1 if worse else 0


def update(allow_increase):
    base, now = load_baseline(), counts()
    if not allow_increase:
        now = {f: min(n, base.get(f, 0)) if base else n for f, n in now.items()}
        now = {f: n for f, n in now.items() if n}
    with open(BASELINE, "w") as f:
        json.dump(dict(sorted(now.items())), f, indent=1)
        f.write("\n")
    print(f"baseline: {sum(now.values())} comments in {len(now)} files")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--allow-increase", action="store_true")
    ap.add_argument("--list", metavar="FILE", nargs="*")
    a = ap.parse_args()
    if a.list is not None:
        for f in a.list or tracked():
            for n, c in comments(os.path.join(ROOT, f) if not os.path.isabs(f) else f):
                print(f"{f}:{n}: {c}")
        return 0
    return update(a.allow_increase) if a.update else check()


if __name__ == "__main__":
    sys.exit(main())
