# 股东回报 v2：指标、字段与迁移

## 唯一指标中心

`config/metric_definitions_v2.json` 是 API 和前端解释文字的唯一来源；
`config/metric_policy_v2.json` 保存公式系数、阈值、评分权重、行业参数、对账
容差和 Codex 固定模型。两者都锁定到
`shareholder-return-v2.0.2`。代码启动及测试会拒绝版本不一致或解释字段缺失。
v2.1第一阶段已新增SEEV、快变量覆盖和统一assessment契约；正式公式/指标注册表
仍将在后续计算PR统一升至`shareholder-return-v2.1.0`，本阶段不提前改变线上值。

核心实现位于 `liberty_v2/calculations.py`，金额、股本、价格、汇率、收益率和
评分均使用 `Decimal`；float、NaN 和 Infinity 被拒绝。公式包括：

```text
B_eligible = B_gross * min(1, max(0,N)/C)，C=0时为0
X_t        = D_t + qB * B_eligible_t
R2         = 0.65*X_latest + 0.35*X_previous
M5         = median(last <=5 X)
T10        = winsorized mean(last <=10 X)
H          = 按2年、3-4年、5-7年、>=8年分支的非对称保守额
S          = max(0,min(H,0.90*max(FCF5,0)))（普通非金融企业）
raw yield  = R2/company market cap
SSY        = S/company market cap
CR10       = SSY + g_cons + valuation_drag
```

特别股息只进入 `distribution_history.special_dividend`，不影响 H。未注销、未形成
稀释后净减少或被稀释抵消的回购不提高 SSY。RI、ERI、历史限分和 A/B/C/D 分类
均由版本化策略计算；缺少业务耐久度或治理分时 RI 只保留受限内部参考，最高60。

## 原始数据字典

每个 `raw_data_points[]` 至少包含：

| 字段 | 含义 |
|---|---|
| `field_id` | 稳定且唯一的业务字段ID |
| `company_id` / `security_id` | 公司层与可选证券层标识 |
| `share_class` | A/H/其他股份类别 |
| `source_name` / `source_document` | 来源和文件标题 |
| `source_url_or_local_path` | 公告URL或Linux证据路径 |
| `source_publish_date` / `source_fetch_time` | 发布和抓取时间 |
| `fiscal_period` | 明确财年，如 FY2025 |
| `currency` / `unit` / `value` | 币种、单位、Decimal字符串值 |
| `data_status` | VALID、KNOWN_ZERO、MISSING、NOT_DISCLOSED、NOT_APPLICABLE、CONFLICT、STALE、CALCULATION_FAILED |
| `restatement_status` | ORIGINAL 或 RESTATED 等重述状态 |

真实零必须为 `KNOWN_ZERO`；不可用状态不得携带数字。核心来源缺失或重复公告会
使本次更新失败，不能以零补齐。`source_summary` 是可公开摘要；原始本地路径不会
自动同步到 Ali。

年度标准化记录 `annual_distributions[]` 使用 `fiscal_year`、
`fiscal_year_end_date`、`period_type=FULL_YEAR`、`ordinary_dividend_status`、
`ordinary_dividend`、`special_dividend`、`gross_cancelled_buyback`、
`cancelled_shares` 和 `diluted_net_share_reduction`。为确定性判定一次性分配与
过度分配，还应提供明确的 `asset_sale_distribution`、`one_off_buyback`（它是
总回购中的子集）或经对账的 `total_distribution`；未知字段不能以0补齐。
`balance_sheet_history[]` 至少提供财年和同口径 `net_debt`。代码按财年排序后直接
推导分红下降、连续两年分配/FCF、净负债变化、一次性分配占比及金融资本缓冲，
运行时模型不能覆盖这些标志。

核心资产到期、重大资本计划、审计/内控/资金占用/关联交易和重大监管处罚等
人工或行业结构化否决配置不能写裸布尔值；每项都必须同时包含 `value`、`source`、
`as_of_date`、`expires_at` 和 `reason`。缺项或过期会使评分数据不完整并归入C类，
不会默认解释为“没有风险”。

业务耐久度和治理与资本配置分可以来自人工结构化配置，也可以来自Codex联网调研
后提出的候选值。后者只有在严格输出Schema、来源URL交叉核对、快照日期一致和
最长365日有效期检查全部通过后，才写入分析运行目录内独立的
`reviewed_overlay.json`。计算时人工当前配置优先，reviewed overlay只作后备；
每项必须标记 `produced_by_codex=true` 和
`review_status=DETERMINISTICALLY_ACCEPTED`，旧报告不会自动升级为配置；过期即
失效。该机制不能写原始财务、其他风险分、核心指标或任何自动否决项。

每个进入计算的数值字段必须有约定的来源账本ID，例如
`FY2025.ordinary_dividend`、`MARKET.<security_id>.price`、
`SECURITY.<security_id>.issued_shares`和
`FY2025.balance_sheet.total_assets`。`SelectedInputPlan`先确定实际采用的分红、
覆盖、市值、估值、资产负债表和增长口径，再只校验这些字段；未计入的回购和
不适用的对账不会被预先列为必填。行情、汇率和当前估值从快照生成内存来源记录，
不会写回慢staging；仅快变量数值变化不会使慢变量缓存失效。

## 公司层、证券层与财年

`company_id` 和 `security_id/share_class` 分离。RI的分母改为当前监控证券的
“监控证券等价权益价值”（SEEV）：所有重要股份类别先按经济权利折算为监控证券
等价股数，再乘监控证券价格和汇率。单一普通股类别时SEEV等于公司市值；权利
相同的A/H公司则以所选A股或H股价格乘A/H总等价股数。实际各类别市值之和可以
另行展示，但不是RI必要分母。

Futu `totalMarketValue`只有在 `issuer_capital_structure_v1.json` 授权后才能作为
供应商SEEV。每次运行仍重新检查 `totalMarketValue/currentPrice` 隐含股数：与
官方等价股数差异不超过2%可可靠使用，2%—5%降级并警告，超过5%阻断；ADS必须
核对存托比例，双柜台不得重复计数，分配权不清的多类别公司继续阻断。

证券层4%参考价先计算 `S/4%` 的公司价值，再按经济权利因子、等价公司股本和
汇率分配；不会把公司分配额直接除以单一 H 股或 A 股股本。

输入默认只接受已经结束的完整财年。建议但未批准的股息应单列为待确认，不能
进入普通分红。拆并股、发行、转股、注销和稀释后股本变化必须在同一复权口径下
对账。

有机增长输入为带 `fiscal_year`、财年结束日和 `FULL_YEAR` 标志的连续年度序列；
普通企业只接受标准化公司FCF，银行接受调整后利润/资本生成，保险接受自由盈余，
不接受已含回购影响的每股增长。估值字段必须声明同口径可比：普通企业限
`P_FCF/EV_EBIT`，银行限带可比ROE的 `P_B_WITH_ROE`，保险限 `P_EV` 或带ROE的
P/B；否则估值拖累保持不可计算，绝不计正向扩张收益。

## 行业适配器

- `NonFinancialFCFAdapter`：经营现金流减资本开支减租赁本金；租赁本金无法拆分
  时返回 PARTIAL 并披露简化口径。
- `BankCapitalAdapter`：要求调整后利润、资本生成、CET1缓冲、RWA增长、NPL、
  拨备、信用成本、净息差。
- `InsuranceSurplusAdapter`：要求自由盈余、可分配盈余、综合/核心偿付能力、
  投资质量、利率敏感性、新业务价值。
- `UnsupportedAdapter`：券商和其他未配置行业返回 INSUFFICIENT_DATA。

银行和保险绝不会回退到普通 FCF 算法。适配器 haircut 和资本缓冲阈值位于策略
JSON。

## 发布契约

发布状态拆成四个正交维度：

- Release Validity：`VALID_RELEASE/REJECTED_RELEASE`，只判断schema、唯一ID、
  manifest、SHA-256、版本、有限数和来源引用；一批中包含`BLOCKED`公司仍可合法；
- Company Data Tier：`BLOCKED/ESTIMATED/CALCULABLE/VERIFIED`；
- Metric Basis：`DIRECT/DERIVED/VENDOR_AUTHORIZED/PROXY/CONSERVATIVE_DEFAULT/UNAVAILABLE`；
- Freshness：`CURRENT/MARKET_CLOSED_CURRENT/STALE_LAST_GOOD`。

公司记录将包含 `data_tier`、`data_confidence`、`freshness`、`metric_bases`、
`warnings`、`blockers`、`selected_input_plan`和`source_summary`。精确值为Decimal
字符串；未知仍是`null`，只有明确的评分政策才可使用0并标记basis。

## 一次性迁移

迁移默认 dry-run，不改旧数据：

```bash
.venv/bin/python scripts/shareholder_v2.py migrate
.venv/bin/python scripts/shareholder_v2.py migrate --apply
```

若 staging 已存在，必须显式传 `--backup-root`。迁移只建立 67 家 ID/证券映射和
v1 provenance，占位数据保持缺失并触发来源校验；它不会把旧每股均值伪装成
v2 公司层指标。生产回填需按本页原始数据契约补齐。

56家实际来源账本采集、独立输出、字段覆盖和剩余股本缺口见
[`source-ledger-backfill.md`](source-ledger-backfill.md)。来源账本为`PARTIAL`时不会
因经营现金流已经存在就越过股本、租赁或A/H口径校验。

## 测试

`tests/test_shareholder_v2_calculations.py` 覆盖25个黄金场景、不变量、来源契约、
A/H、行业适配器、自动否决、对账、拆并股及快慢缓存。价格变化只重算快变量；
同一日慢输入哈希未变时复用 `cache/slow/<company_id>.json`，计算版本变化会强制
失效旧缓存。`tests/test_v21_assessment.py`另行覆盖67家公司授权表、SEEV动态复核、
行情新鲜度、非写入覆盖层、资产负债表直接/代理适配、统一输入计划和mixed-tier
release合法性。
