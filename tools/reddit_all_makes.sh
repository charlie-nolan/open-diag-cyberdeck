#!/bin/bash
# Reddit for every CHARM make, the way Subaru got it: one reddit-get for all profiles' subreddits from the
# Pushshift per-subreddit torrent (raw/reddit/pushshift), then each profile's reddit step into
# raw/reddit/reddit_<make>.db. extract_fixes (GPU/LLM) is NOT run here: it waits for the CHARM build.
#   setsid nohup bash tools/reddit_all_makes.sh >> logs/reddit_all_makes.log 2>&1 &
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
set -o pipefail
# one copy at a time; make_full.sh checks this lock to know the loop is running
exec 8>"$CK/.reddit_loop.lock"
flock -n 8 || { echo "another reddit_all_makes.sh is already running"; exit 1; }
P=$CK/venv/bin/python
MP=$CK/chevrolet/make_pipeline.py
PR=$CK/chevrolet/profiles/makes
echo "=== $(date -Is) reddit-get (all subreddits)"
$P "$MP" "$PR/_all_reddit.json" reddit-get || echo "=== reddit-get FAILED"
for f in "$PR"/*.json; do
    case "$(basename "$f")" in _*) continue;; esac
    echo "=== $(date -Is) reddit $(basename "$f" .json)"
    $P "$MP" "$f" reddit 2>&1 | grep -vE "^\s*$" | tail -12 || echo "=== FAIL $(basename "$f" .json)"
done
echo "=== ALL DONE $(date -Is)"
