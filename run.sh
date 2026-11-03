#!/bin/sh
cd "$(dirname "$0")"
while true; do
  PYTHONUNBUFFERED=1 .venv/bin/python -m app
  echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) bot stopped, restart in 2s"
  sleep 2
done
