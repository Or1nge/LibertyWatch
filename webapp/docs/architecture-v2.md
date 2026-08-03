# Liberty 股东回报 v2 实际架构

更新日期：2026-08-03

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
  -> Decimal慢变量缓存 + 价格快变量
  -> 对账 + VALID/PARTIAL/INVALID/STALE + 最后合法快照
  -> structured manifest release
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
| 来源与对账 | `models.py`、`validation.py` |
| 否决项 | `veto.py` |
| 最后合法快照 | `snapshot_store.py` |
| manifest release | `release.py`、`sync.py` |
| 任务/触发/worker | `liberty_v2/analysis/` |
| 公网任务状态白名单 | `liberty_v2/analysis/publication.py` |
| 只读 Web 数据层 | `app/published_store.py` |
| API | `app/main.py` |
| 显示与解释卡片 | `public/app.js`、`public/styles.css` |

旧 `/api/watchlist` 继续返回，旧字段明确显示为 v1；新接口位于 `/api/v1`。
设置 `SHAREHOLDER_RETURN_V2_ENABLED=false` 可关闭所有 v2 读取而不影响旧页面。

## 发布失败语义

单家公司异常会生成自己的失败状态，不中断其他公司。关键来源、A/H 市值或对账
失败时，推荐值关闭；有历史合法快照则发布该快照并标记更新受阻。Codex 失败只
改变独立的公开任务状态，绝不阻塞 structured release 或覆盖最后成功报告；
stderr、认证状态、内部错误消息和本地路径不进入公网release。
