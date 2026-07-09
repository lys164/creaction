#!/usr/bin/env bash
# 自愈守护：反复拉起萌宠 nonhuman 批处理，直到 52 张全部完成。
# 批处理脚本自带断点续跑（读 state 文件跳过已完成），被杀后本循环会自动续跑。
set -u
cd "$(dirname "$0")/.."

PY=/Library/Frameworks/Python.framework/Versions/3.11/bin/python3
STATE=data/batch_nonhuman_mengchong_state.json
IMG_DIR="/Users/a0/Downloads/萌宠素材"
TOTAL=52

export PYTHONPATH="$(pwd)"

for i in $(seq 1 100); do
  DONE=$("$PY" -c "import json;print(len(json.load(open('$STATE'))['done']))" 2>/dev/null || echo 0)
  echo "===== supervisor 第 $i 轮，已完成 $DONE/$TOTAL @ $(date '+%H:%M:%S') ====="
  if [ "$DONE" -ge "$TOTAL" ]; then
    echo "===== 全部完成，supervisor 退出 ====="
    break
  fi
  caffeinate -i "$PY" -u scripts/batch_nonhuman_online.py \
    --image-dir "$IMG_DIR" --state "$STATE" --concurrency 4
  echo "----- 批处理本轮结束(exit=$?)，2s 后检查是否需要续跑 -----"
  sleep 2
done
