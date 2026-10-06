# Mosaic — 多阶段构建：builder 只负责出 wheel，runtime 只带运行期依赖，且不以 root 运行。
#
# 注意：不要加 "# syntax=docker/dockerfile:1" 指令——那会让 BuildKit 先去 Docker Hub
# 拉 frontend 镜像才能开始构建；本文件用到的特性（多阶段/HEALTHCHECK/COPY --from）
# 内置 frontend 全部支持，去掉它离线/弱网环境也能 build（2026-10-06 实测：
# auth.docker.io 超时导致 build 卡死在 resolve image config，删掉后正常）。
#
# 运行要点（详见 README「部署（Docker）」）：
#   1. 应用默认读 WORKDIR 下的 .env —— 容器里请用 compose 的 env_file / -e 注入，
#      不要把 .env 打进镜像（.dockerignore 已排除，Dockerfile 里也没有任何 COPY .env）。
#   2. Market Memory 是项目内的 SQLite（app/memory/storage.py 默认 <项目根>/memory/memory.db）。
#      容器里代码在 site-packages，项目根之外不可写，所以必须用 MOSAIC_MEMORY_DB
#      指到可写卷：下面默认 /data/memory.db。
#   3. MARKET_GATEWAY_MODE=mcp（.env.example 之外的默认值）需要容器里有 iiix CLI；
#      镜像里没有，所以 compose 显式给 http 模式 + MARKET_GATEWAY_API_KEY。
#   4. 容器内必须绑 0.0.0.0（app/main.py 的 __main__ 历史默认是 127.0.0.1，映射不到宿主机），
#      这里的 CMD 直接绑 0.0.0.0。

# ── 构建阶段：把依赖与应用都编成 wheel ───────────────────────────────────────
FROM python:3.13-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# 个别包在没有 cp313 预编译 wheel 时需要从源码编译；编译工具不进最终镜像。
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# 先只拷 pyproject.toml：依赖没变时可复用这一层缓存
COPY pyproject.toml README.md ./
# tests/ 与 .venv 已在 .dockerignore 里排除，这里拷进去的基本只有应用源码
COPY . .

RUN pip install --upgrade pip \
    && pip wheel --wheel-dir /wheels .

# ── 运行阶段 ────────────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST=0.0.0.0 \
    PORT=8000 \
    MOSAIC_MEMORY_DB=/data/memory.db \
    MOSAIC_DATA_DIR=/data

# 非 root 运行；/data 是 Market Memory（SQLite）落盘的可写卷。
# UID/GID 固定 10001，便于宿主机绑定时提前 `chown -R 10001:10001` 成同一属主。
RUN groupadd --gid 10001 mosaic \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin mosaic \
    && mkdir -p /data \
    && chown -R mosaic:mosaic /data

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels mosaic-market-intelligence \
    && rm -rf /wheels

# 只带一份配置模板，方便 `docker run` 时按需覆盖；真正的 .env 由运行时注入
COPY --chown=mosaic:mosaic .env.example ./.env.example

USER mosaic

EXPOSE 8000

# 就绪探针：app/main.py 的 GET /health（slim 镜像里没有 curl，用 python 打）
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.getenv('PORT', '8000') + '/health', timeout=3)"

# 绑 0.0.0.0（容器里绑 127.0.0.1 时端口映射打不通）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
