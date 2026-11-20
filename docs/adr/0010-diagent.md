# ADR-0010: diagent guided diagnosis (diagent/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `diagent/config.py`

- ECU id -> VIN bindings: the main app's file is read, diagent writes only its own (the last one)

### `diagent/gen.py`

- `step_cases`: The same scope identify() gives the run (FSM engine-code rows join every engine)

### `diagent/harness.py`

- `execute`: A trigger's value has to be one a tool result of this case gave.

### `diagent/identify.py`

- `identify`: What was typed wins; a disagreement is asked, not guessed.

### `diagent/kb.py`

- Words in a DTC title that don't say which component (kept out of the relevance check)
- Vocabulary only (same rule as kb_retriever.SYNONYMS): other names the manual uses for the same part.
- `engine_features`: A trim name that doesn't say turbo doesn't say "not turbo" either.
- `procedure`: Under its own DTC heading a chart is this code's chart; the word check is only for tables with no heading.
- Words that start a manual instruction (plain English imperative verbs; nothing about any car)
- Block headings on a page; background blocks are skipped when the page also has test blocks.

### `diagent/testing.py`

- `_check`: Accuracy checks (review 2026-10-25): the chart fits the engine, the question was shown, live values were read when the car could give them, DTC steps were answered only from a scan, another path was tried.
- `_check`: Every run: a finished case cites only ids from this run; accepted texts pass the claim check again.

### `diagent/tools.py`

- Manual steps that need a write to the car: done only from the main app's TESTS screen, with its own guards.
- Notes in a step that are about operating the dealer scan tool, not about the car: dropped from the card.
- `Tools.record_step`: A number Python can judge overrides the model's answer.
- `Tools.read_car`: Scans replace codes, never append.
