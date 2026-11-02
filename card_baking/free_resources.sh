#!/usr/bin/env bash
# Clear the GPU, CPU and RAM before a baking job.
#   ./free_resources.sh            show what would be stopped, ask, then stop it
#   ./free_resources.sh --yes      no question (used by run_bake.sh)
#   ./free_resources.sh --dry-run  only show
# Stops: anything using the GPU for compute, other model servers (llama-server, ollama, vLLM, LM Studio),
# marker / embedding / torch jobs, and any other process over $BIG_MB of RAM or $BUSY_CPU% CPU.
# Never stops: the desktop (KDE/X/Wayland), Claude / Remote Control, ssh, audio, systemd, this script,
# or other cyberdeck work in progress: the Ultimate Subaru Wayback pull and the Qwen3 RKLLM export.
# KEEP="pattern|pattern" adds more to that list (matched against the full command line).
# APPS="firefox chrome discord steam" ./free_resources.sh  also closes those apps.
set -u
ME="${USER:-$(id -un)}"
YES=0; DRY=0
for a in "$@"; do case "$a" in --yes|-y) YES=1;; --dry-run|-n) DRY=1;; esac; done
BIG_MB="${BIG_MB:-1500}"; BUSY_CPU="${BUSY_CPU:-50}"
PROTECT='^(systemd|Xorg|Xwayland|kwin|kwin_wayland|plasmashell|kded|ksmserver|sddm|gnome-shell|pipewire|wireplumber|pulseaudio|sshd?|bash|zsh|sudo|claude|node|tmux|screen|konsole|dbus|polkit|NetworkManager|ckb-next|baloo_file|kactivitymanagerd|xdg-.*)'
ML='llama-server|llama-cli|ollama|vllm|lmstudio|LM Studio|koboldcpp|text-generation|marker|convert_big_manuals|build_subaru_kb\.py embed|sentence_transformers|torchrun|comfyui|stable-diffusion'

KEEP_CMD="wayback|ultimatesubaru|export_qwen3_rkllm|rkllm|claude|remote-control|charm_ingest|squashfuse"
KEEP_CMD+="|build_subaru_kb\.py.* (fsm|diag|charm|labor|cards|finish)${KEEP:+|$KEEP}"   # DB writes: never interrupt

me=$$; parent=$PPID; grand=$(ps -o ppid= -p "$PPID" 2>/dev/null | tr -d ' ')
declare -A why=()
add() { local pid="$1" r="$2"; [[ "$pid" == "$me" || "$pid" == "$parent" || "$pid" == "$grand" ]] && return
        local name; name=$(ps -o comm= -p "$pid" 2>/dev/null) || return
        [[ "$name" =~ $PROTECT ]] && return
        grep -qiE -- "$KEEP_CMD" <(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null) && return
        why[$pid]="${why[$pid]:-}${why[$pid]:+, }$r"; }

if command -v nvidia-smi >/dev/null; then
  while IFS=, read -r pid _; do [[ -n "$pid" ]] && add "${pid// /}" "GPU"; done \
    < <(nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader 2>/dev/null)
fi
while read -r pid; do add "$pid" "model/ML job"; done < <(pgrep -u "$ME" -f -- "$ML")
while read -r pid rss cpu; do
  (( rss / 1024 >= BIG_MB )) && add "$pid" "$((rss / 1024)) MB RAM"
  (( ${cpu%.*} >= BUSY_CPU )) && add "$pid" "${cpu}% CPU"
done < <(ps -u "$ME" -o pid=,rss=,pcpu=)
for app in ${APPS:-}; do while read -r pid; do add "$pid" "listed app"; done < <(pgrep -u "$ME" -x "$app"); done

if (( ${#why[@]} == 0 )); then echo "free_resources: nothing to stop"; else
  echo "free_resources: will stop"
  for pid in "${!why[@]}"; do printf "  %-8s %-28s %s\n" "$pid" "$(ps -o comm= -p "$pid")" "${why[$pid]}"; done
  (( DRY )) && exit 0
  if (( ! YES )); then read -r -p "Stop these? [y/N] " ok; [[ "$ok" == [yY]* ]] || { echo "nothing stopped"; exit 1; }; fi
  kill -TERM "${!why[@]}" 2>/dev/null
  for _ in $(seq 20); do alive=0; for pid in "${!why[@]}"; do kill -0 "$pid" 2>/dev/null && alive=1; done
                         (( alive )) || break; sleep 0.5; done
  for pid in "${!why[@]}"; do kill -0 "$pid" 2>/dev/null && kill -KILL "$pid" && echo "  forced $pid"; done
fi
sync
echo "free_resources: $(free -g | awk '/Mem:/{print $7" GB RAM available of "$2}')" \
     "$(command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader | awk -F, '{print "| GPU "$1" used of"$2}')"
