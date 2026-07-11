#!/usr/bin/env bash
# 串行全量重跑 heermeng -> mengnv（各 --force 全覆盖，用新 default prompt）。
# 同一时刻只有一个 source 在跑，峰值并发 = CONC。
set -u
cd "$(dirname "$0")/.."
CONC="${CONC:-8}"

echo "==== [$(date '+%T')] 开始 heermeng（并发 $CONC）===="
python3 scripts/batch_landing_online.py --source heermeng --variant default \
  --force --concurrency "$CONC" --state data/batch_landing_heermeng_state.json
echo "==== [$(date '+%T')] heermeng 结束，开始 mengnv（并发 $CONC）===="
python3 scripts/batch_landing_online.py --source mengnv --variant default \
  --force --concurrency "$CONC" --state data/batch_landing_mengnv_state.json
echo "==== [$(date '+%T')] 全部完成 ===="
