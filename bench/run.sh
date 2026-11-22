#!/usr/bin/env bash
# A/B/C benchmark of the memory hook, measured in billed usage.
#   A = no hook            B = recallhook            C = no hook, facts retyped into the prompt
# Requires: the recallhook gateway running (`recallhook status`), `claude` on PATH,
# and the three facts stored first (`bash bench/seed.sh`).
# Usage: bash bench/run.sh [rounds]      (default 3; ~45 calls, ~8 minutes)
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/out"; mkdir -p "$OUT"; rm -f "$OUT"/[ABC]_*.json
cd "$(mktemp -d)"                      # neutral cwd: no project CLAUDE.md or files to find
export CLAUDE_CODE_DISABLE_AUTO_MEMORY=1
F="--strict-mcp-config --disable-slash-commands --model haiku --max-turns 2 --output-format json --setting-sources project"
CTX="Context: my project Atlas runs unit tests with pnpm test:unit and its dev server listens on port 4310; I name branches like feat/ABC-123-short-slug and deploy to Fly.io in the fra region; it uses PostgreSQL 16 on port 5433 in development; I use tabs, never spaces."
P=(
"What command runs the tests for Atlas, and which port does its dev server listen on? Answer in one sentence."
"Which branch naming convention do I use for Atlas, and where do I deploy it? Answer in one sentence."
"Which database does Atlas use in development and on what port? Answer in one sentence."
"Explain in two sentences what a mutex is."
"Write a Python one-liner that reverses a string. Just the code."
"What is the difference between TCP and UDP? Three sentences max."
)
claude -p "Reply with just: ok" $F < /dev/null > /dev/null 2>&1       # warm the prompt cache, discarded
for rep in $(seq 1 "${1:-3}"); do
  for i in 0 1 2 3 4 5; do
    claude -p "${P[$i]}" $F < /dev/null > "$OUT/A_${rep}_$i.json" 2>/dev/null
    claude -p "${P[$i]}" $F --settings "$HERE/settings_hook.json" < /dev/null > "$OUT/B_${rep}_$i.json" 2>/dev/null
    if [ "$i" -lt 3 ]; then claude -p "$CTX ${P[$i]}" $F < /dev/null > "$OUT/C_${rep}_$i.json" 2>/dev/null; fi
  done
done
python "$HERE/analyze.py" "$OUT"
