#!/usr/bin/env bash
# 自愈守护：反复拉起 flirt 批处理，直到全部图跑完。
# 批处理自带断点续跑（读 state 跳过已完成），被杀后本循环自动续跑。
# 剩余数由批处理脚本 --check-remaining 计算，避免内联 heredoc 在 set -u 下卡住。
set -u
cd "$(dirname "$0")/.."

PY=python3
STATE=data/batch_flirt_online_state.json
export PYTHONPATH="$(pwd)"

for i in $(seq 1 100); do
  REMAIN=$("$PY" scripts/batch_flirt_online.py --check-remaining 2>/dev/null || echo 1)
  echo "===== flirt supervisor 第 $i 轮，剩余待跑 $REMAIN @ $(date '+%H:%M:%S') ====="
  if [ "$REMAIN" -le 0 ] 2>/dev/null; then
    echo "===== 全部完成，supervisor 退出 ====="
    break
  fi
  caffeinate -i "$PY" -u scripts/batch_flirt_online.py --concurrency 1
  echo "----- 批处理本轮结束(exit=$?)，3s 后检查是否续跑 -----"
  sleep 3
done
