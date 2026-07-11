#!/usr/bin/env bash
# 守护补封面：进程若退出(非正常完成)则自动重启续跑，直到全部处理完。
# 断点续跑由 fill_covers_online.py + data/fill_covers_state.json 保证。
set -u
cd "$(dirname "$0")/.."
IDS="${1:-/tmp/nocover.json}"
CONC="${2:-3}"
LOGDIR="logs"
mkdir -p "$LOGDIR"
STAMP=$(date +%Y%m%d)
WLOG="$LOGDIR/cover_watchdog_$STAMP.log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$WLOG"; }

log "watchdog 启动: ids=$IDS conc=$CONC"
attempt=0
while true; do
  attempt=$((attempt+1))
  # 若已无待跑(所有目标都在 done 里)，退出
  remaining=$(python3 - "$IDS" <<'PY'
import json,sys
ids=json.load(open(sys.argv[1]))
targets=ids.get('with_src',[]) if isinstance(ids,dict) else list(ids)
try:
    st=json.load(open('data/fill_covers_state.json'))
    done=set(st.get('done',[])); gaveup=set(st.get('gaveup',[]))
except Exception:
    done=set(); gaveup=set()
print(len([c for c in targets if c not in done and c not in gaveup]))
PY
)
  log "第 $attempt 轮启动，剩余待跑约 $remaining"
  if [ "$remaining" = "0" ]; then
    log "全部完成，watchdog 退出。"
    break
  fi
  RLOG="$LOGDIR/cover_run_${STAMP}_$(date +%H%M%S).log"
  PYTHONPATH=. python3 scripts/fill_covers_online.py "$IDS" --concurrency="$CONC" >"$RLOG" 2>&1
  code=$?
  log "补封面进程退出 code=$code (log=$RLOG)"
  if [ "$code" = "0" ]; then
    log "正常完成(exit 0)。"
    # 二次确认是否真的全跑完；若还有剩余则继续循环
  fi
  sleep 5
done
