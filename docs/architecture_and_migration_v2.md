# Liberty 股东回报 v2：现状审计与迁移计划

更新日期：2026-08-03

## 实际架构

本仓库采用单一 Git 根目录，但保留两个边界清晰的运行域：

- `/home/or1ngelinux/Liberty` 是仓库根目录和 Linux 数据运行域。它保存 67 家
  正式公司清单、Futu OpenD 原始响应、SQLite 事件库、年报证据、公司级历史
  Markdown，以及两小时一次的用户级 systemd 采集任务；原始响应、数据库、
  年报 PDF、登录状态和本地运行产物不进入 Git。
- `/home/or1ngelinux/Liberty/webapp` 是同一仓库内的 Web 应用子目录。它包含 FastAPI、原生
  HTML/CSS/JavaScript 前端、Futu 行情和周线采集器、SSH 推送脚本、Docker
  Compose、用户级 systemd 单元及 pytest/Node 测试。

当前没有 Redis、Celery、APScheduler 或浏览器端数据库。Linux 侧状态使用
SQLite 和原子 JSON 文件；Ali 侧 FastAPI 只读挂载 `/usr/LibertyWatch/shared`。

### 已有数据流

```text
Futu OpenD / 年报证据
  -> data/monitor/raw + liberty_monitor.sqlite3
  -> scripts/dividend_buyback_monitor.py
  -> outputs/companies/*/分红回购历史.md

Futu OpenD 行情/周线
  -> webapp/collector/push_quotes.py / push_history.py
  -> webapp/runtime/*.json
  -> SCP 到 Ali shared/*.json 的临时文件并原子替换
  -> FastAPI WatchlistStore
  -> /api/watchlist、/api/securities/*、静态网页
```

WebApp 代码通过版本化 release tarball 部署到
`/usr/LibertyWatch/releases/<release_id>`；健康检查成功后更新 `current`。行情与
周线目前是 shared 目录内的独立原子文件，不是 manifest release。

## 审计结果

| 项目 | 实际位置/状态 |
|---|---|
| 正式公司/证券映射 | `data/source/companies.json`，实际 67 家且每家仅一只证券 |
| Web 清单 | `webapp/config/watchlist.json`，67 证券/67 issuer |
| 原始来源 | `data/monitor/raw/<issuer>/`、`data/raw/annual_reports/` |
| 事件标准化 | `scripts/dividend_buyback_monitor.py::normalize_events` |
| 旧股息回购率 | `webapp/app/domain.py::_apply_yield_model`，使用年均每股回报 |
| 行情快变量 | `webapp/collector/push_quotes.py`，工作日每分钟 |
| 周线 | `webapp/collector/push_history.py`，每周一 |
| FastAPI | `webapp/app/main.py`、`app/store.py`、`app/domain.py` |
| 前端 | `webapp/public/`，原生模块化 JavaScript/CSS |
| SSH/部署 | collector 的参数数组 SCP/SSH；`scripts/deploy_ali.sh` release 部署 |
| 调度 | 用户级 systemd timer；无 cron |
| 测试 | pytest 与 Node `--test` |
| 缓存 | ECB HKD/CNY 本地 JSON 缓存；无服务端缓存进程 |

已有证券记录包含 issuer/security ID、市场、币种、行情时间、前复权周线和来源
provider。2026-08-02已补齐其余56家的官方年报归档，并保存56家Futu详细财务
报表证据和独立来源账本；最近5年280个公司财年中，经营现金流覆盖280个、资本
开支覆盖262个。修复两份误选年报后，官方年报二次对账产生223个经营现金流、45个资本开支、
162个发行股数；修复“待股东大会批准”误判后，普通股息窄条件候选为4个。这些字段
仍隔离在候选目录，尚未写入核心账本。随后独立复核确认了全部268个现金流候选；
历史13条股息审计记录保留逐条决定，并依靠后续实施证据重建出11个完整财年普通
股息总额。6个注销事实也已确认，但都缺少可对账的稀释后股本
端点，不能计算合格回购。A/H及其他重要股份类别、发行/稀释股数、注销桥接、租赁本金、
重述链和拆并股事件账本仍不完整，因此这56家来源账本全部保持`PARTIAL`，不得
计算公司级收益率。现有 67 家清单与任务描述中的“68 家”不一致；迁移不得虚构
第 68 家。

## 目标数据流和责任边界

```text
Linux 原始来源
  -> 带来源/币种/单位/财年的原始记录
  -> Decimal 慢变量计算与行业覆盖适配器
  -> 数据对账和 VALID/PARTIAL/INVALID/STALE
  -> 最后合法公司快照 + v2 结构化 release
  -> 确定性触发器 + SQLite Codex job
  -> 固定输入快照 + gpt-5.6-sol/xhigh/read-only
  -> 严格 Schema 校验 + 公开 JSON/Markdown release
  -> Linux 主动 SSH manifest 发布
  -> Ali FastAPI 只读 current release
  -> 浏览器仅格式化、筛选、绘图和解释卡片
```

Codex 结果永远不写回原始记录、标准化记录、财务指标、自动评分或否决项。

## 分阶段迁移

1. 建立 `config/metric_definitions_v2.json` 和 `metric_policy_v2.json`，后端、API
   和前端从同一注册表读取。
2. 新增 `webapp/liberty_v2/` 领域包，使用 `Decimal` 实现历史分配、覆盖、
   A/H 公司市值、增长、估值拖累、评分、否决、慢/快变量与发布校验。
3. 新增 v2 SQLite/JSON 输入迁移器。旧 `yieldBasis` 保留为
   `shareholder-return-v1`，绝不复用同名字段表达新口径。
4. 结构化输出先发布；单公司失败保留最后合法快照并标记更新受阻。
5. FastAPI 以功能开关读取 v2 release，同时保留旧 `/api/watchlist`。
6. 原生前端增量加入 v2 列、详情分区和由指标注册表驱动的可访问解释卡片；
   浏览器不计算核心指标。
7. 新增 SQLite job store、确定性触发器、fake 可替换执行器、Codex worker、
   严格输出 Schema、公开文件白名单及恢复逻辑。
8. 将结构化数据与分析报告作为独立 manifest release 原子推送；任何失败均不
   改变 Ali 当前合法版本。
9. 默认复用现有项目所有者运行systemd流程；独立模型用户仅为可选加固。Codex
   CLI仍固定使用ephemeral、read-only sandbox、无shell参数调用和最小公开输出；
   Ali 仍只运行FastAPI容器。

## 兼容和回滚原则

- 旧行情和周线路由、67 家清单、Futu 采集频率与前端主要页面继续工作。
- `SHAREHOLDER_RETURN_V2_ENABLED=false` 时完全回到 v1 展示。
- 每次结构化/分析发布保留 release ID、manifest 和 SHA-256；回滚只切换
  `current` 符号链接，不删除当前或历史原始数据。
- WebApp 代码回滚到上一 Git/release；Linux 数据迁移先备份 SQLite，迁移脚本
  默认 dry-run，显式 `--apply` 才写新表，且不改旧表。

## 实施状态

上述模块已在 `webapp/` 落地。旧清单生成器在已删除历史CSV时改为只读使用当前
已核验 `watchlist.json` 的 issuer 和 v1 yieldBasis，因此不会伪造或丢失现有
映射。非紧急Codex分析增加同公司30日冷却，新增紧急事件绕过；业务耐久度和治理
可由Codex提出候选，但只有通过确定性来源/日期/量表审核后才进入Linux私有overlay。
完整回归统一使用 WebApp 开发环境；普通测试只使用 fake Codex。
