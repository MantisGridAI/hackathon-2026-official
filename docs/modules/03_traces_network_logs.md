# M3 — Traces、网络线索与定向日志

负责人角色：Traces / Logs。目标：补足 metrics 看不到的候选和机制线索，提供可靠的调用关系、延迟/错误对照及原始记录引用。

共享类型和完整签名见 [rca-v1](../INTERFACES.md)；数据接口由 [M1](01_contract_data_runtime.md) 提供。本文件是实施规范，不代表所有工具已经完成。

## 1. 输入、输出、效果

| 项目 | 契约 |
|---|---|
| 输入 | CaseContext、TelemetryStore、deadline；补查时的组件/边/问题 |
| 输出 | AnalysisBundle：独立 trace 候选、依赖边、时序事实、日志证据、缺失与不确定性 |
| 目标效果 | metrics 不突出的网络/调用异常仍有机会进入候选；能沿原始 trace/log 找到证据 |
| 不负责 | 最终根因排名、决定模型是否升级、随意执行模型生成代码、读取评分答案 |

## 2. 调查工具与复核入口

```python
triage_traces(case, store, *, deadline) -> AnalysisBundle
inspect_dependencies(case, component_ids, edges, store, *, deadline) -> AnalysisBundle
search_fault_evidence(case, component_ids, patterns, sources, store, *, deadline) -> AnalysisBundle
replay_evidence(record, store, *, deadline) -> dict
```

第一轮 `triage_traces` 做覆盖窗口的轻量摘要，**不要求组件先进入 metrics shortlist**。之后只对重要组件、operation 和边深查。该设计避免网络故障在 service/metrics triage 阶段被过滤掉。

只有 M4 调用这些工具；M3 不自行启动 Agent loop，不直接再调用 M2。

## 3. 时间与父子关系

- 使用原始 trace timestamp 的毫秒单位及 UTC+8 显示。
- 用 `(trace_id, parent_span)` 配对 `(trace_id, span_id)`，避免仅 span_id 连接造成跨 trace 错配。
- 记录重复 key、缺失父 span、边界截断、type/status 混合情况；不完整 trace 不能声称完整路径。
- 查询窗口边界可带有界 padding，保留父子上下文；超出题目窗口的上下文不作为窗口内故障直接标签。
- `duration` 单位必须单独确认；未知时只显示 native values 和同类操作相对变化。
- parent/child 开始时间差不等于网络延迟；并发、调度、异步 span 和跨机时钟都影响解释。
- 父耗时减子耗时不一定等于自身处理时间；多个重叠子 span 不可简单相减。

**调用图是依赖关系证据，不自动构成因果图。** 调用方向不等于故障传播方向：被调用方异常可能让调用方等待变慢。参考计划中的 downstream/upstream penalty 只能在明确依赖语义和可靠异常先后时采用，无法确定时给 unknown。

## 4. 输出哪些特征

按 component + operation + type 分组，比较参考期与窗口/episode：

- span 数、单位时间观测频率；参考区间长度不同必须归一化。
- duration 的 median/p95、样本数与单位状态；少量样本的 p95 不当稳定统计量。
- 各种原始 status_code 的频数；`0`、`Ok` 等不能按单一非零规则混判。
- 父子配对覆盖、异常边及开始差分布；时间差异常只作候选证据。
- 代表性 trace ID、span ID、父 ID、operation、源文件和记录定位。
- 对多个组件共变，说明谁可能是等待者、谁可能是被等待者及依据；无足够语义就不作 causal claim。

Trace 观测 span/min 不等于全业务请求量，采样率和未追踪后台任务都需保留为限制。

## 5. 网络标签和 logs

十五个官方原因中的网络子类可能无法仅凭同类 latency 异常区分。工具应输出“支持 network family，subtype unresolved”，不要凭关键词强制区分 packet loss、corruption、retransmission。

如果终止时仍模糊，M4 必须选一个合法原因作最佳猜测；M5 在 evidence 明确未知。不能因为参考计划写了“Require fault-specific evidence”就拒绝输出，违反 always guess。

定向 logs：

1. M4 指定要区分的假设、组件和时间。
2. 先查 service logs，必要时查 proxy/mesh；后者首次读取成本计入预算。
3. 搜索 error/timeout/reset/refused/OOM/killed/retry 等只是第一版筛选，返回原文和上下文。
4. 没有关键词命中，输出 searched_rows/matched_rows/查询范围；不输出“没有故障”。
5. 保留正反证：例如 timeout 本身也可能是传播症状。
6. 相同查询签名不重复扫描；来源缺失时返回 missing，不悄悄换成其他部署数据。

## 6. 示例交接含义

row 0 本地分析提示：读活动显著变化时，GetQuote 的 duration 中位数可能没有明显变化。该观察用于提出需要重新验证的对照，不是本仓库已发布的复现实验。实现时保存实际参考期/episode、样本数、duration 和 span/min；即便统计不变，也只支持该 operation 的有限观察，不支持“全服务完全健康”或“网络已排除”。

M3 应将这类结果作为中性/矛盾证据交给 M4，而不是为了配合 read I/O 答案生成一段不存在的 trace slowdown。

## 7. 第一版交付与验收

- 能查询窗口内所有组件的基础 trace 摘要，再对指定组件深查。
- 缺失 parent、重复 ID、非数字 status、异步/不同 type、少量样本均被明确标记。
- 同 operation、不同区间长度的对照有样本数和频率归一化。
- 显示的 trace/log ID 在源数据中存在；M5 能自动定位抽查。
- 工具不把 native duration 写成未经确认的 ms，也不把 parent gap 称为已测网络延迟。
- 无 metrics 异常的组件仍可进入 trace 候选。
- row 0 缺乏强 trace 变化时如实返回；再选网络类开发窗口检验工具是否有实际价值。

首版优先 trace 正确配对、轻量候选和 service log。复杂拓扑评分、proxy/mesh 扩展在初步链路跑通后加入，并由耗时和增量证据决定保留。

## 8. 文件所有权

M3 独占仓库根相对路径 `track-1/starter/agents/rca/traces.py`、`track-1/starter/agents/rca/logs.py`、`track-1/starter/agents/rca/network.py` 和 `track-1/starter/tests/m3/`。底层 CSV、缓存与时间转换通过 M1；候选融合归 M4；证据文字渲染归 M5。


## 9. 接线与交给 AI 的任务

traces.py 导出 triage/inspect/replay；logs.py 导出 search。triage 返回 `AnalysisBundle.edges` 中完整 DependencyEdge，M4 再把选择的 edges 交 inspect；不接受没有生产端的任意 edge_id。证据与候选 ID 使用 M1 helper 的 m3 命名空间。replay 按 transform 分派到本模块内部 log/network helpers，不让 M5 重写 trace 统计。

日志 patterns 首版为不区分大小写的字面子串，不执行模型代码或任意正则。baseline/window 使用多条 QuerySpec。无匹配、没查询、文件缺失和低样本分别标记。网络弱证据仍可形成未定 subtype 的候选；最后猜什么由 M4 负责。

从 starter 运行本模块实现的测试：
```bash
python -m unittest discover -s tests/m3 -p 'test_*.py'
```

> 你只负责 M3。先读根 AGENTS.md、TEAM.md、docs/INTERFACES.md、docs/INTEGRATION.md 和本文件；用 M1 Store 实现独立 trace triage、同 trace 父子配对、DependencyEdge、指定依赖深查、定向日志与 replay_evidence，按 rca-v1 返回 bundle。只改 M3 文件/tests/m3；不改 M2、controller、contracts、依赖或最终 renderer，不调用模型。先交可定位的真实 trace/log 证据和缺父/重复/混合status/低样本测试，再扩展 mesh。清楚区分依赖关系和因果推断；无数据不等于正常。返回实际检查与可复现的输入/输出/限制。
