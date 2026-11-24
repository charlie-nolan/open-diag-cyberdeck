# ADR-0012: shopdeck (shopdeck/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `shopdeck/shopdeck/chart.py`

- Older manuals (1999-2001) print each step as '10AA2 : CHECK ...' followed by the question and two answer lines whose YES NO marks are pictures, so the converted text keeps the answers but not their labels.

### `shopdeck/shopdeck/kb.py`

- `KB.engine_chapters`: The same chart in two chapters that both fit (e.g. 'EN(H4SO)' and 'EN(H4SO U5)'): one set of steps only.

### `shopdeck/shopdeck/repair.py`

- `procedure_candidates`: The manual may call it something else ("front exhaust pipe" for a manifold): any word.
