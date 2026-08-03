# 56家公司年报、现金流与股本来源账本回填

更新日期：2026-08-03

## 作用与安全边界

`scripts/backfill_source_ledger.py`处理此前没有旧十年年报归档的56家公司。它把
Futu详细财务报表响应保存为不可变证据，再用`Decimal`字符串建立逐财年来源账本。
输出写入独立目录，不覆盖生产staging、最后合法快照或公开release。

Futu现金流数据是二级数据库证据；官方年报PDF是一级来源底稿。账面科目“股本”
是金额，不等于发行或稀释后股份数，代码只保存为
`reported_share_capital_amount`，不会写入`issued_shares`。Futu回购事件也只进入
`REVIEW_REQUIRED`证据，不会被当作已注销回购。

## 命令

确认固定范围为67家减去原11家：

```bash
cd /home/or1ngelinux/Liberty/webapp
.venv/bin/python scripts/backfill_source_ledger.py targets
```

普通执行默认dry-run。只有显式`--apply`才采集原始证据；该命令连接本机Futu
OpenD，不调用Codex：

```bash
../tools/futu-opend/.venv/bin/python scripts/backfill_source_ledger.py \
  collect-futu --max-years 10 --apply
```

生成独立回填副本和manifest，不覆盖
`data/shareholder-v2/staging/companies/`：

```bash
.venv/bin/python scripts/backfill_source_ledger.py build-ledger \
  --output-dir ../data/shareholder-v2/backfill-output/futu-ledger-v1 --apply
```

验证manifest列出的报告和56个公司JSON：

```bash
cd /home/or1ngelinux/Liberty/data/shareholder-v2/backfill-output/futu-ledger-v1
jq -r '.files[] | "\(.sha256)  \(.path)"' manifest.json | sha256sum -c -
```

`pdf-candidates`子命令接受`pdftotext -layout`文本和官方manifest元数据，只生成带
页码/行号的候选。合并报表行必须唯一、币种和单位明确，且本期报告的比较数与
上期报告对上后才可转为`VALID`；多匹配或比较数不一致保持`CONFLICT`。

### 官方年报股本桥候选

`scripts/support/extract_equity_bridge_candidates.py`直接读取官方年报manifest，逐份
校验PDF SHA-256，以参数数组调用`pdftotext -layout ... -`。完整文本只在单份报告
处理期间驻留内存，不保存大文本；落盘内容只有表内“股份总数”期初/期末、页码、
行号和短摘录。缺数、无表或仅声明“未变化”均保持`REVIEW`和`null`，不会写成0。

快速两年样本命令：

```bash
cd /home/or1ngelinux/Liberty/webapp
.venv/bin/python scripts/support/extract_equity_bridge_candidates.py \
  --market CN --fiscal-year 2024 --fiscal-year 2025 --workers 4
.venv/bin/python scripts/support/extract_equity_bridge_candidates.py --verify-only
```

生产回填使用下列全历史命令同时处理419份A股和107份港股报告；德昌电机
FY2026也必须显式纳入：

```bash
.venv/bin/python scripts/support/extract_equity_bridge_candidates.py \
  --market ALL --workers 4 \
  --fiscal-year 2016 --fiscal-year 2017 --fiscal-year 2018 \
  --fiscal-year 2019 --fiscal-year 2020 --fiscal-year 2021 \
  --fiscal-year 2022 --fiscal-year 2023 --fiscal-year 2024 \
  --fiscal-year 2025 --fiscal-year 2026
.venv/bin/python scripts/support/extract_equity_bridge_candidates.py --verify-only
```

输出固定隔离在
`data/shareholder-v2/backfill-output/equity-bridge-candidates-v1/`，包含
`candidates/`、`report.json`和带SHA-256的`manifest.json`，拒绝写入生产staging
或不可变年报目录。相邻财年“上一年期末=下一年期初”才把发行股数候选标为
`VALID`。`reported_net_issued_share_change`仅为表内期末减期初的发行股本变化，
即使通过对账也不等于稀释后股本变化；拆股/并股/缩股标记会强制保留人工复核。
港股候选额外保存原报表的`股/千股/百万股`单位与确定性乘数；单位缺失、披露
取整、多投票权类别或表格无法唯一定位时保持`REVIEW/null`。只有年报在唯一表格
中明确写明已经“回购及注销”的实际股份数，才生成
`cancelled_shares_candidate`；该字段仍不会写入核心`cancelled_shares`。程序始终
不推算`diluted_total_shares`、`cancelled_shares`或
`diluted_net_share_reduction`。

### 官方年报普通/特别股息候选账本

`scripts/support/extract_dividend_candidates.py`只读取
`official_backfill_v1/companies/*/manifest.json`列出的已验证当前PDF。每份PDF先
复核manifest中的SHA-256，再以参数数组运行`pdftotext -layout <pdf> -`；不使用
shell、不做OCR，也不保存完整文本。输出证据逐条包含源URL、PDF SHA-256、报告
财年、候选关联财年、币种、原始单位、金额口径（`TOTAL/PER_SHARE`）、
`ORDINARY/SPECIAL`、`PAID/APPROVED/DECLARED/PROPOSED`、页码、页内和全文行号及
最多500字符的摘录。

全历史批次和校验命令：

```bash
cd /home/or1ngelinux/Liberty/webapp
.venv/bin/python scripts/support/extract_dividend_candidates.py --workers 4
.venv/bin/python scripts/support/extract_dividend_candidates.py --verify-only
```

输出固定隔离在
`data/shareholder-v2/backfill-output/dividend-candidates-v1/`，包含56个公司候选
JSON、`report.json`及SHA-256 `manifest.json`，拒绝写入生产staging和不可变来源
目录。重复的同值披露仍为`REVIEW`；同一组件出现不同值时为`CONFLICT/null`；
中期、末期等多个真实组件不会自动相加。拟派普通股息、特别股息、财年关联不明、
币种或单位不明的证据均不会进入核心普通分红。即使唯一的已支付/股东大会批准
候选也只标记为`eligible_after_manual_review`，本程序所有记录始终保持
`core_import_allowed=false`。

### 官方年报现金流候选对账

`scripts/support/extract_official_cashflow_candidates.py`只读
`official_backfill_v1/`官方PDF和`futu-financials/*/latest.json`。每份PDF先复核
SHA-256，再以参数数组运行`pdftotext -layout <pdf> -`；不使用shell，完整文本不
落盘。候选保留财年、币种、单位、页码、页内/全文行号、短摘录、官方URL、PDF
SHA-256和Futu证据SHA-256。

`VALID`必须同时满足：合并/综合现金流量表唯一匹配；本期及比较期金额、币种和
单位明确；与Futu同财年金额和币种完全一致；本报告上年比较数与上一份官方年报
本期数完全一致。多匹配、单位不明、HK组件不完整或任一对账不完整均保持
`REVIEW`/`CONFLICT`且`value=null`，不使用模糊容差。所有候选的
`eligible_for_core_write=false`。

```bash
cd /home/or1ngelinux/Liberty/webapp
.venv/bin/python scripts/support/extract_official_cashflow_candidates.py
.venv/bin/python scripts/support/extract_official_cashflow_candidates.py --verify-only
```

隔离输出为
`data/shareholder-v2/backfill-output/official-cashflow-candidates-v1/`，拒绝写入
生产staging、官方PDF目录和Futu证据目录。

## 2026-08-02实际结果

- Futu原始证据：56/56家公司、112次现金流量表/资产负债表请求、失败0；
- 最多十年索引550个公司财年；覆盖报告使用最近五年280个公司财年；
- 经营现金流280/280，资本开支262/280，报表股本金额275/280；
- 租赁本金、真实发行/稀释股数、注销股数和稀释后净减少均为0/280；这里的0是
  “零个字段通过验证”，不是财务数值0；
- SQLite内38家公司、2,184条回购事件只作为待审证据，核心可计入回购为0；
- 状态为`VALID=0 / PARTIAL=56 / INVALID=0`，公司级收益率可用数为0；
- 福耀玻璃、顺丰控股、百济神州等六个多市场发行人标记
  `CROSS_LISTING_REVIEW_REQUIRED`；其他公司也保持`rights_verified=false`。
- 股本桥全历史批次：A股43家公司、2016—2025财年419份官方PDF
  全部处理成功，失败0；162份找到唯一数值行，跨年对账后发行股数
  候选`VALID=138 / REVIEW=268 / CONFLICT=13`。这里的`VALID`只表示发行股数
  候选通过相邻年报对账，不是公司v2发布状态；核心稀释股数和自动注销
  股数写入仍均为0。
- 港股全历史批次：13家公司、2016—2026财年107份官方PDF全部处理成功，失败0；
  保守结果为发行股数候选`VALID=24 / REVIEW=57 / CONFLICT=26`，另有6份年报
  产生“明确已实际注销”的候选。A股419份原结果逐字段语义哈希保持不变。
- 合并输出共56家公司、526份PDF，状态为
  `VALID=162 / REVIEW=325 / CONFLICT=39`；manifest包含57个候选/报告文件并已
  通过SHA-256自校验。核心稀释股数、核心注销股数和稀释后净减少写入仍均为0。
- 股息候选全历史批次处理56家公司、526份PDF，失败0；491份报告找到至少一条
  保守候选，355份报告找到至少一条可明确关联本财年的候选。修复条件性审批
  误判后，共保留3,296条证据：普通3,217、特别67、类型冲突12，其中809条明确
  关联报告财年。139条被识别为已支付或已批准，569条为拟派；只有4条满足
  “人工核验后可能进入普通股息核心”的窄条件，自动核心写入仍为0。
  `REVIEW=163 / CONFLICT=20 / MISSING=6,129`
  是逐类型、状态和金额口径的候选槽统计，不是财务数值。
- 候选缺口：35/526份报告没有可安全识别的金额，171/526份没有可唯一关联报告
  财年的金额；百济神州没有任何候选，德昌电机、康师傅及百济神州没有精确财年
  候选。这些PDF需人工表格核验或另找正式分红实施公告，程序没有扩大为模糊OCR。
- 官方现金流全历史批次处理56家公司、526份PDF，失败0；经营现金流候选
  `VALID=223 / REVIEW=229 / CONFLICT=74`，资本开支候选
  `VALID=45 / REVIEW=435 / CONFLICT=46`。这里的`VALID`只表示唯一官方行同时
  通过Futu和跨报告比较数对账；核心字段写入数为0。manifest覆盖报告和56个公司
  文件，共57个文件，SHA-256复核全部通过。

当前不能把这56家切换成完整v2推荐。下一步必须逐年核验官方年报/股本公告中的
普通与特别股息候选、拆并股/转股/激励稀释和租赁本金；港股还需
补齐18个资本开支公司财年。单家公司只有通过这些对账后才能独立升级为`VALID`，
不要求等待其他公司。

## 2026-08-03候选对账结果

- 223条经营现金流与45条资本开支候选重新打开本期及相邻年报，并与Futu同财年
  数值精确核对；268条全部`ACCEPT`。这只是字段级通过，仍未写生产账本。
- 修复审批状态后，当前真正的窄条件候选为4条；生命周期修复前进入审计的13条
  历史记录仍全部保留决定，原记录接受3条、拒绝10条。9个被拒原记录对应的实际
  分配由后续年报和实施事件重新建立，形成11个可供后续受控导入的完整财年普通
  现金股息总额。杭氧FY2023只确认了
  已实施的中期每10股2元，缺最终现金总额，不能形成该财年的`D_t`。
- 6条注销候选的注销事实均可确认；安踏FY2025由26,571,000股纠正为
  26,570,200股。由于6条都缺少可对账的稀释后股本端点，不计算
  `net_reduction_factor`和`B_eligible`。
- 全部结果保存在`data/shareholder-v2/reconciliation/`，并保持
  `writes_production=false`。字段对账通过不等于公司级`VALID`。

## 2026-08-03受控导入

候选和人工对账产物本身仍为只读。正式写入只能经过
`scripts/import_reconciled_source_ledgers.py`：它先校验Futu账本、两版现金流、
两版股息、注销和股本七套manifest的完整文件集合及SHA-256，在内存中构造全部
目标文件，预演差异，再对每个旧文件做逐文件备份和原子替换。导入不会填写
未知值、不会授权合格回购、不会开放推荐，也不会触发Codex。

本次实际导入范围：

- 56家公司Futu逐财年辅助账本；
- 56家公司455个不重复的官方年报现金流字段；
- 14家公司25个不重复的完整财年普通现金股息总额；
- 2家公司6个已确认注销股份数；
- 18家公司21个官方股份类别事实；
- 未验证的股份权利、真实已发行股数、估值和四项会计对账显式记录为
  `NOT_DISCLOSED/null`，从不写成0。

实际运行ID为`reconciled-v1-20260803`、来源缺失状态补充
`reconciled-v1-provenance-amendment-20260803`，以及官方来源层级修正
`reconciled-v1-official-source-fix-20260803`；随后依次运行
`cashflow-v2-20260803`、`share-capital-v1-20260803`和`dividend-v2-20260803`。
每个run的目标文件均通过post-write SHA-256；最后再次预演为
`changed_company_count=0`。现金流v1最终保留真实发布平台：巨潮资讯网239条、香港
交易所披露易29条，不再因英文固定标签错误而保留同值Futu来源。重算仍为
`INVALID=67`，其中这56家已没有“来源记录本身缺失”的错误，但仍因普通股息
历史、稀释后股本桥、公司总股本/股份权利、估值、增长和版本化风险配置不足而
关闭推荐。剩余11家未纳入本轮56家受控导入。

```bash
cd /home/or1ngelinux/Liberty/webapp

# 只读预演
.venv/bin/python scripts/import_reconciled_source_ledgers.py plan

# 写入必须显式给备份目录
.venv/bin/python scripts/import_reconciled_source_ledgers.py apply \
  --backup-root /home/or1ngelinux/Liberty/data/shareholder-v2/controlled-import-backups \
  --run-id <unique-run-id>

# 验证
.venv/bin/python scripts/import_reconciled_source_ledgers.py verify --run-id <unique-run-id>
```

回滚必须从最后一个run倒序执行；工具会先确认当前文件仍等于该run的post hash，
如有后续漂移会拒绝覆盖：

```bash
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback \
  --run-id dividend-v2-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback \
  --run-id share-capital-v1-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback \
  --run-id cashflow-v2-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback \
  --run-id reconciled-v1-official-source-fix-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback \
  --run-id reconciled-v1-provenance-amendment-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback \
  --run-id reconciled-v1-20260803
```

### 现金流v2扩展

`cashflow-v2`重新提取331份相关年报，解析错误0。最近五年560个字段槽位中，
相对v1新增187个官方可接受字段（CFO 41、资本开支146）；与v1所有历史字段合并
后，staging共有455个不重复的官方现金流字段。最近五年内，23家公司CFO和资本
开支都具备官方来源。剩余阻塞为CFO：Futu-only 49、冲突29、复核25；资本开支：
Futu-only 32、冲突39、复核25、双方均缺12。租赁本金仍为0个已取得字段，不
按财务值0处理。受控run `cashflow-v2-20260803`的56个post hash全部通过。

### 股本和股份权利v1

最近五年277个公司年中，发行股数候选为`VALID=93 / REVIEW=168 / CONFLICT=16`。
受控bundle确认21个精确股份类别事实，覆盖18家公司，其中17个类别权利已核实；
青啤A/H和顺丰A/H的4个类别只确认发行股数。稀释后总股本和净变化仍全部为空，
且财年末股本不能直接配对2026年7月行情，因此56家公司均保持
`company_market_value_denominator_authorized=false`。run
`share-capital-v1-20260803`只写官方来源事实，核心`share_classes`零项提升。

### 普通股息v2扩展

`dividend-v2`逐一覆盖56家公司最近最多五个完整财年，共277个位置。16个位置
核清完整普通现金股息、实际支付状态、币种和官方年报页，其中14个为新增、2个
重新校验v1事实；与v1合并并去重后，staging共有25个财年事实、覆盖14家公司。
其余261个位置继续为`BLOCKED/null`：193个尚未核清全年组成，62个港股位置尚未
核清币种或支付状态，6个没有官方完整总额。汇川技术FY2023/FY2024使用最终实施
额而非预案额；导入器会重新读取每个官方PDF并校验根目录边界和SHA-256。受控run
`dividend-v2-20260803`的56个post hash全部通过，重复预演为0变化。

### 当前重新计算结果

最后一次本地结构化release为`20260803T055150Z-3c686190af34`：67家公司全部
`INVALID`，公司计算异常0。纳入本轮的56家公司已无来源记录缺失错误，但仍未
取得足够的逐财年普通股息、稀释后总股本变化、公司级A/H实时市值分母、估值、
增长和带有效期的结构化风险配置，因此没有产生Codex任务，也没有同步到Ali。

## 测试

```bash
cd /home/or1ngelinux/Liberty/webapp
.venv/bin/python -m pytest -q \
  tests/test_shareholder_v2_calculations.py \
  tests/test_source_ledger_backfill.py \
  tests/test_equity_bridge_candidates.py \
  tests/test_dividend_candidates.py \
  tests/test_dividend_reconciliation_v2.py \
  tests/test_import_dividends_v2.py \
  tests/test_official_cashflow_candidates.py
```

普通测试使用内存构造的Futu响应和PDF文本，不连接Futu、官方站点或Codex。
