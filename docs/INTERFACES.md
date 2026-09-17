# Shared interfaces — rca-v1

这是五模块共同实现的接口规范。**本文件是共享结构与签名的唯一维护源**；实现类型位于 `track-1/starter/agents/rca/contracts.py`，其余模块导入，不复制。版本为 `rca-v1`。添加可选字段保持默认值；重命名、删除或改语义先提交契约 PR，由受影响模块同步迁移。

实现位置：`runtime.py` 提供 parser/RunState，`data_access.py` 提供有界 CSV store，`contracts.py` 提供 `stable_id` 与类型。`stable_id` 的规范化 JSON envelope 为 `{"case_key": case_key, "payload": payload}`。证据的 `transform_params.retained_rows_per_query` 记录实际消费的匹配记录前缀长度，partial 扫描也按该前缀重放；重放无法取得足量原始输入时显式报未验证，不使用后续记录替换。

诊断文件不是官方 Solution 的扩展字段：M4 保存 `out/diagnostics/evidence/<invocation_index>.json`，包含 case、最终 decision、完整 evidence 与 catalog，供离线审核；M1 runner 保存 `out/diagnostics/attempts/<attempt_id>.json` 的原始 row_id 映射。续跑从现有 invocation 最大值继续，避免覆盖旧证据；M5 汇总所有尝试的 usage。

所有实现路径从仓库根计算。运行时 import 以 `track-1/starter/` 为 Python 根，如 `from agents.rca.contracts import CaseContext`。以下结构推荐用 dataclass / TypedDict / Protocol，不引入新框架。

## 1. 基本约定

- 内部时间是 aware `datetime`（UTC+8）；区间 `[start, end)`。只有 M5 在输出边界格式化为 `%Y-%m-%d %H:%M:%S`。
- `deadline: float` 一律为 `time.monotonic()` 的绝对截止值，不是秒数或 Unix 时间戳。
- `None` 表示未知；禁止把未知、无匹配、没查和失败都编码为 0。
- `case_key` 是 instruction 的稳定摘要，不是 row_id。官方 runner 保留真实 row_id，团队不重编号。
- 数值用于排序的 score/margin 不等于概率；confidence 仅 `low / medium / high`。
- source 固定为 `metric_container / metric_node / metric_service / metric_runtime / metric_mesh / trace_span / log_service / log_proxy`，映射到官方同名 CSV。只允许 `telemetry/<date>/{metric,trace,log}/` 内的数据，拒绝路径穿越和逃出 dataset 的符号链接。
- reason 的合法列表以 [官方 data.md](../track-1/docs/data.md#the-possible-reasons) 的 15 个精确字符串为准；M1 导出 `NODE_REASONS`、`CONTAINER_REASONS`、`LEGAL_REASONS` 三个 `frozenset[str]`，测试与官方现有标签集一致，不沿用 heuristic 的 KPI→标签规则作为诊断结论。

## 2. Case / Query / Coverage

```python
CaseContext:
    case_key: str
    instruction: str
    start: datetime
    end: datetime
    reference_start: datetime          # v1 默认 start - 10 min，可配置；不是健康保证
    failure_count: int                # >= 1
    requested_fields: tuple[str, ...] # 从 (datetime, component, reason) 按序取子集
    parse_status: str                 # ok / degraded
    parse_warnings: list[str]

QuerySpec:                            # frozen / 不可变
    source: str
    start: datetime
    end: datetime
    columns: tuple[str, ...]          # 官方原始列名
    component_ids: tuple[str, ...] | None = None
    operation_names: tuple[str, ...] | None = None
    kpi_names: tuple[str, ...] | None = None

Coverage:
    query_id: str
    source: str
    status: str                      # not_queried / complete / empty / partial / missing / failed
    rows_scanned: int
    rows_matched: int
    first_time: datetime | None
    last_time: datetime | None
    missing_value_count: int | None
    duplicate_key_count: int | None
    warnings: list[str]
```

Query 的组件过滤使用 canonical node/pod ID；M1 按来源映射原始 ID。service 聚合数据可按观察到的 pod→service 关系映射，但不能声称隔离出了单 pod；mesh 匹配任一端并保留 source/destination。关系未知则标记不能完成该过滤，不猜 ID。`None` 表示不限制；空 tuple 表示匹配零项。不支持的 source/column/filter 组合抛 `QueryValidationError`，不能悄悄忽略。

`complete` 只表示指定文件/日期/过滤范围完整扫描，不证明遥测本身无采样缺失。完整扫描无匹配用 `empty`；缺文件用 `missing`（部分文件已扫时为 `partial` 并列缺失文件）；截止、中断或主动关闭 iterator 用 `partial`；读取不可恢复错误用 `failed`。未测缺失/重复数用 None，不伪装零。coverage 从不自动触发新的扫描。

M1 的数据块保留调用者请求的官方原始列，并始终增加：

| 列 | 语义 |
|---|---|
| `_source_file` | dataset 相对路径，禁止绝对本机路径 |
| `_record_index` | CSV header 后 1-based 数据记录序号，不是物理文本行号 |
| `_timestamp_s` | 规范化 epoch 秒；原始 timestamp 仍保留 |
| `_component_id` | canonical node/pod ID；service/mesh 无唯一组件时 None |

禁止排序、过滤后重算原始记录号。trace 原始 timestamp 除 1000 生成规范化列；duration 不跟随转换。M1 提供 source-specific 组件解析，保留 service/mesh 原字段供 M2/M3 检查。

## 3. ComponentCatalog 与数据读取

```python
Component:
    component_id: str                # 精确 node / pod 名称
    kind: str                        # node / container
    raw_ids: tuple[str, ...]
    node_id: str | None
    service: str | None
    source_files: tuple[str, ...]

ComponentCatalog:
    components: dict[str, Component]  # canonical ID → 观察到的组件
    raw_to_components: dict[tuple[str, str], tuple[str, ...]] # (source, raw ID)，mesh 可多端
    coverage: list[Coverage]
```

目录在扫描中增量扩充，不能为全量目录先把 12 GB 数据扫一遍；初始化只用有界的小数据，覆盖有限必须披露。M4 选择当前观察到的组件；trace 新 ID 也能扩充目录，不限定开发部署名字。

M1 实现：

`contracts.py` 声明 `TelemetryStore` Protocol；具体 CSV 实现为 `data_access.CSVTelemetryStore`。公共类型不得反向 import data_access 或模型客户端，避免循环依赖。

```python
parse_case(instruction: str, *, reference_minutes: int = 10) -> CaseContext
get_run_state(dataset_dir: Path, ctx: dict, config: RunConfig) -> RunState
stable_id(case_key: str, namespace: str, payload: dict) -> str

# state.store；M1 提供具体类，消费者按以下接口使用
store.iter_window(query: QuerySpec, *, deadline: float) -> Iterator[pd.DataFrame]
store.query_id(query: QuerySpec) -> str
store.coverage(query: QuerySpec) -> Coverage
store.component_catalog() -> ComponentCatalog
```

调用者 `try/finally` 关闭 iterator，完整消费才记 complete；M1 在 chunk 间检查 deadline 并最终更新 coverage。已获得数据可用于 partial bundle，必须附部分覆盖状态。不保存隐式全日 DataFrame。缓存键包含数据身份、完整 QuerySpec 与变换版本；缓存命中仍返回可验证 coverage。索引未证明有序前不能按遇到第一条超时记录早停。

无法解析窗口时 `parse_case` 抛 `CaseParseError`；M4 捕获后不编造 CaseContext/真实证据，走低信心格式化降级。可合理解析但有歧义时返回 degraded 和 warnings。M1 必须覆盖七种字段组合、多故障和跨午夜。未知 instruction 不是读取 dev 答案的理由。

## 4. Evidence / Candidate / AnalysisBundle

```python
SourceRecord:
    source_file: str
    record_index: int | None
    trace_id: str | None
    span_id: str | None
    log_id: str | None

EvidenceRecord:
    evidence_id: str
    kind: str                        # metric / trace / log / coverage
    component_ids: list[str]
    interval: tuple[datetime, datetime]
    source_files: list[str]
    queries: list[QuerySpec]          # baseline + incident / replica / node 可多条
    transform: str                   # 版本化标识，例如 metrics.baseline_compare.v1
    transform_params: dict           # 分组、参考期、窗口、缺失策略、阈值等
    values: dict                     # 仅观测/计算值，JSON 可序列化，无 NaN/Inf
    units: dict[str, str]            # 与数值 key 对应，未知 native/unknown；计数 count
    source_records: list[SourceRecord] # 小型定位样本；非聚合的完整输入副本
    coverage: list[Coverage]         # 每个 query 对应的状态
    limitations: list[str]

Candidate:
    candidate_id: str
    component: str
    reason: str | None
    onset_interval: tuple[datetime, datetime] | None
    onset_estimate: datetime | None
    features: dict                   # 模块命名空间，例如 metrics.read_contrast
    supporting_ids: list[str]
    contradicting_ids: list[str]
    unresolved: list[str]
    episode_id: str | None

DependencyEdge:
    edge_id: str
    caller: str
    callee: str
    operation: str | None
    supporting_ids: list[str]
    limitations: list[str]

AnalysisBundle:
    module: str                      # m2 / m3
    candidates: list[Candidate]
    evidence: list[EvidenceRecord]
    coverage: list[Coverage]
    edges: list[DependencyEdge]       # M2 返回 []；M3 创建，M4 保存/传回深查
    suggested_queries: list[QuerySpec]
    warnings: list[str]
```

列表字段可默认空列表（dataclass 用 default_factory，不用可变共享默认值）。Candidate 与证据、edge 的 ID 用 M1 `stable_id`：namespace 分为 `m2.candidate / m2.evidence / m3.candidate / m3.evidence / m3.edge / m4.candidate`；payload 包含 case、组件、查询签名及变换版本。相同 ID 内容不同是集成错误，M4 不得覆盖后继续假装正常。输入多个 bundles 后 M4 去重并保留 provenance。

stable_id 输出 `namespace:hash`；hash 对 case_key 与规范化 payload 的 JSON（排序键、datetime 转含时区 ISO-8601、禁止 NaN/Inf）做 SHA-256 后取前16位。不同查询/变换必须产生不同 payload；helper 由 M1 唯一实现，消费者不各自散列。

每个 EvidenceRecord 的 query 列表和 transform 必须足以重新计算显示的值；原始记录样本仅用于定位，不能声称用 5 条样本重算了全部 p95。M2/M3 为各自 transform 提供 `replay_evidence(record, store, *, deadline) -> dict`，返回同一 values schema，M5 审核时使用。阈值配置纳入 transform_params/版本。证据事实与解释分开；没有来源的模型文字不进 values。

`reason=None` 的候选可进入排名，但模型决策前 M4 必须把需要原因的候选扩展为合法 component/reason 假设并保留原证据来源；未知 network subtype 的备选可分别作为弱假设，绝不当新增观测。M3 是 edge_id 的唯一生产者，M4 只传实际返回过的 IDs。

## 5. 模块公共函数：固定签名

```python
# M2 — agents.rca.metrics；onset.py 是模块内部帮助函数
triage_metrics(case: CaseContext, store: TelemetryStore, *, deadline: float) -> AnalysisBundle
inspect_metrics(case: CaseContext, component_ids: tuple[str, ...],
                metric_families: tuple[str, ...], store: TelemetryStore,
                *, deadline: float) -> AnalysisBundle
compare_replicas_and_node(case: CaseContext, component: str,
                          store: TelemetryStore, *, deadline: float) -> AnalysisBundle
replay_evidence(record: EvidenceRecord, store: TelemetryStore, *, deadline: float) -> dict

# M3 — triage/inspect/replay in agents.rca.traces; search in agents.rca.logs
triage_traces(case: CaseContext, store: TelemetryStore, *, deadline: float) -> AnalysisBundle
inspect_dependencies(case: CaseContext, component_ids: tuple[str, ...],
                     edges: tuple[DependencyEdge, ...], store: TelemetryStore,
                     *, deadline: float) -> AnalysisBundle
search_fault_evidence(case: CaseContext, component_ids: tuple[str, ...],
                     patterns: tuple[str, ...], sources: tuple[str, ...],
                     store: TelemetryStore, *, deadline: float) -> AnalysisBundle
replay_evidence(record: EvidenceRecord, store: TelemetryStore, *, deadline: float) -> dict
```

M3 traces 的 replay 入口按 transform 路由到自己的 logs/network helper。日志 patterns v1 是不区分大小写的字面子串列表，不执行模型给的正则/代码。metric_families 固定 `cpu / memory / read_io / write_io / process / network_hints`，不认识的参数报输入错误。可预期的数据缺失/超时返回带 coverage/warnings 的 bundle；编程异常由 M4 最外层隔离并记录失败，不能吞成 complete。

## 6. RunState、配置与模型记账

```python
RunConfig:
    schema_version: str              # rca-v1
    mode: str                        # deterministic / routed
    pinned_model: str | None
    reference_minutes: int
    case_soft_seconds: float         # 起始 45
    run_soft_seconds: float          # 起始 1080；端到端验证，非性能宣称
    case_cost_limit_usd: float        # <= 3，预留余量
    run_cost_limit_usd: float         # 起始 20，低于官方 25
    max_followups: int               # 首版 1
    max_model_stages: int            # 首版 2；HTTP 尝试另有总上限
    max_http_attempts_per_case: int  # 首版 4，重试/fallback均占次数

RunState:
    store: TelemetryStore
    config: RunConfig
    out_dir: Path
    started_monotonic: float
    deadline: float
    invocation_index: int            # 每次 solve +1；不是 row_id
    client: object | None            # M4 懒初始化的 LLM
    model_health: dict
    usage_ledger: dict               # {model: {calls, prompt_tokens, completion_tokens}}
    estimated_cost_usd: float
```

M1 建立并在 `ctx['rca_state']` 缓存 RunState；M4 提供 `agents.rca.routing.load_config() -> RunConfig`，管理模型状态与账本。共享 state 不得跨不同 dataset/out_dir 偷用。执行截止取 `min(state.deadline, case_start + config.case_soft_seconds)`；模型超时、重试等待和数据扫描都服从它。初始化/模块加载前的开销由 M1 入口计时或保守预留，并在外部整次计时中统计。

配置入口的最终实现要求：`RCA_MODE=deterministic` 完全不创建模型客户端；未设置时 routed；`RCA_MODEL=<允许 GLM ID>` 钉住所有实际调用且禁跨模型 fallback，**不强制每题调用**。其他预算用版本化配置常量，M5 在 manifest 保存解析后配置与 hash。不是当前 starter 已支持 RCA_MODE 的声明。

M4 追加 `out/diagnostics/routes.jsonl`，每次实际 HTTP 尝试一条：`case_key, invocation_index, stage, requested_model, actual_model, reason, fallback, status, latency_s, prompt_tokens, completion_tokens, estimated_cost_usd`；未知 usage/cost 记 null 并保留保守预算预留，不编造 0。HTTP 失败与无模型 bypass 也有事件，区别 request 与 bypass。Solution.usage 用本题前后差量，不写累计 usage。M1 的 ctx invocation_index 与输入顺序用于离线映射；续跑另建 attempt manifest，不假定它等于 row_id。

在任何正常/降级/异常返回前，M4 solve 都从 RunState 账本独立结算本题最终 usage，覆盖提前保存的 fallback Decision 中的旧 usage_delta；模型建议即使没被采用，已经发生的调用仍计入。不能因选了调用前的 fallback 就把费用报成零。

## 7. Decision → RenderedResult → Solution

```python
Answer:
    datetime: datetime | None        # 内部时间类型；未要求字段可为 None
    component: str | None
    reason: str | None

Alternative:
    candidate_id: str
    status: str                      # weakened / unresolved / excluded
    evidence_ids: list[str]
    explanation: str                 # 推断，不是测量

Decision:
    answers: list[Answer]
    selected_candidate_ids: list[str]
    confidence: str
    supporting_ids: list[str]
    alternatives: list[Alternative]
    limitations: list[str]
    stop_reason: str
    route_events: list[dict]
    usage_delta: dict

InvestigationResult:
    decision: Decision
    fallback: Decision
    evidence: list[EvidenceRecord]    # 合并后的完整账本

RenderedResult:
    prediction: str
    evidence: str
    validation_status: str           # valid / degraded / invalid
    warnings: list[str]
    errors: list[str]

# M4 — agents.rca.controller
investigate(case: CaseContext, state: RunState, *, deadline: float) -> InvestigationResult

# M5 — agents.rca.validation；evidence.py 由其内部调用
validate_and_render(case: CaseContext, decision: Decision,
                    evidence: list[EvidenceRecord],
                    catalog: ComponentCatalog) -> RenderedResult

# M4 — agents.routed；唯一正式 Agent 入口，保持官方契约
solve(instruction: str, dataset_dir: Path, ctx: dict) -> Solution
```

M5 不调用 M4、不改变答案、不读数据补事实；只对 requested_fields 做必填校验，组件/原因两者都已知时才检查层级相容性。未要求字段的 None 不使 time-only 等题型非法。

校验状态无状态地判定：形状、数量、所需字段、时间范围/格式、非法原因、已知层级冲突或悬空证据引用为 invalid；格式完整但 catalog 缺失/覆盖不完整导致组件无法确认时为 degraded，记录未经验证与低信心；证据或观测明显否定的无效组件为 invalid。覆盖完整也不是证明根因成立，valid 仅表示契约通过。M4 接受 degraded，但不得删去警告。

**只有 M4 能切换 Decision**：首选 invalid 时最多改用一次预存 fallback 并重新渲染；fallback 仍 invalid 则由 M4 紧急路径输出尽力猜测与四节失败说明，不要求 M5 第三次改变状态，不伪造 catalog。M5 只将 requested_fields 对应项交官方 formatter；未要求 onset 仍可用于内部排序。无法区分故障顺序时确定性排序并披露，不能补虚构精确时间。

parse_case 完全失败的紧急路径由 M4 包装最小合法数量/字段猜测并直接返回官方 Solution（四节说明“未能可靠解析”，无实测声明）；不伪造共享 CaseContext。对此路径单独测试并在评测中计失败/降级。

## 8. Offline eval 接口（M5）

根 `eval/` 不由 runtime 导入。M5 提供 `python eval/run_comparison.py --help` 与 `python eval/audit_run.py --help`；这是需实现的交付入口，不是现成命令。harness 使用 subprocess 调用唯一 starter runner，保存完整计划 row_id、配置、调用顺序、数据/代码身份与独立输出目录。提供 `--dry-run` 只列命令、不付费。

`audit_run(manifest, output_dir, dev_queries) -> RunReport`：报告计划/返回/缺失/重复/意外 ID、每题与平均 partial、fully_solved、证据覆盖、总时间、每模型 tokens/费用、route/fallback、完整性错误。缺失计零，重复/意外标 invalid，不隐藏在分母。`compare_runs(reports) -> ComparisonReport`：核对 case 清单、代码/工具/预算/价格/缓存条件一致，报告差异与重复次数；不一致不声称公平 routing 对照。
