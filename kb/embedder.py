"""
Query embedder for kb_retriever's vector search: Qwen3-Embedding-0.6B on the CPU, the same model,
truncation, normalisation and int8 scaling that build_subaru_kb.py's embed step used for the documents.

    from embedder import QueryEmbedder
    embed = QueryEmbedder("~/subaru_cyberdeck/models/qwen3emb")    # local folder or "Qwen/Qwen3-Embedding-0.6B"
    r = Retriever(db, model="Forester", year=2002, embed=embed)

Needs sentence-transformers (torch CPU is enough) and numpy. The model loads on first use (~1.2 GB).
"""
import os
import threading

EMBED_DIM = 1024
# Qwen3-Embedding wants a one-line task description in front of the query (documents get none).
# It describes the task only; it says nothing about which causes or parts are likely.
TASK = "Given a vehicle problem description or repair question, retrieve service manual passages that answer it"


class QueryEmbedder:
    def __init__(self, model="Qwen/Qwen3-Embedding-0.6B", device="cpu", task=TASK, threads=None):
        self.model_name = os.path.expanduser(model)
        self.device, self.task, self.threads = device, task, threads
        self._model = None
        self._cache = {}
        self._lock = threading.Lock()

    def warm(self):
        """Load the model in the background so the first question doesn't wait for it."""
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        with self._lock:
            return self._load_locked()

    def _load_locked(self):
        if self._model is None:
            import torch
            if self.threads:
                torch.set_num_threads(self.threads)
            from sentence_transformers import SentenceTransformer
            # transformers 5 loads the checkpoint's bfloat16 by default; on a CPU that is ~3x slower than float32
            # (537 vs 166 ms per query on the 5900X, 4 threads) for the same vector (cosine 0.9998).
            # (.to() rather than a load kwarg: the deck's transformers may be older and not take dtype=.)
            self._model = SentenceTransformer(self.model_name, device=self.device)
            if self.device == "cpu" and self._model[0].auto_model.dtype != torch.float32:
                self._model.to(torch.float32)
        return self._model

    def vector(self, text):
        """Normalised float vector (numpy) for a query."""
        import numpy as np
        prompt = f"Instruct: {self.task}\nQuery:" if self.task else ""
        v = self._load().encode([text], prompt=prompt, convert_to_numpy=True)[0][:EMBED_DIM]
        return v / max(float(np.linalg.norm(v)), 1e-9)

    def __call__(self, text):
        """int8 bytes for sqlite-vec's vec_int8(), scaled like the stored document vectors."""
        if text not in self._cache:
            import numpy as np
            v = self.vector(text)
            self._cache[text] = np.clip(np.round(v * 127), -127, 127).astype(np.int8).tobytes()
            if len(self._cache) > 256:
                self._cache.pop(next(iter(self._cache)))
        return self._cache[text]


def to_int8(v):
    """Rows of float vectors -> int8 rows in the stored format: first EMBED_DIM dims, unit length, x127, clipped."""
    import numpy as np
    v = np.asarray(v, dtype=np.float32)[:, :EMBED_DIM]
    v = v / np.linalg.norm(v, axis=1, keepdims=True).clip(1e-9)
    return np.clip(np.round(v * 127), -127, 127).astype(np.int8)


class DocEncoder:
    """Documents for the build steps' embed (no prompt), the same model, pooling and int8 format as QueryEmbedder.

    Faster than SentenceTransformer.encode on long, mixed-length jobs: texts are sorted by token count per chunk,
    batches are cut to a token budget (short rows go in big batches, long rows in small ones, little padding), and
    the next chunk is tokenized on a thread while the GPU works. Out of GPU memory: halve the budget and redo that
    batch; at one row per batch move to the CPU. Any source (manual sections, TSBs, cards, community posts) goes
    through encode_rows(); what each row's text says is up to the caller.

        enc = DocEncoder(device="cuda")
        for ids, vecs in enc.encode_rows(rows):     # rows: [(id, text)], vecs: int8 [n, 1024]
            ...write and commit...
    """

    def __init__(self, model="Qwen/Qwen3-Embedding-0.6B", device="cuda", dtype="float16", max_tokens=2048,
                 token_budget=16384, max_batch=512, chunk=16384, log=print):
        import torch
        from sentence_transformers import SentenceTransformer
        if device.startswith("cuda") and not torch.cuda.is_available():
            raise SystemExit(f"embed: --device {device} asked for but CUDA is not available "
                             f"(torch {torch.__version__}, built for CUDA {torch.version.cuda}); "
                             "use --device cpu to run on the CPU on purpose")
        dt = {"bfloat16": torch.bfloat16, "float16": torch.float16, "float32": torch.float32}[dtype]
        if device == "cpu" and dt != torch.float32:
            dt = torch.float32      # no bf16/fp16 matmul units on these CPUs: half precision is slower there
        self.torch, self.log = torch, log
        self.model_name, self._fp32 = os.path.expanduser(model), None
        try:
            self.model = SentenceTransformer(self.model_name, device=device, model_kwargs={"dtype": dt})
        except torch.cuda.OutOfMemoryError:
            # another job holds the GPU (often Ollama answering the picture server): same rule as at batch 1
            torch.cuda.empty_cache()
            log("embed: GPU out of memory while loading the model (another GPU job?), running on the CPU (slow)",
                flush=True)
            self.model = SentenceTransformer(self.model_name, device="cpu", model_kwargs={"dtype": torch.float32})
        self.model.max_seq_length = max_tokens
        self.tok = self.model.tokenizer
        self.budget, self.max_batch, self.chunk = token_budget, max_batch, chunk
        self.budget0, self.max_batch0, self._ok = token_budget, max_batch, 0
        self.truncated = 0
        am = self.model[0].auto_model
        where = (f"cuda:{torch.cuda.current_device()} {torch.cuda.get_device_name()}" if self.device.type == "cuda"
                 else "CPU")
        log(f"embed: device {where}, dtype {am.dtype}, attention {am.config._attn_implementation}, "
            f"max {max_tokens} tokens/row, {token_budget} tokens/batch, torch {torch.__version__}", flush=True)

    @property
    def device(self):
        return self.model.device

    def _prepare(self, rows):
        """One chunk on the prefetch thread: drop repeated texts, count tokens, sort by length, cut batches to the
        token budget and build their features with the model's own preprocess (so padding, truncation and the
        end-of-text token are exactly what SentenceTransformer.encode uses)."""
        first, uniq = {}, []
        for _, t in rows:
            if t not in first:
                first[t] = len(uniq)
                uniq.append(t)
        cap = self.model.max_seq_length
        lens = [min(len(x) + 1, cap) for x in self.tok(uniq, add_special_tokens=False)["input_ids"]]
        order = sorted(range(len(uniq)), key=lens.__getitem__)
        plan = self._batches(lens, order)
        feats = [self.model.preprocess([uniq[i] for i in b]) for b in plan]
        return rows, [first[t] for _, t in rows], uniq, lens, plan, feats

    def _batches(self, lens, order):
        """Index lists over order, each within the token budget (rows x longest row) and max_batch rows."""
        out, cur, longest = [], [], 0
        for i in order:
            n = lens[i]
            if cur and (max(longest, n) * (len(cur) + 1) > self.budget or len(cur) >= self.max_batch):
                out.append(cur)
                cur, longest = [], 0
            cur.append(i)
            longest = max(longest, n)
        if cur:
            out.append(cur)
        return out

    def _forward(self, feats):
        feats = {k: (v.to(self.device, non_blocking=True) if hasattr(v, "to") else v) for k, v in feats.items()}
        with self.torch.inference_mode():
            v = self.model(feats)["sentence_embedding"].float()
            if not bool(self.torch.isfinite(v).all()):
                # half precision overflowed on this batch (never seen on manual text): redo it in float32
                self.log("embed: non-finite vector in half precision, redoing that batch in float32", flush=True)
                if self._fp32 is None:
                    from sentence_transformers import SentenceTransformer
                    self._fp32 = SentenceTransformer(self.model_name, device=self.device,
                                                     model_kwargs={"dtype": self.torch.float32})
                    self._fp32.max_seq_length = self.model.max_seq_length
                v = self._fp32(feats)["sentence_embedding"].float()
            return v.cpu().numpy()

    def _encode_chunk(self, uniq, lens, plan, feats):
        import numpy as np
        torch = self.torch
        vecs = np.zeros((len(uniq), EMBED_DIM), dtype=np.float32)
        queue = list(zip(plan, feats))
        while queue:
            batch, f = queue[0]
            try:
                vecs[batch] = self._forward(f)[:, :EMBED_DIM]
                queue.pop(0)
                self._ok += 1
                if self._ok >= 200 and self.budget < self.budget0:
                    # the other GPU job (e.g. Ollama for a picture) has usually gone by now: grow back slowly
                    self.budget, self.max_batch, self._ok = min(self.budget * 2, self.budget0), self.max_batch0, 0
            except torch.cuda.OutOfMemoryError:
                # another GPU job, or long rows: free the cache, halve the budget, re-plan the rest of the chunk
                del f
                torch.cuda.empty_cache()
                self._ok = 0
                if len(batch) > 1:
                    self.budget = max(self.budget // 2, 1)
                    self.max_batch = max(len(batch) // 2, 1)
                    self.log(f"embed: GPU out of memory, budget -> {self.budget} tokens/batch", flush=True)
                elif self.device.type != "cpu":
                    self.model.to("cpu", dtype=torch.float32)
                    self.log("embed: GPU out of memory at one row per batch, continuing on the CPU (slow)", flush=True)
                else:
                    raise
                rest = [i for b, _ in queue for i in b]
                queue = [(b, None) for b in self._batches(lens, rest)]
            if queue and queue[0][1] is None:
                queue[0] = (queue[0][0], self.model.preprocess([uniq[i] for i in queue[0][0]]))
        self.truncated += sum(1 for n in lens if n >= self.model.max_seq_length)
        return vecs

    def encode_rows(self, rows):
        """Yield (ids, int8 vectors) per chunk of rows [(id, text)]; the next chunk tokenizes during this one."""
        from concurrent.futures import ThreadPoolExecutor
        chunks = [rows[i:i + self.chunk] for i in range(0, len(rows), self.chunk)]
        with ThreadPoolExecutor(1) as pool:
            nxt = pool.submit(self._prepare, chunks[0]) if chunks else None
            for k in range(len(chunks)):
                part, where, uniq, lens, plan, feats = nxt.result()
                nxt = pool.submit(self._prepare, chunks[k + 1]) if k + 1 < len(chunks) else None
                vecs = self._encode_chunk(uniq, lens, plan, feats)
                yield [i for i, _ in part], to_int8(vecs[where])

    def encode(self, texts):
        """int8 [n, 1024] for a list of texts, in order."""
        import numpy as np
        out = [v for _, v in self.encode_rows(list(enumerate(texts)))]
        return np.concatenate(out) if out else np.zeros((0, EMBED_DIM), dtype=np.int8)


_SHARED = {}


def shared(model, **kw):
    """One embedder per model folder for the whole app, so switching cars doesn't load the model again."""
    key = os.path.expanduser(model)
    if key not in _SHARED:
        _SHARED[key] = QueryEmbedder(model, **kw)
        _SHARED[key].warm()
    return _SHARED[key]
