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

### `shopdeck/shopdeck/billing.py`, `shopdeck/shopdeck/mail.py`

- Estimates, invoices and receipts are built from the ticket's jobs and `repair.quote`, so the paperwork can't disagree with the quote. They are HTML pages like the work order, printed from the browser.
- Email goes out over SMTP with the standard library (no new dependency). A send that fails for lack of a connection is kept in `~/.shopdeck/outbox/` as an .eml file, because the deck is often offline at the car. A refused login is raised, not queued, so it gets fixed instead of piling up.
- The app password lives in `~/.shopdeck/mail_password` (mode 600) or `SHOPDECK_MAIL_PASSWORD`, never in `config.json` or a ticket, so a copied ticket or config file carries no credential.
- `quote(tax_labor=True)` taxes labor, supplies and fees as well as parts. Washington taxes repair labor; the old parts-only tax stays the default for other places.
