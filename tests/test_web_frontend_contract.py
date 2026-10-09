"""T25 契约：前端 index.html 的并发/健壮性最小防线（文本断言）。

前端没有 JS 测试环境——按计划用「文本契约测试」锁住关键反模式的消失
与新防护结构的存在。**改动 index.html 的 ask()/renderResult 前先读本文件**，
每条断言对应一个真实事故场景：

- 全局单值兜底计时器：并发两条 SSE 时，先结束者的 finally 会 clearTimeout
  掉后到者的唯一中止手段 → 后到请求只能等服务端超时；
- ``severity.toUpperCase()`` 直接调用：severity 是后端透传的自由字段，
  数字/布尔会抛 TypeError → 整份三分钟报告被 catch 当"调查失败"丢弃；
- loader 按 id 全局删除：会误删别的请求的 loader 行。
"""

from pathlib import Path

INDEX = Path(__file__).resolve().parents[1] / "app" / "web" / "index.html"


def _src() -> str:
    return INDEX.read_text(encoding="utf-8")


def test_no_global_error_timeout_variable():
    """全局单值兜底计时器必须消失，改为按请求持有（activeRequest）。"""
    src = _src()
    assert "lastErrorTimeout" not in src, "全局单值计时器必须删除（T25：并发互踩）"
    assert "let activeRequest = null;" in src


def test_error_timeout_is_request_local():
    """兜底计时器必须是 ask() 的局部变量，finally 只 clearTimeout 自己的。"""
    src = _src()
    assert "const errorTimeout = setTimeout(" in src
    assert "clearTimeout(errorTimeout);" in src


def test_ask_has_reentry_guard():
    """调查中追问 → 静默中止旧请求（superseded + abort），绝不并发跑两条。"""
    src = _src()
    assert "if (activeRequest) {" in src
    assert "activeRequest.superseded = true;" in src
    assert "activeRequest.controller.abort();" in src
    # 被接替的旧请求必须静默收场，不许再弹"调查失败"气泡
    assert "if (state.superseded) return;" in src


def test_remove_loader_scoped_to_request_row():
    """removeLoader 只删调用方自己的 loader 行（addAILoader 返回行元素）。"""
    src = _src()
    assert "function removeLoader(row)" in src
    assert "const loaderRow = addAILoader('正在调查…');" in src
    assert "removeLoader(loaderRow);" in src
    assert "removeLoader(activeRequest.loaderRow);" in src


def test_severity_whitelist_rendering():
    """severity 走白名单渲染：数字/布尔不再触发 toUpperCase() TypeError。"""
    src = _src()
    assert "a.severity.toUpperCase" not in src
    assert "severityBadge(a.severity)" in src
    assert "{ low: 'low', medium: 'medium', high: 'high', critical: 'critical' }" in src


def test_anomaly_render_degrades_per_item():
    """单条异常数据渲染失败只降级自己那张卡，不毁整份报告。"""
    src = _src()
    assert "anomaly card render failed" in src
    # map 的模板串里必须有 try/catch（旧实现是裸 map 模板串）
    anomalies_block = src[src.index("report.anomalies || []).map") :]
    anomalies_block = anomalies_block[: anomalies_block.index(").join('')")]
    assert "try {" in anomalies_block
    assert "} catch (e) {" in anomalies_block


def test_render_result_gates_on_delivery_status():
    """阶段 5：未经审计的结果不得被渲染成正常报告。

    锁住"调用方只能基于 delivery_status 判断可信"这条契约——旧实现直接
    ``addAIResponse(report)``，blocked 的结果会被当成一次正常完成的研究。
    """
    src = _src()
    assert "const delivery = data.delivery_status;" in src
    assert "delivery !== 'verified' && delivery !== 'degraded'" in src
    # 拦截分支必须在取 report / 渲染之前 return
    blocked_branch = src[
        src.index("const delivery = data.delivery_status;") : src.index("const report = data.report || data;")
    ]
    assert "return;" in blocked_branch
    assert "addAIResponse(" not in blocked_branch
    assert "delivery === 'degraded'" in src


def test_frontend_never_infers_trust_from_empty_errors():
    """前端不得把 errors 为空当作可信信号（它只是"没报错"）。"""
    src = _src()
    assert "if (data.errors" not in src
    assert "errors.length === 0" not in src
