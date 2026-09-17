# M2 — 指标分析、资源候选与 onset

负责人角色：Metrics。目标：将真实指标变成可复核的候选、变化开始区间、同 pod/node 对照；为 M4 提供事实，不直接宣布最终根因。

共享类型与完整签名以 [rca-v1](../INTERFACES.md) 为准；数据访问由 [M1](01_contract_data_runtime.md) 提供。本文件是实施规范，不是当前实现完成声明。

## 1. 输入、输出、效果

| 项目 | 契约 |
|---|---|
| 输入 | CaseContext、TelemetryStore、剩余数据处理时间；可选指定组件 |
| 输出 | AnalysisBundle：指标候选、onset 区间、资源特征、证据与覆盖 |
| 使用者 | M4 排名和决策；M3 可收到 M4 转交的候选位置；M5 渲染事实 |
| 目标效果 | 同一组件的开始时间、异常强度、持续性、对照组件与节点关联可追溯 |
| 不负责 | GLM、跨来源最终排名、span 分析、CSV 输出、开发答案评分 |

## 2. 工具接口

```python
triage_metrics(case, store, *, deadline) -> AnalysisBundle
inspect_metrics(case, component_ids, metric_families, store, *, deadline) -> AnalysisBundle
compare_replicas_and_node(case, component, store, *, deadline) -> AnalysisBundle
replay_evidence(record, store, *, deadline) -> dict
```

第一轮 triage 必须同时考虑 service 摘要和 container/node 的粗粒度覆盖；详细查询才收窄到少量候选及其邻居。

## 3. Service triage 只能排序，不能硬过滤

参考计划提出 `metric_service → shortlist → container/node`，读取小文件的思路值得采用，但服务级正常并不证明每个 pod 正常。我们的 row 0 本地分析提示：pod 读活动强烈异常时，某 operation 的 trace 中位耗时仍可能基本不变。

采用以下策略：

1. 读取 service 的 rr/sr/mrt/count 作为优先级线索；确认含义与聚合口径，不把字段名字直接当成已知百分比或每秒速率。
2. 对所有有覆盖的 node/pod 做廉价资源摘要，保留 service 信号不明显的 pod 候选。
3. 合并候选，交 M4 决定深入查询顺序。
4. M3 的独立 trace 候选仍可加入；M2 不能删除它们。

记录 service-only shortlist 会漏掉多少已知开发根因，由 M5 作为候选召回诊断报告。

## 4. 指标到特征的处理

先定义机制相关的指标族：CPU、memory、read I/O、write I/O、process/restart、network-related hints。

- 每条序列保留原始 cmdb_id/kpi_name/单位和采样时间。
- 对指标说明是否为 gauge、counter 或语义未确认；只有确认 counter 才做差分/速率，并处理 reset。
- 参考段先用窗口前邻近数据，但标记它可能已经异常。增加敏感性比较，不把全日窗口外数据视为永远健康。
- 计算 baseline 的 n/median/MAD，窗口 min/max/median、持续变化点、缺口等。
- MAD=0 必须有“稳定→变化”路径，不能直接丢弃，也不能只用极小分母制造无意义的天文分数。
- 筛选分数与置信度区分。常量、比值下限、异常阈值记录在配置；不把不同单位下的原始峰值直接相加。

输出示意（省略其他必填字段，不是可直接运行的 fixture，也不是虚构实验值）：

```python
Candidate(
    component=observed_pod,
    reason='container read I/O load',  # 机制候选，尚非最终断言
    onset_interval=(last_normal_sample, first_sustained_change),
    onset_estimate=best_sample_based_estimate,
    features={
        'baseline_median': measured_median,
        'baseline_n': reference_sample_count,
        'window_peak': measured_peak,
        'persistence_samples': observed_persistence,
        'replica_contrast': measured_same_metric_contrast,
        'sampling_interval_s': observed_cadence,
    },
    supporting_ids=metric_evidence_ids,
    contradicting_ids=counterevidence_ids,
    unresolved=['sampled ordering does not prove causal precedence'],
)
```

同一序列的多个派生特征或高度相关的 read-count/read-volume 不计为多份独立证明。

## 5. Onset 与多故障

- 优先最早持续变化，保留最后正常点与首个异常点之间的区间。
- 持续判定考虑真实采样间隔；中间有大缺口不算连续。
- onset_estimate 只是最佳估计；不能输出秒级伪精度却不说明分钟采样。
- 短尖峰需要独立检测路径，否则会漏掉 `node CPU spike`。
- 同一组件可能在窗口内出现多个 episode；多个组件也可能属于同一 episode。先保留候选 episode，由 M4 合并/分离。
- 未知 onset 保留 None 并说明覆盖，M4 在最终必须回答时间时作保守猜测；不要把峰值自动伪装成 onset。

## 6. Node / container 对照

同一 KPI 比较兄弟 pod，并检查同节点不同服务。需要记录：是否同时变化、幅度和持续性、采样区间是否允许判断先后。

“多 pod 同节点异常”只是 node 假设的支持；“node 和 pod 都异常”不等于 node 为根因。若变化落在同一采样间隔，输出 `ordering_uncertain`，禁止编造四秒领先等数值。

资源上升也不必然代表根因。row 0 中读 I/O、CPU、内存及节点 I/O 共变，M2 必须全部保留；不能为了输出 read I/O 删除 CPU/node 证据。

## 7. 实施顺序与验收

1. 用 M1 窗口 API，完成 container/node 的基本特征与 coverage。
2. 加入 MAD=0、缺失、counter reset 和 onset 区间处理。
3. 加入 sibling/node 对照及轻量 service 摘要。
4. 生成有来源和查询参数的 EvidenceRecord，不让 M5 重算指标。

第一版验收：

- row 0 的 `shippingservice-1` 来自扫描结果而非名字白名单；可复核读通道突变。
- 09:08–09:09 的资源变化区间与 09:10 峰值分开表示；这只是本例观测，不写死时间。
- 即使 baseline 全零，候选仍保留。
- 给定无数据窗口返回 coverage=empty，不写 healthy。
- 具有跨 episode 表达能力；未知部署 ID 不被拒绝。
- M5 可测 candidate recall、所需时间字段误差、冷启动耗时；不以单个例子的正确率验收通用方法。

## 8. 文件所有权与交接

M2 独占仓库根相对路径 `track-1/starter/agents/rca/metrics.py`、`track-1/starter/agents/rca/onset.py`、`track-1/starter/tests/m2/`。通过 AnalysisBundle 交 M4，不写 `routed.py`、共享 parser、模型客户端或最终 evidence renderer。

第一轮交一个完整真实窗口的 bundle 与一段复现调用；M4 可先用保存的小型无答案 bundle 开发。阈值变化随配置版本记录，不能只在 notebook 手工调整后忘记同步。


## 9. 接线与交给 AI 的任务

M1 是唯一 CSV/时间转换来源；`triage_metrics` 等函数在 `agents.rca.metrics` 导出，onset.py 为内部帮助模块。M4 按 rca-v1 调用；M5 通过 `metrics.replay_evidence` 重算指标 EvidenceRecord。baseline/window、replica/node 的比较把全部 QuerySpec 放进 queries；单位逐数值 key 记录。不要只留一句“显著增加”而丢原值。

真实开发检查优先 row 0 窗口与至少一个不同资源/节点窗口；本地单例观察不是已随此仓库发布的 benchmark，必须由本模块重新生成可定位的结果。不得把 shippingservice-1、09:09 或 read I/O 写进通用检测规则。

从 starter 运行本模块实现的测试：
```bash
python -m unittest discover -s tests/m2 -p 'test_*.py'
```

> 你只负责 M2。先读根 AGENTS.md、TEAM.md、docs/INTERFACES.md、docs/INTEGRATION.md 和本文件；确认认领状态。使用 M1 Store，实现指标粗筛、机制特征、onset区间和 replica/node 对照，以及 replay_evidence，按 rca-v1 返回 AnalysisBundle。共享接口未实现时可 mock 测试，但不创建第二份 contracts 或 CSV reader。只修改 M2 文件和 tests/m2；不改最终排名、调用模型、routed.py、依赖或 formatter。先交一个真实 bundle、缺失/恒定序列/缺口/尖峰测试，再优化阈值。返回实际检查、输入输出、候选漏检与限制，不把单案例得分称通用性能。
