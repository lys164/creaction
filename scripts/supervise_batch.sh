#!/usr/bin/env bash
# 守护式跑批：反复重跑某个 online 批量脚本，直到线上「待生成」清零才停。
# - 幂等：已完成的角色自动跳过；每轮开始清空 state 里的 failed，让上一轮失败者重新进待办被重试。
# - 自愈：脚本异常退出（崩溃/被杀）会自动重启，直到判定全部完成。
# 用法：
#   scripts/supervise_batch.sh landing 48
#   scripts/supervise_batch.sh posts   32
set -u
cd "$(dirname "$0")/.."

KIND="${1:?用法: supervise_batch.sh <landing|posts> <concurrency>}"
CONC="${2:?缺少并发数}"
TS() { date +%H%M%S; }

case "$KIND" in
  landing)
    SCRIPT="scripts/batch_landing_online.py"
    STATE="data/batch_landing_online_state.json"
    EXTRA="--variant default"
    ;;
  posts)
    SCRIPT="scripts/batch_posts_online.py"
    STATE="data/batch_posts_online_state.json"
    EXTRA=""
    ;;
  *) echo "未知类型: $KIND (应为 landing|posts)"; exit 2 ;;
esac

# 每轮开始清空 state.failed（保留 done），让失败者重新进入待办被重试。
clear_failed() {
  python3 - "$STATE" <<'PY'
import json, sys
p = sys.argv[1]
try:
    d = json.load(open(p))
except Exception:
    d = {"done": [], "failed": []}
d.setdefault("done", []); d["failed"] = []
json.dump(d, open(p, "w"), ensure_ascii=False, indent=1)
PY
}

round=0
while true; do
  round=$((round + 1))
  LOG="logs/${KIND}_online_$(TS).log"
  echo "==== [$KIND] 第 ${round} 轮开始 $(date '+%F %T') → $LOG ===="
  clear_failed
  PYTHONPATH=. caffeinate -i python3 -u "$SCRIPT" --source all_nonempty $EXTRA --concurrency "$CONC" 2>&1 | tee "$LOG"

  if grep -q "全部已完成" "$LOG"; then
    echo "==== [$KIND] 全部完成，守护退出 $(date '+%F %T') ===="
    break
  fi
  echo "==== [$KIND] 第 ${round} 轮结束，仍有残留，10s 后重跑 ===="
  sleep 10
done
