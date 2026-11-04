#!/bin/bash
# Cron, RUN_9B_SCHEDULED.md: a free GPU window starts -> stop the 9B community extraction cleanly, stop llama-server,
# unload Ollama, start charm-pics. Finished threads are already saved; the next community_gpu_on.sh resumes.
export PATH=/usr/local/bin:/usr/bin:/bin
export XDG_RUNTIME_DIR=/run/user/$(id -u)
export DBUS_SESSION_BUS_ADDRESS=unix:path=$XDG_RUNTIME_DIR/bus
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in
cd "$CK" || exit 1
LOG=$CK/logs/community_run_9b.log
RUNPID=$CK/logs/community_run_9b.pid
SRVPID=$CK/logs/serve_community_9b.pid
ARGS=(--make subaru,chevrolet --work-dir "$CK/kb/community/work_9b_run" --min-replies 3 --any-of test,outcome,code --log "$LOG")
say() { echo "$(date '+%F %T') gpu_off: $*" >> "$LOG"; }
alive() { [ -f "$1" ] && kill -0 "$(cat "$1")" 2>/dev/null; }

if alive "$RUNPID"; then     # SIGTERM: it finishes and saves the requests in flight, starts no new ones
    pid=$(cat "$RUNPID"); kill -TERM "$pid"
    for _ in $(seq 60); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    if kill -0 "$pid" 2>/dev/null; then kill -KILL "$pid"; say "extraction killed after 60 s (its open requests will be redone)"
    else say "extraction stopped cleanly"; fi
fi
rm -f "$RUNPID"              # .gpu.lock is released with the extraction process
if alive "$SRVPID"; then
    pid=$(cat "$SRVPID"); kill "$pid"
    for _ in $(seq 20); do kill -0 "$pid" 2>/dev/null || break; sleep 1; done
    kill -0 "$pid" 2>/dev/null && kill -KILL "$pid"
fi
rm -f "$SRVPID"
for m in $(ollama ps 2>/dev/null | awk 'NR>1{print $1}'); do ollama stop "$m"; say "unloaded Ollama $m"; done
for _ in $(seq 30); do ps -eo args | grep -q '^/usr/lib/ollama/llama-server' || break; sleep 1; done
systemctl --user start charm-pics
"$CK/venv/bin/python" kb/build_community.py "${ARGS[@]}" progress > /dev/null
say "GPU free: $(nvidia-smi --query-gpu=memory.used --format=csv,noheader) used, charm-pics $(systemctl --user is-active charm-pics)"
