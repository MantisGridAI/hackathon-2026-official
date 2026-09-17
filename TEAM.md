# Track 1 五人开发入口

目标是一个无人值守的 RCA Agent：根据真实遥测判断题目要求的时间、组件和原因，按调用选择 GLM，输出可核查证据，并完成同 Agent 的 single-model / routed 对照。交付物是 **Agent + Evaluation + Explanation**。

本次文档配置基于 starter 与 implementation plan 的 `ce54409`。下列新模块是实施契约，不是已完成功能；现有可运行代码仍在 `track-1/starter/`。不创建第二套 Agent，不把 notebook 当正式入口。

## 1. 认领一块完整交付，不认领一个模糊主题

| 模块 | 最终交付 | 输入 → 输出 | 独占实现目录/文件（仓库根相对路径） |
|---|---|---|---|
| [M1 数据与运行集成](docs/modules/01_contract_data_runtime.md) | 正确、可限时的数据接口；默认入口与 Docker | instruction / dataset / ctx → CaseContext / TelemetryStore / RunState | `track-1/starter/agents/rca/{contracts,data_access,runtime}.py`；runner / 依赖 / Docker / Makefile |
| [M2 指标与开始时间](docs/modules/02_metrics_onset.md) | 资源候选、onset 区间、replica/node 对照 | CaseContext + Store → AnalysisBundle | `track-1/starter/agents/rca/{metrics,onset}.py` |
| [M3 Traces 与日志](docs/modules/03_traces_network_logs.md) | 独立 trace 候选、可靠依赖边、定向日志 | CaseContext + Store → AnalysisBundle | `track-1/starter/agents/rca/{traces,network,logs}.py` |
| [M4 Agent 与路由](docs/modules/04_controller_routing.md) | 有限调查、排名、GLM 路由、故障回退 | bundles + RunState → Decision → Solution | `track-1/starter/agents/routed.py`；`agents/rca/{controller,ranking,routing}.py`（同 starter 下）；`llm.py` |
| [M5 证据与评测](docs/modules/05_evidence_evaluation.md) | 可信四节证据、严格校验、完整分母对照 | Decision + evidence → RenderedResult；实验 → 报告 | `track-1/starter/agents/rca/{evidence,validation}.py`；根 `eval/`、`REPORT.md` |

共同必读：[共享接口 v1](docs/INTERFACES.md)、[接线与验收](docs/INTEGRATION.md)、[AI 工作边界](AGENTS.md)、官方 [data](track-1/docs/data.md) / [models](track-1/docs/models.md) / [scoring](track-1/docs/scoring.md) / [submission](track-1/docs/submission.md)。

```text
run.py → M4 调度
           ├─ M1：解析 / 数据 / 共享运行状态
           ├─ M2：指标事实 ─┐
           └─ M3：trace/log ┴→ M4：候选、补查、路由、决定
                                  → M5：校验 / 证据 → Solution → run.py
离线：完整运行输出 + dev labels → M5 eval → REPORT.md
```

## 2. 认领表是唯一负责人记录

仓库不依赖 GitHub Issues。认领者发一个只修改自己这一行的 PR：填 GitHub 用户名，状态改为 `claimed`，填写该认领 PR 的链接。**认领 PR 合并才锁定模块**；同一模块同时有人认领时先协调，不开始重复实现。不要替其他人填名字。

| 模块 | 负责人 GitHub | 状态 | 认领 PR | 实现 / 验收 PR |
|---|---|---|---|---|
| M1 | — | available | — | — |
| M2 | — | available | — | — |
| M3 | — | available | — | — |
| M4 | — | available | — | — |
| M5 | — | available | — | — |

状态含义：`available` 可认领 → `claimed` 已锁定 → `in-progress` 开发中 → `integrated` 已合入且真实接线通过 → `verified` 本模块最终验收通过。写完代码或生成模板不等于后两个状态。模块负责人通过同一行的 PR 更新状态和检查链接。

M1 兼任集成协调者，负责共享契约和打包；不是代写其他四个模块。每个 PR 至少让一个接口消费者审阅。M1 的接口由 M2/M3/M4/M5 至少一位审阅；M4 由 M5 审阅输出与记账；M5 由 M4 审阅回退边界。GitHub 未强制审核时也执行此团队约定。

## 3. 每个人怎么开工

1. 阅读公共三份文档与自己的模块文档，认领一行。
2. 从最新 `main` 开独立模块分支。多 AI 并发写代码使用独立 checkout/worktree，不共享一个可写目录。
3. 将自己模块文档末尾的「交给 AI 的任务」连同仓库路径交给 AI；不要只说“帮我实现 Metrics”。
4. M1 优先提交可导入的 v1 类型和无答案 fixture。其他人同时写模块私有逻辑/测试，但不另造 `contracts.py`。
5. 先交最小可接线版本，再做检测算法优化。PR 使用仓库模板，贴真实检查输出、失败行为、输入输出样例和资源开销。

示例分支命令（认领 M2；其他模块替换模块编号）：

```bash
git switch main
git pull --ff-only
git switch -c feat/m2-metrics
# 编写并检查，只暂存本人负责文件；完成后 push 此分支并发 PR。
```

共享接口变更先发小型契约 PR，列出受影响消费者；不能一边改字段一边指望别人猜。新增依赖先与 M1 协调；没有依赖共识前用已有 pandas / numpy / 标准库。不要让五人都修改 `routed.py`、`run.py` 或 `requirements.txt`。

## 4. 交付节奏与最终效果

| 门槛 | 可观察效果 | 负责人 |
|---|---|---|
| G0 契约 | 全员导入同一类型；fixture 不含答案 | M1，所有消费者检查 |
| G1 工具 | M2/M3 分别交一个真实窗口的 bundle；M5 能渲染 fixture | M2 / M3 / M5 |
| G2 接线 | 无 key 也能完成 Agent 路径，输出格式与证据齐全 | M4 + M1 + M5 |
| G3 模型 | 同样工具支持 routed / single-model；错误响应可回退 | M4 + M5 |
| G4 交付 | 完整分母报告、根 Docker、默认入口、2 CPU / 8 GB 验收 | M1 + M5，全员修复所属问题 |

G0–G4 的具体命令、失败用例和证据要求见 [INTEGRATION.md](docs/INTEGRATION.md)。评分提高不是“模块能导入”的替代，代码能导入也不是最终正确率结论。

## 5. 工作范围与记录

- 正式推理只读提供的 telemetry；公开 dev 答案只在离线评测使用。禁止下载上游 OpenRCA 数据集。
- 每个正式 row 都给最佳猜测，把不确定性写在 evidence；不能漏题或伪造观测。
- 新 Agent 的目标是平均约一分钟/题；必须实测，不把本机单案例耗时当 2 CPU / 8 GB 的结果。
- 原计划的技术方向保留，五人职责与接口以本页、模块规范及 v1 契约为准。[M4 对照表](docs/modules/04_controller_routing.md)解释细化之处。
- 本配置没有实现新 Agent、运行付费评测或导入本机数据/notebook。后续实验结果须附代码版本、配置与复现入口。
