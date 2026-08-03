# Liberty shareholder-return v2.1 实用化方案

状态：实施基线；计算版本候选 `shareholder-return-v2.1.0`

本方案把“投资指标是否足以用于观察”与“数据是否达到完整审计级验证”彻底分离。核心原则是：严重身份、财年、币种、单位、数量级和来源冲突继续硬阻断；其余缺口通过明确代理、保守默认、置信度扣减和风险提示处理。浏览器只展示后端发布值，不计算核心指标。

## 1. 执行摘要

v2.1 采用三个互相独立的输出维度：

1. `RI`：0—100，衡量回报吸引力与分配质量，越高越好；
2. `ERI`：0—100，衡量入手与长期持有风险，越低越好；
3. `data_confidence`：0—100，衡量当前结论的数据覆盖、来源质量和代理程度，不能进入 RI 或 ERI 权重。

公司数据状态改为 `BLOCKED / ESTIMATED / CALCULABLE / VERIFIED`。行情新鲜度另设 `CURRENT / STALE_LAST_GOOD`，不再与数据质量混在同一个枚举中。

缺少稀释后股本桥时，原始回购字段仍为未知；仅在评分计算层把“本期可计入回购贡献”保守设为 0，并显示“未验证回购未计入”，普通现金分红继续计算。缺少租赁本金时允许使用 `经营现金流－资本开支`，但采用更低的现金流容量折扣并扣减置信度。缺少完整 A/H 股本时优先使用有来源、币种和时间戳的行情商公司总市值；仅覆盖部分股份类别时，只有在经济覆盖比例已验证且不低于 80% 时才允许保守放大。

RI/ERI 不使用“剩余权重重标”。缺少定性分项时，RI 使用中性默认 50，ERI 使用谨慎默认 60；同时扣减置信度。原因是剩余权重重标会让缺失风险信息被其他优秀指标替代，系统性抬高评分。

## 2. 对当前设计过严之处的诊断

当前实现存在五个耦合问题：

1. 最近五年任一年度缺少稀释后股本变化，`qB` 即不可计算，继而整段普通分红历史也无法形成 `X_t`；
2. 分配质量要求六个分项全部存在，ERI 要求八个分项全部存在，任何低权重字段缺失都会使总分为空；
3. `required_provenance_field_ids()` 把未实际使用的特别股息、回购、增长、估值和四项对账字段全部列为强制来源字段；
4. 四项对账缺输入与真正对账冲突同为 `ERROR`，并统一清空 RI；
5. `VALID` 同时代表可计算、推荐完整、行业适配器完整、来源完备和会计对账通过，导致“可以合理用于筛选”与“达到审计级复核”无法区分。

v2.1 将这五类条件拆成：致命完整性阻断、指标最低数据集、代理值、评分缺失处理、数据置信度、投资风险否决和发布新鲜度七个独立层次。

## 3. 新的数据状态分层

### 3.1 数据状态

| 状态 | 精确定义 | 是否发布新 RI/ERI | 投资分类上限 |
|---|---|---:|---|
| `BLOCKED` | 存在致命完整性问题；或最低可计算数据集不满足；或置信度低于 35 | 否。可继续展示上一合法快照，并标记 `STALE_LAST_GOOD` | 不给 A/B/C/D，显示“—” |
| `ESTIMATED` | 最低数据集满足，RI/ERI 可计算；但使用至少一个高影响代理，或置信度为 35—59.9 | 是 | 最高 C |
| `CALCULABLE` | 核心分红、覆盖、估值和市值可计算；没有致命冲突；非关键字段允许缺失；置信度为 60—84.9 | 是 | 可达 A/B |
| `VERIFIED` | 置信度不低于 85；核心输入为直接值或由直接值确定性推导；无高影响代理；适用的核心对账已通过或有明确不适用理由 | 是 | 可达 A/B |

`ESTIMATED` 不是低质量数据的委婉表达，而是明确告诉用户：分数可用于排序和补数优先级，不可作为高确信度买入结论。

### 3.2 指标级取值基础

每个指标增加 `basis`：

- `DIRECT`：直接披露或行情商直接公司级值；
- `DERIVED`：仅由已接受的直接值确定性计算；
- `PROXY`：使用版本化替代口径；
- `CONSERVATIVE_DEFAULT`：未知不被冒充为事实，只在评分层采用明确保守默认；
- `UNAVAILABLE`：不可用，不携带数值。

同时保留原始字段的 `data_status`。例如稀释后股本桥缺失时：

- `diluted_net_share_reduction.value = null`，状态 `MISSING`；
- `eligible_buyback.value = null`，状态 `UNAVAILABLE`；
- `credited_buyback_for_score.value = 0`，基础 `CONSERVATIVE_DEFAULT`，原因“未验证回购不计入”。

### 3.3 新鲜度

- `CURRENT`：核心行情在政策允许时限内；
- `STALE_LAST_GOOD`：当前重算被阻断，发布上一合法快照并显示其日期；
- 行情超过 24 小时但不超过 72 小时：允许显示，置信度减 5；
- 行情超过 72 小时：当前收益率和 RI 阻断。

## 4. 必填、推荐和可选字段

### 4.1 最低必填字段

| 领域 | 最低要求 | 允许的替代 |
|---|---|---|
| 身份 | `company_id`、证券映射、财年、币种、单位和来源可确认 | 不允许代理 |
| 市值 | 当前公司总市值；或所有重要股份类别的价格×股数×汇率 | 行情商公司总市值；有验证覆盖率的股份类别放大 |
| 普通分红 | 至少 2 个已结束完整财年，且为已支付、股东批准或形成明确法律承诺的普通现金分红 | 不允许用拟派股息；可由每股分红×有权股份确定性推导 |
| 覆盖能力 | 普通非金融企业至少 2 年经营现金流和资本开支；银行/保险至少有对应资本生成与缓冲核心指标 | 非金融企业租赁本金缺失时用简化 FCF |
| 资产负债表 | 至少一个适用的核心风险指标 | 普通企业可用净现金/净负债、净负债/EBITDA；银行用 CET1 缓冲；保险用偿付能力缓冲 |
| 当前估值 | 至少一个当前估值指标或行业估值分位 | 可用 PE、PB、EV/EBITDA、P/FCF、P/EV 或行业分位；必须注明口径 |
| 来源 | 每个进入核心计算的直接值或代理值有来源、日期、币种、单位和财年/时点 | 不允许无来源数值 |

至少 2 年分红和 2 年覆盖数据是最低门槛。增长序列、回购股本桥和全部定性风险不是硬门槛。

### 4.2 推荐字段

- 3—5 年普通现金分红；
- 3—5 年经营现金流与资本开支或金融资本生成；
- 连续 3—5 年标准化 FCF/资本生成增长；
- 完整 A/H 及其他重要股份类别市值；
- 2 年以上净负债或资本缓冲历史；
- 当前与历史可比估值；
- 业务耐久度、治理与资本配置及三项结构风险；
- 七项否决配置的有来源状态。

### 4.3 可选字段

- 稀释后股本桥、注销回购和回购现金对账；
- 特别股息及资产出售分配；
- 8—10 年历史；
- 每个证券的 4% 参考价；
- 四项完整会计对账；
- Codex 定性研究和异常解释。

### 4.4 代理值与置信度扣减

| 代理或缺口 | 计算处理 | 置信度扣减 |
|---|---|---:|
| 行情商公司总市值 | 直接作为公司级分母，要求币种、时间戳和来源 | 4 |
| 已覆盖股份类别保守放大 | 覆盖率需已验证且 ≥80%；`MCap = covered_cap / coverage_ratio × 1.05` | 8 |
| 租赁本金缺失 | `FCF_simple = OCF - Capex`，容量折扣由 90% 降至 85% | 5 |
| 稀释后股本桥缺失 | 回购原始值保持未知；评分层不计入回购 | 3 |
| 仅 2 年普通分红 | 允许 H、趋势和稳定性计算，历史上限收紧 | 8 |
| 仅 3—4 年普通分红 | 允许计算 | 4 |
| 仅 2 年覆盖数据 | 使用两年中位数 | 6 |
| 仅 3 年覆盖数据 | 使用三年中位数 | 3 |
| 2—3 年增长序列 | 正增长贡献上限降至 2%，负增长完整计入 | 5 |
| 无可用增长序列 | `g_cons_for_score=0`，标记“增长未获确认” | 8 |
| 行业估值分位/同业中位数 | 使用代理估值拖累和估值陷阱风险 | 4 |
| 只有当前估值、无比较基准 | 估值拖累保守设为 -0.5%，估值风险默认 60 | 7 |
| 业务耐久度缺失 | RI 中性默认 50 | 5 |
| 治理与资本配置缺失 | RI 中性默认 50；ERI 治理风险默认 60 | 4 |
| 每个其他 ERI 定性分项缺失 | 风险默认 60 | 每项 3，合计最多 9 |
| 每项对账未运行 | 不阻断，仅警告 | 每项 1，合计最多 4 |
| 对账差异为 2%—5% | 保留值并警告 | 每项 1 |
| 对账差异为 5%—10% | 采用较保守值并警告 | 每项 3 |
| 每项否决状态未知 | ERI 增加未知风险，另扣置信度 | 每项 1，合计最多 5 |
| 核心字段只有单一非官方来源 | 允许受控使用 | 每个领域 2，合计最多 6 |
| 行情 24—72 小时 | 允许展示但标记过期 | 5 |

同一根因不得重复扣分。例如“行情商总市值”已经扣 4，不再因它同时是单一来源重复扣分。

## 5. 简化后的 RI 公式

### 5.1 年度有效分配

```text
credited_buyback_t = verified_eligible_buyback_t，若无法验证则评分层为0
X_t = ordinary_cash_dividend_t + qB × credited_buyback_t
```

未验证回购为 0 仅是评分政策，不改变原始未知状态。

### 5.2 历史保守分配 H

沿用现有非对称历史公式：

```text
R2 = 0.65 × X_latest + 0.35 × X_previous
M5 = median(latest up to 5 X)
T10 = winsorized mean(latest up to 10 X)
```

历史长度分支沿用现有 H 逻辑。推荐分历史上限改为：

| 完整财年数 | RI 上限 |
|---:|---:|
| 少于 2 年 | 不计算 |
| 2 年 | 70 |
| 3—4 年 | 85 |
| 5 年及以上 | 100 |

### 5.3 可持续分配与 SSY

普通非金融企业：

```text
FCF_t = OCF_t - Capex_t - LeasePrincipal_t
FCF5 = median(available 2 to 5 complete fiscal years)
S = max(0, min(H, haircut × max(FCF5, 0)))
```

租赁本金完整时 `haircut=0.90`；任一用于中位数的财年租赁本金缺失时使用 `OCF-Capex`，并将 `haircut=0.85`。

```text
SSY = S / company_market_cap
```

银行和保险继续使用资本生成/自由盈余适配器，不允许回退到普通 FCF。

### 5.4 保守增长与估值调整

增长：

- 4—5 年连续序列：沿用稳健增长，正贡献上限 3%，负增长完整计入；
- 2—3 年序列：正贡献上限 2%，负增长完整计入；
- 无序列：评分层 `g_cons=0`，并标记 `CONSERVATIVE_DEFAULT`。

可比历史估值：

```text
valuation_adjustment = min(0, (historical_median/current)^(1/10) - 1)
```

行业分位代理，`p` 为 0—1：

```text
valuation_adjustment = -min(0.03, max(0, p-0.50) × 0.06)
```

只有当前估值而无比较基准：`valuation_adjustment=-0.005`，不得产生正向估值扩张收益。

```text
CR10 = SSY + g_cons + valuation_adjustment
```

### 5.5 ReturnScore

使用线性插值的分段表：

| CR10 | ReturnScore |
|---:|---:|
| ≤0% | 0 |
| 1% | 20 |
| 2% | 40 |
| 3% | 60 |
| 4% | 75 |
| 5% | 88 |
| ≥6% | 100 |

### 5.6 分配质量

```text
PayoutQuality =
  35% × CoverageScore
+ 20% × RecentTrendScore
+ 20% × HistoryStabilityScore
+ 15% × BalanceSheetScore
+ 10% × BuybackQualityScore
```

数据完整性不再进入分配质量。

普通企业 `BalanceSheetScore` 的默认映射：净现金为 100；净负债/EBITDA 为 0—1、1—2、2—3、3—4、>4 时分别为 90、75、50、25、0，并在线性区间内插值。银行和保险使用资本缓冲映射。

`BuybackQualityScore`：验证后持续净减少按现有 qB 规则计分；明确无回购且无实质摊薄为 70；回购信息或股本桥未知为 50；有回购但未形成净减少或存在实质摊薄为 0。

### 5.7 RI

```text
RI_raw =
  45% × ReturnScore
+ 30% × PayoutQuality
+ 15% × BusinessDurability*
+ 10% × GovernanceCapitalAllocation*

RI = clip(min(RI_raw, history_cap), 0, 100)
```

星号分项缺失时固定取 50，不重标其他权重。RI 的数据不足通过 `data_confidence` 和状态表达，而不是把“数据完整性”重复计入 RI。

## 6. 简化后的 ERI 公式

```text
ERI_base =
  20% × DistributionDeteriorationRisk
+ 20% × CoverageRisk
+ 15% × BalanceSheetRisk
+ 15% × StructuralCycleRisk*
+ 12% × PolicyAndAssetLifeRisk*
+ 10% × ValuationTrapRisk*
+  8% × GovernanceRisk*
```

其中：

```text
DistributionDeteriorationRisk = 100 - RecentTrendScore
CoverageRisk = 100 - CoverageScore
BalanceSheetRisk = 100 - BalanceSheetScore
GovernanceRisk = 100 - GovernanceCapitalAllocation
```

星号风险分项缺失时取 60，不做剩余权重重标。

估值陷阱风险优先使用行业分位或当前/历史估值比。行业分位 `p` 的映射为 `clip(20+80p,0,100)`；只有当前估值时取 60。

七项静态否决状态缺失不再阻断：

```text
unknown_veto_uplift = min(8, 1.25 × unknown_veto_count)
warning_veto_uplift = min(10, 5 × triggered_non_major_veto_count)
ERI = clip(ERI_base + unknown_veto_uplift + warning_veto_uplift, 0, 100)
```

重大投资否决不强制把 ERI 写成 100，但会强制投资分类为 D。数据完整性类重大错误属于 `BLOCKED`，不属于“公司风险很高”。

## 7. 数据置信度公式

```text
DataConfidence = clip(100 - Σ(versioned_deductions), 0, 100)
```

扣分采用第 4.4 节的版本化规则，并输出逐项明细。致命错误先进入 `BLOCKED`，不通过额外扣分替代阻断。

置信度分档：

| 分数 | 解释 |
|---:|---|
| 85—100 | 高置信度，可进入 `VERIFIED` |
| 60—84.9 | 可用于正式筛选，通常为 `CALCULABLE` |
| 35—59.9 | 可用于排序和补数，状态 `ESTIMATED` |
| <35 | 不发布新 RI/ERI，状态 `BLOCKED` |

达到 85 分并不自动成为 `VERIFIED`；还必须满足无高影响代理、核心字段直接/确定性推导、无重大对账冲突等结构条件。

## 8. 缺失值、代理值和阻断规则

### 8.1 绝对阻断

以下任一项发生时，相关公司当前重算为 `BLOCKED`：

- 公司或证券身份冲突；
- 财年、完整年度/中期识别错误；
- 把拟派股息当作已支付、已批准或法律承诺；
- 币种、单位、复权口径或数量级无法确认；
- 核心来源相互冲突且差异超过 10%，或数值比达到 10 倍以上的明显尺度错误；
- 没有任何获准的公司总市值口径；
- 少于 2 个可用普通分红财年；
- 少于 2 个适用覆盖财年/资本生成时点；
- 没有任何当前估值指标或获准估值代理；
- 核心数值为非有限数、负股本、负价格等不可能值；
- 核心来源 URL/文件索引、日期、币种或单位全部缺失。

### 8.2 警告或局部降级

- 缺租赁本金：简化 FCF；
- 缺稀释后股本桥：只关闭回购贡献；
- 缺完整 A/H 股本：使用获准总市值代理；
- 四项对账未运行：警告，不阻断；
- 回购现金或股本桥对账冲突：关闭回购贡献，不关闭普通分红；
- 业务耐久度、治理或结构风险缺失：固定默认值并扣置信度；
- 否决配置缺失：状态 `UNKNOWN`，增加 ERI 和降低置信度；
- 合理的小额会计差异：采用保守值、警告和置信度扣分。

### 8.3 对账分级

| 结果 | 处理 |
|---|---|
| 输入缺失/未运行 | `NOT_RUN`，警告 |
| 差异 ≤2% | `PASSED` |
| 2%—5% | `WARNING`，保留主来源值 |
| 5%—10% | `WARNING_CONSERVATIVE`，采用使收益率更低/风险更高的值 |
| >10% 或明显尺度错误 | 市值/普通分红冲突阻断相关核心指标；回购/股本桥冲突仅关闭回购贡献 |

## 9. Codex、确定性程序和人工的职责边界

### 9.1 确定性程序

负责：批量下载、公司映射、财年识别规则、币种/单位转换、生命周期过滤、Decimal 算术、分红/现金流/市值/估值计算、容差对账、缺失默认、置信度、RI/ERI、版本化发布和回滚。

### 9.2 Codex

只负责需要语义理解的候选工作：

- 判断表述更接近拟派、股东批准、已支付、撤回或特别股息；
- 在年报中定位股息、现金流、股份变动和资本缓冲表格；
- 解释异常差异，提出可能的币种、单位、重述或合并范围原因；
- 提出业务耐久度、治理、结构周期、政策/资产寿命和估值陷阱候选分；
- 为人工复核生成候选说明。

Codex 输出必须包含：

```text
company_id, field_id, fiscal_year_or_as_of_date,
value_candidate, currency, unit, candidate_status,
source_url, document_title, publication_date, page_number,
verbatim_excerpt, table_header_context, reasoning_summary,
confidence, model, prompt_version, generated_at,
candidate_only=true
```

缺少页码、原文、来源、日期、币种或单位的核心财务候选不得进入受控导入。

### 9.3 人工最终确认

只处理少量高风险歧义：公司/证券身份、币种和单位冲突、A/H 权利及覆盖率、拟派与实施边界、重大资本行动、超过 10% 的核心对账冲突、重大否决项。

### 9.4 防止 Codex 写错核心财务数据

Codex 只能写隔离 `candidate`；确定性验证器校验 Schema、来源可访问性、页码、原文片段、财年、币种、单位、数量级、同财年比较数和允许字段白名单。候选通过后仍写入隔离账本；高风险候选须人工确认。生产账本只接受受控导入生成的 manifest、SHA-256 和审计日志，Codex 无权直接写 staging、评分或 release。

## 10. 分阶段上线与预计覆盖率

### 第一阶段：让真实 RI/ERI 上线

最小代码修改：

1. 新增本方案的数据状态、指标 `basis`、置信度扣分和警告结构；
2. 分红历史改为普通分红独立计算，未验证回购不计入但不阻断；
3. 非金融覆盖适配器允许 2—5 个可用财年，租赁本金缺失采用简化 FCF；
4. required provenance 只要求本次实际进入计算的字段；
5. 对账缺输入改为 `NOT_RUN/WARNING`，仅重大核心冲突阻断；
6. 支持公司总市值代理和估值代理；
7. RI/ERI 改为固定默认值加置信度，不做剩余权重重标；
8. 发布 `data_tier`、`data_confidence`、`basis`、`warnings` 和 `confidence_adjustments`；
9. 运行 67 家 readiness 统计，随后 dry-run 重算并发布结构化 v2 release；
10. 前端展示 RI、ERI、置信度、状态徽标和代理/缺失说明。

现有 268 个已接受现金流字段、11 个完整财年普通股息、现有行情刷新、年报来源、manifest、受控导入、回滚、v2 API/前端卡片均可复用。

当前 Git 仓库不包含被 `.gitignore` 隔离的生产 staging 和完整本机未提交数据，因此不能诚实给出第一阶段可展示公司的点估计。必须运行以下统计：

- `companies_with_2plus_eligible_dividend_years`；
- `companies_with_2plus_coverage_years_or_financial_capital_inputs`；
- `companies_with_direct_or_approved_proxy_market_cap`；
- `companies_with_current_valuation_or_approved_proxy`；
- `companies_with_basic_balance_sheet_metric`；
- `companies_with_fatal_identity_currency_unit_scale_conflict`；
- 上述条件交集及预计 `BLOCKED/ESTIMATED/CALCULABLE/VERIFIED` 数量。

本分支提供 `webapp/scripts/shareholder_v2_readiness.py` 生成这些统计。第一阶段目标应写成“交集中的全部公司”，而不是预先承诺一个未经数据验证的数字。

发布流程：先生成不激活的 v2.1 release，校验 manifest、SHA-256、API Schema 和前端快照；再切换 v2 `current` 符号链接并启用功能开关。旧 `/api/watchlist` 和 v1 字段保持不变。

### 第二阶段：提高覆盖和可比性

- 接入行情商公司总市值并逐步补齐完整 A/H 股本；
- 把普通分红扩展到 3—5 年；
- 补充基础资产负债表映射和行业估值分位；
- 用确定性规则生成结构周期、估值和政策风险的基础分；
- 只对异常和缺失公司触发 Codex 候选任务；
- 目标是把多数 `ESTIMATED` 提升为 `CALCULABLE`。

### 第三阶段：提升到 VERIFIED

- 补齐稀释后股本端点和回购桥；
- 完成适用的四项会计对账；
- 扩展至 5—10 年历史及可比估值序列；
- 定期复核业务耐久度、治理和否决项；
- 将高质量公司提升为 `VERIFIED`，但不以全部 67 家达到 VERIFIED 作为上线前提。

## 11. 需要修改的模块

| 层 | 文件/模块 | 修改 |
|---|---|---|
| 模型 | `webapp/liberty_v2/models.py` | 新增数据层级、basis、置信度调整、警告和新鲜度 |
| 计算 | `calculations.py` | 缺失感知加权、分段 ReturnScore、RI/ERI、分类 |
| 覆盖 | `coverage.py` | 可用年度而非所有年度齐全；简化 FCF；金融核心/推荐字段拆分 |
| 校验 | `validation.py` | 致命错误与警告拆分；按实际使用字段要求来源；对账分级 |
| 管线 | `pipeline.py` | 普通分红与回购解耦、市值/估值代理、置信度和状态决策 |
| 缓存 | `slow_cache.py` | 序列化新增字段并以 v2.1 版本强制失效旧缓存 |
| 配置 | `metric_policy_v2.json` | v2.1 权重、默认值、代理、扣分、阈值和分档 |
| 定义 | `metric_definitions_v2.json` | 新增置信度、状态、代理和评分层回购说明 |
| Release | `release.py` | index 发布置信度、状态、basis、warnings |
| API | `app/published_store.py`、`app/main.py` | 只读返回新字段，保留 v1 路由和功能开关 |
| 脚本 | `scripts/shareholder_v2.py` | readiness、dry-run、覆盖统计和 v2.1 发布参数 |
| 前端 | `public/index.html`、`app.js`、`modules/presentation.js`、`styles.css` | RI/ERI/置信度列、状态徽标、代理与警告卡片 |
| 测试 | Python/Node 测试 | 缺租赁、无股本桥、代理市值、对账未运行、缺定性分项、未知否决和回滚场景 |

本分支的 `practical_scoring.py` 是这些规则的可执行评分基线；接入生产管线时应由 Codex 按本表逐项替换现有硬门槛，不应在浏览器复制公式。

## 12. 主要风险及回滚方案

主要风险：

- 代理值造成假精确：所有代理必须显示 basis、原因和扣分；`ESTIMATED` 最高为 C；
- A/H 分母低估：禁止仅用 H 股或 A 股市值；部分覆盖只有在覆盖率已验证时才允许放大，并额外增加 5% 分母缓冲；
- 缺增长被误读为零增长事实：原始增长保持未知，仅评分层不计正增长；
- 未验证回购被误读为没有回购：原始值保持未知，界面显示“未计入”；
- 风险缺失被误读为安全：ERI 默认 60，并增加未知否决风险；
- 版本漂移：计算、定义、政策和 API Schema 必须同版本锁定；
- 新旧快照混淆：数据层级与新鲜度分开，上一合法快照显示原日期；
- 本机未提交改动冲突：本方案先在独立分支和 draft PR 落地，合并前与本机工作区逐文件比较。

回滚：

1. 将 `SHAREHOLDER_RETURN_V2_ENABLED=false`，前端和 `/api/watchlist` 回到 v1；
2. 将 structured `current` 符号链接切回上一 release；
3. 代码 release 回滚到上一 Git 版本；
4. v2.1 使用新 calculation version，使旧慢变量缓存自动失效；
5. 所有迁移默认 dry-run，原始数据、旧表和历史 release 不删除、不覆盖。

## 13. 最终推荐方案

采用本文件定义的 v2.1 单一方案：四层数据状态、独立置信度、固定默认值而非剩余权重重标、未验证回购不计入但普通分红继续、简化 FCF 加折扣、获准公司总市值与估值代理、对账分级、未知否决增加 ERI、低置信度限制投资分类。

实施顺序固定为：先运行 readiness 统计；再接入普通分红独立计算、覆盖适配器、代理市值/估值和置信度；随后重算 67 家并发布 v2.1 只读 release；最后开启前端展示。不得等待全部 67 家达到 VERIFIED，也不得为了提高覆盖率放松身份、财年、币种、单位、数量级或来源冲突的硬阻断。
