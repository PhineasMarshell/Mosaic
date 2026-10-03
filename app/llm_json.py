"""LLM JSON 输出的提取与解析 —— 三个 LLM 调用点共用一份。

以前每个调用点各有一套：

    evaluator  完整的围栏剥离 + 大括号兜底（最好的一份）
    planner    一份内联的简化版
    reasoning  什么都没有 —— 直接 json.loads(content)

后果是同样一句带 ```json 围栏的回复，planner 能救回来，reasoning 直接抛
LLMOutputError → 502，而且是在所有工具调用都已经付费之后的最后一步。
更糟的是 reasoning 的后处理不在任何 try 里，模型给出一个 evidence 元素缺 id、
或把 timestamp 写成 epoch 毫秒（pydantic v2 不做 int→str 强制转换），
都会抛裸 ValidationError，被 HTTP 层兜成 **400「你的请求有问题」**。
"""

import json
import re

from app.errors import LLMOutputError


def extract_json_text(text: str | None) -> str | None:
    """从 LLM 响应中提取**可解析**的 JSON 文本；提取不出来返回 None。

    只做提取，不做类型检查（调用方可能期待数组）。
    顺序：整体就是 JSON → Markdown 围栏内 → 首尾大括号之间。
    """
    if not text:
        return None

    text = text.strip()

    try:
        json.loads(text)
        return text
    except json.JSONDecodeError:
        pass

    match = re.search(r"(?:```(?:json)?\s*?\n)([\s\S]*?)(?:```)", text)
    if match:
        candidate = match.group(1).strip()
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


def parse_json_object(content: str | None, *, source: str) -> dict:
    """提取并解析成一个 dict。任何不合规都抛 :class:`LLMOutputError`（→ HTTP 502）。

    Args:
        content: LLM 返回的原始 message.content
        source: 出错时用来指明是哪个阶段（"Planner" / "Reasoning" / …）

    三种以前会静默通过或错误归类的情况，现在都是明确的 502：

    - **空/纯空白 content** —— 配置的 qwen3.7-flash 是 thinking model，
      在思考阶段被截断时 content 就是空串。以前 ``content or "{}"`` 会把它
      变成合法的空对象，pydantic 全默认字段照样校验通过，于是任何问题都
      静默翻转成 domain="a_share" 再触发 9 个 A 股兜底工具。
    - **解析不出 JSON** —— 以前 reasoning 直接 502，planner 能救围栏。
    - **解析出来不是 dict**（比如顶层是数组）—— 以前会在后处理里抛
      ``AttributeError: 'list' object has no attribute 'get'`` → 500。
    """
    if content is None or not content.strip():
        raise LLMOutputError(
            f"{source} returned an empty completion (thinking models do this when truncated mid-reasoning)"
        )

    cleaned = extract_json_text(content)
    if cleaned is None:
        raise LLMOutputError(f"{source} returned invalid JSON:\nraw={content!r}")

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise LLMOutputError(f"{source} returned invalid JSON:\nraw={content!r}\nerror={exc}") from exc

    if not isinstance(data, dict):
        raise LLMOutputError(
            f"{source} returned a JSON {type(data).__name__}, expected an object:\nraw={content[:500]!r}"
        )

    return data
