# Liberty 股东回报 v2 实际架构

更新日期：2026-08-04

## 审计结论

实际工程采用单一 Git 根目录，并包含两个运行边界：仓库根目录
`/home/or1ngelinux/Liberty` 是 Linux 数据侧，`webapp/` 是 Web 应用子目录。
根级 `.gitignore` 排除原始数据、SQLite、Futu 登录状态、运行快照和密钥文件。
正式清单实际为 67 家公司、67 只证券（53 只 A 股、14 只 H 股），不是任务文本
中的 68 家。系统没有 Redis、Celery、APScheduler 或 cron。

现有数据源与调度如下：

- Futu OpenD：`127.0.0.1:11111`；
- 公司清单：父项目 `data/source/companies.json`；
- 原始事件与修订：`data/monitor/raw/` 与
  `data/monitor/liberty_monitor.sqlite3`；
- 年报证据：`data/raw/annual_reports/`；
- 旧监控入口：父项目 `scripts/dividend_buyback_monitor.py`，约每两小时运行；
- 行情/周线：`collector/push_quotes.py` 与 `push_history.py`；
- Web：FastAPI `app/main.py`、原生 HTML/CSS/ES modules；
- Ali 部署：Docker Compose 与 `scripts/deploy_ali.sh` 的代码 release；
- 调度：systemd user timers（旧行情/周线）和本次新增的 system units（v2）。

## 当前与目标数据流

```text
Futu/年报/公告
  -> Linux原始文件与SQLite
  -> v2 staging（逐字段来源、币种、单位、财年、状态）
  + latest_snapshot只读快变量 + 资本结构授权 + 资产负债表适配
  -> SelectedInputPlan + 统一assessment + Decimal慢变量缓存
  -> Release Validity / Company Data Tier / Metric Basis / Freshness
  -> mixed-tier structured manifest release
  -> 确定性触发器 -> SQLite job -> 不可变输入目录
  -> 本地Codex CLI gpt-5.6-sol/xhigh/read-only
  -> 严格JSON Schema -> Linux完整运行目录
  -> public-only analysis manifest release
  -> Linux主动SSH上传、远端校验、原子current切换
  -> Ali FastAPI只读current -> 网页/API
```

FastAPI 不抓取、不计算、不调用模型，也不等待分析任务。浏览器只格式化、排序、
筛选、绘图、展开解释；核心收益率和评分没有 JavaScript 公式。

## 代码责任边界

| 责任 | 实现 |
|---|---|
| 指标唯一来源 | `config/metric_policy_v2.json`、`metric_definitions_v2.json` |
| 纯计算与评分 | `liberty_v2/calculations.py` |
| 行业覆盖 | `liberty_v2/coverage.py` |
| 慢/快变量编排 | `liberty_v2/pipeline.py`、`slow_cache.py` |
| 快行情与SEEV | `market_observation.py`、`market_value_resolver.py`、`capital_structure.py` |
| 资产负债表与输入选择 | `balance_sheet_adapter.py`、`input_resolution.py` |
| 统一评估与置信度 | `assessment.py`、`confidence.py` |
| 来源与对账 | `models.py`、`validation.py` |
| 否决项 | `veto.py` |
| 当前公司层级快照与旧版兼容快照 | `snapshot_store.py` |
| manifest release | `release.py`、`sync.py` |
| 任务/触发/worker | `liberty_v2/analysis/` |
| 公网任务状态白名单 | `liberty_v2/analysis/publication.py` |
| 只读 Web 数据层 | `app/published_store.py` |
| API | `app/main.py` |
| 显示与解释卡片 | `public/app.js`、`public/styles.css` |

旧 `/api/watchlist` 继续返回，旧字段明确显示为 v1；新接口位于 `/api/v1`。
设置 `SHAREHOLDER_RETURN_V2_ENABLED=false` 可关闭所有 v2 读取而不影响旧页面。

## 发布失败语义

单家公司数据不足会生成合法的`BLOCKED`记录，不中断或否定整批release。身份、
财年、币种、单位、数量级、核心来源或SEEV授权失败时关闭分数；未计入的回购和
不适用对账不会造成伪失败。v2.1的`BLOCKED`是当前诚实状态，不再用历史分数覆盖；
旧版无公司层级字段的快照仍保留原兼容行为。Codex 失败只
改变独立的公开任务状态，绝不阻塞 structured release 或覆盖最后成功报告；
stderr、认证状态、内部错误消息和本地路径不进入公网release。
