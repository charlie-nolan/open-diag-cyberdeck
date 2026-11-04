#!/bin/bash
# Cron, RUN_9B_SCHEDULED.md: a free GPU window just ended -> run the 9B community extraction (Subaru, then Chevy).
# Stops charm-pics, unloads Ollama, starts llama-server (9B, 4 slots) and the extraction loop. Safe to run twice.
export PATH=/usr/local/bin:/usr/bin:/bin
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
cd "$CK" || exit 1
LOG=$CK/logs/community_run_9b.log
RUNPID=$CK/logs/community_run_9b.pid
SRVPID=$CK/logs/serve_community_9b.pid
ARGS=(--make subaru,chevrolet --work-dir "$CK/kb/community/work_9b_run" --min-replies 3 --any-of test,outcome,code --log "$LOG")
say() { echo "$(date '+%F %T') gpu_on: $*" >> "$LOG"; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

if alive "$RUNPID"; then say "extraction already running (pid $(cat "$RUNPID")), nothing to do"; exit 0; fi

left=$("$CK/venv/bin/python" kb/build_community.py "${ARGS[@]}" progress | awk '/^LEFT /{s+=$3} END{print s+0}')
if [ "$left" -eq 0 ]; then
    systemctl --user start charm-pics
    say "community run done (charm-pics $(systemctl --user is-active charm-pics)); the cron lines can be removed"
    exit 0
fi

systemctl --user stop charm-pics
for m in $(ollama ps 2>/dev/null | awk 'NR>1{print $1}'); do ollama stop "$m"; say "unloaded Ollama $m"; done
for _ in $(seq 30); do ps -eo args | grep -q '^/usr/lib/ollama/llama-server' || break; sleep 1; done   # VRAM really freed
# a stale server of ours without its extraction loop: stop it so it doesn't count as another GPU job
if alive "$SRVPID"; then kill "$(cat "$SRVPID")"; sleep 5; rm -f "$SRVPID"; fi
source "$CK/tools/gpu_guard.sh"
busy=$(gpu_jobs | while read -r pid rest; do      # nvidia-smi may show other users' processes as "[No data]"
    ps -o args= -p "$pid" | grep -q '^/usr/lib/ollama/' || echo "$pid $(ps -o args= -p "$pid" | cut -c1-60) $rest"; done)
other_llama=$(ps -eo args | awk '$1 ~ /llama-server$/ && $1 !~ /ollama/' | head -1)   # Ollama's own runner doesn't count
if [ -n "$busy" ] || [ -n "$other_llama" ] || pgrep -f "build_charm_all.sh|build_subaru_kb.py.* embed" >/dev/null \
        || { [ -e "$GPU_LOCK" ] && ! flock -n "$GPU_LOCK" true; }; then
    say "another GPU job is running (${busy:-${other_llama:-CHARM build, embed or a held .gpu.lock}}); not started"
    systemctl --user start charm-pics
    exit 0
fi

MODEL=$CK/models/gguf/Qwen3.5-9B-Q4_K_M.gguf SLOTS=4 CTX=32768 \
    setsid nohup bash card_baking/serve_extract.sh > "$CK/logs/serve_community_9b.log" 2>&1 < /dev/null &
echo $! > "$SRVPID"
for _ in $(seq 60); do curl -s 127.0.0.1:8080/health | grep -q '"ok"' && break; sleep 3; done
if ! curl -s 127.0.0.1:8080/health | grep -q '"ok"'; then
    say "llama-server did not come up (see logs/serve_community_9b.log); GPU left free"
    kill "$(cat "$SRVPID")" 2>/dev/null; rm -f "$SRVPID"
    systemctl --user start charm-pics
    exit 1
fi

# the extraction takes .gpu.lock itself (held until it exits); finished threads are skipped on resume
setsid nohup "$CK/venv/bin/python" kb/build_community.py "${ARGS[@]}" --backend llama --model Qwen3.5-9B-Q4_K_M \
    --parallel 4 --order priority --stats-every 500 extract >> "$LOG" 2>&1 < /dev/null &
echo $! > "$RUNPID"
say "started: llama-server pid $(cat "$SRVPID"), extraction pid $(cat "$RUNPID"), $left threads left"
