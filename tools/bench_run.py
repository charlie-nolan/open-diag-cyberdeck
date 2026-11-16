#!/usr/bin/env python3
"""
Run one command and append a benchmark row: wall s, user+sys CPU s, max RSS MB of the command and its children,
input/output sizes, and (with --gpu) GPU utilisation/VRAM sampled every --every seconds while it runs.
Stand-in for /usr/bin/time -v (not installed everywhere). The command's own exit code is returned.

  tools/bench_run.py --csv bench.csv --step fsm --in FILE_OR_DIR --out DB [--gpu] -- CMD ARGS...

CSV columns: step, started, exit, wall_s, cpu_s, user_s, sys_s, max_rss_mb, in_mb, out_mb_before, out_mb_after,
gpu_util_avg, gpu_util_max, vram_max_mb, gpu_samples, note
"""
import argparse
import csv
import os
import resource
import subprocess
import sys
import threading
import time

COLS = ["step", "started", "exit", "wall_s", "cpu_s", "user_s", "sys_s", "max_rss_mb", "in_mb", "out_mb_before",
        "out_mb_after", "gpu_util_avg", "gpu_util_max", "vram_max_mb", "gpu_samples", "note"]


def size_mb(paths):
    tot = 0
    for p in paths:
        if os.path.isfile(p):
            tot += os.path.getsize(p)
        elif os.path.isdir(p):
            for root, _, files in os.walk(p):
                for f in files:
                    fp = os.path.join(root, f)
                    if os.path.isfile(fp) and not os.path.islink(fp):
                        tot += os.path.getsize(fp)
    return round(tot / 1e6, 2)


def gpu_sampler(stop, out, every, gpu_csv):
    while not stop.is_set():
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                               capture_output=True, text=True, timeout=10)
            u, m = (float(x) for x in r.stdout.strip().splitlines()[0].split(","))
            out.append((u, m))
            if gpu_csv:
                with open(gpu_csv, "a") as f:
                    f.write(f"{time.strftime('%F %T')},{u:.0f},{m:.0f}\n")
        except Exception:
            pass
        stop.wait(every)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--step", required=True)
    ap.add_argument("--in", dest="inp", action="append", default=[], help="input file/folder (repeat)")
    ap.add_argument("--out", action="append", default=[], help="output file/folder (repeat)")
    ap.add_argument("--gpu", action="store_true", help="sample nvidia-smi while the command runs")
    ap.add_argument("--every", type=float, default=5)
    ap.add_argument("--gpu-csv", help="also write every GPU sample (time, util pct, VRAM MB) here")
    ap.add_argument("--note", default="")
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    a = ap.parse_args()
    cmd = a.cmd[1:] if a.cmd and a.cmd[0] == "--" else a.cmd
    if not cmd:
        sys.exit("no command")
    before = size_mb(a.out)
    samples, stop = [], threading.Event()
    th = None
    if a.gpu:
        th = threading.Thread(target=gpu_sampler, args=(stop, samples, a.every, a.gpu_csv), daemon=True)
        th.start()
    started = time.strftime("%F %T")
    t0 = time.time()
    p = subprocess.Popen(cmd)
    _, status, ru = os.wait4(p.pid, 0)
    wall = time.time() - t0
    rc = os.waitstatus_to_exitcode(status)
    if th:
        stop.set()
        th.join(timeout=15)
    # wait4 gives the direct child; grandchildren that were waited on show in RUSAGE_CHILDREN of this process
    rc_all = resource.getrusage(resource.RUSAGE_CHILDREN)
    user, sys_ = max(ru.ru_utime, rc_all.ru_utime), max(ru.ru_stime, rc_all.ru_stime)
    rss = max(ru.ru_maxrss, rc_all.ru_maxrss) / 1024          # KiB on Linux
    utils = [u for u, _ in samples]
    row = {"step": a.step, "started": started, "exit": rc, "wall_s": round(wall, 1), "cpu_s": round(user + sys_, 1),
           "user_s": round(user, 1), "sys_s": round(sys_, 1), "max_rss_mb": round(rss), "in_mb": size_mb(a.inp),
           "out_mb_before": before, "out_mb_after": size_mb(a.out),
           "gpu_util_avg": round(sum(utils) / len(utils), 1) if utils else "",
           "gpu_util_max": max(utils) if utils else "", "vram_max_mb": max(m for _, m in samples) if samples else "",
           "gpu_samples": len(samples), "note": a.note}
    new = not os.path.isfile(a.csv)
    os.makedirs(os.path.dirname(os.path.abspath(a.csv)), exist_ok=True)
    with open(a.csv, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        if new:
            w.writeheader()
        w.writerow(row)
    print(f"bench {a.step}: {row['wall_s']} s wall, {row['cpu_s']} s CPU, {row['max_rss_mb']} MB RSS"
          + (f", GPU avg {row['gpu_util_avg']}%" if utils else ""), file=sys.stderr)
    sys.exit(rc)


if __name__ == "__main__":
    main()
