#!/usr/bin/env python3
"""
Convert Qwen3 Hugging Face models to .rkllm for the Orange Pi 5 (RK3588 NPU).

Runs on the x86-64 Linux desktop, NOT on the Pi, inside the venv that has rkllm-toolkit
installed (the toolkit wheel must be the same version as the Pi runtime: v1.3.1).

  python tools/export_qwen3_rkllm.py                 # all presets (fast + deep + think)
  python tools/export_qwen3_rkllm.py --only fast     # just Qwen3-1.7B
  python tools/export_qwen3_rkllm.py --hf Qwen/Qwen3-8B --name Qwen3-8B   # any other model

Output goes to models/rkllm/<name>_W8A8_RK3588.rkllm, which is where
app/config.json looks. Copy that folder to the Pi.

RAM: float32 export needs roughly 4-5x the model's fp16 size in system RAM
(1.7B ~ 16 GB, 4B ~ 32 GB). Use --dtype bfloat16 if the desktop runs out of memory.
"""
import argparse
import json
import os
import random
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # repo root
PRESETS = {
    "fast": ("Qwen/Qwen3-1.7B", "Qwen3-1.7B"),
    "deep": ("Qwen/Qwen3-4B-Instruct-2507", "Qwen3-4B-Instruct-2507"),
    "think": ("Qwen/Qwen3-4B-Thinking-2507", "Qwen3-4B-Thinking-2507"),  # optional "think harder" verdict
}
QUESTIONS = [
    "What does this section of the service manual say, and what should I check first?",
    "Summarise the procedure below as numbered steps.",
    "Which parts and specs are mentioned below?",
    "Based on the manual excerpt below, what is the most likely cause and the next test?",
]


def chat(user):
    # Same template the GUI sets at runtime (thinking disabled).
    return (f"<|im_start|>system\nYou are a Subaru diagnostic assistant.<|im_end|>\n"
            f"<|im_start|>user\n{user}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n")


def build_calibration(path, knowledge_db, n=48):
    """Quantization calibration set from real FSM text, so W8A8 scales fit this domain."""
    samples = []
    if os.path.isfile(knowledge_db):
        con = sqlite3.connect(f"file:{knowledge_db}?mode=ro", uri=True)
        rows = con.execute("SELECT content FROM manual_chunks WHERE length(content) > 400 "
                           "ORDER BY random() LIMIT ?", (n,)).fetchall()
        con.close()
        for (content,) in rows:
            text = content[:1200]
            cut = len(text) // 2
            samples.append({"input": chat(f"{random.choice(QUESTIONS)}\n\n{text[:cut]}"),
                            "target": text[cut:]})
    if not samples:
        print(f"  (no {knowledge_db}; using a tiny generic calibration set)")
        for q in ("How do I read a trouble code with a scan tool?", "How do I check fuel trims?",
                  "What does a coolant temperature sensor do?"):
            samples.append({"input": chat(q), "target": "Start with the cheapest checks first."})
    with open(path, "w") as f:
        json.dump(samples, f, ensure_ascii=False, indent=1)
    print(f"  calibration set: {len(samples)} samples -> {path}")


def export(hf_id, name, args, dataset):
    from rkllm.api import RKLLM

    out = os.path.join(args.out, f"{name}_W8A8_{args.platform}.rkllm")
    if os.path.isfile(out) and not args.force:
        print(f"  exists, skipping (use --force to rebuild): {out}")
        return 0
    local = os.path.join(args.hf_dir, name)
    if not os.path.isfile(os.path.join(local, "config.json")):
        from huggingface_hub import snapshot_download
        print(f"  downloading {hf_id} -> {local}")
        snapshot_download(repo_id=hf_id, local_dir=local)

    llm = RKLLM()
    ret = llm.load_huggingface(model=local, model_lora=None, device=args.device, dtype=args.dtype,
                               custom_config=None, load_weight=True)
    if ret != 0:
        print(f"  load failed ({ret})")
        return ret
    ret = llm.build(do_quantization=True, optimization_level=1, quantized_dtype="W8A8",
                    quantized_algorithm="normal", target_platform=args.platform, num_npu_core=3,
                    extra_qparams=None, dataset=dataset, hybrid_rate=0, max_context=args.max_context)
    if ret != 0:
        print(f"  build failed ({ret})")
        return ret
    ret = llm.export_rkllm(out)
    print(f"  {'wrote ' + out if ret == 0 else 'export failed (%d)' % ret}")
    return ret


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", choices=sorted(PRESETS), help="export just one preset")
    ap.add_argument("--hf", help="any Hugging Face model id instead of the presets")
    ap.add_argument("--name", help="output name for --hf (default: last part of the id)")
    ap.add_argument("--out", default=f"{ROOT}/models/rkllm")
    ap.add_argument("--hf-dir", default=f"{ROOT}/models/hf")
    ap.add_argument("--knowledge-db", default=f"{ROOT}/test_subaru_knowledge.db")
    ap.add_argument("--device", default="cpu", choices=["cpu", "cuda"])
    ap.add_argument("--dtype", default="float32", choices=["float32", "float16", "bfloat16"])
    ap.add_argument("--platform", default="RK3588")
    ap.add_argument("--max-context", type=int, default=4096)
    ap.add_argument("--force", action="store_true", help="rebuild even if the .rkllm exists")
    args = ap.parse_args()

    try:
        import importlib
        importlib.import_module("rkllm.api")
    except ImportError:
        sys.exit("rkllm-toolkit is not installed in this Python. Activate the venv that has it "
                 "(pip install rkllm_toolkit-1.3.1-*.whl from rknn-llm/rkllm-toolkit/).")

    if args.hf:
        jobs = [(args.hf, args.name or args.hf.rstrip("/").split("/")[-1])]
    else:
        jobs = [PRESETS[args.only]] if args.only else list(PRESETS.values())

    os.makedirs(args.out, exist_ok=True)
    os.makedirs(args.hf_dir, exist_ok=True)
    dataset = os.path.join(args.out, "data_quant.json")
    build_calibration(dataset, args.knowledge_db)

    failed = 0
    for hf_id, name in jobs:
        print(f"== {hf_id}")
        failed += export(hf_id, name, args, dataset) != 0
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
