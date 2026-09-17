# M5 — 确定性证据、输出校验与评测

负责人角色：Evaluation / Evidence。目标：把 Decision 变成符合官方接口、可核查的输出，并公平测量 Agent 的正确性、费用、耗时与失败。运行期渲染和离线评分是两条隔离路径。

共享类型与完整接口以 [rca-v1](../INTERFACES.md) 为准。本文件是实施规范，不能当成评测已完成声明。

## 1. 两类输入与输出

| 路径 | 输入 | 输出 | 不允许 |
|---|---|---|---|
| 运行期 | CaseContext、Decision、EvidenceRecord 列表、ComponentCatalog | prediction 字符串、四节 evidence、校验状态 | 读取 scoring_points、借答案补证据 |
| 离线评测 | 冻结的 case manifest、每配置输出、dev labels、模型与价格配置 | strict/partial、覆盖率、费用、时间、分组错误和对照报告 | 丢弃漏题、修改官方 matching、把已调参分数称为 unseen test |

接口草案：

```python
validate_and_render(case, decision, evidence, catalog) -> RenderedResult
audit_run(manifest, output_dir, dev_queries) -> RunReport
compare_runs(run_reports) -> ComparisonReport
```

`RenderedResult` 包含 prediction/evidence/validation_status/warnings/errors；M4 用它组成 Solution。M5 不自己写 `evidence/<row_id>.md`，因为官方 runner 持有原始 row_id 并完成逐题写入。

## 2. Prediction 校验

复用官方 `format_prediction()` 和原样 `evaluate()`：

1. 输出恰好 failure_count 个对象；多故障按时间排序。
2. 只输出题目需要字段；相对顺序 datetime → component → reason。
3. 只对 requested_fields 检查必填项；要求的组件应来自合法 node/pod 目录，要求的原因来自官方十五标签。未要求字段可为 None，不让 time-only 题被组件校验拒绝。
4. 时间使用 UTC+8 的规定字符串；数值来自估计或明确最佳猜测，不能声称高于采样精度。
5. component/reason 都已知时校验 node/container 层级一致。模型给了错误层级，返回 invalid/errors；M5 不改答案，由 M4 最多切换一次预存 fallback 并重新渲染。
6. 别把 validation 的语义失败转成 runner 异常并丢失整题：M4 负责选择最佳猜测并记录问题，M5 仅检验/渲染，不形成双向调用循环。
7. 形状完整但 catalog 缺失/不完整无法确认组件时返回 degraded，明确未经验证；已知无效、非法标签或悬空证据等返回 invalid，具体规则按 rca-v1。仍按官方 evaluator 计算 accuracy，另记完整性/降级状态；不得擅自改变评分。语法有效不代表答案有效。

官方 validator 仅检查部分输出形状，不足以替代完整检测。

## 3. Evidence 确定性渲染

固定四节：

```markdown
## Answer
最佳答案；若时间不属于题目字段，不把辅助 onset 当正式预测。

## Confidence
定性信心、依据、最大疑点；未经校准不用 93% 一类数值。

## Evidence
逐条引用 EvidenceRecord 的来源、窗口、组件、指标或记录 ID、真实数值、单位和比较方法。

## Ruled out
区分已排除、被削弱和仍未排除；如没有能严格排除的解释，直接说明。
```

渲染规则：

- 数值、时间、源文件、trace/log ID 由结构化账本填入，不从模型自由文本抄。
- Decision 的 supporting_ids/alternatives IDs 必须在账本中存在。
- 同一个证据可被多个假设引用，但不能包装成多份独立支持。
- `missing / partial / empty / failed` 要进入解释，不把未查到记录写成指标为零。
- 指标原始单位未知时显示 native/unknown；不得把 reads_MB 直接解释为 MB/s。
- 没有日志关键词命中不等于健康；node/CPU 共变必须保留。
- 模型解释是推断，不能当作观测写入 EvidenceRecord。
- 表格或自然语言模板尽量简短，但不能省略关键矛盾来提高表面信心。

row 0 应保留的事实包括：pod 读活动集中、CPU/内存和节点也变化、GetQuote 中位 duration 未明显改变、日志关键词查询范围与命中数。这些事实共同支持有限结论，而非完整因果证明。

## 4. 与原有结果的关系

官方 [starter README](../../track-1/starter/README.md) 公布 heuristic 开发基线：mean partial 0.073、fully solved 2/70；这不是新 Agent 的结果。此前本机 baseline 和 row 0 demo 输出未随本次文档发布，不将其作为目标仓库已具备的可复现实验。

M5 保存新运行的 manifest、predictions、evidence 和 usage，建立仓库自己的结果。报告标明代码版本、配置、数据范围和调参情况。单例已知开发窗口的演示不能替代全量 development score 或 model routing 对照。

## 5. 完整分母与实验目录

每次运行前固定 manifest：

```text
experiment_id, code_revision, source_hashes, config_id,
planned_row_ids_in_order, query_hash, repetition,
dataset_identity, price_table_revision,
cache_condition, hardware_limits, tuning_status
```

每个 configuration/repetition 使用独立目录。例如 `--out /out/<experiment>/<config>/rep-01`，路径由 harness 创建后传给 runner，不重用 out/dev。

- 缺失、重复、unexpected IDs、空预测、数量错误和 evidence 缺失单独检查。
- 调用官方 evaluate 为每个计划 case 打分；未返回 case 计零。官方 score.py 的 inner join 不能作为完整性保证。
- duplicate 不重复计分，也不默认“保留最后一条”掩盖事故；标记运行完整性失败。
- 中途重启的 tokens/cost 全部计入花费，不能仅取最后一条 usage 掩盖重试成本。必要时使用独立 attempt 账本；官方 cost.py 可作简便参考，不替代全实验账本。
- timing 包含启动、扫描、缓存建立、重试、渲染和保存。sum(wall_s) 与外部总时间分开报。

## 6. 配置对照：什么先做，什么后做

| 配置 | 用途 | 优先级 |
|---|---|---|
| 原 heuristic | 已有免费起点 | 保留，不重复替代其他实验 |
| 改进 deterministic | 分离工具改进与模型收益 | 建议 |
| 同 Agent，指定单 GLM | routing 对照基准 | 必需 |
| 同 Agent，confidence-routed | 测量 route 带来的质量/费用/时间变化 | 必需 |
| Flash-only 与 strong-only 各一组 | 更细的模型消融 | 预算允许后补充 |

比较时保持相同案例顺序、代码、工具、输出要求和预算政策。模型导致决策或升级次数变化属于端到端结果的一部分，但不能同时换工具版本后把提升全归因于 routing。

为了更干净地区分原因，可做额外的固定 evidence bundle 回放：保持每题证据与逻辑调用日程不变，只替换模型。这是诊断实验，不能取代包含实时数据查询成本的正式端到端对照。

single-model 控制组不跨模型 fallback；失败后 deterministic 降级需要记录。若实际用了第二个模型，另标 mixed-model degraded run，不继续称纯单模型。

记录重复次数与方差。只有一次运行就写未测方差。公开 70 题允许调参；报告称 development result。不同 task 可能共用同一故障窗口，可选 holdout 必须按相关窗口分组，而非随机行拆分后声称独立。

## 7. 报告字段与证据审核

每题：row_id、task、状态、partial、fully_solved、time、calls、per-model tokens、美元、route/escalation/fallback、stop_reason、evidence 覆盖。

汇总：完整计划分母、strict/partial、按 task/difficulty 分组、完成率、空答案率、mean/median/p95/max 时间与费用、总运行、升级率、重复差异。

错误分类：parser/time、candidate recall、component、reason、node/container、network subtype、多故障分离、format、provider、timeout、evidence unsupported。一个 case 可有多个诊断标签。

证据检查分三层：

1. 所有引用 ID、源文件、原始记录是否存在。
2. 抽样或自动重新查询，核对显示的聚合数值和单位；固定并公布抽样方式与数量。
3. 人审证据是否支持推断，是否把相关性误写成因果。记录级核对通过不等于因果已证明。

## 8. 文件所有权、集成与验收

M5 独占仓库根相对路径 `track-1/starter/agents/rca/evidence.py`、`track-1/starter/agents/rca/validation.py`、`track-1/starter/tests/m5/`、根 `eval/` 和 `REPORT.md`。M1 负责 Docker/runner 写入，M4 负责 Decision；M5 不各自实现第二套查询或决策器。

第一版交付：可用小型无答案 bundle 渲染四节 evidence；验证非连续 ID、多故障数、合法标签和时间；输出一个包含失败 case 的完整分母报告。

最终验收：

- 每个计划 row_id 都被计入分母，预测与 evidence 对应。
- 验证规则比官方 shape validator 覆盖更完整，但官方 accuracy matching 不变。
- evidence 数值可复算，推断与观测分开，缺失和模型失败如实记录。
- 至少有同 Agent 的单模型与 routed 对照；耗时和成本从实际记录计算。
- 最终 Docker 资源测试由 M1 执行，M5 保存其配置与结果。
- 输出英文提交报告与实际 AI/starter 使用披露；不把 demo 或原 starter 功能冒充团队新增能力。

## 9. 五人接线时的交付顺序

1. M1 先交 v1 类型及窗口查询；所有人以同一个无答案 bundle 样例接线，完整门槛见 [INTEGRATION.md](../INTEGRATION.md)。
2. M2/M3 各交真实 AnalysisBundle；M4 同时用小型 fixture 实现调度，M5 用 fixture 实现校验/渲染。
3. M4 接入真实工具，先完成 deterministic 端到端；M1 确认 runner 默认入口。
4. M4 接入 GLM，M5 开始冻结案例的对照；M1 跑实际 Docker。
5. 发现跨模块问题按所有权修复；只读评审共享目录，多个 AI 并行写仓库时各用独立 worktree。

每个模块交付必须包含：函数签名、一次真实调用的输入/输出、限制说明、与风险相称的验证。仅有 notebook 截图或口头“做完了”不算可集成产物。


## 10. 最小可复现入口与交给 AI 的任务

运行期入口在 `agents.rca.validation.validate_and_render`，内部调用 evidence.py；只依赖共享类型与官方 formatter，不导入根 eval。JSON时间/序列化按契约，不要求模型写正文。指标与 traces/logs 的聚合重放分别调用 M2/M3 的 replay_evidence，M5 不另建 CSV reader。

离线交付 `eval/run_comparison.py`、`eval/audit_run.py` 的 CLI/help/dry-run。从仓库根执行，runner 始终指向 track-1/starter/run.py。harness 从 routes.jsonl 关联 manifest，不能从不存在的 Solution.route_events 取数据。run_comparison 在 dry-run 中列 case 清单、实际命令、两种配置、独立目录和预算；真实运行后才报告费用/分数。

从 starter 运行本模块实现的测试：
```bash
python -m unittest discover -s tests/m5 -p 'test_*.py'
```

> 你只负责 M5。先读根 AGENTS.md、TEAM.md、docs/INTERFACES.md、docs/INTEGRATION.md、本文件及官方 scoring/submission。实现确定性四节证据和无副作用校验器；invalid 返回错误，由 M4 选择 fallback。实现独立 eval harness、完整分母审核、同 Agent 单模型/routed对照、dry-run、manifest 和英文报告。只改 M5 文件/tests/m5/根eval/REPORT；不改官方 evaluate 或 controller/数据工具。不为填结果直接开启未约定的大额付费运行。先用 fixture 和缺失case验证计分，再用真实结果填写报告；明确分清模拟测试、公开开发成绩和隐藏测试。
