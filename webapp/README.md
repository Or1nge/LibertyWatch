# Liberty 长期投资观察室

这是 Liberty 研究项目的只读价格与股东回报观察网站。v1 继续回答标的离旧版
4%每股目标线有多远；v2 使用监控证券口径的可持续股东回报率、CR10、RI、ERI和结构化
否决项完成更保守的筛选。

它不是交易终端、持仓系统或收益跟踪器，也不提供下单功能。正式观察清单为
67 只 A/H 股证券（67 个发行人、53 只 A 股、14 只港股）；用户重复列出的
迈瑞医疗只保留一次；
`config/demo-watchlist.json` 只用于检查界面，所有内容均明确标为虚构。

当前视觉层采用黑白高对比的广告杂志风格，以几何线稿、大字标题、深浅状态卡
和胶囊控件呈现；视觉改版不改变行情字段、目标价规则或工作日每分钟刷新链路。

股东回报v2的完整文档：

- [实际架构](docs/architecture-v2.md)；
- [指标、数据字典、A/H和行业口径](docs/shareholder-return-v2.md)；
- [56家公司来源账本回填](docs/source-ledger-backfill.md)；
- [现金流候选对账](docs/reconciliation-cashflow-v1.md)、
  [普通股息候选对账v1](docs/reconciliation-dividend-v1.md)、
  [最近五年普通股息对账v2](docs/reconciliation-dividend-v2.md)、
  [注销回购候选对账](docs/reconciliation-cancellation-v1.md)；
- [股本与股份权利对账](docs/reconciliation-share-capital-v1.md)；
- [本地Codex分析服务](docs/codex-analysis-service.md)；
- [部署、故障排查与回滚](docs/deployment-and-rollback-v2.md)。

## 架构

```text
本机 Linux
  Futu OpenD 127.0.0.1:11111
          ├─ 工作日每分钟快照（每批最多 20 只）→ collector/push_quotes.py
          └─ 每周十年前复权周线          → collector/push_history.py
          │ 两份 JSON 分别 SCP 上传并原子替换
          ▼
Ali /usr/LibertyWatch/shared/
  ├─ latest_snapshot.json
  └─ weekly_history.json
          │ 只读挂载
          ▼
FastAPI + 静态前端 :5048
          │ GET only
          ▼
电脑 / iPhone 浏览器
```

Ali 不会反向连接本机，也不保存或运行 Futu 登录状态。公网服务没有行情写入
接口；只有已经配置的 SSH 身份可以替换快照。

v2新增的数据流严格保持相同方向：Linux以Decimal计算并生成structured release，
确定性触发器在Linux创建SQLite任务，本地worker只使用
`gpt-5.6-sol`/`xhigh`，验证后再生成独立analysis release。Ali FastAPI只读两个
`current`，不计算、不抓取、不调用模型。

`SHAREHOLDER_RETURN_V2_ENABLED`默认关闭；代码部署不会自动把页面切到v2。只有数据
release达到约定门槛并由运维显式设置为`true`后，FastAPI才开放v2只读数据。激活
canary要求67条合法记录、至少5家公司同时有真实RI/ERI，且这些公司全部列入
`config/shareholder_v2_activation_reviews.json`人工审批清单；当前清单为空即关闭。

当前部署：

- 正式入口：`http://106.14.134.33:5048/`
- 虚构演示：`http://106.14.134.33:5048/?demo=1`
- 本机 timer：`liberty-quote-push.timer`，仅工作日每分钟执行
- 本机周线 timer：`liberty-history-push.timer`，每周一执行

## 页面

- `/`：首屏总览、刷新状态、四项摘要和可筛选表格；
- `/watchlist`：完整观察清单；
- `/sectors`、`/sectors/<行业>`：观察池行业排行与行业详情；
- `/opportunities`：价格机会、热门行业错杀和逆向观察；
- `/alerts`：目标价提醒；
- `/data-status`：Futu、SSH 快照和字段覆盖状态；
- `/methodology`：目标距离、行业热度和技术超跌规则；
- `/securities/<id>`：桌面右侧抽屉、手机全屏详情。

访问 `/?demo=1` 可手动开启虚构界面演示。默认 `/` 永远读取正式清单。
港股现价和三档目标价同时显示港币与约合人民币；表格中的估值状态、提醒状态
可直接点击筛选。详情图使用近十年前复权周收盘价，并叠加 3% / 4% / 5% 三档
自动目标价水平线。证券详情中的 PE、PE-TTM、PB、TTM 股息率、总市值、EPS
和每股净资产来自同一份 Futu 市场快照，随工作日每分钟行情链路更新。

## 本地运行与测试

```bash
cd /home/or1ngelinux/Liberty/webapp
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest -q
node --test frontend-tests/*.test.mjs
.venv/bin/python scripts/shareholder_v2.py migrate
.venv/bin/python scripts/shareholder_v2.py refresh-prices
.venv/bin/python scripts/shareholder_v2.py readiness --compact
.venv/bin/python scripts/shareholder_v2.py health-check
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 15048
```

然后访问：

```text
http://127.0.0.1:15048/
http://127.0.0.1:15048/?demo=1
```

健康检查为 `/healthz`；发布门槛为 `/readyz`。空正式清单是可用状态，不会被
错误地当成服务故障。

新增只读接口：

```text
GET /api/v1/companies
GET /api/v1/companies/{company_id}
GET /api/v1/companies/{company_id}/analysis/latest
GET /api/v1/metric-definitions
GET /api/v1/pipeline/status
```

## 正式观察清单

正式源文件是 `config/watchlist.json`。缺失数值必须使用 `null`，不能用 `0`
代表未知。每个标的的最小结构如下：

```json
{
  "id": "issuer-market-ticker",
  "issuerId": "issuer-id",
  "quoteCode": "HK.00700",
  "name": "证券名称",
  "ticker": "00700",
  "market": "HK",
  "currency": "HKD",
  "sector": "一级行业",
  "industry": "细分行业",
  "targetPrices": {
    "watch": null,
    "preferred": null,
    "deep": null
  },
  "yieldBasis": {
    "annualAveragePerShareCny": 0.36,
    "windowYears": 10,
    "startYear": 2016,
    "endYear": 2025,
    "method": "观察期内年度确认现金分红与净注销回购每股人民币金额的算术平均"
  },
  "expectedDividendYieldPct": null,
  "valuationStatus": "unconfigured",
  "metrics": {
    "pe": null,
    "peTtm": null,
    "pb": null,
    "dividendYieldTtmPct": null,
    "totalMarketValue": null,
    "earningsPerShare": null,
    "bookValuePerShare": null
  },
  "investmentThesis": [],
  "risks": [],
  "notes": "",
  "targetRevisionHistory": []
}
```

Futu 代码支持 `HK.*`、`SH.*`、`SZ.*` 和 `US.*`。当前正式 67 只标的按用户
最终名单及指定市场维护。重新生成或只核对名单：

```bash
python3 scripts/support/build_watchlist_config.py
python3 scripts/support/build_watchlist_config.py --check
```

生成器固定核对 67 只证券、67 个独立发行人、唯一的证券 ID 和 Futu 代码。
可追溯的 `yieldBasis` 优先来自当前年度股息回购率输出，再使用同口径的存档
逐年明细或扩展筛选；当前覆盖 22 只。其余标的目标价保持未配置，不使用推测
数据。目标价不从 CSV 手工填写；API 在存在基数时按统一公式自动生成。

## 目标价、估值与价格深浅

系统以观察期内各年度“确认现金分红 + 净注销回购”的每股人民币金额算术平均
作为净现金回报基数：

```text
关注价（3%）     = 周期年均每股净现金回报 ÷ 3%
理想目标价（4%） = 周期年均每股净现金回报 ÷ 4%
深度价值价（5%） = 周期年均每股净现金回报 ÷ 5%
当前股息回购率   = 周期年均每股净现金回报 ÷ 当前人民币价格
```

4% 是提醒主阈值。估值状态不是人工判断：当前股息回购率 `≥5%` 为“深度价值”、
`4%–5%` 为“具吸引力”、`3%–4%` 为“合理”、`<3%` 为“偏贵”。股价颜色按同一
阈值逐档加深，收益率越高颜色越深。

提醒状态表示现价与 4% 理想目标价的距离：已经到价为“已触发”，高出
`0–3%` 为“即将触发”，高出 `3–10%` 为“关注中”，更远为“未触发”。这些
都是页面筛选状态，不是交易指令。

港股人民币参考价按 `HKD/CNY` 日参考汇率换算。collector 用欧洲央行每日
EUR 交叉汇率计算人民币/港币，缓存 6 小时；暂时取不到新汇率时保留最近一次
有效缓存并标为过期缓存，绝不会把缺失汇率当成 0。页面使用“≈”明确表示参考
换算价，不是实时可成交汇率。

## 本机行情推送

只校验名单不会连接 Futu；生成本机正式快照会连接
`127.0.0.1:11111`，但 `--no-push` 不会上传：

```bash
python3 scripts/support/build_watchlist_config.py --check
/home/or1ngelinux/Liberty/tools/futu-opend/.venv/bin/python \
  /home/or1ngelinux/Liberty/webapp/collector/push_quotes.py \
  --no-push
```

collector 使用 `get_market_snapshot` 批量读取 67 只证券，默认每批 20 只，
兼容港股 BMP 快照的单批限制；任一批失败时不会上传不完整快照。
每一行快照除现价、涨跌幅和更新时间外，还映射以下富途正股字段：

| 网页字段 | Futu 快照字段 | 口径 |
|---|---|---|
| `metrics.pe` | `pe_ratio` | 市盈率 |
| `metrics.peTtm` | `pe_ttm_ratio` | 市盈率 TTM |
| `metrics.pb` | `pb_ratio` | 市净率 |
| `metrics.dividendYieldTtmPct` | `dividend_ratio_ttm` | TTM 股息率，单位 `%` |
| `metrics.totalMarketValue` | `total_market_val` | 供应商市值候选，单位为证券本币元；须经资本结构表授权后才可进入SEEV |
| `metrics.earningsPerShare` | `earning_per_share` | 每股收益 |
| `metrics.bookValuePerShare` | `net_asset_per_share` | 每股净资产 |

无效值继续写成 `null`，不会用 `0` 代替未知。网页 `/data-status` 分别显示
PE、PB 和七项快照字段的覆盖数量。运行时签名只忽略上述明确列出的动态字段，
证券 ID、市场、行业、目标价基准等静态元数据仍必须与正式清单完全一致。
港股清单存在时，collector 还会读取并缓存 ECB 日参考汇率，缓存文件默认是
`runtime/ecb_hkd_cny.json`；汇率失败不阻断 Futu 行情快照。
systemd 服务只额外开放富途 SDK 自身的
`~/.com.futunn.FutuOpenD/Log` 日志目录写权限；Home 其余位置继续保持只读。

股东回报v2.1不会再通过 `refresh-prices` 改写慢staging。该命令只验证67家公司
行情覆盖和freshness；`compute`与`readiness`直接读取同一 `latest_snapshot.json`
并在内存叠加价格、汇率、当前估值和经授权的SEEV。资本结构静态契约位于
`config/issuer_capital_structure_v1.json`，可用以下命令检查是否与正式清单同步：

```bash
python3 scripts/support/build_capital_structure_registry.py --check
```

用户级 systemd 单元位于 `systemd/`。安装方式：

```bash
install -d -m 0700 ~/.config/liberty-watch
install -m 0600 systemd/collector.env.example \
  ~/.config/liberty-watch/collector.env
install -d -m 0755 ~/.config/systemd/user
install -m 0644 systemd/liberty-quote-push.service \
  systemd/liberty-quote-push.timer \
  systemd/liberty-history-push.service \
  systemd/liberty-history-push.timer \
  ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now \
  liberty-quote-push.timer liberty-history-push.timer
```

已安装旧版两分钟 timer 的机器需要重新复制 `.timer` 文件后执行
`systemctl --user daemon-reload` 和
`systemctl --user restart liberty-quote-push.timer`。

检查最近一次推送：

```bash
systemctl --user status liberty-quote-push.timer
journalctl --user -u liberty-quote-push.service -n 50
```

collector 先在本机原子写入完整 JSON，再上传到 Ali 的同目录临时文件，设置为
容器可读权限后执行原子替换。任一步失败都保留 Ali 上一次有效快照。

## 十年周线

历史曲线与每分钟行情拆分保存。`collector/push_history.py` 使用 Futu OpenD
历史 K 线接口，为正式 67 只证券读取滚动近十年的前复权周收盘价，先完整
校验证券集合、日期顺序与价格，再原子写入 `runtime/weekly_history.json` 并
推送到 Ali。FastAPI 通过 `/api/securities/<id>/history` 按标的提供数据；
观察清单主接口不携带大批周线点。

手动运行首轮或检查更新：

```bash
/home/or1ngelinux/Liberty/tools/futu-opend/.venv/bin/python \
  /home/or1ngelinux/Liberty/webapp/collector/push_history.py
systemctl --user status liberty-history-push.timer
journalctl --user -u liberty-history-push.service -n 50
```

定时器默认每周一 08:15 触发，并允许最多五分钟随机延迟。Futu 历史 K 线额度
按证券计数，采集器会在开始前读取额度；额度不足时整批拒绝，不会向 Ali 推送
残缺文件。额度按七天滚动释放。若正式名单刚发生变化而新增证券暂时没有额度，
可用 `--retain-compatible` 只保留新旧名单交集中的既有 Futu 周线；网页会按
实际覆盖数显示，其余标的明确等待下一次完整周更。

## 部署到 Ali

部署脚本只打包网站白名单，不发送 `data/`、`outputs/`、图片、年报、缓存、
Futu 二进制、虚拟环境、登录状态或凭据。容器所需的 Python 3.11/x86_64
wheel 已固定在 `vendor/wheels/`，Ali 构建时离线安装，不依赖远端 Python 包源。

```bash
cd /home/or1ngelinux/Liberty/webapp
./scripts/deploy_ali.sh --dry-run
./scripts/deploy_ali.sh
```

默认部署到 `ali:/usr/LibertyWatch/releases/<release>`，持久快照放在
`/usr/LibertyWatch/shared/`，Compose 项目名为 `liberty-watch`，公网端口为
`5048`。每次发布保留独立 release；健康检查失败时脚本尝试恢复上一个 release。

## 公网边界

当前 `5048` 是直接 HTTP 公网访问，尚未配置域名、TLS 或登录保护。因此写入
正式清单的任何投资笔记都会被公网访问者看到。若后续需要保存私人备注，应先
增加访问控制或把私人字段留在不公开的数据层。
