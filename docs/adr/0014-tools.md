# ADR-0014: Build and deck tools (tools/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `tools/bench_run.py`

- `main`: Wait4 gives the direct child; grandchildren that were waited on show in RUSAGE_CHILDREN of this process.

### `tools/build_charm_all.sh`

- One copy at a time: two loops writing the same DBs gave 'database is locked' and GPU out-of-memory (Oct 23)
- Success is the Python exit code (PIPESTATUS[0]); the grep/tail only trims what goes to the log.

### `tools/charm_pic_server.py`

- `worker`: The busy check also sees our own reader model, which Ollama keeps loaded after each read (keep_alive): unload it and check again, so the queue isn't stuck waiting on itself.

### `tools/deck_pull.sh`

- 'make_full.sh --all' itself doesn't count: it works on one make at a time, and that make's own job (build_subaru_kb.py --db kb/<make>.db, extract_fixes, ...) names the DB it is writing.
- The PC keeps it in its Hugging Face cache; the deck app looks in models/qwen3emb first. -L copies the real files behind the cache's links. Unchanged files aren't sent again.
- Subaru.threads.db = every owner thread, searchable (unchecked, ideas only); subaru_diag.db = NHTSA + RomRaider.

### `tools/gpu_guard.sh`

- Ollama runs its models through its own llama-server (/usr/lib/ollama/...): that one doesn't count, gpu_lock unloads Ollama's models anyway. Only our own llama-server (card bake forum fixes) does.

### `tools/make_full.sh`

- B2. car names: CHARM cars renamed by the make's NHTSA model list ('K Tahoe 4WD' was stored as 'K'). CPU, keeps pages and vectors. Needs the CHARM text mount (~/mnt/charm), like step a.

### `tools/push_home_models_to_deck.sh`

- No autostart: keep deck_setup.sh's entry as .off (rename back to turn it on again)

### `tools/reddit_all_makes.sh`

- One copy at a time; make_full.sh checks this lock to know the loop is running.

### `tools/vpic_setup_deck.sh`

- 17+-only settings and ownership lines would only print errors on 16: leave them out.
