# Community data: what owners tested, ruled out and fixed

As of Nov 11 2026 the Subaru knowledge database carries a second layer next to the factory manual: what owners and
mechanics actually did on Reddit and the forums, pulled out thread by thread, checked against the thread's own words,
linked to the manual pages it touches, and searchable by meaning. This page explains why it exists, how it was vetted
and how it's stored. The data itself stays private (it's built from forum text and linked to manual pages); the code
that builds it is `kb/build_community.py`, and the apps read it through `app/community_ref.py`.

## Why build it

Factory service manuals are written around the factory's procedures, warranty repairs and a car maintained by the
book. Most cars on the road aren't: skipped services, earlier repairs and worn parts cause problems the manual never
describes, and owners have found tests and shortcuts it never mentions.

The deck already had one community layer: **baked cards**, one per trouble code and car, listing the fixes owners
confirmed. Cards are deliberately strict, and that strictness throws most of the knowledge away. They keep only
"confirmed fix for this code", so the tests people ran, the readings they got, what they ruled out, the wrong turns
and the advice from experienced replies were all dropped, and many card causes rest on only 1-3 posts. Subaru's
Reddit data alone is about 88,000 posts and 767,000 comments.

The community database keeps that diagnostic reasoning instead. Cards stay as they are, as a separate, stricter tool.

## Cards vs community: two filters on the same threads

| | Baked cards | Community database |
|---|---|---|
| Thread filter | Keyword prefilter for "likely fix thread" | "Is this about diagnosing or repairing this make": a code, a symptom or noise, a part or system, a test or measurement, or an outcome, **and** at least one reply |
| Subaru threads kept (Reddit + one forum, 103,422 threads) | 11,590 (11%) | 80,036 (77%) |
| What's kept per thread | The confirmed fix for a code | The car, codes, symptoms and conditions, every test with its tool and result, what was ruled out and why, suggestions with the reply's score, the outcome and whether it came back, short reasoning chains |
| How the apps use it | Second source after the manual's diagnostic tree | Ideas and supporting evidence, below the manual and the cards; outcomes are labelled "claimed" |

All 80,036 kept threads are searchable as raw text. The full model read went to the most useful subset: threads with
at least 3 replies that mention a test, an outcome or a trouble code (38,970 for Subaru), with threads where the owner
reports a fix first.

## How it was vetted

1. **Every item quotes its source.** The extraction model fills a fixed JSON schema per thread, and every item (a
   test, a ruled-out part, a fix, a reasoning step) must carry an exact quote of 3-30 words from a numbered segment
   of the thread. Python then checks that the quote really is in the thread, that the item's main words appear in its
   quote, and that years and mileages match the quote. Items that fail are dropped, never repaired. Whether a line
   came from the original poster or a reply, and the reply's score, come from the thread itself, never from the model.
2. **The model was chosen by a blind test, not by speed.** On the same 171 pilot threads, a Qwen3.5-9B (4-bit, four
   requests at a time on an RTX 3070) was compared with the 30B model it replaced. In a blind grade of 15 random
   records each, the 9B made 9 wrong items against the 30B's 26, failed the quote check less often (1.1% vs 3.7%)
   and ran about 4.5x faster. A lighter mode that let code fill the easy fields was faster still and was dropped,
   because it missed important items.
3. **The full Subaru run held the same quality.** 38,504 of 38,970 threads read cleanly (1.2% malformed replies,
   which were skipped). Of 555,015 extracted items, 0.8% failed the quote check and 5.3% were unsupported by their
   quote; both were dropped.
4. **No bias rules.** There are no hand-written symptom-to-cause maps or keyword-to-cause rules; counts come from
   Python over the extracted records, and nothing about any particular car or code is special.
5. **Privacy.** Usernames were hashed at download; no personal details go into records.

## What's in it (Subaru, merged Nov 11 2026)

- 29,899 threads with at least one checked item (threads with nothing that survived the checks aren't stored)
- 149,444 cause mentions, each tagged by role: suggested 86,350, tested 46,405, ruled out 9,538, fix claimed
  4,701, fix confirmed by the original poster 2,450 (58,140 distinct causes)
- 61,381 tests with tool, component and result, and 33,578 reasoning chains
- 493,649 links from tests, fixes and ruled-out parts to the manual sections they touch
- Counts per code and symptom, per model, year and engine: how many separate threads report each cause, how many
  the original poster confirmed, and how many ruled it out

## How it's stored and searched

- Built as its own file (`kb/community/subaru.community.db`), then merged into `kb/subaru.db` as `comm_*` tables,
  the same pattern as the telemetry tables. The merge backs up the previous copy first and can be re-run.
- **Links to the manual:** every test, fix and ruled-out part is run through the knowledge base's own search to find
  the manual sections it corresponds to, so a community lead can be turned into the manual's own test.
- **Search by meaning:** 170,762 short documents (thread summaries, symptoms, tests and chains) are embedded with the
  same model as the manual pages, Qwen3-Embedding-0.6B (1024 dimensions, stored as 8-bit integers with sqlite-vec),
  plus a full-text index. Embedding the Subaru set took about 8 minutes on the RTX 3070.
- **Lookup:** `community_ref.community()` is read-only and stdlib-only, so it runs on the deck. Given a code or a
  description and the car, it returns the ranked causes with their thread counts and confirmation counts, the
  tests owners ran, and the reasoning chains, preferring the same model and years. It answers in about 0.02-0.04 s.
  The Home app, Brainstorm, /debate, /wildcard and Team mode all read it, always ranked below the manual.

Chevrolet is next (about 8% extracted so far); the other makes have pass 0 done and wait their turn on the GPU.
