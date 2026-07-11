#!/usr/bin/env bash
# 自愈守护：反复拉起 meme nonhuman 批处理，直到 60 张全部完成。
# 批处理自带断点续跑（读 state 跳过已完成），被杀后本循环自动续跑。
set -u
cd "$(dirname "$0")/.."

PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
STATE=data/batch_nonhuman_online_state.json
IMG_DIR="/Users/a0/Downloads/meme"
TOTAL=60

export PYTHONPATH="$(pwd)"

for i in $(seq 1 100); do
  DONE=$("$PY" -c "import json;print(len(json.load(open('$STATE'))['done']))" 2>/dev/null || echo 0)
  echo "===== meme supervisor 第 $i 轮，已完成 $DONE/$TOTAL @ $(date '+%H:%M:%S') ====="
  if [ "$DONE" -ge "$TOTAL" ]; then
    echo "===== meme 全部完成，supervisor 退出 ====="
    break
  fi
  caffeinate -i "$PY" -u scripts/batch_meme_online.py \
    --image-dir "$IMG_DIR" --state "$STATE" --concurrency 2
  echo "----- meme 批处理本轮结束(exit=$?)，2s 后检查续跑 -----"
  sleep 2
done
