# ADR-0008: Card baking and forum-fix extraction (card_baking/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `card_baking/bake_cards.py`

- 'check' with a minimum length: a field called 'step' got filled with the manual's step codes ("10AA1")
- `forum_counts`: A thread can list several codes; a fix that names other codes but not this one belongs to those.
- `verify_card.supported`: 200 could cut the reply off mid-JSON.
- `verify_card.supported`: 'N confirmed fixes' statements come from this table, not from the one thread cited.
- OEM part-number shapes, every make the cards are baked for (formats only, no part is named):.
- `codes_for`: Codes owners of this car post about most come first (so --limit tries the useful ones)
- `step_bake`: One bad reply, server hiccup or odd DB row skips this card (retried next run), never the bake.
- `step_bake`: Verbatim facts from the DB, for the top causes (never model-written)

### `card_baking/extract_fixes.py`

- `map_source`: Regex tags the scrapers already added (reddit_subaru.db, ultimatesubaru.db): fallbacks only.
- `step_filter`: The scraper's dtcs tag can be the string "[]": count it only if it holds a real code.
- `evidence_ok`: "fixed" or "it worked" is in almost every thread: too short to prove anything.
- `step_run`: Can't point at the sentence: don't count it as confirmed.
- `step_run`: The server died: stop instead of skipping everything.
- The model picks 'category' before it writes 'part' and 'fix' (JSON key order), so its bucket is often a guess (cam sensors and O2 sensors filed under 'differential/viscous coupling'). recat re-derives the bucket from the part and fix the owner actually named. Word lists only translate part names into bucket names; first match wins, so the more specific names come first.

### `card_baking/llm.py`

- `LLM.json`: Ollama: its /v1 keeps a 4k default context and thinks first; the native API sets both.

### `card_baking/rebake.sh`

- Card rows are replaced on each load but keep the same rowids, so --missing would keep the old cards' vectors.

### `card_baking/run_bake.sh`

- The database builder writing subaru.db is never stopped; the bake reads that file, so always wait.

### `card_baking/serve_cards.sh`

- While nvidia-smi shows spare VRAM: every layer moved to the GPU is faster.
