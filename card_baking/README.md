# Card baking

Pre-written diagnostic cards, one per trouble code, built on the desktop so the Pi only has to look them up.
A card lists the likely causes in order of how often owners confirmed them as the fix, each with its sources,
plus the manual's test outline, specs and book labor copied straight from the database.

Prepared 2026-10-16. Tested only against a fake llama-server and small fake DBs built with the real
reddit_subaru.db and ultimatesubaru.db schemas. Not run on real data yet.

## Files

| File | What it does |
|---|---|
| `extract_fixes.py` | Job 1. Reads forum threads, keeps likely repair threads with a keyword filter, and asks a small model for car, codes, fix and whether it was confirmed. Every extract has to quote the sentence it came from; if the quote isn't in the thread, it doesn't count as confirmed. |
| `bake_cards.py` | Job 2. For each code: FSM procedure (plus the sections it says to refer to), confirmed forum fixes counted per cause, TSBs and recalls. The card model writes the card with a source id on every claim, then a second pass drops any claim its sources don't support. |
| `llm.py` | Client for llama-server. Forces JSON that matches a schema, thinking off. |
| `serve_extract.sh` | llama-server for job 1: Qwen3-8B Q4 fully on the 3070, 4 slots, port 8080. |
| `serve_cards.sh` | llama-server for job 2: Qwen 3.6-35B-A3B Q4, experts in RAM (`--n-cpu-moe`), port 8081. |
| `run_bake.sh` | One command for both jobs (`sample`, `extract`, `cards`, `all`). |
| `free_resources.sh` | Clears GPU, CPU and RAM before each job; keeps the desktop and other cyberdeck work running. |
| `cards_schema.sql` | Reference for the `dtc_card` table. The DB builder now has a `cards` step that loads it into subaru.db (as a rowid table with FTS and vectors), and `Retriever.card(code)` / `context_for()` read it. |

All Python is standard library only. Copy this folder to `~/open-diag-cyberdeck/card_baking/` and run from there.
Work files default to `~/open-diag-cyberdeck/kb/` (`bake_work.db`, `cards.db`).

## Where it fits in building the final database

Cards are made *from* the database, so they come after everything else is loaded, and before the last two builder steps:

1. Finish loading content (DB builder): CHARM labor fix, converted FSM manuals.
2. **Job 1**: `extract_fixes.py filter / run / export` writes confirmed fixes into `subaru_diag.db` `forum_fix`.
3. Builder `diag` step: pulls those fixes into `subaru.db` `knowledge`, so the deck can search them too.
4. **Job 2**: `bake_cards.py` writes `cards.db`.
5. Builder: `python3 build_subaru_kb.py cards ~/open-diag-cyberdeck/kb/cards.db` (replaces all cards each run).
6. Builder `embed` (also embeds the cards), then `finish` (indexes and compacts the file).

Run `filter` after the Ultimate Subaru Wayback fetch has finished: a thread already extracted isn't re-read if more of its pages arrive later.

GPU order on the desktop: marker conversions, then job 1, then job 2, then embeddings. Never two at once.

## Running it

The easy way is `run_bake.sh`. Before each job it waits for any manual conversion or embedding run to finish,
clears the machine with `free_resources.sh`, starts that job's llama-server, runs the job, and stops the server:

```bash
cd ~/open-diag-cyberdeck/card_baking
./run_bake.sh sample                                   # 100 threads, prints the projected total
setsid nohup ./run_bake.sh all > ~/open-diag-cyberdeck/logs/bake.log 2>&1 &   # everything, detached
```

`free_resources.sh` stops anything using the GPU, other model servers (old llama-server, ollama, LM Studio),
marker/embedding/torch jobs, and any other program using over 1.5 GB of RAM or 50% CPU (a browser or game, for
example). It never stops the desktop, Claude / Remote Control, the Ultimate Subaru Wayback pull, the RKLLM
export, or the database builder while it writes subaru.db (the bake waits for that instead). Run
`./free_resources.sh --dry-run` to see what it would stop. `KEEP="name"` protects something else, and
`APPS="firefox discord"` closes named apps even if they're small.

Step by step, by hand:

```bash
cd ~/open-diag-cyberdeck/card_baking

# Job 1
python3 extract_fixes.py inspect --reddit ~/open-diag-cyberdeck/raw/reddit/reddit_subaru.db   # check the column mapping
python3 extract_fixes.py filter  --reddit ~/open-diag-cyberdeck/raw/reddit/reddit_subaru.db \
                                 --forum  ~/open-diag-cyberdeck/raw/forums/ultimatesubaru.db
./serve_extract.sh &                                   # wait for "server is listening"
python3 extract_fixes.py run --limit 100               # timing sample: prints threads/min and the projected total
python3 extract_fixes.py stats --code P0303            # look at what it pulled out before the long run
python3 extract_fixes.py run --parallel 4              # the rest; safe to stop and restart
python3 extract_fixes.py export --diag-db ~/open-diag-cyberdeck/subaru_diag.db
kill %1

# Job 2
./serve_cards.sh &
python3 bake_cards.py --model Forester --years 1998-2002 --limit 3   # 3 cards to read first
python3 bake_cards.py show P0303 --model Forester
python3 bake_cards.py --model Forester --years 1998-2002             # every code for the SF Forester
```

Every step resumes where it stopped. `bake_cards.py --force` rebuilds cards; `--codes P0303,P0304` picks codes.

## Knobs worth knowing

- `filter --min-score` (default 5): lower keeps more threads and costs more time; `--all` keeps everything (~4x slower).
- `bake_cards.py --years`: the model years a card covers. SF Forester is 1998-2002. Forum fixes from one year either side count too (`--forum-year-slack`).
- `--min-forum 3`: codes with no manual procedure still get a card once owners of this car confirmed 3+ fixes.
- `serve_cards.sh CPU_MOE=…`: lower it until the 3070 is nearly full for more speed.
- `--no-verify` skips the source check. Don't, unless timing a run.

## Safety

Cards never contain model-written torque values, capacities or part numbers; those lines are copied from the
manual and CHARM tables as printed. Claims the check pass removes are kept in `dropped_json` for review.
