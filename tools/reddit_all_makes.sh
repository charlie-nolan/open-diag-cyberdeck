#!/bin/bash
# Reddit for every CHARM make, the way Subaru got it: one reddit-get for all profiles' subreddits from the
# Pushshift per-subreddit torrent (raw/reddit/pushshift), then each profile's reddit step into
# raw/reddit/reddit_<make>.db. extract_fixes (GPU/LLM) is NOT run here: it waits for the CHARM build.
#   setsid nohup bash ~/subaru_cyberdeck/tools/reddit_all_makes.sh >> ~/subaru_cyberdeck/logs/reddit_all_makes.log 2>&1 &
set -o pipefail
P=~/subaru_cyberdeck/venv/bin/python
MP=~/subaru_cyberdeck/chevrolet/make_pipeline.py
PR=~/subaru_cyberdeck/chevrolet/profiles/makes
echo "=== $(date -Is) reddit-get (all subreddits)"
$P "$MP" "$PR/_all_reddit.json" reddit-get || echo "=== reddit-get FAILED"
for f in "$PR"/*.json; do
    case "$(basename "$f")" in _*) continue;; esac
    echo "=== $(date -Is) reddit $(basename "$f" .json)"
    $P "$MP" "$f" reddit 2>&1 | grep -vE "^\s*$" | tail -12 || echo "=== FAIL $(basename "$f" .json)"
done
echo "=== ALL DONE $(date -Is)"
