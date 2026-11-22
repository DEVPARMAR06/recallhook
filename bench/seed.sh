#!/usr/bin/env bash
# Store the benchmark's facts through the running gateway and wait for extraction to finish.
set -u
PORT="${RECALLHOOK_PORT:-8788}"
BASE="http://127.0.0.1:$PORT/_recallhook/api"
post() {
  curl -s -X POST -H "x-hm-client: bench" -H "content-type: application/json" \
       -d "{\"text\":\"$1\",\"session\":\"seed\"}" "$BASE/prompt" > /dev/null
}
post "My project Atlas runs its unit tests with the command pnpm test:unit and its dev server listens on port 4310."
post "For Atlas I name git branches like feat/ABC-123-short-slug and I deploy it to Fly.io in the fra region."
post "Atlas uses PostgreSQL 16 on port 5433 in development, and I always use tabs rather than spaces for indentation in Atlas."
echo "waiting for extraction (the local model works in the background)..."
for _ in $(seq 1 60); do
  s=$(curl -s "$BASE/summary")
  q=$(echo "$s" | python -c "import sys,json;d=json.load(sys.stdin);print(d['capture_queue'],d['memories'])")
  set -- $q
  [ "$1" = "0" ] && [ "$2" -ge 3 ] && break
  sleep 4
done
echo "queue/memories: $q"
curl -s "$BASE/memories" | python -c "import sys,json;[print(' -',m['memory']) for m in json.load(sys.stdin)]"
