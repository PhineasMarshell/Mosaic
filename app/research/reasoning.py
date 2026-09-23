"""Reasoning Engine — 将工具结果 + Evidence → 结构化市场情报。

包含向后兼容的 LLM 输出转换逻辑：
- 旧的 evidence 格式：[{claim, evidence_ids}]
- 新的 evidence 格式：[{id, source_tool, metric, value, ...}] (EvidenceItem)
- what_changed 可能是段落字符串或数组
"""

import json
from openai import AsyncOpenAI
from pydantic import ValidationError

from app.agent.prompts import REASONING_PROMPT
from app.config import Settings
from app.errors import LLMOutputError
from app.llm_json import parse_json_object
from app.models.evidence import Evidence
from app.models.market import ToolResult
from app.models.response import EvidenceItem, MarketIntelligence


def _ensure_list(val, default=None):
    """确保值是列表（LLM 可能返回字符串）。"""
    if val is None:
        return default or []
    if isinstance(val, list):
        return val
    return [val]


def _parse_evidence(raw_evidence: list, original_evidence: list[Evidence]) -> list[EvidenceItem]:
    """将 LLM 返回的 evidence 转换为 EvidenceItem 列表。

    支持两种格式：
    1. 新格式: [{id, source_tool, metric, value, domain, status, partial, timestamp, source, note}]
    2. 旧格式: [{claim, evidence_ids}] → 映射到原始 Evidence 数据

    无论哪种格式，**value/status/partial/source_tool/domain/metric 等核心字段**都来自
    真实工具调用结果（original_evidence），不是 LLM 说的。LLM 只负责提供 note / interpretation
    等辅助文字。这样防止 LLM 编造一个 "evidence-999" 并赋予虚假的 high 置信度数值——
    这是审计发现的高危问题：UI 上那个证据面板和置信度条目前是模型的作文，不是数据的呈现。

    当原始证据列表不可用时，回退为用证据ID构建占位 EvidenceItem。
    """
    items: list[EvidenceItem] = []

    # 建立 evidence_id → Evidence 的查找表
    id_to_evidence = {e.id: e for e in original_evidence}

    for raw_item in (raw_evidence or []):
        if not isinstance(raw_item, dict):
            continue

        item_id = raw_item.get("id")
        claim = raw_item.get("claim")
        ev_ids = raw_item.get("evidence_ids", [])

        # ── 识别是否为旧格式（有 claim 但没有 id）──
        is_old_format = bool(claim) and not item_id

        if is_old_format:
            # 旧格式：{claim, evidence_ids}
            matched_count = 0
            for eid in (ev_ids or []):
                ev = id_to_evidence.get(eid)
                if ev:
                    matched_count += 1
                    items.append(EvidenceItem(
                        id=eid,
                        source_tool=ev.source_tool,
                        domain=ev.domain,
                        metric=ev.metric,
                        value=ev.value,
                        timestamp=ev.timestamp,
                        source=ev.source,
                        status=ev.status,
                        partial=ev.partial,
                        note=f"{claim} | {ev.note}" if ev.note else claim,
                    ))
            # 如果没有匹配的证据IDs，至少创建一条说明性证据
            # （注意：matched_count 按每个 claim 单独计数，不是全局累计 —— 避免漏掉后续 claim）
            if matched_count == 0 and claim:
                items.append(EvidenceItem(
                    id="evidence-interp",
                    source_tool="reasoning_engine",
                    metric="interpretation",
                    value=claim,
                    status="error",       # 没有证据支撑，标记为错误而非成功
                    partial=True,
                    note=claim,
                ))
        else:
            # 新格式：从真实 Evidence 取 value/status/partial/source_tool/domain/metric，
            # LLM 只负责提供 note / interpretation。未知 id → 丢弃（不凭空编造证据）。
            # 这堵死了审计发现的严重缺陷：LLM 可以给不存在的 evidence-id 赋予任意
            # 数值和 confident status，而 UI 会渲染成 "数据高度一致"。
            if not item_id:
                # 没 id 的无法匹配真实数据 → 丢弃而不是生成假 evidence
                continue
            original_ev = id_to_evidence.get(item_id)
            if original_ev is None:
                # LLM 提到了一个不存在于证据链中的 id → 可能是幻觉
                # （比如报告说"我的模型能直接看到所有市场数据，不需要调用工具"）
                continue

            llm_note = raw_item.get("note")
            interpretation = raw_item.get("interpretation") or ""
            combined_note = f"{llm_note} — {interpretation}".strip() if llm_note and interpretation else (llm_note or interpretation)

            items.append(EvidenceItem(
                id=item_id,
                source_tool=original_ev.source_tool,
                domain=original_ev.domain,
                metric=original_ev.metric,
                value=original_ev.value,
                timestamp=original_ev.timestamp,
                source=original_ev.source,
                status=original_ev.status,
                partial=original_ev.partial,
                note=combined_note if combined_note else None,
            ))

    return items


class ReasoningEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client = AsyncOpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            timeout=settings.llm_timeout_seconds,
        )

    async def reason(
        self,
        question: str,
        results: list[ToolResult],
        evidence: list[Evidence],
        history_context: str = "",
    ) -> MarketIntelligence:
        normalized_data = [
            datum.model_dump()
            for result in results
            for datum in result.normalized
        ]

        prompt = REASONING_PROMPT.format(
            question=question,
            data=json.dumps(normalized_data, ensure_ascii=False, default=str),
            evidence=json.dumps(
                [x.model_dump() for x in evidence],
                ensure_ascii=False,
                default=str,
            ),
            history_context=history_context,
        )

        response = await self.client.chat.completions.create(
            model=self.settings.openai_model,
            messages=[
                {
                    "role": "system",
                    "content": "你必须只返回合法 JSON。",
                },
                {"role": "user", "content": prompt},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )

        content = response.choices[0].message.content
        # 空 completion / ```json 围栏 / 顶层数组，三种都在 parse_json_object 里
        # 统一变成 LLMOutputError（→ 502）。以前这里只有裸 json.loads：
        # 围栏直接 502（而且是在所有工具调用都已付费之后），数组会在下面的
        # payload.get(…) 上抛 AttributeError → 500。
        payload = parse_json_object(content, source="Reasoning")

        # ── 后处理：转换为模型可接受的格式 ──
        #
        # 整段必须包在 try 里。_parse_evidence 会 EvidenceItem(**…) 构造 pydantic
        # 模型，而以下两种**极常见**的模型输出会抛裸 ValidationError：
        #   - evidence 元素缺 id
        #   - timestamp 写成 epoch 毫秒（pydantic v2 不做 int→str 强制转换）
        # 裸 ValidationError 是 ValueError 的子类，会被 HTTP 层的 except ValueError
        # 兜成 400「你的请求有问题」—— 把上游模型的毛病甩锅给客户端。
        try:
            # Ensure arrays are actually lists
            for key in ("why", "strong_areas", "what_changed", "what_matters", "risks", "data_caveats"):
                payload[key] = _ensure_list(payload.get(key))

            # Process evidence with backward compatibility
            raw_evidence = payload.get("evidence", [])
            parsed_evidence = _parse_evidence(raw_evidence, evidence)
            payload["evidence"] = [ei.model_dump() for ei in parsed_evidence]

            # Ensure confidence is valid
            conf = payload.get("confidence")
            if conf not in ("high", "medium", "low"):
                # 默认 low 而不是 medium：缺失置信度时应表示"没有足够把握"，
                # 不是"中等把握"。medium 会让用户看到"数据高度一致"的错觉。
                payload["confidence"] = "low"

            # Ensure used_tools is a list
            payload["used_tools"] = _ensure_list(payload.get("used_tools"))

            return MarketIntelligence.model_validate(payload)
        except ValidationError as exc:
            # 后处理已尽力兜底，仍不符合 schema → 上游模型的问题，不是客户端入参问题
            raise LLMOutputError(
                f"Reasoning returned JSON that violates the schema: {exc}"
            ) from exc
        except (TypeError, AttributeError, KeyError) as exc:
            raise LLMOutputError(
                f"Reasoning returned JSON that could not be post-processed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
