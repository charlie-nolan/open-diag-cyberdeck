# ADR-0006: Search and retrieval (kb/kb_retriever.py)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `kb/kb_retriever.py`

- Where/what kind of fastener, not which part: kept out of the strict spec match (they stay when they are part of the manual's own name from SYNONYMS, as in "front exhaust pipe" or "wheel nut").
- Wiring faults and control units: a torque value for these is never what a card needs.
- The knowledge table keeps NHTSA's model names; these are the same cars as the manuals' model names.
- The baked cards say "confirmed fix", "most common confirmed fix", "#1 cause": the bake's words for owner reports. Models read that as proof and led with it over the manual (sim v3). Same meaning, neutral words: how often owners reported a fix. Wording only, no cause is named or changed.
- `Retriever.card`: An FSM hit ranks as if it were this many places higher than a CHARM hit.
- `Retriever`: What kind of page a question wants. Problem descriptions want the manual's symptom trouble tables and diagnostic procedures; repair jobs want removal installation pages. This says nothing about causes.
- `Retriever._matrix_locked`: Only this car's rows: reading every row and filtering in Python took 6.6 s for a 2003 Tahoe on chevrolet.db (1.2M rows) on the PC, this 0.4 s; the deck is several times slower at both.
- `Retriever._vec_search`: No car picked on a big make: minutes on the main thread, and the answer wouldn't be about this car.
- `Retriever._vec_search`: A block at a time: the whole matrix as int32 is 4 GB+ on a big make, too much beside the deck's model.
- `Retriever.search_ids`: CHARM repeats one page per engine/emission variant; keep the best of near-identical pages so the few slots the models get aren't five copies of the same text.
- `Retriever.search_ids`: A trouble-code chart read from the manual's pictures (kb/car_db.py vision) leads only for a case that has that code (then it is in `first`); otherwise it goes after the other pages.
- `Retriever.search_fsm`: Pages sharing more than this share of word triples count as one.
- `Retriever.search_fsm`: Long plain-English questions match keywords badly, so the vector list counts more.
- `Retriever.specs`: Strictest first: every part word of the job (or of its manual wording) must appear, so 'exhaust manifold gasket' can't pull in front ABS sensors just because both say 'front'. Then it drops one or two words at a time, keeping the last ones (the part noun comes last: 'fuel injector', 'intake manifold'). It never falls back to any-word matching: no spec beats a spec for a different part.
- `Retriever.specs`: Rows from the part's own procedure ('13. Crankshaft Pulley') before rows that only mention it. 'front'/'rear' are too common to require, but they decide between the part's own procedures.
- `Retriever.knowledge_ids`: The maker's own bulletins and recalls before one owner's complaint about the same thing.
- `Retriever.knowledge`: NHTSA files one complaint once per component it names.
