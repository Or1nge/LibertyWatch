# 股本、股份权利与稀释桥受控对账 v1

本对账只解决“官方年报里能否确认一个精确的已发行普通股股数”。它不把已发行股本当作稀释后总股本，也不授权公司总市值、股东回报率或回购收益计算。

## 审计范围与结果

输入为 `equity-bridge-candidates-v1` 的 56 家公司、526 份官方 PDF，以及 `official_backfill_v1` 的来源账本。最近最多 5 个完整财年共有 277 个公司年：

- 原候选状态：`VALID=93`、`REVIEW=168`、`CONFLICT=16`；
- 其中 90 个是“官方精确到 1 股”的已发行股数候选；
- 排除 A/H 或其他多类别公司的总数/单类混用后，82 个历史已发行股数可作为“reported issued shares”保留；
- 277 个公司年都没有完成期初、期末稀释后总股本桥，因此 `diluted_total_shares` 与 `diluted_net_share_reduction` 全部为 `null`；
- 当前共有 60 个 material legal share-class 槽位，21 个精确事实获 `ACCEPT`，覆盖 18 家公司；其余 39 个槽位保持 `REVIEW/null`。

`ACCEPT` 只表示股数和法定股份类别口径得到官方精确材料支持，不表示行情、汇率、股份权利或公司市值分母已经可用。

## 当前获确认的 21 个股份类别事实

| 公司 | 证券/类别 | 已发行股数 | rights_verified | 说明 |
|---|---:|---:|---|---|
| 国电南瑞 | SH600406 / A | 8,031,756,156 | 是 | 单一 material 普通股类别 |
| 康师傅控股 | HK0322 / H | 5,636,516,360 | 是 | 单一 material 普通股类别 |
| 创科实业 | HK0669 / H | 1,829,209,941 | 是 | 单一 material 普通股类别 |
| 安琪酵母 | SH600298 / A | 867,978,471 | 是 | 单一 material 普通股类别 |
| 华鲁恒升 | SH600426 / A | 2,123,219,998 | 是 | 单一 material 普通股类别 |
| 青岛啤酒 | SH600600 / A | 709,125,943 | 否 | A/H 股数已拆分，权利映射待核 |
| 青岛啤酒 | HK00168 / H | 655,069,178 | 否 | A/H 股数已拆分，权利映射待核 |
| 福耀玻璃 | SH600660 / A | 2,002,986,332 | 是 | 年报明确 A/H 每股同额现金股利 |
| 福耀玻璃 | HK03606 / H | 606,757,200 | 是 | 439,679,600 + 65,951,600 + 101,126,000，且与公司总股本对平 |
| 晨光股份 | SH603899 / A | 920,970,377 | 是 | 单一 material 普通股类别 |
| 东阿阿胶 | SZ000423 / A | 643,976,824 | 是 | 单一 material 普通股类别 |
| 伟星股份 | SZ002003 / A | 1,188,889,653 | 是 | 单一 material 普通股类别 |
| 思源电气 | SZ002028 / A | 782,057,732 | 是 | 单一 material 普通股类别 |
| 苏泊尔 | SZ002032 / A | 801,660,653 | 是 | 单一 material 普通股类别 |
| 顺络电子 | SZ002138 / A | 806,318,354 | 是 | 单一 material 普通股类别 |
| 汉钟精机 | SZ002158 / A | 534,724,139 | 是 | 单一 material 普通股类别 |
| 顺丰控股 | SZ002352 / A | 4,799,430,409 | 否 | A/H 股数已拆分，权利映射待核 |
| 顺丰控股 | HK06936 / H | 240,000,000 | 否 | A/H 股数已拆分，权利映射待核 |
| 涪陵榨菜 | SZ002507 / A | 1,153,919,028 | 是 | 单一 material 普通股类别 |
| 豪迈科技 | SZ002595 / A | 800,000,000 | 是 | 单一 material 普通股类别 |
| 国瓷材料 | SZ300285 / A | 997,048,299 | 是 | 单一 material 普通股类别 |

`rights_verified=true/economic_rights_factor=1` 的范围仅为上述 15 个单一 material 普通股类别，以及福耀 A/H 两类。青啤和顺丰的 4 个股数事实可以写入 `issued_shares`，但权利因子仍为 `null`。

无论 `rights_verified` 是否为真，本 bundle 对 56 家公司的 `company_market_value_denominator_authorized` 均为 `false`。原因是当前价格、行情时间戳、币种转换和完整市场价值对账不在本次范围内。

## 多地上市与股份类别边界

- 青岛啤酒、福耀玻璃、顺丰控股分别建立 A、H 两个 material legal share-class 行，不允许用 A 股或 H 股一类冒充公司总股本。
- 百济神州建立 A 股和 overseas ordinary 两行，但都保持 `REVIEW/null`。年报表内 1,441,075,618 股不含全资子公司持有的 133,000,000 股激励用股份；法律已发行与享有当期表决/分红权的经济流通口径不同。
- 华住、中通和百胜的 ADS/港股是同一底层普通股的不同上市表示，不可重复计数。
- 安踏的港币、人民币交易柜台是同一法律股份类别，不可重复计数。其 2025 年年报股本表以千股列示，2,796,653,000 不能作为精确到 1 股的当前事实；已单独核实的注销事实不改变这一结论。

## 注销、发行、股权激励和转股桥

新 bundle 只读验证并引用既有 `cancellation-v1`：最近 5 财年窗口内有 5 个精确注销事实（创科 2023—2025、安踏 2024—2025）。创科年报中已明确并对平的期权行权新增股数作为 `known_issued_additions` 保存。

这些已知动作仍不是完整的稀释桥：

- 注销：有独立官方精确事实才为 `ACCEPT`，其余为 `REVIEW`；
- 发行：只保存已核实的已知组成，不能假定没有其他发行；
- 股权激励：未完成期初、期末奖励/期权/库存股/子公司持股的全量对账，统一 `REVIEW`；
- 可转债转股：未完成期初、期末潜在普通股全量对账，统一 `REVIEW`；
- 稀释后股本桥：56 家全部 `INSUFFICIENT_DATA`。

因此本阶段没有计算、写入或授权 gross buyback、合格回购、净减少系数等派生值。

## 文件与只读导入

实现位置：

- 审核配置：`config/share_capital_reconciliation_v1.json`
- 对账领域代码：`liberty_v2/share_capital_reconciliation.py`
- 纯读导入器：`liberty_v2/import_share_capital.py`
- 构建命令：`scripts/support/reconcile_share_capital.py`
- 输出：`data/shareholder-v2/reconciliation/share-capital-v1/`

输出目录包含：

```text
share-capital-v1/
├── companies/           # 56 个公司决策文件
├── report.json          # 数量、阻塞和安全汇总
├── review_basis.json    # 本次版本化审核配置副本
├── review_cases.json    # 百济等保留 REVIEW 的证据
└── manifest.json        # 全文件 SHA-256 与大小
```

纯读 API：

```python
load_confirmed_share_capital_facts(reconciliation_root, annual_report_root)
load_confirmed_issued_share_points(reconciliation_root, annual_report_root)
```

第一个 API 同时返回 `rights_verified` 与 `economic_rights_factor`；第二个只返回 21 条 `RawDataPoint`。两者都不修改 staging、数据库或旧 bundle。

## 运行与校验

```bash
cd /home/or1ngelinux/Liberty/webapp
python scripts/support/reconcile_share_capital.py
python scripts/support/reconcile_share_capital.py --verify-only
pytest -q tests/test_share_capital_reconciliation.py tests/test_import_share_capital.py
```

只有在后续补齐全部 material class 当前价格、价格时效、FX，以及官方期初/期末稀释后股本桥后，才能在另一个受控步骤中决定是否开放公司总市值和回购资格计算。
