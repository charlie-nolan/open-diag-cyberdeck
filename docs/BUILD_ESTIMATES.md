# Single-car build: rough time and size estimates

These numbers come from building one car (a 2002 compact SUV) twice on one desktop PC: once from a factory
service manual PDF plus a LEMON Manuals download, and once from the LEMON download alone. Each step ran once, and
some ran while another job shared the machine.

**They are rough.** Use them for the order of magnitude, not as a promise. Your manual, your car's popularity on
forums and your hardware will move every number.

## Hardware used

| | |
|---|---|
| CPU | AMD Ryzen 9 5900X, 12 cores / 24 threads |
| RAM | 62 GB |
| GPU | NVIDIA RTX 3070, 8 GB |
| Disk | NVMe SSD |
| Embedding model | Qwen3-Embedding-0.6B (fp16 on the GPU, fp32 on the CPU) |
| Community extraction model | Qwen3.5-9B Q4_K_M on llama.cpp `llama-server`, 4 parallel slots, 6k context per slot |

## Per-unit rates

| Work | Measured | Rate |
|---|---|---|
| Factory PDF -> markdown (`kb/pdf_to_md.py`, text layer, one CPU core, no OCR) | 2,073 pages, 37.8 MB: 285 s (quiet PC) to 454 s (busy PC) | ~0.14-0.22 s per page, ~8-12 s per PDF MB |
| LEMON zip -> pages + pictures (`kb/car_db.py html-manual`, CPU) | 7,701 HTML files, 110 MB unzipped -> 3,117 pages + 4,487 pictures: 29 s | ~4 ms per file |
| Converted manual -> sections (builder `fsm`) | 2.1 MB -> 1,632 sections: 0.6 s | negligible |
| Embedding, GPU (fp16) | 2,000 sections: 17 s, 22 s with model load | ~90-117 rows/s, ~9-11 s per 1,000 sections |
| Embedding, CPU (fp32, 24 threads, PC busy) | same 2,000 sections: ~810 s, 7.8 GB RAM | ~2-2.5 rows/s, ~400 s per 1,000 sections |
| Community pass 0 (prefilter, CPU) | 103,422 raw threads (421 MB): 103 s | ~1 ms per thread |
| Community extraction (9B on GPU, 4 slots) | 1,912 threads, 2.89M tokens in, 2.02M out: 3.9 h | ~7.1 s per thread (~500 threads/h); 145 output tok/s aggregate; ~1,500 in / ~1,050 out tokens per thread |
| Everything else (NHTSA, diag, cards, telemetry, trim, finish) | | ~10 s total |

Whole build once the inputs are ready (manual converted, community extracted), GPU embedding:

| Build | Time | DB size |
|---|---|---|
| Factory PDF + LEMON | 213 s | 149 MB |
| LEMON only | 136 s | 116 MB |

On a CPU only, embedding the ~5,500 rows of the larger build takes roughly 37-45 minutes instead of about one.

## Scaling formulas

```
build_min   ≈ pdf_pages × 0.2 s / 60
            + lemon_files × 0.004 s / 60
            + sections / embed_rows_per_s / 60      # GPU ~100, CPU ~2-2.5 on the PC above
            + 3                                     # loaders, NHTSA, community tables, pictures

community_h ≈ threads × out_tokens_per_thread / (aggregate_gen_tok_s × 3600)
            ≈ threads × 1,050 / (145 × 3600)        # PC above: ~7 s per thread
```

- **A different GPU:** measure your 9B Q4 generation speed with 4 parallel slots (or one slot × ~2.5 as a guess).
  Community time scales as 145 / your tok/s. Embedding scales with fp16 throughput: an RTX 3060 might be
  ~0.6-0.8× a 3070, an RTX 4090 ~3×.
- **CPU only:** embedding ≈ 2.5 rows/s × (your physical cores / 12), roughly linear. A 9B model on a CPU at
  ~5-10 tok/s would need ~2-4 minutes per thread, so do the community pass on a GPU or skip it.
- **Another car:** PDF time scales with pages, LEMON time with files in the zip, embedding with sections
  (~1.5 sections per factory-manual page, ~0.4 per LEMON file), community time with thread count. The test car
  had 6,652 matching threads, 3,538 after the filters; 1,912 were extracted before the time budget ran out.
  Popular cars have many more, so cap the run with `kb/car_db.py community-select --budget-hours H --rate R`.
- **DB size:** ~0.6-0.7 MB per 1,000 sections of text and vectors, plus pictures: ~80 MB for a LEMON car's
  pictures, ~26 MB for a factory manual's figures as WebP.

## Factory PDF vs LEMON: what each gave for the test car

| | Factory PDF + LEMON | LEMON only |
|---|---|---|
| Source | PDF with a real text layer (2,073 pages, 37.8 MB) | HTML pages + images (7,701 files, 96 MB zip) |
| Manual sections | 4,749 | 3,117 |
| Torque/spec rows | 454 | 14 |
| Labor rows | 1,454 | 1,454 |
| DTCs with a diagnostic procedure | 117 | 117 |
| Walkable diagnostic step trees | 74 (486 steps) | 0 |
| Pictures stored in the DB | 7,615 | 4,487 |
| Search: top-3 hits shared over 15 check queries | 26 of 45 shared; the extra hits were factory procedure pages | |

Why the gaps: LEMON's diagnostic step charts are pictures, so the app can show them but can't walk them step by
step the way it walks a factory PDF's text charts. LEMON torque values sit in page prose and pictures, so the
spec table only picks up the few that are in text tables.

In 20 simulated diagnosis cases the database changed a model's lead in only a few cases; the bigger difference
was the model, not the manual source.
