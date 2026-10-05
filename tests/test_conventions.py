"""T35 机制性防线 — 测试有效性约定的机械检查。

本文件是约定的执行器（约定正文见 tests/conftest.py 模块 docstring）：
1. 每个测试模块必须有模块 docstring，声明"本文件 mock 掉了什么、因此没有
   覆盖什么"（纯单元测试声明"不 mock 任何东西"）——没有声明载体的文件
   无法通过收集。
2. 测试代码里整体替换节点 ``__call__``（monkeypatch.setattr 或属性赋值）
   的每一处都必须带 ``# T35-OK: <理由>`` 注释——被测节点禁止整体替换；
   标记强制写明"该节点不是本用例的验证目标"这一判断。

把对应防线改坏（删 docstring / 删豁免标记），本文件用例必红。
"""

import ast
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent


def test_every_test_module_declares_its_mock_scope():
    """每个测试模块必须有 docstring（声明 mock 范围与未覆盖面的载体）。"""
    missing = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if not ast.get_docstring(tree):
            missing.append(path.name)
    assert not missing, (
        "以下测试文件缺少模块 docstring。T35 约定：每个测试文件必须在顶部声明"
        '"本文件 mock 掉了什么、因此没有覆盖什么"（不打桩的写明"不 mock 任何'
        f'东西"）：{missing}'
    )


def _is_setattr_call(node: ast.AST) -> bool:
    func = getattr(node, "func", None)
    return isinstance(func, (ast.Attribute, ast.Name)) and getattr(func, "attr", getattr(func, "id", None)) == "setattr"


def _targets_call_attr(node: ast.AST) -> bool:
    """setattr(...) 的任一参数指向 __call__，或赋值目标是 <expr>.__call__。"""
    if isinstance(node, ast.Call) and _is_setattr_call(node):
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                if arg.value == "__call__" or arg.value.endswith(".__call__"):
                    return True
        for kw in node.keywords:
            if isinstance(kw.value, ast.Constant) and kw.value.value == "__call__":
                return True
    if isinstance(node, ast.Assign):
        if any(isinstance(t, ast.Attribute) and t.attr == "__call__" for t in node.targets):
            return True
    return False


def test_node_call_replacement_requires_explicit_waiver():
    """整体替换节点 __call__ 的每个替换点必须在语句范围内带 # T35-OK: 注释。

    AST 级扫描：monkeypatch.setattr（含字符串路径形式）与 ``<Class>.__call__ =``
    属性赋值都会被抓到，ruff format 折行不影响检测。
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not _targets_call_attr(node):
                continue
            span = range(node.lineno, (node.end_lineno or node.lineno) + 1)
            if any("T35-OK" in lines[lineno - 1] for lineno in span if lineno <= len(lines)):
                continue
            snippet = " ".join(lines[node.lineno - 1][:80].split())
            offenders.append(f"{path.name}:{node.lineno}: {snippet}")
    assert not offenders, (
        "以下位置整体替换了节点 __call__ 但缺少 # T35-OK: 豁免注释。T35 约定："
        "monkeypatch __call__ 只能用于'非本次验证目标'的节点，且必须写明理由"
        f"（被测节点禁止整体替换，见 tests/conftest.py）：{offenders}"
    )
