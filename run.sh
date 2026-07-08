#!/usr/bin/env bash
# Start the POPOP production pipeline server.
# 需要 Python 3.10+（代码使用 PEP 604 的 `X | Y` 类型标注；系统自带 3.9 会启动即崩）。
set -e
cd "$(dirname "$0")"

PORT="${PORT:-8077}"
HOST="${HOST:-127.0.0.1}"

# 加载本地 .env（若存在）：API/embedding 等运行时凭证。已被 .gitignore 忽略。
if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# 选一个 3.10+ 解释器：优先项目 venv，其次任意较新的 python3。
_is_py310() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3, 10) else 1)' 2>/dev/null; }

pick_python() {
  if [ -x ".venv/bin/python" ] && _is_py310 ".venv/bin/python"; then
    echo "$(pwd)/.venv/bin/python"; return 0
  fi
  for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && _is_py310 "$c"; then
      command -v "$c"; return 0
    fi
  done
  return 1
}

PY="$(pick_python)" || {
  echo "错误：未找到 Python 3.10+（本项目使用 3.10+ 语法，系统 python3 若为 3.9 会启动即崩）。" >&2
  echo "建议创建 venv： python3.10+ -m venv .venv && ./.venv/bin/pip install -r requirements.txt" >&2
  exit 1
}
echo "interpreter: $PY ($("$PY" --version 2>&1))"

# 首次运行安装依赖（装进所选解释器；venv 就地装，系统 python 装到 --user）。
"$PY" -c "import fastapi, uvicorn, requests, PIL, multipart, jwt, tos" 2>/dev/null || {
  if "$PY" -c 'import sys; sys.exit(0 if sys.prefix != sys.base_prefix else 1)' 2>/dev/null; then
    "$PY" -m pip install -r requirements.txt
  else
    "$PY" -m pip install --user -r requirements.txt
  fi
}

echo "POPOP pipeline -> http://${HOST}:${PORT}"
PYTHONPATH="$(pwd)" "$PY" -m uvicorn app.server:app --host "$HOST" --port "$PORT" --reload
