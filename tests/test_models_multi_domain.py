"""tests/test_models_multi_domain.py — 多域数据模型测试。"""

from typing import get_args

import pytest
from pydantic import ValidationError

from app.models.research import (
    DEFAULT_DOMAINS,
    MarketDomain,
    ResearchIntent,
    ResearchPlan,
    ToolCallPlan,
)


class TestMarketDomainTypes:
    """验证 MarketDomain 包含所有期望的值。"""

    def test_default_domains_contains_expected(self):
        assert "a_share" in DEFAULT_DOMAINS
        assert "crypto" in DEFAULT_DOMAINS
        assert "us_stock" in DEFAULT_DOMAINS

    def test_market_domain_type_accepted_values(self):
        """对 get_args(MarketDomain) 的每个合法值，pydantic 模型都必须真实接受。

        T33：旧写法 `_: MarketDomain = val` 是运行时 no-op（注解求值后即丢弃，
        不产生任何校验，"bogus" 也能通过）。这里改为真构造 ResearchIntent，
        并以类型本身（get_args）作为合法值来源，避免手工清单漂移。
        """
        members = get_args(MarketDomain)
        assert len(members) >= 7  # 类型被改成非 Literal（如 str）时 get_args 为空，不许空转通过
        for val in members:
            intent = ResearchIntent(question="问题", domain=val)
            assert intent.domain == val

    def test_market_domain_type_expected_members(self):
        """Literal 成员集合必须覆盖七个已知市场域（允许未来新增，不许悄悄删减）。"""
        assert set(get_args(MarketDomain)) >= {
            "a_share",
            "crypto",
            "hk_stock",
            "us_stock",
            "commodities",
            "macro",
            "unknown",
        }

    def test_market_domain_rejects_bogus_value(self):
        """非法域值必须被 pydantic 拒绝——类型注解不是摆设。"""
        with pytest.raises(ValidationError):
            ResearchIntent(question="问题", domain="bogus")


class TestResearchIntentMultiDomain:
    """验证 ResearchIntent 支持多域。"""

    def test_default_domain_is_a_share(self):
        intent = ResearchIntent(question="今天市场怎么样？")
        assert intent.domain == "a_share"

    def test_can_set_crypto_domain(self):
        intent = ResearchIntent(question="BTC 现在情况如何？", domain="crypto")
        assert intent.domain == "crypto"

    def test_can_set_us_stock_domain(self):
        intent = ResearchIntent(question="苹果股价走势怎样？", domain="us_stock")
        assert intent.domain == "us_stock"

    def test_can_set_macro_domain(self):
        intent = ResearchIntent(question="本周 CPI 数据发布了吗？", domain="macro")
        assert intent.domain == "macro"

    def test_task_types_extended(self):
        # 验证新增的 task 类型可以被接受
        intent1 = ResearchIntent(question="研究贵州茅台", domain="a_share", task="company_research")
        assert intent1.task == "company_research"

        intent2 = ResearchIntent(question="监控市场异常", domain="a_share", task="anomaly_detection")
        assert intent2.task == "anomaly_detection"

    def test_model_dump_and_validate_roundtrip(self):
        data = {
            "domain": "crypto",
            "task": "market_diagnosis",
            "time_scope": "today",
            "question": "BTC 健康度评估",
            "needs_comparison": True,
            "needs_evidence": True,
        }
        intent = ResearchIntent.model_validate(data)
        assert intent.domain == "crypto"
        assert intent.question == "BTC 健康度评估"
        dumped = intent.model_dump()
        assert dumped["domain"] == "crypto"


class TestResearchPlanIntegrity:
    """验证 ResearchPlan 结构完整性。"""

    def test_plan_with_steps(self):
        intent = ResearchIntent(
            question="Crypto 市场状况如何？",
            domain="crypto",
            task="market_summary",
        )
        steps = [
            ToolCallPlan(
                tool_key="snapshot",
                purpose="查看 Crypto 实时快照",
                priority="high",
            ),
            ToolCallPlan(
                tool_key="klines",
                purpose="查看近期 K 线趋势",
                priority="medium",
            ),
        ]
        plan = ResearchPlan(intent=intent, steps=steps)
        assert len(plan.steps) == 2
        assert plan.intent.domain == "crypto"
        assert plan.steps[0].tool_key == "snapshot"

    def test_plan_early_stop_flag(self):
        plan = ResearchPlan(early_stop=True)
        assert plan.early_stop is True

    def test_plan_empty_by_default(self):
        plan = ResearchPlan()
        assert len(plan.steps) == 0
        assert plan.intent.domain == "a_share"
        assert plan.early_stop is False
