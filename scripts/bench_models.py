"""Compare Ollama models for mem0 fact extraction.  Usage:
    python scripts/bench_models.py qwen2.5-coder:latest llama3.2:3b [--num-ctx 4096]
Reports seconds per message and how many expected facts were extracted."""
import argparse, tempfile, time

from recallhook.config import Config
from recallhook.memory import EXTRACT_RULES, build_mem0

CASES = [  # (message, keywords that must appear in the extracted facts)
    ("My side project is called Nimbus. Its staging database is PostgreSQL 16 on port 5433, "
     "and in all my JavaScript projects I always use pnpm, never npm.", ["nimbus", "5433", "pnpm"]),
    ("Can you refactor the auth module? By the way I deploy everything on Fly.io and my team "
     "does trunk-based development.", ["fly", "trunk"]),
    ("Fix the failing test in utils.py", []),  # no durable facts: should extract nothing
]

ap = argparse.ArgumentParser()
ap.add_argument("models", nargs="+")
ap.add_argument("--num-ctx", type=int, default=4096)
a = ap.parse_args()
for model in a.models:
    m = build_mem0(Config(store_path=tempfile.mkdtemp(), llm_model=model, num_ctx=a.num_ctx))
    m.add([{"role": "user", "content": "warm up the model"}], user_id="w")  # exclude cold load
    tot, hit, want = 0.0, 0, 0
    for text, kws in CASES:
        t = time.time()
        facts = [r["memory"].lower() for r in m.add([{"role": "user", "content": text}], user_id="b", prompt=EXTRACT_RULES)["results"]]
        dt = time.time() - t
        found = sum(any(k in f for f in facts) for k in kws)
        tot, hit, want = tot + dt, hit + found, want + len(kws)
        print(f"  {model:28s} {dt:5.1f}s  keywords {found}/{len(kws)}  facts={len(facts)}")
    print(f"{model}: total {tot:.0f}s, keywords {hit}/{want}\n")
