"""Summarise bench/run.sh output: cost, tokens, time and correctness per condition."""
import collections
import glob
import json
import os
import statistics as st
import sys

out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "out")
rows = collections.defaultdict(list)
for f in sorted(glob.glob(os.path.join(out, "[ABC]_*_*.json"))):
    cond, _rep, i = os.path.basename(f)[:-5].split("_")
    i = int(i)
    try:
        d = json.load(open(f, encoding="utf-8"))
    except (OSError, ValueError):
        continue
    u = d.get("usage", {})
    res = (d.get("result") or "").lower()
    ok = None
    if i == 0:
        ok = "pnpm test:unit" in res and "4310" in res
    elif i == 1:
        ok = "feat/abc-123" in res and ("fly" in res or "fra" in res)
    elif i == 2:
        ok = "postgres" in res and "5433" in res
    rows[(cond, "needs facts" if i < 3 else "unrelated")].append(dict(
        cost=d.get("total_cost_usd", 0),
        tokens=u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0) + u.get("cache_read_input_tokens", 0),
        ms=d.get("duration_ms", 0), ok=ok))

names = {"A": "no memory", "B": "recallhook", "C": "retyped facts"}
print(f"{'setup':14s} {'prompts':12s} {'n':>2s} {'$/call':>9s} {'tokens in':>10s} {'secs':>5s}  correct")
for key in sorted(rows, key=lambda k: (k[1], k[0])):
    r = rows[key]
    m = lambda a: st.mean(x[a] for x in r)
    c = [x["ok"] for x in r if x["ok"] is not None]
    print(f"{names[key[0]]:14s} {key[1]:12s} {len(r):2d} {m('cost'):9.5f} {m('tokens'):10.0f} {m('ms')/1000:5.1f}  "
          f"{sum(c)}/{len(c)}" if c else
          f"{names[key[0]]:14s} {key[1]:12s} {len(r):2d} {m('cost'):9.5f} {m('tokens'):10.0f} {m('ms')/1000:5.1f}  -")
