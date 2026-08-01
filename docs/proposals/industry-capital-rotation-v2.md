# 行业资金迁徙方案 v2：直接资金流优先，价量负责确认

> 状态：数据源调研与推荐方案，尚未接入正式页面。

## 结论

行业排名不再用纯价格动量推测资金流。

- 主排名：直接使用主力大单净流入；
- 规模校正：用成交额计算主力净流入占比；
- 价量指标：只判断资金流是否得到价格和成交额确认；
- 直接资金流缺失时留空，不把价量代理和直接资金流混在同一排名中。

## 可用数据源

### 1. Futu OpenAPI：A/H 股统一主数据源

当前项目安装的 `futu-api 10.09.6908` 已包含：

- `get_capital_flow`：个股整体净流入、主力大单净流入、特大单、大单、中单、
  小单净流入；历史日/周/月周期最多返回最近一年；
- `get_capital_distribution`：当日不同订单规模的流入额和流出额；
- `request_history_kline`：开、高、低、收、成交量、成交额和换手率；
- `get_plate_list` / `get_plate_stock`：板块及板块成分股。

优点是 A 股和港股可以沿用同一套接口和现有 OpenD，不需要把两个供应商的不同
算法硬拼到同一个分数。缺点是资金流接口按单只股票调用，并没有直接返回整个
行业的资金流，需要在本地按行业汇总；接口限制为每 30 秒 30 次，因此适合
收盘后批量运行，不适合塞入每分钟行情任务。

### 2. Tushare Pro：A 股行业级现成数据

两个可选接口均在盘后更新：

- `moneyflow_ind_dc`：东方财富行业/概念/地域资金流，包含主力净额、净占比、
  超大单、大单、中单和小单；
- `moneyflow_ind_ths`：同花顺行业资金流，包含流入、流出和净额。

它们能直接回答 A 股行业资金流排名，但需要 6000 积分，并且行业分类与 Liberty
现有细分行业需要建立映射。建议将其用作 A 股外部校验源；如果未来决定购买并
固定分类体系，也可以替代 A 股的本地聚合。

### 3. AKShare / 东方财富网页：免费备用，不作为唯一正式源

AKShare 的 `stock_sector_fund_flow_rank` 可以读取东方财富今日、5 日和 10 日
行业资金流排名，还提供行业历史资金流接口。它适合原型验证和断面对照，但属于
对网页数据的封装，接口或字段可能变化，不建议成为无人值守生产链路的唯一来源。

### 4. 港股通持仓变化：中期交叉验证

南向港股通逐股持股变化可以按行业汇总，作为“中期配置资金”信号；它只覆盖
港股通资金，不代表全部港股主力，也不能替代交易资金流。北向日度持股披露已从
2024-08-20 起停止，因此不能把同一方法完整复制到当前 A 股。

## 推荐数据架构

### 正式主源

使用 Futu 同时覆盖 A 股和港股：

1. 为每个关注行业维护高流动性代理池；
2. 每个交易日收盘后拉取逐股 `main_in_flow` 和日成交额；
3. 港股按当日 HKD/CNY 汇率换算；
4. 按市场和行业分别汇总；
5. 页面提供 `合并 / A股 / 港股` 切换。

Tushare DC/THS 只做 A 股日终差异检查。不同供应商的金额不能直接相加，也不
应同时进入一个综合分。

## 主排名

对行业 `g`、窗口 `W`：

```text
MainFlow(g,W) = Σ main_in_flow(s,d) × FX(s,d)
Turnover(g,W) = Σ turnover(s,d) × FX(s,d)
MainFlowRate(g,W) = MainFlow(g,W) / Turnover(g,W)
```

页面同时展示：

- 主力净流入：回答“净流入了多少钱”；
- 主力净流入率：回答“相对于行业成交规模，这股力量有多强”；
- 迁徙分：使用金额和净流入率的同向、稳健缩放结果做排序。

金额占 70%，净流入率占 30%。这既保留大资金的绝对迁移，也避免大市值行业只
凭成交规模长期霸榜。

## 价量确认，不参与主排名

原价格动量增加成交额后，改为“价量确认”字段：

```text
CloseLocation(s,d) = (2×Close - High - Low) / (High - Low)
SignedTurnover(s,d) = CloseLocation(s,d) × Turnover(s,d)

PriceVolumePressure(g,W)
  = Σ SignedTurnover(s,d) / Σ Turnover(s,d)

TurnoverImpulse(g,W)
  = 当前日均成交额 / 过去20日日均成交额 - 1
```

判定：

- 主力净流入为正、价量压力为正且成交额放大：`价量确认流入`；
- 主力净流入为负、价量压力为负：`价量确认流出`；
- 资金流与价量方向相反：`资金/价格背离`；
- 成交额未放大：`弱确认`。

这里使用“成交额”而不是股数成交量。A/H 股票价格、每手股数和拆并股差异很大，
原始成交股数不能直接跨证券或跨市场相加。

## 采集和文件

- 新增 `collector/push_capital_flow.py`；
- 日终采集，首次回填最近一年，之后增量更新；
- 单独生成 `runtime/daily_capital_flow.json`；
- 不修改每分钟 `latest_snapshot.json`；
- 严格记录供应商、市场、币种、汇率、成分池版本、覆盖率和缺失原因；
- 行业覆盖成交额不足 80% 时，该期不排名。

## 参考文档

- Futu 资金流：
  https://openapi.futunn.com/futu-api-doc/quote/get-capital-flow.html
- Futu 资金分布：
  https://openapi.futunn.com/futu-api-doc/quote/get-capital-distribution.html
- Futu 历史 K 线：
  https://openapi.futunn.com/futu-api-doc/quote/request-history-kline.html
- Tushare 东方财富行业资金流：
  https://tushare.pro/document/2?doc_id=344
- Tushare 同花顺行业资金流：
  https://tushare.pro/document/2?doc_id=343
- AKShare 板块资金流：
  https://akshare.akfamily.xyz/data/stock/stock.html
- Tushare 沪深港股通持股：
  https://tushare.pro/document/2?doc_id=188
