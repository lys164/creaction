FROM --platform=linux/amd64 python:3.13-slim
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8077
CMD ["python", "-m", "uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8077"]
