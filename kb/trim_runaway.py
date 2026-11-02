# Copy a marker .md, dropping runaway table rows: any table row over 20 KB or with more than 50 cells,
# and runs of >=200 rows whose cells are all tiny fragments.
import os, sys
src, dst = sys.argv[1], sys.argv[2]
wide = lambda ln: ln.startswith("|") and (len(ln) > 20000 or ln.count("|") > 51)
tiny = lambda ln: ln.startswith("|") and all(len(c.strip()) <= 8 for c in ln.strip().strip("|").split("|"))
run, wide_n, tiny_n, heads, kept = [], 0, 0, 0, 0
with open(src, encoding="utf-8", errors="replace") as f, open(dst, "w", encoding="utf-8") as o:
    def flush():
        global run, tiny_n
        if len(run) >= 200: tiny_n += len(run)
        else: o.writelines(run)
        run = []
    for ln in f:
        if wide(ln):
            wide_n += 1
            continue
        if tiny(ln):
            if len(run) < 200: run.append(ln)
            else: tiny_n += 1
            continue
        flush()
        o.write(ln)
        heads += ln.startswith("#")
    flush()
print(f"{os.path.basename(src)}: {os.path.getsize(src)/1e6:.0f} MB -> {os.path.getsize(dst)/1e6:.1f} MB, "
      f"{wide_n} wide rows + {tiny_n} fragment rows dropped, {heads} headings kept")
