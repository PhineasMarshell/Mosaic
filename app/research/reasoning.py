"""Reasoning Engine — 将工具结果 + Evidence → 结构化市场情报。

包含向后兼容的 LLM 输出转换逻辑：
- 旧的 evidence 格式：[{claim, evidence_ids}]
- 新的 evidence 格式：[{id, source_tool, metric, value, ...}] (EvidenceItem)
- what_changed 可能是段落字符串或数组
"""

import json
from openai import AsyncOpenAI

from app.agent.prompts import REASONING_PROMPT
from app.config import Settings
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
        is_old_format = "claim" in raw_item and not item_id

        if is_old_format:
            # 旧格式：{claim, evidence_ids}
            for eid in (ev_ids or []):
                ev = id_to_evidence.get(eid)
                if ev:
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
            if not items and claim:
                items.append(EvidenceItem(
                    id="evidence-interp",
                    source_tool="reasoning_engine",
                    metric="interpretation",
                    value=claim,
                    status="success",
                    note=claim,
                ))
        else:
            # 新格式：直接构造 EvidenceItem
            items.append(EvidenceItem(**{
                k: v for k, v in raw_item.items()
                if k in EvidenceItem.model_fields
            }))

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

        content = response.choices[0].message.content or "{}"
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Reasoning returned invalid JSON: {content}") from exc

        # ── 后处理：转换为模型可接受的格式 ──

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
            payload["confidence"] = "medium"

        # Ensure used_tools is a list
        payload["used_tools"] = _ensure_list(payload.get("used_tools"))

        return MarketIntelligence.model_validate(payload)
