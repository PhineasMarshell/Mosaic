"""部署契约 — Dockerfile / .dockerignore / docker-compose.yml 与容器可写卷配置。

背景（部署准备）：容器里代码装在 site-packages，`MarketMemory` 的历史默认路径
`<项目根>/memory/memory.db` 会落到 site-packages 之外，非 root 用户写不进去；
`app/main.py` 的 `__main__` 历史上硬绑 `127.0.0.1:8000`，容器里端口映射不到；
`.env` 含真实密钥，绝不能进镜像。本文件把这几条钉成机械断言。

反向验证：删掉 Dockerfile 里的 `USER` / `EXPOSE`、把 compose 改回 `MARKET_GATEWAY_MODE=mcp`、
去掉 compose 的 `MOSAIC_MEMORY_DB`、让 `.dockerignore` 放行 `.env`、或把
`storage.py` 的配置覆盖改回"只看项目根"，对应用例都必红。

mock 范围声明：**不 mock 任何东西**——只读仓库里的 Dockerfile/.dockerignore/compose 文本，
外加一次真实的 `MarketMemory(db_path=None)` 构造（写临时目录，不触网）。因此**不覆盖**
"docker build 真的能成功"（那需要 Docker daemon，属 CI/运维职责）。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8")
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    """`get_settings()` 是 @lru_cache：不清缓存，前一个用例设的环境变量会串到下一个。"""
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ignore_patterns() -> set[str]:
    return {line.strip() for line in DOCKERIGNORE.splitlines() if line.strip() and not line.strip().startswith("#")}


# ── Dockerfile ──────────────────────────────────────────────────────────────


def test_dockerfile_runs_unprivileged_and_declares_port():
    """必须有非 root 用户与 EXPOSE 8000（否则容器以 root 跑、端口声明缺失）。"""
    assert "\nUSER mosaic" in "\n" + DOCKERFILE, "Dockerfile 没有切到非 root 用户 mosaic"
    assert "EXPOSE 8000" in DOCKERFILE, "Dockerfile 没有 EXPOSE 8000"
    assert "PYTHONUNBUFFERED=1" in DOCKERFILE, "日志需要 PYTHONUNBUFFERED=1 才能实时进 docker logs"


def test_dockerfile_has_healthcheck_hitting_health_endpoint():
    """HEALTHCHECK 必须打到 /health（app/main.py:99 的端点）。"""
    assert "HEALTHCHECK" in DOCKERFILE
    assert "/health" in DOCKERFILE, "HEALTHCHECK 没有打到 app/main.py 的 /health 端点"


def test_dockerfile_sets_container_host_port_and_memory_db():
    """容器里必须绑 0.0.0.0（127.0.0.1 映射不到）且把 memory.db 指到可写卷。"""
    assert "HOST=0.0.0.0" in DOCKERFILE, "容器内必须 HOST=0.0.0.0，否则端口映射不通"
    assert "MOSAIC_MEMORY_DB=" in DOCKERFILE, "Dockerfile 未把 memory.db 指到可写卷"
    assert "MOSAIC_DATA_DIR=" in DOCKERFILE, "Dockerfile 未把运行期数据目录（简报 JSON）指到可写卷"
    assert "/data" in DOCKERFILE, "缺少可写卷目录 /data"


def test_dockerfile_never_copies_dotenv():
    """`.env` 含真实密钥，绝不能 COPY 进镜像。"""
    for line in DOCKERFILE.splitlines():
        stripped = line.strip()
        if not stripped.startswith("COPY"):
            continue
        assert ".env" not in stripped.split(), f"这行 COPY 把 .env 带进镜像了：{stripped}"


# ── .dockerignore ───────────────────────────────────────────────────────────


def test_dockerignore_excludes_secrets_state_and_dev_cruft():
    """构建上下文必须排除密钥、虚拟环境、运行时库、测试与缓存。"""
    patterns = _ignore_patterns()
    for required in (".env", ".git", ".venv", ".tmp", "memory", "tests", ".pytest_cache", "*.egg-info"):
        assert required in patterns, f".dockerignore 缺少 {required!r}（当前：{sorted(patterns)}）"


# ── docker-compose.yml ──────────────────────────────────────────────────────


def test_compose_builds_this_dockerfile_and_maps_the_port():
    assert COMPOSE_FILE.is_file(), "缺少 docker-compose.yml"
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert "dockerfile: Dockerfile" in text
    assert "8000" in text and "ports:" in text, "compose 未发布 8000 端口"


def test_compose_uses_http_gateway_and_writable_memory_volume():
    """本地 .env 常是 mcp 模式（容器内没有 iiix CLI），compose 必须显式给 http 模式；
    同时必须挂可写卷并把 memory.db 指进卷里。"""
    text = COMPOSE_FILE.read_text(encoding="utf-8")
    assert '"MARKET_GATEWAY_MODE=http"' in text or "MARKET_GATEWAY_MODE=http" in text, (
        "compose 必须显式声明 MARKET_GATEWAY_MODE=http（容器里没有 iiix MCP CLI）"
    )
    assert "MOSAIC_MEMORY_DB=/data/memory.db" in text, "compose 未把 memory.db 指到卷里"
    assert "MOSAIC_DATA_DIR=/data" in text, "compose 未把运行期数据目录指到卷里"
    assert "volumes:" in text and "/data" in text, "compose 未挂 /data 卷"


# ── 容器可写卷配置（代码侧） ────────────────────────────────────────────────


def test_memory_db_path_can_be_redirected_by_settings(monkeypatch, tmp_path):
    """`MOSAIC_MEMORY_DB` 必须真的被 MarketMemory(db_path=None) 采纳。"""
    import app.memory.storage as storage

    target = tmp_path / "vol" / "memory.db"
    monkeypatch.setenv("MOSAIC_MEMORY_DB", str(target))
    mem = storage.MarketMemory()
    try:
        assert mem.db_path == target, f"配置覆盖没生效：拿到 {mem.db_path}"
        assert target.parent.is_dir(), "没有按配置创建上级目录"
    finally:
        mem.close()


def test_memory_db_path_falls_back_to_project_root(monkeypatch):
    """没配置 `MOSAIC_MEMORY_DB` 时保持历史行为：项目根下 memory/memory.db。"""
    import app.memory.storage as storage

    monkeypatch.delenv("MOSAIC_MEMORY_DB", raising=False)
    assert storage._configured_db_path() is None
    monkeypatch.setattr(storage, "_configured_db_path", lambda: None)
    created: list[Path] = []
    real_mkdir = Path.mkdir

    def spy(self, *args, **kwargs):
        created.append(self)
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", spy)
    try:
        mem = storage.MarketMemory()
    finally:
        monkeypatch.setattr(Path, "mkdir", real_mkdir)
    try:
        assert mem.db_path == REPO_ROOT / "memory" / "memory.db", f"默认路径漂了：{mem.db_path}"
        assert REPO_ROOT / "memory" in created, "没有去创建默认的 memory/ 目录"
    finally:
        mem.close()


def test_brief_json_dir_follows_data_dir_override(monkeypatch, tmp_path):
    """定时简报 JSON 也必须落到可写卷：`MOSAIC_DATA_DIR` 覆盖 `_brief_file_dir`。

    容器里 app/scheduler/briefs.py 的历史路径 `<项目根>/memory/<morning|evening>/`
    落在 site-packages 之外，非 root 进程写不进去（简报会静默保存失败）。
    """
    import app.memory.storage as storage
    import app.scheduler.briefs as briefs

    monkeypatch.setenv("MOSAIC_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MOSAIC_MEMORY_DB", raising=False)
    assert storage.data_dir() == tmp_path, f"MOSAIC_DATA_DIR 未生效：{storage.data_dir()}"
    d = briefs._brief_file_dir("morning")
    assert d == tmp_path / "morning", f"简报目录没跟着数据目录走：{d}"
    assert d.is_dir(), "没有按配置创建简报目录"
