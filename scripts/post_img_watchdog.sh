#!/usr/bin/env bash
# 守护补帖子图：进程退出(非完成)则自动重启续跑，直到全部处理完。
# 断点续跑由 fill_post_images_online.py + data/fill_posts_state.json 保证。
set -u
cd "$(dirname "$0")/.."
JOBS="${1:-/tmp/posts_to_fill.json}"
CONC="${2:-2}"
LOGDIR="logs"
mkdir -p "$LOGDIR"
STAMP=$(date +%Y%m%d)
WLOG="$LOGDIR/post_img_watchdog_$STAMP.log"

log() { echo "[$(date +%H:%M:%S)] $*" | tee -a "$WLOG"; }

log "watchdog 启动: jobs=$JOBS conc=$CONC"
attempt=0
while true; do
  attempt=$((attempt+1))
  remaining=$(python3 - "$JOBS" <<'PY'
import json,sys
jobs=json.load(open(sys.argv[1]))
allk=set(f"{c}/{p}" for c,ps in jobs.items() for p in ps)
try:
    st=json.load(open('data/fill_posts_state.json'))
    done=set(st.get('done',[])); gaveup=set(st.get('gaveup',[]))
except Exception:
    done=set(); gaveup=set()
print(len([k for k in allk if k not in done and k not in gaveup]))
PY
)
  log "第 $attempt 轮启动，剩余待跑约 $remaining"
  if [ "$remaining" = "0" ]; then
    log "全部完成，watchdog 退出。"
    break
  fi
  RLOG="$LOGDIR/post_img_run_${STAMP}_$(date +%H%M%S).log"
  PYTHONPATH=. python3 scripts/fill_post_images_online.py "$JOBS" --concurrency="$CONC" >"$RLOG" 2>&1
  code=$?
  log "补帖子图进程退出 code=$code (log=$RLOG)"
  sleep 5
done
