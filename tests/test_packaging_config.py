"""T37 打包契约 — pyproject.toml 的显式打包声明。

背景（T37 复现）：仓库是 flat-layout 多顶层目录，`app`（代码包）与 `memory`
（简报 JSON 数据目录，被 PEP 420 当成命名空间包）都是顶层目录。pyproject.toml
原本只有 [build-system] 没有 [tool.setuptools]，setuptools 自动发现直接报
``Multiple top-level packages discovered in a flat-layout: ['app', 'memory']``，
editable 与 wheel 安装都装不上，CI 的 `pip install -e ".[dev]"` 会在
"getting requirements to build editable" 阶段就失败。

本文件把这条契约钉成可执行断言：删掉 [tool.setuptools.packages.find] 或
[tool.setuptools.package-data] 段，下面每一条对应用例都必红。

mock 范围声明：**不 mock 任何东西**——本文件只读 pyproject.toml 与文件系统，
不触碰网络、不跑 pip、不写盘，因此不覆盖"pip 在隔离构建环境里的真实行为"
（那需要联网拉 setuptools，属于 CI 职责）。
"""

import fnmatch
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"

# 仓库根下这些目录不是 Python 包，不参与顶层包判定
_NON_PACKAGE_DIRS = {
    ".git",
    ".github",
    ".venv",
    ".tmp",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "build",
    "dist",
    "docs",
    "memory",
}


def _config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _top_level_packages() -> list[str]:
    """仓库根下带 __init__.py 的顶层包（真实包，不是 PEP 420 命名空间目录）。"""
    return sorted(
        child.name
        for child in REPO_ROOT.iterdir()
        if child.is_dir() and child.name not in _NON_PACKAGE_DIRS and (child / "__init__.py").exists()
    )


def test_packages_find_directive_is_declared():
    """必须显式声明 packages.find —— 缺它就是 T37 复现的 flat-layout 构建失败。"""
    find = _config().get("tool", {}).get("setuptools", {}).get("packages", {}).get("find")
    assert find is not None, (
        "pyproject.toml 缺少 [tool.setuptools.packages.find]：仓库是 flat-layout 多顶层目录，"
        "setuptools 自动发现会报 Multiple top-level packages 并拒绝构建（见 T37）"
    )
    assert find.get("include") == ["app*"], f"packages.find.include 应只打包 app*，实际 {find.get('include')!r}"
    excluded = find.get("exclude") or []
    for pattern in ("memory*", "tests*", "scripts*"):
        assert pattern in excluded, f"packages.find.exclude 缺少 {pattern}，实际 {excluded!r}"


def test_include_covers_every_top_level_package():
    """include 的模式必须覆盖全部真实顶层包（新增顶层包忘了加 include 就红）。"""
    include = _config()["tool"]["setuptools"]["packages"]["find"]["include"]
    top_level = _top_level_packages()
    assert top_level == ["app"], f"仓库顶层包集合变了：{top_level}，请同步 packages.find 的 include/exclude"
    uncovered = [name for name in top_level if not any(fnmatch.fnmatch(name, p) for p in include)]
    assert not uncovered, f"以下顶层包不会被打包：{uncovered}；include={include}"


def test_data_dir_is_excluded_and_is_not_a_package():
    """memory/ 是简报 JSON 数据目录：没有 __init__.py，且被显式排除出打包。"""
    memory = REPO_ROOT / "memory"
    assert memory.is_dir(), "memory/ 目录不见了"
    assert not (memory / "__init__.py").exists(), "memory/ 变成真包了，打包配置与实际状态不符"
    excluded = _config()["tool"]["setuptools"]["packages"]["find"]["exclude"]
    assert "memory*" in excluded, f"memory/ 必须排除出打包（它是数据目录），实际 exclude={excluded!r}"


def test_web_index_html_is_declared_as_package_data():
    """app/main.py 的 INDEX 在运行时读 app/web/index.html，非 editable 安装必须带上它。"""
    package_data = _config().get("tool", {}).get("setuptools", {}).get("package-data")
    assert package_data is not None, (
        "pyproject.toml 缺少 [tool.setuptools.package-data]：wheel 安装默认不打包 .py 以外的文件，"
        "app/web/index.html 会缺失，GET / 直接 500（见 T37）"
    )
    patterns = package_data.get("app.web", [])
    assert "index.html" in patterns, f"app.web 的 package-data 应含 index.html，实际 {patterns!r}"
    assert (REPO_ROOT / "app" / "web" / "index.html").is_file(), "app/web/index.html 不存在"
    main_src = (REPO_ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert '"web" / "index.html"' in main_src, "app/main.py 的 INDEX 不再指向 app/web/index.html，package-data 需同步"
