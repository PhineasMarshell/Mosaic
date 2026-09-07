# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ---------- 运行阶段 ----------
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000

WORKDIR /app

# 从 builder 阶段复制已安装的包
COPY --from=builder /install /usr/local

# 拷贝应用代码
COPY . .

EXPOSE 8000

CMD ["python", "run.py"]
