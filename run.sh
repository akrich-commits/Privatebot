#!/bin/sh
cd "$(dirname "$0")"
heart=data/heartbeat
limit=120

while true; do
  PYTHONUNBUFFERED=1 .venv/bin/python -m app &
  pid=$!
  started=$(date +%s)
  while kill -0 "$pid" 2>/dev/null; do
    sleep 30
    now=$(date +%s)
    if [ -f "$heart" ]; then
      stamp=$(stat -c %Y "$heart")
      age=$((now - stamp))
    else
      age=$((now - started))
    fi
    if [ "$age" -gt "$limit" ]; then
      echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) no heartbeat for ${age}s, killing $pid"
      kill -9 "$pid" 2>/dev/null
      break
    fi
  done
  wait "$pid" 2>/dev/null
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) bot stopped, restart in 2s"
  sleep 2
done
