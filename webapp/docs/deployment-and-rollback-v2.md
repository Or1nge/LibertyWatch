# 部署、同步、故障排查与回滚

## Linux 首次安装

先检查 `.env.example`，再执行：

```bash
cd /home/or1ngelinux/Liberty/webapp
sudo ./scripts/install_shareholder_v2_services.sh
sudoedit /etc/liberty/shareholder-v2.env
```

安装器默认复用当前项目目录所有者作为 `LIBERTY_SERVICE_USER`，不要求创建
`liberty` 或 `liberty-codex` 账户；独立模型账户只是可选加固，不是上线前置。
如需指定另一个已经存在的服务用户，可执行
`sudo LIBERTY_SERVICE_USER=<user> LIBERTY_CODEX_HOME=<path> ./scripts/install_shareholder_v2_services.sh`。
安装器创建只读代码release `/opt/liberty/shareholder-v2/releases/<id>`、原子 `current`、专用venv、
`/var/lib/liberty/shareholder-v2` 数据根、0600环境文件、systemd和logrotate。
若本机已有 standalone Codex，它会把静态二进制安装到 `/opt/liberty/codex/bin`，
但不会复制个人认证。

选定服务用户尚未认证时才需要登录；默认可以安全复用该用户现有Codex认证目录：

```bash
sudo -u <LIBERTY_SERVICE_USER> env HOME=<service-home> \
  CODEX_HOME=<service-home>/.codex \
  /opt/liberty/codex/bin/codex login
```

若复用现有SSH隧道别名，应把仅该别名所需的安全配置提供给发布用户（不要复制
整个个人HOME），并验证解析结果：

```bash
sudo install -d -m 0700 -o liberty -g liberty /var/lib/liberty/.ssh
sudoedit /var/lib/liberty/.ssh/config
sudo chown liberty:liberty /var/lib/liberty/.ssh/config
sudo chmod 0600 /var/lib/liberty/.ssh/config
sudo -u <LIBERTY_SERVICE_USER> env HOME=<service-home> ssh -G <ALI_SSH_HOST> >/dev/null
```

把以下值加入现有行情 collector 环境文件，使同一快照原子写入低权限交接目录：

```text
SHAREHOLDER_V2_QUOTE_SNAPSHOT=/var/lib/liberty/shareholder-v2/inputs/latest_snapshot.json
```

然后按安装器打印的命令执行一次 `migrate --apply`。占位迁移不会改写生产原始
数据；回填来源账本后才可能产生 VALID 公司。

## 环境变量

| 变量 | 用途 |
|---|---|
| `SHAREHOLDER_RETURN_V2_ENABLED` | FastAPI v2功能开关；默认`false`，只有明确切换时设为`true` |
| `SHAREHOLDER_V2_CANARY_INDEX` | 显式开启v2时接受激活检查的本地`companies.json`；默认指向本地structured current |
| `SHAREHOLDER_V2_LOCAL_ROOT` / `STAGING_DIR` | Linux数据与标准化输入 |
| `SHAREHOLDER_V2_QUOTE_SNAPSHOT` | 行情快变量交接文件 |
| `ANALYSIS_JOB_DB` | SQLite任务库 |
| `CODEX_BINARY` / `CODEX_TIMEOUT_SECONDS` | 固定CLI与超时 |
| `CODEX_GLOBAL_CONCURRENCY` | 默认1 |
| `CODEX_MAX_AUTOMATIC_RETRIES` / `CODEX_SCHEMA_RETRIES` | 重试上限 |
| `ALI_SSH_HOST/PORT` | SSH隧道或现有SSH config别名 |
| `ALI_SSH_USER/KEY_PATH` | 可选；别名未配置User/IdentityFile时显式提供 |
| `ALI_RELEASE_ROOT` / `ALI_KEEP_RELEASES` | 远端数据release根与保留数 |
| `ALI_CONNECT_TIMEOUT` | SSH超时 |
| `LIBERTY_REMOTE_HOST` | 现有Web代码部署的SSH配置/隧道别名 |
| `LIBERTY_REMOTE_BASE` | Web代码远端release根 |
| `LIBERTY_PUBLIC_HOST/PORT` | 部署后的公网健康检查地址 |

主机、用户名、端口、私钥和目标目录均不在代码中硬编码。可直接把现有隧道别名
放入 `ALI_SSH_HOST`，由 `~/.ssh/config` 提供User/IdentityFile；若显式设置私钥，
必须0600。安装器按选定现有服务用户写入unit的HOME/CODEX_HOME；API key不能写入Git、unit或命令行。
Codex子进程使用环境白名单，不继承 `ALI_*`、任务数据库或部署变量。

## 服务命令

```bash
sudo systemctl start shareholder-codex-worker.service
sudo systemctl start shareholder-data-pipeline.timer shareholder-publisher.timer
sudo systemctl stop shareholder-codex-worker.service
sudo systemctl status 'shareholder-*'
sudo journalctl -u shareholder-codex-worker.service -f
sudo journalctl -u shareholder-data-pipeline.service -n 100
# 外部额度问题排除后，显式恢复已暂停的等待任务
sudo -u <LIBERTY_SERVICE_USER> /opt/liberty/shareholder-v2/current/.venv/bin/python \
  /opt/liberty/shareholder-v2/current/scripts/shareholder_v2.py resume-waiting
```

`shareholder-data-pipeline.timer` 工作日每2分钟更新快变量，慢输入哈希未变时使用
缓存；成功后触发 dispatcher。worker 常驻但不进入 FastAPI 请求线程。publisher
每5分钟只同步变更或等待重试的 channel。

本地诊断：

```bash
sudo -u <LIBERTY_SERVICE_USER> env HOME=<service-home> \
  CODEX_HOME=<service-home>/.codex \
  CODEX_BINARY=/opt/liberty/codex/bin/codex \
  SHAREHOLDER_V2_LOCAL_ROOT=/var/lib/liberty/shareholder-v2 \
  ANALYSIS_JOB_DB=/var/lib/liberty/shareholder-v2/analysis/jobs.sqlite3 \
  /opt/liberty/shareholder-v2/current/.venv/bin/python \
  /opt/liberty/shareholder-v2/current/scripts/shareholder_v2.py \
  health-check --codex --worker-scope
sudo -u <LIBERTY_SERVICE_USER> env SHAREHOLDER_V2_LOCAL_ROOT=/var/lib/liberty/shareholder-v2 \
  ANALYSIS_JOB_DB=/var/lib/liberty/shareholder-v2/analysis/jobs.sqlite3 \
  /opt/liberty/shareholder-v2/current/.venv/bin/python \
  /opt/liberty/shareholder-v2/current/scripts/shareholder_v2.py status
```

状态记录最近计算、release、任务计数、同步、模型、推理级别和版本，不公开密钥、
认证、绝对输入路径、stderr或JSONL。

## Ali 原子数据发布

structured 与 analysis 是独立通道：

```text
本地 releases/<id> + manifest.json + SHA256SUMS
 -> Ali <channel>/releases/.incoming/<id>
 -> 远端逐文件SHA-256校验
 -> rename到 releases/<id>
 -> 原子替换 current 符号链接
```

先校验本地release与SSH配置、但不连接或修改远端：

```bash
python scripts/shareholder_v2.py sync --channel structured --dry-run
python scripts/shareholder_v2.py sync --channel analysis --dry-run
```

上传失败只写 WAITING_RETRY，本地文件和Ali当前版本均保留。analysis白名单不包含
输入、events、stderr、错误文本、本地结果路径或认证详情；队列 `status.json`
与最后成功的 `latest.json` 分离发布，因此重试中的新任务不会隐藏旧报告。FastAPI只读挂载：

```text
/usr/LibertyWatch/shared/shareholder-v2/structured/current
/usr/LibertyWatch/shared/shareholder-v2/analysis/current
```

Web代码仍使用现有独立原子release：

```bash
set -a
source /etc/liberty/web-deploy.env
set +a
./scripts/deploy_ali.sh --dry-run
./scripts/deploy_ali.sh
```

部署脚本不再内置主机、目标目录、公网地址或端口；缺少上述变量时会直接拒绝运行。

部署脚本先跑全量测试，远端Compose健康和公网API检查均通过后才切换代码
`current`；失败自动恢复上一代码release。

当且仅当显式设置`SHAREHOLDER_RETURN_V2_ENABLED=true`时，部署还会在任何远端写入
前运行激活canary，并在Ali回环与公网API重复验证：release必须为v2.1
`VALID_RELEASE`，恰有67条合法记录，至少5家公司同时发布RI/ERI，所有分数为
0—100有限数，且全部可评分公司已列入
`config/shareholder_v2_activation_reviews.json`。当前审批清单为空，因此误设
`true`会在本地直接失败；`false`的常规v1代码dry-run不受影响。

## 回滚

来源账本staging的受控导入与发布release是两个独立回滚面。先停止后续结构化计算，
再按run的逆序还原staging；每次还原都会校验当前post hash和备份pre hash，检测到
人工修改或后续导入时拒绝覆盖：

```bash
cd /home/or1ngelinux/Liberty/webapp
.venv/bin/python scripts/import_reconciled_source_ledgers.py verify --run-id <run-id>
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id <latest-run-id>
```

本次已执行的六个run必须按以下顺序回滚：

```bash
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id dividend-v2-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id share-capital-v1-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id cashflow-v2-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id reconciled-v1-official-source-fix-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id reconciled-v1-provenance-amendment-20260803
.venv/bin/python scripts/import_reconciled_source_ledgers.py rollback --run-id reconciled-v1-20260803
```

随后才按下述命令切回旧的结构化发布release。来源账本回滚不删除官方PDF、Futu
不可变证据、对账bundle或计算release。

列出可用数据release后，本地切换：

```bash
python scripts/shareholder_v2.py rollback --channel structured --release-id <id>
python scripts/shareholder_v2.py rollback --channel analysis --release-id <id>
```

远端会先重新校验目标release，再原子切换：

```bash
python scripts/shareholder_v2.py rollback --channel structured --release-id <id> --remote
python scripts/shareholder_v2.py rollback --channel analysis --release-id <id> --remote
```

Linux worker代码回滚：

```bash
sudo ln -sfn /opt/liberty/shareholder-v2/releases/<previous-id> \
  /opt/liberty/shareholder-v2/.rollback-current
sudo mv -Tf /opt/liberty/shareholder-v2/.rollback-current \
  /opt/liberty/shareholder-v2/current
sudo systemctl restart shareholder-codex-worker.service
```

Web代码release由 `scripts/deploy_ali.sh` 在验证失败时自动恢复；手工回滚使用
同一服务器上一个 `/usr/LibertyWatch/releases/<id>` 重新运行 Compose，再更新
`/usr/LibertyWatch/current`。任何回滚都不删除原始数据或分析运行目录。

## 常见故障

- `WAITING_MODEL`：检查 `codex debug models` 是否含 `gpt-5.6-sol`，不改用其他
  模型。
- `WAITING_AUTH`：以unit实际使用的服务用户重新登录；不要复制个人token到Git。
- `BLOCKED`：查看公司 `blockers`、`warnings`和`selected_input_plan`；补齐选中来源、
  SEEV授权、近期分红、覆盖或资产负债表后重算。当前BLOCKED记录不展示旧分数。
- `WAITING_RETRY`：检查网络、额度或SSH；publisher会指数退避/定时重试。
- Ali 仍显示旧版：比较两个channel的 manifest release ID 和 SHA-256，不直接
  覆盖 current 内JSON。
