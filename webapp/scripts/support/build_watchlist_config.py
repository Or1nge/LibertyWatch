#!/usr/bin/env python3
"""Build the user-approved Liberty watchlist with optional audited targets."""

from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[3]
WEBAPP_ROOT = PROJECT_ROOT / "webapp"
ISSUER_CSV = (
    PROJECT_ROOT / "outputs" / "processed_data" / "公司指标与结论.csv"
)
ANNUAL_YIELD_CSV = (
    PROJECT_ROOT / "outputs" / "processed_data" / "年度股息回购率.csv"
)
ARCHIVED_ANNUAL_YIELD_CSV = (
    PROJECT_ROOT
    / "outputs"
    / "archive"
    / "20260727_20260731_screening"
    / "净回购分红率_逐年明细_2016_2025.csv"
)
ARCHIVED_EXTENSION_CSV = (
    PROJECT_ROOT
    / "outputs"
    / "archive"
    / "20260727_20260731_screening"
    / "53家之外_扩展龙头量化初筛_20260730.csv"
)
DEFAULT_OUTPUT = WEBAPP_ROOT / "config" / "watchlist.json"

# User-approved final order. Each tuple is name, listing, ticker, sector,
# industry. 迈瑞医疗 appeared twice in the request and is intentionally kept once.
TRACKED_SECURITIES = (
    ("长江电力", "A", "600900", "公用事业", "水电"),
    ("华能水电", "A", "600025", "公用事业", "水电"),
    ("川投能源", "A", "600674", "公用事业", "水电"),
    ("中国移动", "A", "600941", "通信", "电信运营"),
    ("腾讯控股", "H", "00700", "信息技术", "互联网平台"),
    ("美的集团", "A", "000333", "可选消费", "家电制造"),
    ("海尔智家", "A", "600690", "可选消费", "家电制造"),
    ("海信家电", "A", "000921", "可选消费", "家电制造"),
    ("苏泊尔", "A", "002032", "可选消费", "小家电"),
    ("万华化学", "A", "600309", "材料", "化工"),
    ("华鲁恒升", "A", "600426", "材料", "化工"),
    ("杭氧股份", "A", "002430", "工业", "工业气体设备"),
    ("安琪酵母", "A", "600298", "日常消费", "食品原料"),
    ("安徽合力", "A", "600761", "工业", "工业车辆"),
    ("杭叉集团", "A", "603298", "工业", "工业车辆"),
    ("中密控股", "A", "300470", "工业", "机械零部件"),
    ("宏发股份", "A", "600885", "工业", "电气部件"),
    ("法拉电子", "A", "600563", "信息技术", "电子元件"),
    ("豪迈科技", "A", "002595", "工业", "专用设备"),
    ("三花智控", "A", "002050", "工业", "热管理部件"),
    ("汇川技术", "A", "300124", "工业", "工业自动化"),
    ("公牛集团", "A", "603195", "可选消费", "电工产品"),
    ("伟星股份", "A", "002003", "可选消费", "服饰辅料"),
    ("山东药玻", "A", "600529", "医药卫生", "药用包装"),
    ("华测检测", "A", "300012", "工业", "检测服务"),
    ("伊利股份", "A", "600887", "日常消费", "乳制品"),
    ("海天味业", "A", "603288", "日常消费", "调味品"),
    ("青岛啤酒", "A", "600600", "日常消费", "啤酒"),
    ("重庆啤酒", "A", "600132", "日常消费", "啤酒"),
    ("华润三九", "A", "000999", "医药卫生", "中药与消费健康"),
    ("华润江中", "A", "600750", "医药卫生", "中药与消费健康"),
    ("迈瑞医疗", "A", "300760", "医药卫生", "医疗器械"),
    ("百胜中国", "H", "09987", "可选消费", "餐饮"),
    ("康师傅控股", "H", "00322", "日常消费", "食品饮料"),
    ("统一企业中国", "H", "00220", "日常消费", "食品饮料"),
    ("华润啤酒", "H", "00291", "日常消费", "啤酒"),
    ("申洲国际", "H", "02313", "可选消费", "纺织制造"),
    ("创科实业", "H", "00669", "工业", "工具制造"),
    ("海天国际", "H", "01882", "工业", "机械制造"),
    ("中国电信", "A", "601728", "通信", "电信运营"),
    ("国投电力", "A", "600886", "公用事业", "综合电力"),
    ("国电南瑞", "A", "600406", "工业", "电力设备"),
    ("思源电气", "A", "002028", "工业", "电力设备"),
    ("海兴电力", "A", "603556", "工业", "电力设备"),
    ("汉钟精机", "A", "002158", "工业", "压缩机"),
    ("福耀玻璃", "A", "600660", "可选消费", "汽车零部件"),
    ("新和成", "A", "002001", "材料", "精细化工"),
    ("恒立液压", "A", "601100", "工业", "液压件"),
    ("国瓷材料", "A", "300285", "材料", "先进陶瓷材料"),
    ("顺络电子", "A", "002138", "信息技术", "电子元件"),
    ("巨星科技", "A", "002444", "工业", "工具制造"),
    ("浙江鼎力", "A", "603338", "工业", "高空作业平台"),
    ("赛轮轮胎", "A", "601058", "可选消费", "轮胎"),
    ("涪陵榨菜", "A", "002507", "日常消费", "食品"),
    ("晨光股份", "A", "603899", "可选消费", "文具"),
    ("安井食品", "A", "603345", "日常消费", "速冻食品"),
    ("东阿阿胶", "A", "000423", "医药卫生", "中药"),
    ("马应龙", "A", "600993", "医药卫生", "中药"),
    ("鱼跃医疗", "A", "002223", "医药卫生", "医疗器械"),
    ("华润饮料", "H", "02460", "日常消费", "饮料"),
    ("安踏体育", "H", "02020", "可选消费", "运动服饰"),
    ("百威亚太", "H", "01876", "日常消费", "啤酒"),
    ("华住集团-S", "H", "01179", "可选消费", "酒店"),
    ("顺丰控股", "A", "002352", "工业", "物流"),
    ("中通快递-W", "H", "02057", "工业", "物流"),
    ("德昌电机控股", "H", "00179", "工业", "电机制造"),
    ("百济神州", "A", "688235", "医药卫生", "创新药"),
)
EXPECTED_SECURITY_COUNT = 67


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _existing_securities() -> list[dict[str, Any]]:
    if not DEFAULT_OUTPUT.is_file():
        return []
    payload = json.loads(DEFAULT_OUTPUT.read_text(encoding="utf-8"))
    securities = payload.get("securities")
    return securities if isinstance(securities, list) else []


def _number(value: str, *, field: str, security_id: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(
            f"{security_id} 的 {field} 不是有效数值: {value!r}"
        ) from error
    if number < 0:
        raise ValueError(f"{security_id} 的 {field} 不能为负数")
    return number


def _yield_bases() -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in _read_rows(ANNUAL_YIELD_CSV) if ANNUAL_YIELD_CSV.is_file() else []:
        grouped.setdefault(row["security_id"], []).append(row)

    result: dict[str, dict[str, Any]] = {}
    for security_id, rows in grouped.items():
        ordered = sorted(rows, key=lambda row: int(row["year"]))
        years = [int(row["year"]) for row in ordered]
        annual_returns = [
            _number(
                row["cash_dividend_per_share_cny"],
                field="cash_dividend_per_share_cny",
                security_id=security_id,
            )
            + _number(
                row["net_cancelled_buyback_per_share_cny"],
                field="net_cancelled_buyback_per_share_cny",
                security_id=security_id,
            )
            for row in ordered
        ]
        if not years:
            continue
        result[security_id] = {
            "annualAveragePerShareCny": round(
                sum(annual_returns) / len(annual_returns), 12
            ),
            "windowYears": len(years),
            "startYear": min(years),
            "endYear": max(years),
            "method": (
                "观察期内年度确认现金分红与净注销回购每股人民币金额的算术平均"
            ),
        }

    archived_grouped: dict[str, list[dict[str, str]]] = {}
    for row in _read_rows(ARCHIVED_ANNUAL_YIELD_CSV) if ARCHIVED_ANNUAL_YIELD_CSV.is_file() else []:
        if row["市场"] != "A股":
            continue
        archived_grouped.setdefault(row["证券代码"], []).append(row)
    for security_id, rows in archived_grouped.items():
        if security_id in result:
            continue
        ordered = sorted(rows, key=lambda row: int(row["年度"]))
        years = [int(row["年度"]) for row in ordered]
        returns = [
            _number(
                row["净现金回报每股_本币"],
                field="净现金回报每股_本币",
                security_id=security_id,
            )
            for row in ordered
        ]
        result[security_id] = {
            "annualAveragePerShareCny": round(sum(returns) / len(returns), 12),
            "windowYears": len(years),
            "startYear": min(years),
            "endYear": max(years),
            "method": "存档逐年明细中的A股净现金回报每股算术平均",
        }

    for row in _read_rows(ARCHIVED_EXTENSION_CSV) if ARCHIVED_EXTENSION_CSV.is_file() else []:
        security_id = f"{row['市场']}{row['代码'].zfill(6)}"
        if security_id in result or row["市场"] not in {"SH", "SZ"}:
            continue
        start_text, end_text = row["观察年份"].split("-", 1)
        result[security_id] = {
            "annualAveragePerShareCny": round(
                _number(
                    row["周期年均净回购分红_每股"],
                    field="周期年均净回购分红_每股",
                    security_id=security_id,
                ),
                12,
            ),
            "windowYears": int(row["可观察年数"]),
            "startYear": int(start_text),
            "endYear": int(end_text),
            "method": "存档扩展筛选中的A股周期年均净回购分红每股",
        }
    for security in _existing_securities():
        security_id = str(security.get("id") or "")
        basis = security.get("yieldBasis")
        if security_id and security_id not in result and isinstance(basis, dict):
            result[security_id] = dict(basis)
    return result


def _a_share_fields(code: str) -> tuple[str, str, str]:
    ticker = code.strip().zfill(6)
    if not ticker.isdigit() or len(ticker) != 6:
        raise ValueError(f"无效 A 股代码: {code!r}")
    exchange = "SZ" if ticker.startswith(("0", "3")) else "SH"
    return f"{exchange}{ticker}", f"{exchange}.{ticker}", ticker


def _h_share_fields(code: str) -> tuple[str, str, str]:
    raw = code.strip()
    if not raw.isdigit() or len(raw) > 5:
        raise ValueError(f"无效 H 股代码: {code!r}")
    futu_ticker = raw.zfill(5)
    display_ticker = f"{int(raw):05d}"
    return f"HK{int(raw):04d}", f"HK.{futu_ticker}", display_ticker


def build_watchlist() -> dict[str, Any]:
    issuers = {
        str(item.get("name")): str(item.get("issuerId"))
        for item in _existing_securities()
        if item.get("name") and item.get("issuerId")
    }
    if ISSUER_CSV.is_file():
        issuers.update(
            {
                row["company_name"]: row["issuer_id"]
                for row in _read_rows(ISSUER_CSV)
            }
        )
    yield_bases = _yield_bases()
    securities: list[dict[str, Any]] = []

    for name, listing, code, sector, industry in TRACKED_SECURITIES:
        if listing == "A":
            security_id, quote_code, ticker = _a_share_fields(code)
            market, currency = "CN", "CNY"
        elif listing == "H":
            security_id, quote_code, ticker = _h_share_fields(code)
            market, currency = "HK", "HKD"
        else:
            raise ValueError(f"不支持的上市市场: {listing}")
        yield_basis = yield_bases.get(security_id)
        security = {
                "id": security_id,
                "issuerId": issuers.get(name, security_id),
                "quoteCode": quote_code,
                "name": name,
                "ticker": ticker,
                "market": market,
                "currency": currency,
                "sector": sector,
                "industry": industry,
                "targetPrices": {
                    "watch": None,
                    "preferred": None,
                    "deep": None,
                },
                "expectedDividendYieldPct": None,
                "valuationStatus": "unconfigured",
                "metrics": {},
                "investmentThesis": [],
                "risks": [],
                "notes": "",
                "targetRevisionHistory": [],
            }
        if yield_basis is not None:
            security["yieldBasis"] = yield_basis
        securities.append(security)

    security_ids = [item["id"] for item in securities]
    quote_codes = [item["quoteCode"] for item in securities]
    if len(securities) != EXPECTED_SECURITY_COUNT:
        raise ValueError(
            f"正式观察清单必须恰好为 {EXPECTED_SECURITY_COUNT} 只，"
            f"当前为 {len(securities)}"
        )
    if len(set(security_ids)) != len(security_ids):
        raise ValueError("正式观察清单存在重复 security id")
    if len(set(quote_codes)) != len(quote_codes):
        raise ValueError("正式观察清单存在重复 Futu quoteCode")
    if len({item["issuerId"] for item in securities}) != len(securities):
        raise ValueError("正式观察清单必须每个发行人只保留一只证券")
    return {
        "schemaVersion": 1,
        "mode": "live",
        "isDemo": False,
        "title": "Liberty 长期投资观察清单",
        "description": (
            "67只正式观察标的（重复的迈瑞医疗已去重）；"
            "价格由本机 Futu OpenD 每分钟采集并推送。"
        ),
        "disclaimer": (
            "行情仅用于长期价格观察；目标价和研究字段未配置时保持为空，"
            "不构成投资建议。"
        ),
        "refreshIntervalMs": 60_000,
        "marketData": {
            "provider": "futu-opend",
            "realtime": False,
            "status": "awaiting_snapshot",
        },
        "securities": securities,
    }


def _render(payload: dict[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        indent=2,
        allow_nan=False,
    ) + "\n"


def _atomic_write(path: Path, content: str) -> None:
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成 Liberty 正式 67 只观察清单"
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help="输出 JSON；默认写入正式 watchlist.json",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验现有输出是否与生成结果一致",
    )
    args = parser.parse_args()

    content = _render(build_watchlist())
    output = args.output.resolve()
    if args.check:
        if not output.is_file() or output.read_text(encoding="utf-8") != content:
            raise SystemExit(f"观察清单需要重新生成: {output}")
        print(f"watchlist ok: 67 securities, 67 issuers, {output}")
        return 0

    _atomic_write(output, content)
    print(f"watchlist written: 67 securities, 67 issuers, {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
