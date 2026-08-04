#!/usr/bin/env python3
"""Fake Codex CLI used by tests; it never contacts a model service."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def valid_payload() -> dict:
    input_dir = Path(os.environ["CODEX_JOB_INPUT_DIR"])
    metadata = json.loads((input_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
    trigger = json.loads((input_dir / "trigger.json").read_text(encoding="utf-8"))
    company = json.loads((input_dir / "company_snapshot.json").read_text(encoding="utf-8"))
    securities = company.get("securities") or [{}]
    first = securities[0] if isinstance(securities[0], dict) else {}
    market = str(first.get("market") or "OTHER")
    if market not in {"CN", "HK", "US", "OTHER"}:
        market = "OTHER"
    payload = {
        "schema_version": "2.0",
        "prompt_version": metadata["prompt_version"],
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "analysis_id": metadata["analysis_id"],
        "analysis_mode": metadata["analysis_mode"],
        "company_id": metadata["company_id"],
        "company_name": company.get("company_name") or "测试公司",
        "ticker": str(first.get("ticker") or "TEST"),
        "market": market,
        "as_of_date": company.get("as_of_date") or "2026-08-01",
        "input_snapshot_hash": metadata["input_snapshot_hash"],
        "calculation_version": metadata["calculation_version"],
        "trigger": {
            "type": trigger.get("type") or metadata["trigger_type"],
            "summary": trigger.get("summary") or "fake Codex合法输出",
        },
        "data_issue_detected": False,
        "data_issue_notes": [],
        "one_sentence_conclusion": "该公司仍需关注分配覆盖和业务下行风险。",
        "price_assessment": "ATTRACTIVE",
        "trigger_validity": "CONFIRMED",
        "cash_return_sustainability": "MODERATE",
        "opportunity_or_trap": "MIXED",
        "verdict": "WATCH",
        "risk_overlay": "MEDIUM",
        "top_risks": [
            {
                "risk": "现金流覆盖可能下降",
                "evidence": ["结构化快照显示需持续复核覆盖能力"],
                "monitoring_condition": "连续两个完整财年经营现金流覆盖明显改善",
            }
        ],
        "facts_that_would_change_the_view": ["覆盖倍数连续改善或明显恶化"],
        "next_review_triggers": ["新年报或普通分红下降"],
        "sources": [
            {
                "title": "测试年报",
                "publisher": "测试交易所",
                "url": "https://www.sec.gov/",
                "publish_date": "2026-03-31",
                "event_date": "2025-12-31",
                "supports": "仅用于fake执行器测试",
            }
        ],
        "report_markdown": "# 定性风险复核\n\n该报告由 fake Codex 生成，仅用于测试。",
    }
    return payload



def legacy_payload() -> dict:
    input_dir = Path(os.environ["CODEX_JOB_INPUT_DIR"])
    metadata = json.loads((input_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
    trigger = json.loads((input_dir / "trigger.json").read_text(encoding="utf-8"))
    company = json.loads((input_dir / "company_snapshot.json").read_text(encoding="utf-8"))
    securities = company.get("securities") or [{}]
    first = securities[0] if isinstance(securities[0], dict) else {}
    market = str(first.get("market") or "OTHER")
    if market not in {"CN", "HK", "US", "OTHER"}:
        market = "OTHER"
    payload = {
        "schema_version": "1.1",
        "prompt_version": metadata["prompt_version"],
        "model": "gpt-5.6-sol",
        "reasoning_effort": "xhigh",
        "analysis_id": metadata["analysis_id"],
        "analysis_mode": metadata["analysis_mode"],
        "company_id": metadata["company_id"],
        "company_name": company.get("company_name") or "测试公司",
        "ticker": str(first.get("ticker") or "TEST"),
        "market": market,
        "as_of_date": company.get("as_of_date") or "2026-08-01",
        "input_snapshot_hash": metadata["input_snapshot_hash"],
        "calculation_version": metadata["calculation_version"],
        "trigger": {
            "type": trigger.get("type") or metadata["trigger_type"],
            "summary": trigger.get("summary") or "fake Codex合法输出",
        },
        "data_issue_detected": False,
        "data_issue_notes": [],
        "one_sentence_conclusion": "该公司仍需关注分配覆盖和业务下行风险。",
        "crossing_driver": "B",
        "verdict": "WATCH",
        "risk_overlay": "MEDIUM",
        "hard_veto_supported": False,
        "hard_veto_reasons": [],
        "top_risks": [
            {
                "risk": "现金流覆盖可能下降",
                "probability": "MEDIUM",
                "impact": "HIGH",
                "time_horizon": "1-3Y",
                "evidence": ["结构化快照显示需持续复核覆盖能力"],
                "monitoring_indicators": ["覆盖倍数"],
                "falsification_condition": "连续两个完整财年覆盖倍数稳定高于1.5",
            }
        ],
        "hidden_opportunities": [],
        "historical_analogs": [],
        "scenarios": {
            "bear": {
                "summary": "现金流下降并压缩分配。",
                "key_assumptions": ["需求转弱"],
                "main_failure_path": "自由现金流不足导致普通分红下降。",
            },
            "base": {"summary": "分配基本稳定。", "key_assumptions": ["现金流平稳"]},
            "bull": {"summary": "经营改善但不假设估值扩张。", "key_assumptions": ["现金流温和增长"]},
        },
        "facts_that_would_change_the_view": ["覆盖倍数连续改善或明显恶化"],
        "next_review_triggers": ["新年报或普通分红下降"],
        "sources": [
            {
                "title": "测试年报",
                "publisher": "测试交易所",
                "url": "https://www.sec.gov/",
                "publish_date": "2026-03-31",
                "event_date": "2025-12-31",
                "supports": "仅用于fake执行器测试",
            }
        ],
        "reviewed_overlay_candidates": {
            "business_durability": {
                "value": 72,
                "source": "https://www.sec.gov/",
                "as_of_date": company.get("as_of_date") or "2026-08-01",
                "expires_at": "2027-08-01",
                "reason": "主营业务需求和现金产生能力总体稳定，但仍需持续复核竞争格局与资本开支。",
                "rubric_version": "qualitative-score-rubric-v1.0.0",
                "dimension_scores": {
                    "demand_resilience": 75,
                    "competitive_position": 70,
                    "substitution_and_asset_life": 70,
                    "capital_intensity_and_cash_conversion": 73,
                },
                "red_flags": [],
            },
            "governance_capital_allocation": {
                "value": 68,
                "source": "https://www.sec.gov/",
                "as_of_date": company.get("as_of_date") or "2026-08-01",
                "expires_at": "2027-08-01",
                "reason": "现有公开资料支持基本治理纪律，但资本配置成效仍需用后续完整财年验证。",
                "rubric_version": "qualitative-score-rubric-v1.0.0",
                "dimension_scores": {
                    "shareholder_alignment": 70,
                    "capital_allocation_discipline": 65,
                    "disclosure_and_internal_controls": 70,
                    "related_party_and_minority_protection": 67,
                },
                "red_flags": [],
            },
        },
        "report_markdown": "# 定性风险复核\n\n该报告由 fake Codex 生成，仅用于测试。",
    }
    return payload

def main() -> int:
    arguments = sys.argv[1:]
    if arguments == ["--version"]:
        print("codex-cli fake-1.0")
        return 0
    if arguments == ["--help"]:
        print("--ask-for-approval --search")
        return 0
    if arguments == ["exec", "--help"]:
        print("--ephemeral --model --sandbox --json --output-schema --output-last-message")
        return 0
    if arguments == ["debug", "models"]:
        print(json.dumps({"models": [{"slug": "gpt-5.6-sol"}]}))
        return 0
    if arguments == ["login", "status"]:
        print("Logged in using fake credentials")
        return 0
    scenario = os.getenv("FAKE_CODEX_SCENARIO", "valid")
    if scenario == "timeout":
        time.sleep(float(os.getenv("FAKE_CODEX_SLEEP", "5")))
        return 0
    if scenario == "nonzero":
        print("temporary network error", file=sys.stderr)
        return 7
    if scenario == "auth_failure":
        print("authentication failed: unauthorized 401", file=sys.stderr)
        return 1
    if scenario == "model_unavailable":
        print("model gpt-5.6-sol not available", file=sys.stderr)
        return 1
    if scenario == "quota":
        print("quota exhausted: rate limit 429", file=sys.stderr)
        return 1
    _ = sys.stdin.read()
    output_index = arguments.index("--output-last-message") + 1
    output_path = Path(arguments[output_index])
    schema_index = arguments.index("--output-schema") + 1
    schema = json.loads(Path(arguments[schema_index]).read_text(encoding="utf-8"))
    schema_version = str(schema.get("properties", {}).get("schema_version", {}).get("const") or "")
    payload = legacy_payload() if schema_version.startswith("1.") else valid_payload()
    if scenario == "schema_error":
        payload.pop("sources")
    elif scenario == "wrong_company":
        payload["company_id"] = "wrong-company"
    elif scenario == "wrong_hash":
        payload["input_snapshot_hash"] = "0" * 64
    elif scenario == "wrong_ticker":
        payload["ticker"] = "WRONG"
    elif scenario == "overlay_unknown_source" and "reviewed_overlay_candidates" in payload:
        payload["reviewed_overlay_candidates"]["business_durability"]["source"] = "https://www.sec.gov/not-cited"
    elif scenario == "overlay_expiry_too_long" and "reviewed_overlay_candidates" in payload:
        payload["reviewed_overlay_candidates"]["business_durability"]["expires_at"] = "2028-08-01"
    elif scenario == "overlay_rubric_mismatch" and "reviewed_overlay_candidates" in payload:
        payload["reviewed_overlay_candidates"]["business_durability"]["value"] = 99
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"type": "thread.started", "thread_id": "fake"}))
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
