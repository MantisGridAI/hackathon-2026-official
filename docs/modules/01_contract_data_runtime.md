# M1 — 任务契约、数据访问与运行集成

负责人角色：集成 / 数据基础设施。目标：让所有分析模块在正确时间、正确组件、可控资源范围内读同一份数据，并保证评委执行的是团队 Agent。认领状态见 [TEAM.md](../../TEAM.md)。

本文是实施规格，不代表已实现。[INTERFACES.md](../INTERFACES.md) 是共享类型与函数签名的唯一来源；不要从 notebook 或旧稿另建类型。

## 1. 输入、输出与边界

| 项目 | 契约 |
|---|---|
| 输入 | instruction、dataset_dir、runner 的 ctx（含 out_dir）、M4 的 RunConfig |
| 输出 | CaseContext、TelemetryStore、跨 case 复用的 RunState、ComponentCatalog、公共类型/稳定 ID |
| 最终效果 | M2/M3 不再自行读 CSV 或转换时间；M4 有统一预算状态；M5 能重放证据查询 |
| 不负责 | 指标特征、trace 解释、根因选择、模型策略、证据文字或官方评分 |

官方入口为 `solve(instruction, dataset_dir, ctx) -> Solution`；不直接传 task_index/row_id。解析题目确定字段、故障数量和窗口，runner 保留原 row_id 用于输出。case_key 仅用于缓存/诊断，不能取代原始输出 ID。

## 2. 先交公共接口，再完成性能与打包

第一笔实现 PR 交 contracts、七类题目 parser、窗口读取的最小版本及无答案 fixture。公共类型 import 不初始化 LLM、不需要 key、不 import 尚未完成的 M2–M5。

对外 API：

```python
parse_case(instruction: str, *, reference_minutes: int = 10) -> CaseContext
get_run_state(dataset_dir: Path, ctx: dict, config: RunConfig) -> RunState
stable_id(case_key: str, namespace: str, payload: dict) -> str

# state.store
store.iter_window(query: QuerySpec, *, deadline: float) -> Iterator[pd.DataFrame]
store.query_id(query: QuerySpec) -> str
store.coverage(query: QuerySpec) -> Coverage
store.component_catalog() -> ComponentCatalog
```

精确字段、coverage 生命周期、SourceRecord、RunState、合法标签及 ID 规则全部按 [rca-v1](../INTERFACES.md)。底层实现使用 pandas chunks 或等效有界 CSV 读取。正确解析带逗号/换行字段，不使用文本行 split。

## 3. 数据与时间正确性

- 内部过滤以 epoch 为准；metrics/logs 秒，trace timestamp 毫秒；显示/答案 UTC+8。trace duration 单位另查。
- 跨日查询读取涉及的日期，默认参考期 start 前 10 分钟；缺一日返回 partial/missing，不伪装完整。
- 数据块保留源文件、CSV 数据记录号、raw timestamp 和 canonical component；不能过滤后重新编号。
- 文件可能无序，没有验证索引前不按第一条越界记录结束扫描。
- 只读 telemetry 白名单，禁止查询 dev/答案来源、路径穿越、dataset 外符号链接。
- mesh 多端、service 聚合和 node/pod 的映射分来源实现；未知关系明确记录，不能通用 split 猜出全部来源。
- 目录随读取增量扩充，不能仅为得到所有组件就预扫描整个12 GB数据。

## 4. 运行状态、缓存和边界

RunState 放在跨 case 复用的 `ctx['rca_state']`；包含 store、解析后的 config、deadline、模型健康/usage容器。M1 建生命周期，M4 管理模型客户端和调用账本；不要同时维护两套费用或 breaker。

- 扫描在 chunk 间检查 monotonic deadline；主动关闭/超时需完成 coverage 的 partial 标记。
- 小结果可物化；缓存仅在 --out，包含数据身份、QuerySpec 和 transform 版本。设置容量边界，不累计整日大表。
- source/columns/filter 不合法时有明确异常；真正没数据与读失败分开。
- runner 初始化时间也计入整个运行；不能把 solve 前的导入和缓存建立视作免费。
- 输入只读、所有生成物在 out；没有 key 的 deterministic 模式必须可运行。

## 5. 实际文件所有权

以下都是仓库根相对路径：

- `track-1/starter/agents/rca/{__init__,contracts,data_access,runtime}.py`
- `track-1/starter/run.py` 的必要默认入口/计时改动；保留 CLI、原始 row_id、异常隔离和逐题保存
- `track-1/starter/requirements.txt`、`track-1/Makefile`、最终根 `Dockerfile` / `.dockerignore`
- `track-1/starter/tests/m1/`、`tests/integration/`、`tests/fixtures/`（后两者同 starter 下）、公共 tests 初始化
- 公共契约文档和集成检查；其他模块自己的测试归自己

M4 独占 `track-1/starter/agents/routed.py` 和 `llm.py`。M1 不在它们里面添加调查逻辑。

## 6. 最终打包目标

不搬出第二套 Python 源码。完成时将唯一 Dockerfile 迁到仓库根，构建时复制 starter 到容器 /app，保持容器内的 run.py。迁移同一 PR 更新 Makefile build context，移除旧 Dockerfile，排除 data/out/keys。具体操作门槛见 [INTEGRATION G4](../INTEGRATION.md)。

默认 agent 从 heuristic 改为 routed 的时机是 G2 接线通过之后。`make validate AGENT=agents.routed` 不验证默认值；另测不带 --agent 的官方命令。不能同时保留两个 Dockerfile 或两套 Agent。

## 7. 验收与交接

1. UTC+8 09:00 对应 01:00 UTC；trace 的 ms 过滤正确，原始 epoch 不丢失。
2. 七类字段组合、多故障、跨午夜、无法解析窗口有明确结果/异常。
3. 带逗号/换行、无序、缺文件、空匹配、deadline、iterator 早关均有对应测试。
4. M2/M3 只依赖 Store 即可读真实窗口，M5 可从 records/queries 重放定位。
5. 非连续 row_id 输出保持原值；cache/state 不跨 dataset/out 错复用。
6. 默认入口和 Docker 在真实2 CPU/8 GB、只读输入条件下通过，记录端到端耗时/内存。

开发验收从 starter 执行：
```bash
python -m unittest discover -s tests/m1 -p 'test_*.py'
python -m unittest discover -s tests/integration -p 'test_*.py'
```

测试由本模块实现；不得把 0 tests 或真实数据测试的 skip 当完成。交付一个真实窗口 QuerySpec、返回块/coverage样例、复现命令和限制。

## 8. 交给 AI 的任务

> 你只负责 M1。先读根 AGENTS.md、TEAM.md、docs/INTERFACES.md、docs/INTEGRATION.md 和本文件，再核对现有 starter。按 rca-v1 实现共享类型、数据窗口/定位/coverage、RunState 和最小无答案 fixture，先提交可供 M2–M5 导入的小版本。不要写 metrics、traces、controller、evidence 或改变官方 accuracy evaluator；不要调用付费模型。之后完成默认入口、唯一根 Dockerfile、依赖及 Makefile 集成。所有写入属于所属文件；接口变更先说明受影响消费者。收尾给出实际检查、真实调用输入输出、缺失/超时行为、资源结果和仍未通过的验收。不要把占位实现标记 verified。
