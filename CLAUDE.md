# Working in this repo

Cyberdeck Home (`app/`, `cyberdeck_gui.py`) is the main app. Team mode (`teamdeck/`, `team/`) is an optional mode
of the same app. shopdeck (`shopdeck/`) is the no-LLM chart walker and stays. Everything else builds the databases the app reads. The laptop guide
(github.com/charlie-nolan/open-diag-laptop) pins a commit of this repo (`CORE_VERSION`) and runs its entry points,
config and CLI paths, so a move or rename of those needs a note in the PR and a re-pin there.

## Anti-bloat rules

- Never build a parallel app, or a second `kb.py`, `case.py` or GUI next to `app/`. A new mode goes behind a
  backend or config switch in the existing app; anything bigger is prototyped on a throwaway branch.
- Keep a module under about 600-800 lines. When one grows past that, split it into focused modules in the same
  PR. The Home app's screens live in `app/ui/screens/` (one mixin per screen) and its shared code in `app/ui/`.
  `cyberdeck_gui.py` keeps only the window core and re-exports the old names, because teamdeck and other callers
  use `cyberdeck_gui.<name>` (teamdeck swaps `cyberdeck_gui.ModelManager`, so build it there).
- When a script is superseded, delete it in the same PR. Don't leave old scripts at the repo root; git history
  keeps them.

## Other rules

- No code comments: `tools/comment_ratchet.py` fails CI if a file's count goes up. Decisions go in `docs/adr/`.
- Code only: no manuals, CHARM content, built databases, scraped text or model weights
  (`tools/check_repo_files.py`).
- Nothing hard-coded for one car. Fixes target the general bug and are tested on mixed makes and codes.
- `python -m pytest` must pass with no data files (tests that need a built DB skip and say why).
