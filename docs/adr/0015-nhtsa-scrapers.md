# ADR-0015: NHTSA and Reddit loaders (repo root scripts, cyberdeck_forge/)

Status: Accepted (recorded 2026-11-20)

## Context

These decisions were written as code comments next to the code they explain. Comments are banned
(ADR-0001), so they are kept here. Each entry names the file and the function or class it applies to;
search the file for that name. When the code changes, update or remove the entry in the same change.

## Decisions

### `build_subaru_diag_db.py`

- NHTSA flat files on static.nhtsa.gov (checked 2026-10). TSBs and recalls are now split into several zips; every file listed for a kind is fetched. If a URL 404s, get the current links from https://www.nhtsa.gov/nhtsa-datasets-and-apis and drop the zips into raw/nhtsa/<kind>/ (every .zip there is read).
- Fields are read by position. Before a file is used, its first rows must show a model year, a make and a date where the code reads them; a reordered layout stops that file instead of filling the wrong columns.
- `step_nhtsa`: Pre-2010 records often leave DESC_DEFECT empty and put the text in NOTES (22)

### `cyberdeck_forge/charm_ingest.py`

- Leaf-section vocabulary, taken verbatim from isProperty() in CHARM's server.js. These are the only section names CHARM uses, so they are what we route on -- no guessing at folder names.
- `open_charm`: Subdir=True: data.mdb sits directly inside the mount, which is what python-lmdb's default expects. No map_size needed for readonly.

### `scrape_reddit_subaru.py`

- Most relevant first. r/Impreza is empty in the archive; r/subaruimpreza is the real one. value = model implied by the subreddit when the text names none.
- "Ascent"/"XV"/"STI" as plain words are noisy; require the capitalised spelling for those.
- `crawl_posts`: A full page sharing one timestamp would loop forever; step past it.
