# Benchmark

Reproduces the table in the main README: does the memory hook change what a Claude call costs?

```bash
recallhook status            # gateway must be running
bash bench/seed.sh           # stores 3 made-up facts about a project called "Atlas"
bash bench/run.sh 3          # 3 rounds x 6 prompts x (no hook | hook | retyped facts), ~8 min
```

- Uses `claude -p --output-format json`, so numbers are **billed usage** reported by Claude Code, not estimates.
- Setups are interleaved so the shared prompt cache is equally warm for each; the first call is a discarded warm-up.
- `no hook` and `hook` runs differ only by `--settings bench/settings_hook.json`.
- The prompts that need the facts are checked for the right answer; the unrelated ones measure overhead.
- Delete the made-up memories afterwards in the dashboard (`recallhook ui`).

It is a small, synthetic test on a cheap model. If you run it on your own project and models, please share the numbers.
