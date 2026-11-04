# subaru.db

One read-only knowledge database for the cyberdeck. Right now it covers the 2002 Forester (SF, EJ251). More cars can be added later without changing the schema.

| What | Rows | Source |
|---|---|---|
| FSM sections | 2,062 | The 2002 Forester FSM markdown, split at real procedures (`A: REMOVAL`, `B: INSTALLATION`, `AI: DTC P0304 ...`). NOTE/CAUTION blocks stay inside their procedure. Procedures longer than ~6k characters are split into parts. |
| Torque specs | 430 | Pulled from the FSM text, with N·m, ft-lb, tolerance, page and the procedure each came from. Hand-typed or Gemini specs are not included. |
| DTCs | 568 codes, 155 FSM procedure sections | From `subaru_diag.db`: FSM wording first, RomRaider otherwise. `dtc_ssm` holds 589 ECU status addresses/bits for reading codes over SSM |
| Cross references | 1,968 (1,909 resolved) | `<Ref. to FU-52, ...>` links between sections |
| TSBs, NHTSA complaints, recalls | 17,778 + 13,535 + 759 | From `subaru_diag.db` (all Subarus 1990-2012). `knowledge()` returns only your model and year unless you pass `all_models=True`. |
| SSM parameters | 53,900 | RomRaider v370 logger definitions, via `subaru_diag.db` |

Size: 43.9 MB, of which the FSM part is 7.1 MB (compared with 16.7 MB for `test_subaru_knowledge.db`). Lookups take 0.5 to 7 ms.

## Rebuild

```
cd ~/open-diag-cyberdeck/kb
python3 build_subaru_kb.py fsm "../test_batch_out/USDM Forester FSM 2002 G8050BE_V1 (SF)/USDM Forester FSM 2002 G8050BE_V1 (SF).md" \
    --model Forester --chassis SF --years 2002 --engines EJ251 --title "USDM Forester FSM 2002 G8050BE"
python3 build_subaru_kb.py diag ../subaru_diag.db
~/open-diag-cyberdeck/venv/bin/python build_subaru_kb.py charm --charm ~/mnt/charm   # Operation CHARM, needs the image mounted
python3 build_subaru_kb.py cards ~/open-diag-cyberdeck/kb/cards.db    # pre-baked DTC cards (card_baking/), if built
python3 build_subaru_kb.py embed        # optional, desktop GPU only (see below)
python3 build_subaru_kb.py finish
```

To add another car, run `fsm` again with its markdown and a different `--title`. Each step can be rerun safely: rerunning `fsm` replaces that manual's rows.

## Operation CHARM

`charm` reads the mounted CHARM image through `cyberdeck_forge/charm_ingest.py` and loads every Subaru and Subaru Truck vehicle from 1990 to 2012 into the same `section` table, so `search_fsm()` finds those pages too. Results are tagged `[CHARM: ...]` instead of `[FSM: ...]`, and FSM results come first for DTCs and specs.

- Pages are deduped by a hash of their text. CHARM content ids aren't safe to dedupe on, because the same id can point at different HTML. Each page is linked to every vehicle it appears under through `section_vehicle`.
- Folder listing pages (`expand-all`) are skipped. Image-only pages are counted but not stored, because the CHARM image database isn't on disk.
- Torque rows from CHARM tables go into `spec` as printed, with nothing converted. Labor times and part numbers go into `labor`, which you query with `Retriever.labor("exhaust manifold")`.
- `charm_source` keeps the CHARM key that each stored page first came from.
- Use `--only "2002 Forester"` for a test run. Every run replaces all CHARM rows.

CHARM vehicles are stored with CHARM's engine names (`F4-2.5L SOHC`) and no chassis code. Because of that, filter by model and year, not by engine.

## Embeddings (optional)

`embed` writes int8 vectors from Qwen3-Embedding-0.6B (1024 dims) into `section_vec` and `knowledge_vec`. It needs `sentence-transformers` (>= 2.7) and `sqlite-vec`, so run it on the desktop with the 3070. On the Pi, questions must be embedded with the same model using `prompt_name="query"`.

The Pi doesn't need it. FTS5 finds the right FSM procedure for part names, DTCs and job names on its own. Vectors only help with vague symptom wording, and they would require the Pi to run an embedding model as well.

## Using it from the app

`kb_retriever.py` is a drop-in for `app/retrieval.py`'s `Retriever`. It has the same `dtc()`, `search_fsm()`, `specs()` and `context_for()` methods and the same string formats:

```python
from kb_retriever import Retriever
retriever = Retriever("~/open-diag-cyberdeck/kb/subaru.db", model="Forester", year=2002)
```

It also adds `card(code)` (the pre-baked diagnostic card for this car, which `context_for()` puts first), `section(id)`, `refs(id)` (follow the FSM's cross references) and `knowledge(text)` (TSBs and complaints once they're loaded). It knows a few shop-word synonyms. For example, "exhaust manifold" also searches "front exhaust pipe", because that's what the SF manual calls it.
