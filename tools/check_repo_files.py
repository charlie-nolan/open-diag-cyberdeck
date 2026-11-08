#!/usr/bin/env python3
"""This repo is code only: no manual-derived data, built databases, models or raw downloads (see README).
Fails (exit 1) on any tracked file that is one. Run locally before a push:  python3 tools/check_repo_files.py"""
import os
import subprocess
import sys

BLOCKED_EXT = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".mdb", ".lmdb", ".pdf", ".odx", ".cbf", ".pdx",
               ".smr-d", ".jsonl", ".parquet", ".npy", ".npz", ".safetensors", ".gguf", ".rkllm", ".rknn", ".onnx",
               ".pt", ".pth", ".ckpt"}
BLOCKED_DIRS = ("kb/lora/", "kb/images/", "kb/community/", "kb/telemetry/", "test_batch_out/", "raw/", "logs/",
                "cache/", "records/", "drives/", "models/")
MAX_BYTES = 10 * 1024 * 1024


def main():
    files = subprocess.run(["git", "ls-files", "-z"], capture_output=True, check=True).stdout.decode().split("\0")
    bad = []
    for f in filter(None, files):
        ext = os.path.splitext(f)[1].lower()
        if ext in BLOCKED_EXT:
            bad.append(f"{f}: {ext} files hold data, not code")
        elif f.startswith(BLOCKED_DIRS):
            bad.append(f"{f}: {f.split('/')[0]}/ is a data folder")
        elif os.path.isfile(f) and os.path.getsize(f) > MAX_BYTES:
            bad.append(f"{f}: {os.path.getsize(f) / 2**20:.0f} MB, over the {MAX_BYTES // 2**20} MB limit")
    for b in bad:
        print(b)
    print(f"{len(files) - 1} tracked files checked, {len(bad)} problem(s)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
