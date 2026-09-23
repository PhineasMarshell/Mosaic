"""Mosaic 领域异常。

存在的唯一目的：让 HTTP 层能把「谁的错」映射到正确的状态码。

    客户端给的入参不合法        → 400
    上游 LLM 吐了我们解析不了的东西 → 502
    上游 Gateway 连不上/超时      → 504

历史上 planner / reasoning 的解析失败统一抛 ``ValueError``，被
``app/main.py`` 的 ``except ValueError`` 兜成了 400 —— 于是「模型输出坏了」
在客户端看起来像「你的请求有问题」，排查时完全指错方向。
"""


class MosaicError(Exception):
    """所有 Mosaic 领域异常的基类。"""


class LLMOutputError(MosaicError, ValueError):
    """LLM 返回了无法解析、或不符合 schema 的内容。

    刻意继承 ``ValueError``：
    - 兼容既有的 ``except ValueError`` 调用方与测试
    - ``pydantic.ValidationError`` 本身就是 ``ValueError`` 子类，
      这样 HTTP 层可以用一个 ``except LLMOutputError`` 精确拦截上游故障，
      再用 ``except ValueError`` 兜真正的入参错误。

    语义上这是上游故障 → HTTP 502，不是 400。
    """


class UpstreamTimeoutError(MosaicError):
    """一次完整调查超出了总预算（``research_budget_seconds``）。

    HTTP 层映射为 504。与 ``asyncio.TimeoutError`` 区分开，
    避免和单个工具调用的超时混淆。
    """
