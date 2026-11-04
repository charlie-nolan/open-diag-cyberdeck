# Chevrolet (2003 Tahoe beta) pipeline

Same steps as the Subaru build, driven by a profile. Nothing about the Tahoe is in the code: it all lives in
`profiles/chevrolet_tahoe_gmt800.json` (make, years 2000-2006 = the GMT800 Tahoe, CHARM/NHTSA names, subreddits,
forums, regexes). For another car or make, copy the profile.

PC copy: `~/open-diag-cyberdeck/chevrolet/`. Output:

| What | Where | Source |
|---|---|---|
| Knowledge DB (the app loads it by VIN/make) | `kb/chevrolet.db` | CHARM Tahoe pages, specs, labor, DTC pages + NHTSA |
| NHTSA TSBs / complaints / recalls | `chevrolet_diag.db` | the NHTSA zips already in `raw/nhtsa/` (no download) |
| VIN make table + decoded test VINs | `kb/vin.db` (`wmi`, `vin_decoded`) | NHTSA vPIC API |
| Reddit | `raw/reddit/reddit_chevrolet.db` | Pushshift dumps (same torrent as Subaru) + Arctic Shift 2025+ |
| Forums | `raw/forums/<name>.db` | Wayback Machine copies; skipped if the site's robots.txt blocks AI |
| GM Mode $22 PIDs | `kb/gm_mode22_pids.json` | gmtnation community sheet (you download it) |

## Run

```
setsid nohup ~/open-diag-cyberdeck/chevrolet/run_all.sh > ~/open-diag-cyberdeck/logs/chevrolet_pipeline.log 2>&1 &
```
Add the Tahoe's VIN after `run_all.sh` to decode it too. The log ends with `ALL DONE`, then `status` prints one
summary. Every step can be re-run alone: `python3 make_pipeline.py chevrolet_tahoe_gmt800.json <step>`.

Later, after the Subaru bake: `make_pipeline.py chevrolet_tahoe_gmt800.json embed` (GPU), then a Tahoe card bake.

## Live data (`gm_live.py`)

Read-only by construction: only services $01 $02 $03 $07 $09 $22 and a short list of adapter commands can be sent
(clear codes, writes and actuator tests raise before anything is sent). 2003 GM trucks talk J1850 VPW, which the EX
supports.

```
python3 gm_live.py scan                         # protocol, supported PIDs, VIN, stored + pending codes
python3 gm_live.py live --pids 0C,05,0F,11 --seconds 60 --csv run.csv
```
GM-specific (Mode $22) PIDs come from PCMHammer's PcmLogger parameter file (GPL-3, cloned to `raw/pcmhammer`),
written for the P01/P59-era GM PCMs a 2003 Tahoe uses: 194 readings. Build the PID file with
`python3 make_pipeline.py chevrolet_tahoe_gmt800.json gm-pids`. The gmtnation community sheet can be added as a second
source (`gm-pids ~/Downloads/GMT_PIDS_Rev_5.xlsx`); readings where the sources disagree are marked. Everything stays
"unverified" until checked on the truck. Mode 22 readings can be asked for by PCMHammer id (`EngineOilPressure`) or PID.
PCMHammer's device-control ($AE) and unlock ($27) commands are not used. `kb` also takes DTC names from
PCMHammer's code list for codes CHARM doesn't name.

## Not done yet / known gaps
- No factory service manual for the Tahoe (only CHARM). A Lemon zip can be added later like Subaru's plan.
- `kb/build_subaru_kb.py` only recognises Subaru engine names in CHARM model strings, so Tahoe 4.8L and 5.3L pages
  share one vehicle row until `build_subaru_kb_engine_names.patch` is applied (owned by "Databases and baked cards").
  Re-run `kb` after it lands; `kb` always builds a fresh DB and keeps the previous one as `chevrolet.pre_<date>.db`.
- Card baking: `extract_fixes.py` has a "on a Subaru" relevance prompt; it needs a make option before a Tahoe bake.
- Tested offline only (fake CHARM, fake NHTSA zips, fake dumps, fake forum pages, simulated VPW truck).
