#!/bin/bash
# Sourced by make_full.sh and bake_cards.sh. gpu_free tells whether the GPU may be used; gpu_lock takes
# .gpu.lock for this shell (released when the script exits). While a GPU step runs,
# Cyberdeck Home's PC models are slow or unloaded: that's expected.
CK=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # the checkout this script is in

GPU_LOCK=$CK/.gpu.lock

gpu_jobs() {   # GPU compute jobs other than the desktop and Ollama (Ollama is stopped instead)
    nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv,noheader,nounits 2>/dev/null |
        awk -F', ' '$3 > 400 && $2 !~ /kwin|Xorg|claude|chrome|firefox|ollama/ {print $1" "$2" ("$3" MiB)"}'
}

gpu_free() {   # 0 = free. Prints why not.
    local busy
    [ "${GPU_LOCK_HELD:-0}" = 1 ] && return 0   # this shell already holds the lock (its own earlier GPU step)
    busy=$(gpu_jobs)
    if [ -n "$busy" ]; then echo "GPU busy: $busy"; return 1; fi
    # Ollama runs its models through its own llama-server (/usr/lib/ollama/...): that one doesn't count,
    # gpu_lock unloads Ollama's models anyway. Only our own llama-server (card bake / forum fixes) does.
    if pgrep -af "build_charm_all.sh|build_subaru_kb.py.* embed|llama-server" | grep -v -e pgrep -e "/ollama/" |
            grep -q .; then
        echo "GPU busy: the CHARM per-make build, an embed, or llama-server is running (it may be between GPU phases)"
        return 1
    fi
    if [ -e "$GPU_LOCK" ] && ! flock -n "$GPU_LOCK" true 2>/dev/null; then
        echo "GPU busy: another make_full/bake_cards step holds $GPU_LOCK"; return 1
    fi
    return 0
}

gpu_lock() {   # call only after gpu_free; also unloads Ollama's models (the service keeps running)
    [ "${GPU_LOCK_HELD:-0}" = 1 ] && return 0
    exec 9>"$GPU_LOCK"
    flock -n 9 || { echo "GPU lock taken by another step"; return 1; }
    GPU_LOCK_HELD=1
    for m in $(ollama ps 2>/dev/null | awk 'NR>1{print $1}'); do ollama stop "$m"; echo "unloaded Ollama $m"; done
}
