# 经营现金流与资本开支独立对账（cashflow-v1）

## 结论

本轮只复核其余 56 家公司中，候选层已经标为 `VALID` 的经营现金流和资本开支；没有把任何数值写入生产 staging。

修复两份误选年报后，候选范围为：

- 经营活动现金流量净额（CFO）：223 个公司财年；
- 资本开支：45 个公司财年；
- 合计：268 个字段。

独立复核结果：

| 字段 | ACCEPT | REJECT | REVIEW |
|---|---:|---:|---:|
| 经营活动现金流量净额 | 223 | 0 | 0 |
| 资本开支 | 45 | 0 | 0 |
| 合计 | 268 | 0 | 0 |

`ACCEPT` 在这里的含义只是“该字段完成来源对账”，不是“整家公司已经达到 shareholder-return-v2 的 `VALID` 状态”，也不会自动进入核心计算。

## 每个字段实际检查的内容

程序重新打开本期和相邻上一财年的官方 PDF，而不是只读取原候选 JSON，并逐项检查：

1. PDF 的实际 SHA-256、文件大小和页数与公司来源 manifest 完全一致；
2. 下载地址属于巨潮资讯或港交所披露易，且该文件仍是 `SELECTED_CURRENT`；
3. 文件是该发行人的完整年报，正文包含发行人名称或证券代码、财年、独立审计报告及合并/综合现金流量表；
4. 本期字段来自合并/综合现金流量表，页码、行号、原文、币种和单位均有记录；
5. 年报本期值与同财年 Futu 底稿完全相等；
6. 年报上年比较数与相邻上一年度年报本期值完全相等；
7. Futu evidence 文件 SHA-256、内部规范化 payload SHA-256、公司 ID、财年结束日和币种均一致。

金额比较使用 `Decimal` 精确相等，不设置舍入容差，也不把缺失值变成 0。

## 发现并修复的年报误选

来源审计发现两份文件虽然下载和 PDF 校验成功，但并不是完整年报：

- 山东药玻 FY2016：误选 1 页《2016年年报更正公告》；已换为 139 页《2016年年度报告》，公告号 `1203379151`。
- 公牛集团 FY2020：误选 9 页文件；已换为 223 页完整年报，公告号 `1209861217`。

旧文件仍留在来源 manifest 中，状态为 `SUPERSEDED_SOURCE_SELECTION`，便于审计，但不会再参与候选提取。完整的“更正版/修订版年报”不会因为标题含“更正”而被误排除；只有更正公告、补充公告、摘要或通函等非完整报告会被排除。

修复后重新运行官方现金流候选提取，CFO `VALID` 从 221 增至 223，资本开支仍为 45。此次对账使用的候选 manifest SHA-256 为：

```text
05122e4e54865373039e68f7af56d61a71772382f8d0210221ef1db0dce4f038
```

## 输出

```text
data/shareholder-v2/reconciliation/cashflow-v1/
├── ledger.json    # 268 条逐字段决定及三方来源证据
├── report.json    # 汇总、误选来源及替换记录
└── manifest.json  # 输出文件大小和 SHA-256
```

每条 `ledger.json` 记录都包含：本期和相邻年报的 URL、PDF SHA-256、来源 manifest SHA-256、页码、页内行号、原文摘录、数值、币种，以及 Futu 文件和 payload 的 SHA-256。

`eligible_for_core_write` 固定为 `false`。之后如需导入核心账本，必须使用单独、可回滚的导入步骤，并同时补齐租赁本金等自由现金流口径字段。

## 复现与验证

```bash
cd /home/or1ngelinux/Liberty/webapp

.venv/bin/python scripts/support/reconcile_cashflow_candidates.py run \
  --expected-cfo-valid 223 \
  --expected-capex-valid 45 \
  --workers 2

.venv/bin/python scripts/support/reconcile_cashflow_candidates.py verify
.venv/bin/python -m pytest -q tests/test_cashflow_reconciliation.py
```

本轮实际结果：292 份本期/相邻年报重新提取成功，错误 0；输出 manifest 校验 `VALID`；专项测试 `8 passed`。
