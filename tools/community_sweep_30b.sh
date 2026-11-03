#!/bin/bash
# Throughput sweep for the 30B in llama-server (TEST_9B_FAST_EXTRACT.md fallback): --n-cpu-moe x slots, lite mode,
# 40 Subaru pilot threads per config, each in its own work folder. Log: logs/community_sweep_30b.log
cd ~/subaru_cyberdeck
BLOB=$(ollama show --modelfile qwen3:30b-a3b-instruct-2507-q4_K_M | awk '/^FROM \//{print $2; exit}')
LOG=logs/community_sweep_30b.log
run() {   # $1 n-cpu-moe, $2 slots
    pkill -f "llama-server" ; sleep 3
    llama-server -m "$BLOB" --jinja -ngl 99 --n-cpu-moe "$1" -c $(( $2 * 8192 )) -np "$2" -fa on \
        --cache-type-k q8_0 --cache-type-v q8_0 --host 127.0.0.1 --port 8080 > logs/llama_30b_sweep.log 2>&1 &
    for i in $(seq 120); do curl -s 127.0.0.1:8080/health | grep -q ok && break; sleep 3; done
    if ! curl -s 127.0.0.1:8080/health | grep -q ok; then
        echo "$(date +%T) moe=$1 slots=$2: server did not start ($(grep -m1 -i -E 'out of memory|error' logs/llama_30b_sweep.log))" >> $LOG; return; fi
    D=kb/community/work_30b_llama_m$1_s$2; mkdir -p $D; rm -f $D/*
    sqlite3 kb/community/work/subaru.work.db ".backup $D/subaru.work.db"
    sqlite3 $D/subaru.work.db "delete from extract; delete from thread where pilot=0; vacuum"
    venv/bin/python kb/build_community.py --make subaru --pilot --work-dir $D --backend llama \
        --model qwen3-30b-a3b-instruct-2507-q4km --parallel "$2" --lite --limit 40 --stats-every 40 extract
    r=$(sqlite3 $D/subaru.work.db "select round(3600.0*n/seconds) from run order by rowid desc limit 1")
    v=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader)
    echo "$(date +%T) moe=$1 slots=$2: $r threads/h wall (VRAM $v)" >> $LOG
}
echo "$(date +%T) sweep start" >> $LOG
for m in 24 32 40; do run $m 4; done
best=$(grep -E "moe=[0-9]+ slots=4: [0-9]" $LOG | sort -t: -k4 -n | tail -1 | sed -E 's/.*moe=([0-9]+).*/\1/')
[ -n "$best" ] && run "$best" 2
pkill -f "llama-server"
echo "$(date +%T) SWEEP DONE (best moe with 4 slots: $best)" >> $LOG
