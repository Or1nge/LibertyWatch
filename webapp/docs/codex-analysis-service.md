# 本地 Codex 风险分析服务

> 当前公开研究契约已升级为`risk-review-v2.0.2`和
> `analysis/schema/risk_analysis_output_v2.json`，使用逐公司screening触发与
> `research_bundle.json`，不再回写reviewed overlay。下文v1.1触发与overlay内容
> 仅作为legacy回放说明；当前运行方式见[`shareholder-screen-v2.2.md`](shareholder-screen-v2.2.md)。

## 固定运行策略

唯一允许的运行配置为：

```text
model = gpt-5.6-sol
reasoning effort = xhigh
sandbox = read-only
approval = never
```

`liberty_v2/analysis/worker.py` 使用 `subprocess.Popen` 参数数组，Prompt 经 stdin
传入，禁用 shell，超时终止整个进程组。worker默认复用安装时选定的现有服务
用户，不要求另建 `liberty-codex`；另建账户仅是可选加固。无论使用哪个用户，
systemd都把项目代码设为只读，只开放分析任务/输出、analysis发布、状态和选定
CODEX_HOME，并隐藏变化中的inputs/staging/cache/snapshots、structured发布与
observations目录。CLI仍使用ephemeral、read-only sandbox、PrivateTmp和输出白名单。
CLI子进程只继承认证、代理、CA、locale和临时目录白名单，不继承发布/数据库变量。
成功归档按字节复制已校验的冻结输入，不继承源目录的setgid等权限元数据，以兼容
systemd的`RestrictSUIDSGID`加固；归档内容仍由冻结输入SHA和最终产物SHA校验。
当前 CLI 对应命令顺序为：

```bash
codex --ask-for-approval never --search exec \
  --skip-git-repo-check --ephemeral \
  --cd /opt/liberty/shareholder-v2/current \
  --model gpt-5.6-sol -c 'model_reasoning_effort="xhigh"' \
  --sandbox read-only --json \
  --output-schema analysis/schema/risk_analysis_output_v2.json \
  --output-last-message /tmp/final.output.json -
```

安装后的`current`是不可变release且不包含`.git`，因此显式跳过CLI的Git仓库检查；
这不跳过冻结输入的精确文件集/SHA校验、Schema校验、公开来源门禁或read-only sandbox。

启动检查覆盖 `codex --version`、`codex debug models`、`codex login status`、输出
权限、Prompt版本和Schema模型常量。模型或认证不可用时任务分别进入
`WAITING_MODEL` 或 `WAITING_AUTH`；绝不选择替代模型。

## 组成与状态

- `job_store.py`：SQLite事务队列、唯一约束、单公司锁、全局并发、重启恢复；
- `triggers.py`：确定性触发、普通4%分析30日成功分析冷却、7日价格变化冷却、3.5%/5交易日退出滞回；
- `prompt_renderer.py`：临时目录构建后原子切换固定输入，worker运行前复核精确文件集、逐文件SHA-256、公司快照哈希和Prompt；
- `worker.py`：CLI执行、超时、错误分类与指数退避；
- `output_validator.py`：JSON Schema及公司ID/输入哈希/模型/HTML二次校验；
- `reviewed_overlay.py`：只审核业务耐久度/治理候选的来源、快照日期和最长365日有效期，并读取私有overlay；
- `storage.py`：成功运行原子落盘，读取latest时复核路径、身份与SHA-256；
- `publication.py`：把队列状态转换为不含认证详情、错误文本和本地路径的公网状态；
- `release.py`：从字段化JSON确定性生成公开 JSON/Markdown/status release。

任务唯一键是 `company_id + analysis_mode + input_snapshot_hash + prompt_version +
model`。默认全局并发1（可配置为1至8，在同一受锁进程内并行）、自动重试2次、
Schema失败额外重试1次。worker持有独占
进程锁；只有新worker取得该锁后，才把上一个进程遗留的RUNNING恢复为
WAITING_RETRY，API、dispatcher和publisher打开数据库不会误伤运行任务。模型、认证或额度问题耗尽自动重试预算后仍保留为
等待状态且不再自动领取；模型/认证恢复并重启worker时自动恢复相应任务，额度等
人工确认后可运行 `scripts/shareholder_v2.py resume-waiting` 恢复，不会降级模型。
若同模式等待任务之后出现了不同输入快照，新任务会创建，旧任务保留为
`SUPERSEDED` 审计记录；只有真正RUNNING的同公司同模式任务会阻止并发创建。

## 触发规则

所有阈值位于 `config/metric_policy_v2.json`：

- `FULL_ENTRY_REVIEW`：首次进入SSY 4%；raw yield达到4%但SSY不足4%；区间内
  无合法报告；重大Prompt升级；旧基线校验失败。全部非紧急任务只有在同一公司
  距最近成功分析至少30天时才会再次运行。
- `URGENT_VETO_REVIEW`：分红暂停/下降、审计治理警报、回购无净减少、连续过度
  分配且负债上升、资本缓冲、核心资产到期、重大资本计划、监管处罚或来源冲突。
  紧急任务在判断顺序中优先，明确绕过30日公司冷却和7日价格冷却。
- `MATERIAL_CHANGE_REVIEW`：SSY/H/S/CR10/RI/ERI/覆盖/净负债达到配置阈值，
  或新财报、盈利预警、分红、回购、股本、并购、资产出售、监管/政策事件。
- `PERIODIC_REFRESH`：仍在观察区间且普通公司90天、高风险公司30天未成功复核。

真正的PARTIAL、INVALID、STALE、普通价格微动、仅格式变化、重复快照及同类活动
任务均不触发Codex。唯一的PARTIAL例外是核心财务、来源、覆盖、估值和其他风险
输入均有效，仅缺业务耐久度/治理reviewed overlay的bootstrap状态。所有非紧急
FULL/MATERIAL/PERIODIC任务共用30日公司级成功分析冷却；仅由价格造成的变化另有
7日防抖。只有新增URGENT事件绕过30日。未变化的持续否决不会每两分钟重复创建。

## Prompt 与 Schema 版本

公共及四种模式Prompt在 `analysis/prompts/v1/`；输出Schema在
`analysis/schema/risk_analysis_output_v1.json`。Schema所有对象均禁止额外字段，
所有字段必填，数组有限长，枚举、日期、URL、分数有约束，拒绝NaN、Infinity和
原始HTML。Prompt升级必须同时修改常量、策略、测试；重大升级由调度器建立新
基线。

模型主要研究结构化字段难以覆盖的风险。发现冲突时设置
`data_issue_detected=true`，但不能改写原始财务、核心指标或否决项。Prompt
v1.1允许它在 `reviewed_overlay_candidates` 中为业务耐久度和治理各提出一个候选
配置；证据不足必须为null。候选来源必须精确匹配报告来源URL，日期必须等于固定
快照日期，有效期最长365天。严格Schema及确定性复核通过后才写入Linux私有
`reviewed_overlay.json`，人工当前配置优先，overlay只作为缺失/过期时的后备输入。
候选必须使用策略文件中的 `qualitative-score-rubric-v1.0.0`：四个等权维度的
整数分按0.5向上取整得到候选值，结构性/治理红旗分别把上限压到49/39；分值与
维度均值或红旗上限不一致会被确定性拒绝。
每项overlay都固定标记 `produced_by_codex=true` 和
`review_status=DETERMINISTICALLY_ACCEPTED`；旧报告没有该审核产物，不能自动转成
配置。候选字段不会同步到Ali。公开报告不含推理过程、stdout JSONL、stderr或输入目录。

## 存储

```text
$SHAREHOLDER_V2_LOCAL_ROOT/
  analysis/jobs.sqlite3
  analysis/jobs/<job_id>/input/{company_snapshot,metric_definitions,trigger,
      previous_analysis,source_index,prompt_metadata,sha256sums}.json
  analysis/jobs/<job_id>/rendered_prompt.md
  analysis/output/<company_id>/runs/<job_id>/
      input/ final.json report.md reviewed_overlay.json
      run.events.jsonl stderr.log metadata.json
  analysis/output/<company_id>/latest.json
```

只有剔除overlay候选后的公开 `latest.json`、`report.md`、来源摘要和白名单化的
`status.json` 进入 public analysis release；`reviewed_overlay.json` 永远留在Linux。
新任务等待或失败时，`status.json` 更新，但 `latest.json` 和
报告继续指向最后一次成功结果；认证详情、error_message、result_path均不发布。前端使用
`public/modules/safe_markdown.js`：转义原始HTML，仅允许HTTP(S)链接并添加安全
属性，不把模型文本直接赋给 `innerHTML`。

## 测试与真实冒烟

普通测试使用 `scripts/support/fake_codex.py`，覆盖合法输出、候选来源/有效期审核、Schema失败、超时、
非零退出、认证、模型不可用、ID/哈希错、重复任务、上传失败和重启恢复，不消耗
模型额度。

真实调用只能显式执行：

```bash
make codex-smoke-test SNAPSHOT=/absolute/path/to/VALID-company.json
```

命令还要求内部 `--confirm-real-codex`，且输入必须为 VALID。
