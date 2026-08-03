# Liberty

这是一个只覆盖 67 家 A/H 股公司的分红、回购与财报更新项目。日常入口是
[67 家公司索引](outputs/START_HERE.md)。正式名单独立保存在
`data/source/companies.json`，每家公司只保留一只证券。

## 核心文件

- `outputs/companies/<发行人ID_公司名>/分红回购历史.md`：公司级完整历史；
- `data/monitor/liberty_monitor.sqlite3`：事件当前状态、修订和运行记录；
- `data/monitor/raw/<发行人ID>/`：Futu OpenD 原始响应与变更快照；
- `data/raw/annual_reports/`：67 家正式公司的官方年报证据；其中原11家为旧归档，
  其余56家保存在可断点续传的 `official_backfill_v1/`；
- `data/shareholder-v2/source-evidence/futu-financials/`：56家公司不可变的Futu
  现金流量表/资产负债表响应；
- `data/shareholder-v2/backfill-output/futu-ledger-v1/`：不覆盖生产staging的
  v2来源账本副本与字段覆盖报告；
- `data/shareholder-v2/backfill-output/{dividend,official-cashflow,equity-bridge}-candidates-v1/`：
  从526份官方年报提取的股息、现金流和股本桥候选及SHA-256清单；
  仅供复核，不自动写入生产staging；
- `data/shareholder-v2/reconciliation/{cashflow,dividend,cancellation}-v1/`：
  候选逐笔对账后的隔离账本、汇总报告和SHA-256清单；仍需受控导入，不能据此
  把整家公司直接标为`VALID`；
- `scripts/dividend_buyback_monitor.py`：初始化、增量采集和 Codex 维护入口。

`webapp/` 是本仓库的 Web 应用子目录。仓库根目录同时跟踪数据采集代码、公开
配置、文档与 WebApp；原始年报、Futu 响应、SQLite、登录状态和本地运行产物由
根级 `.gitignore` 隔离，不上传 GitHub。

## 股东回报 v2 与风险服务

新版确定性计算、只读 API、Web 展示和本地 Codex 风险服务实现在本仓库的
`webapp/` 子目录。实际架构与迁移边界见
[`docs/architecture_and_migration_v2.md`](docs/architecture_and_migration_v2.md)，
指标/字段、分析服务和部署回滚分别见：

- `webapp/docs/shareholder-return-v2.md`；
- `webapp/docs/codex-analysis-service.md`；
- `webapp/docs/deployment-and-rollback-v2.md`。

根目录的数据侧仍是原始数据和旧事件监控来源；Ali 只接收经过校验的公开
release。新版迁移不会删除或改写本目录的原始历史数据。

## 工作流

首次初始化：

```bash
tools/futu-opend/.venv/bin/python scripts/dividend_buyback_monitor.py bootstrap
```

之后每约两小时运行一次：

```bash
tools/futu-opend/.venv/bin/python scripts/dividend_buyback_monitor.py run
```

每轮先把 Futu 的分红、回购、财报期和相关资讯写入 SQLite 与原始快照。只有
出现新增或修订事件时，才会针对受影响公司以固定
`gpt-5.6-sol`/`xhigh` 运行一次 `codex exec --ephemeral`；
Codex 只在含有该历史文件副本的隔离暂存目录中运行；结构校验通过后，程序才
原子替换正式的 `分红回购历史.md`。任务不发送通知，也不另行生成结构化摘要。

检查项目和运行状态：

```bash
python3 scripts/dividend_buyback_monitor.py check
python3 scripts/dividend_buyback_monitor.py status
```

定时器的安装和检查见 [systemd/README.md](systemd/README.md)，数据口径见
[data/README.md](data/README.md)。
