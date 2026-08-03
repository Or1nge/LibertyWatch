# Data

- `source/companies.json`：唯一正式范围，固定 67 家公司、每家公司一只证券；
- `monitor/liberty_monitor.sqlite3`：结构化事件、事件修订和运行记录；
- `monitor/raw/<issuer_id>/latest.json`：每家公司最近一次完整 Futu 响应；
- `monitor/raw/<issuer_id>/snapshots/`：初始化及有变化时的原始快照；
- `raw/annual_reports/十年候选_2016_2025分红年报PDF/`：原11家公司旧年报归档；
- `raw/annual_reports/official_backfill_v1/`：其余56家从巨潮资讯/港交所下载的
  官方年报、逐公司manifest、原始检索响应和67家公司总覆盖账本；
- `shareholder-v2/source-evidence/futu-financials/`：56家公司详细财务报表原始响应；
- `shareholder-v2/backfill-output/futu-ledger-v1/`：独立来源账本副本及覆盖报告，
  不会自动覆盖生产staging。
- `shareholder-v2/backfill-output/equity-bridge-candidates-v1/`：官方年报发行股数
  桥接候选、短摘录、页码及SHA-256清单；仅供复核，不写入生产staging。
- `shareholder-v2/backfill-output/dividend-candidates-v1/`：普通/特别股息、总额/
  每股、已支付/批准/宣派/拟派候选及逐条来源；
- `shareholder-v2/backfill-output/official-cashflow-candidates-v1/`：官方合并现金流
  与Futu同财年及相邻年报比较数的对账候选；

上述三类候选输出都有独立manifest和SHA-256，且默认不导入生产staging。

Futu 数据用于发现和维护分红、回购、财报期及相关资讯。回购记录不自动视为
净注销回购；员工激励、库存股或注销状态仍以正式公告为准。SQLite 和原始 JSON
是事实底稿，`outputs/companies/` 中的 Markdown 是公司级阅读文件。

年报归档的`VERIFIED`只代表官方PDF文件、页数和SHA-256验证通过。结构化账本另行
判定`VALID/PARTIAL/INVALID`：目前56家现金流账本仍为`PARTIAL`，缺失的租赁本金、
发行/稀释股数和注销桥接保持空值，不以0替代，也不得据此发布公司级收益率。

监控目录的文件说明见 [monitor/README.md](monitor/README.md)。
