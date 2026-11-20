# ADR-0011: Parts sourcing and junkyard quotes (parts_sourcing/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `parts_sourcing/parts_sourcing/interchange.py`

- `Engine.known`: CHARM names turbo engines explicitly, so no 'Turbo' on a named engine means naturally aspirated.
- `lookup`: Hand-checked extra fits keyed by part number or family (e.g. a part also used on another model).
- `parts_for_job.tail`: Every job word must be in the last 3 path parts (the part itself, not 'Engine, Cooling and Exhaust' above it), so 'exhaust manifold gasket' doesn't also return the intake manifold gasket.

### `parts_sourcing/parts_sourcing/junk_quote.py`

- Pick-n-Pull's abbreviations, spelled out so they meet CHARM's names.
- Words CHARM puts in a crumb to say which car or side, not what the part is: "(Manual Trans)", "Right", "(With ABS)"
- Price-list words that name a different system: an item carrying one the CHARM path doesn't mention is the wrong part.
- `is_qualifier`: Labor rows that aren't the replace job itself: tests, and "add" times stacked on another job.
- `match_price`: The item must name the whole part ("Radiator Cap" isn't "RADIATOR"), and not another system ("HEATER MOTOR AND FAN" isn't a radiator fan motor)

### `parts_sourcing/parts_sourcing/yard_sync.py`

- `main`: Row52's Terms (row52.com/About/Terms) forbid robots/scrapers. Only run this with Row52's written permission, then set "row52_crawl_allowed": true in sourcing.json.

### `parts_sourcing/parts_sourcing/yards.py`

- `connect`: A car only known from an alert or a partial paste stops counting after this (yards crush)
- `report`: Still there: on the yard's latest full list, or added since then and not older than MAX_AGE_DAYS.
- `report`: VPIC's series/trim catches Outbacks a yard lists as Legacy.

### `parts_sourcing/tests/test_parts_sourcing.py`

- Fixture cars were added Sep 30 - Oct 10, 2026; a fixed date keeps the 45-day cutoff stable.
