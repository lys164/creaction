FROM --platform=linux/amd64 python:3.13-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8077
# 多 worker：任务状态经 SQLite（data/tasks.db）跨进程共享，轮询落到任一 worker 都可见。
# worker 数可用 POPOP_WEB_WORKERS 覆盖（默认 4）。
CMD ["sh", "-c", "python -m uvicorn app.server:app --host 0.0.0.0 --port 8077 --workers ${POPOP_WEB_WORKERS:-4}"]
