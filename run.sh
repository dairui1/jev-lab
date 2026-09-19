#!/usr/bin/env bash
# Run both agent sessions in parallel, then build the side-by-side viewer.
set -euo pipefail
cd "$(dirname "$0")"
set -a; . .secrets/typesafe.env; [ -f .secrets/anthropic.env ] && . .secrets/anthropic.env; set +a
mkdir -p traces

.venv/bin/python agent.py --engine jev > traces/jev.log 2>&1 &
PID_JEV=$!
.venv/bin/python agent.py --engine llm --llm-model "${LLM_MODEL:-haiku}" > traces/llm.log 2>&1 &
PID_LLM=$!

echo "jev session pid=$PID_JEV, llm session pid=$PID_LLM"
wait $PID_JEV; echo "jev done"
wait $PID_LLM; echo "llm done"

.venv/bin/python build_viewer.py
echo "open viewer.html"

cp viewer.html deploy/public/index.html
cp traces/data.js deploy/public/traces/data.js
echo "deploy/public is ready: (cd deploy && npx wrangler@latest deploy)"
