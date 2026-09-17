# M4 — 候选融合、有限调查与 GLM 路由

负责人角色：Agent / Routing。目标：依据 M2/M3 的证据形成最佳判断，决定是否补查、是否调用模型、是否升级，并在预算内结束。M4 是唯一调查调度者。

共享类型和完整签名以 [rca-v1](../INTERFACES.md) 为准；数据实现由 [M1](01_contract_data_runtime.md) 提供。本文件同时记录外部 implementation plan 与本地设计的差异，作为五模块实施方向的对齐依据。

## 1. 当前计划对照与采用意见

对照来源：[Hybrid RCA Agent Implementation Plan，revision ce54409](https://github.com/catou88/hackathon-2026-official/blob/ce54409b9d0d5034856f8c3d29d618eb007f0252/RCA_IMPLEMENTATION_PLAN.md)。这版已修正早期关于 runner 和传播方向的表述；下表区分“已经一致”和“进一步具体化”，不沿用旧版问题。

| 主题 | 当前参考计划 | 五模块实施规则 |
|---|---|---|
| 总方向 | deterministic → Flash → strong → deterministic evidence | 一致；不建第二套框架 |
| 团队规模 | 四人建议，Agent 兼 schema，Logs 兼评测 | 分成五人：M1 独立数据/契约/集成；Logs 属 M3；M5 聚焦 evidence/eval |
| runner | 已允许提交前改默认 agent，保留 CLI | 一致；M1 改默认值并做无 --agent 验收 |
| 文件布局 | routed + 小 helper | 同方向；明确每人文件及唯一 starter 实现路径 |
| service shortlist | 详细资源检查先收窄到服务/邻居 | service 只排序；所有 pod/node 的廉价粗筛 + 独立 trace triage，避免漏局部故障 |
| trace causality | 已区分调用与传播方向，承认缺失/时序局限 | 一致；明确 trace_id+span_id 配对、native duration、DependencyEdge 不是因果结论 |
| 网络原因 | 需要机制特异证据选择 subtype | 强证据才称已区分；终止仍须猜合法标签并披露 subtype unresolved |
| confidence | 结合margin/矛盾/覆盖，不只信模型confidence | 一致；M4 独占跨来源排名及 deterministic gate |
| 模型 wrapper | 复用其 retries/fallback/breaker | 实际 routed 每题新建 LLM；必须显式实现 RunState 跨题健康状态及 usage差量 |
| evaluation | 优先 single strong vs routed；RCA_MODEL 不强制调用 | 一致；记录 bypass，不能把零调用对照称 routing 收益 |
| 实验目录 | 示例共用 out/dev，要求每次后归档 | 从启动即用独立目录/manifest，避免覆盖和 usage追加混合 |
| evidence | 确定性渲染真实测量 | 一致；多query证据、source定位、replay接口、M4/M5单向回退 |
| 资源控制 | 轻量缓存、平均明显低于一分钟 | 转为显式deadline/费用预留/调用上限，并进行冷启动资源实测 |

参考计划中的方向被保留；检测阈值、候选排名和路由收益仍需实现与验证。此前 row 0 notebook 是一个固定控制流的开发示例，不是通用 Agent 已完成的证明。

## 2. 输入、输出、效果

| 项目 | 契约 |
|---|---|
| 输入 | instruction、dataset、ctx；M1 CaseContext/RunState；M2/M3 AnalysisBundle |
| 输出 | Decision 交 M5；M5 返回 prediction/evidence 后，M4 组成官方 Solution |
| 目标效果 | 可解释选择、有限补查、便宜优先、必要升级、失败仍给最佳判断 |
| 不负责 | 自己扫描原始 CSV、重复计算特征、编造测量、改官方评分 |

## 3. 固定一个调度者，避免无限自主循环

```text
M1 解析 + 初始化预算/组件目录
 → M2 service 摘要 + pod/node 粗筛
 → M3 独立基础 trace triage
 → 合并候选、按 episode 组织、检查反证
 → 最多一次有明确区分问题的补查（M2 或 M3）
 → deterministic gate
     ├─ 足够明确：代码作答
     └─ 模糊：Flash
           ├─ 结果合法、证据支持：采用或保守回退
           └─ 仍模糊且有预算：strong
 → 校验 Decision → M5 渲染 → Solution
```

首版最多一次补查、一次 Flash、一次 strong 的逻辑调用；fallback 的实际 HTTP 请求另计数并受预算约束。先用可重现固定调度实现，再增加模型提出 QuerySpec 的能力。不能从一开始就让模型生成任意 Python/SQL 或无限 ReAct loop。

没有可区分证据时，升级模型不保证解决问题。一个网络 subtype 本来不可辨识，就应在模型与代码中保留这个结论，而非持续升级直到某模型看起来很确定。

## 4. 候选融合和排名

M2/M3 交原始特征和 evidence IDs，M4 独占跨来源最终排名：

- component + reason + onset/episode 组合为候选，允许 reason=None 的弱候选。
- 将指标强度、持续性、replica 对照、trace 线索、时间区间兼容性等变成有上限的分项；保留分项。
- unknown 不能当 healthy 或强反证；缺失来源不自动减成低概率。
- 高度相关 read 指标、父子 span、同一日志的重复消息不可当作多份独立支持。
- early onset 奖励只在误差区间能区分时使用；propagation penalty 需可解释依赖语义。
- 排名分数和 margin 不是校准概率。阈值是配置，并在多个开发案例上验证覆盖率/错误率。
- 多故障先分 episode/可能独立的传播组，再选所需数量；不要直接取 N 个最响的组件。

Deterministic gate 至少检查：直接机制证据、有效覆盖、与第二候选的区分依据、未解决重大矛盾、故障数可满足。最初可以保守关闭 gate，先完成合法 GLM 路径，再用实测开启；不为了省钱把多数题硬判成 easy。

row 0 中 node/CPU 共变，因此我们的 demo 虽按固定规则给出 medium-confidence read I/O，正式 gate 不应直接把它当成已证实的 high-confidence 无模型案例。

## 5. Prompt 的输入与输出

不传原始大表，不传 development scoring_points。输入是：task 字段、故障数量、窗口、候选、EvidenceRecord 精简视图、未知项、剩余允许动作。

Flash / strong 返回同一结构：

```json
{
  "selected_candidate_ids": ["candidate-id-from-input"],
  "confidence": "low",
  "supporting_evidence_ids": ["evidence-id-from-input"],
  "unresolved": ["which explanations could not be separated"],
  "next_query": null
}
```

- 初版只选已有 candidate ID，不让模型自由创造组件/原因。
- 补查扩展时，next_query 只能是允许的 tool + QuerySpec；M4 校验范围、预算和去重。
- 强模型接收原始证据与矛盾，不只接收 Flash 的结论。Flash 的说法是模型建议，不加入事实账本。
- 不要求模型自由撰写权威 measurements；解释引用的 evidence IDs 必须真实存在。
- 校验故障数量、候选合法性和引用后，再转为 Decision。模型 JSON 错误、空 choices 或响应中 error 都不当成功。

## 6. Routing、fallback、usage 的所有权

采用计划中的起始 tiers（具体可用性由调用时验证）：

```python
CHEAP = ['zai-org/GLM-4.7-Flash', 'zai-org/GLM-5.3-Flash']
STRONG = ['zai-org/GLM-5.2', 'zai-org/GLM-5.1']
```

- 使用 `FEATHERLESS_API_KEY` 和 `FEATHERLESS_BASE_URL`；不为开发测试把 key 写入代码。
- 仅当决定调用模型时初始化客户端，否则缺少 key 不应阻断 deterministic 路径。
- ctx 中共享模型失败状态。复用客户端时，每题记录调用前后 usage 差量，不能每题把运行累计账单当当前题账单。
- 显式限制 SDK 自动重试、HTTP timeout、最大输出和 wrapper 重试；两层重试叠加也算时间。
- HTTP 200 + error body 也算失败；连续失败触发跨题熔断，fallback 必须仍在允许 GLM 家族。
- 错误日志脱敏；不记录 Authorization header。
- single-model 配置让所有实际模型调用使用指定模型，关闭跨模型 fallback。不可用时退到 deterministic 并披露；不能暗中调用另一个模型后继续称为 single-model。

每次路由事件至少记录：case_key、阶段、触发原因、请求/实际模型、是否 fallback、延迟、tokens、预计美元、响应校验状态。最终 runner 仍按真实 row_id 保存官方 usage。

## 7. 停止、预算与降级

初始工程目标：每题约 45 秒软预算，预留格式化/落盘时间；全运行内部预算可先设 18 分钟、$20，低于官方 20 分钟/$25。这些是待通过性能实验验证的配置目标，非已达成指标，也不替代每题 10 分钟/$3 硬限制。

停止条件：

1. deterministic gate 成立，或合法模型选择得到可引用证据支持。
2. 达到补查/模型请求预算、case 或 run deadline、费用保守上限。
3. 同一查询重复、补查没有新增区分性事实。
4. 剩余模型全部失效，继续重试预期无收益。

所有分支都尽早保留 deterministic best guess。失败时输出同样数量的答案并说明局限，尽量使用真实组件目录与合法原因。若整个输入没有可用组件，明确返回降级状态和无法验证的占位猜测；不能声称这样的组件已在数据中观察到。M5 区分输出形状可用与语义有效。

仅在调用边界检查时间不够：数据扫描按 chunk 检查；HTTP 用剩余 deadline 计算 timeout；不能取消的操作由 M1/M4 在执行层处理。每次启动调用前为证据生成和保存留余量。

## 8. 文件所有权与验收

M4 独占仓库根相对路径 `track-1/starter/agents/routed.py`、`track-1/starter/agents/rca/controller.py`、`track-1/starter/agents/rca/ranking.py`、`track-1/starter/agents/rca/routing.py`、同 rca 目录下的 prompts，以及 `track-1/starter/llm.py` 和 `track-1/starter/tests/m4/`。其他成员通过契约提修改请求，不同时编辑 controller。

验收：

- 入口仍为官方 solve，返回 Solution；真正最终默认入口由 M1 接好。
- 基础 trace 不被 metrics shortlist 屏蔽；最多一次补查的首版能完整结束。
- 全模型失效、错误 JSON、空数据、预算耗尽均保存最佳猜测和真实失败说明。
- 非法候选/引用不进入最终答案；单题 usage 是差量。
- 同一数据与工具版本支持 routed 和 single-model 配置；每次升级和实际 fallback 可审计。
- 行为在 row 0 之外的案例也运行；禁止 hard-code pod、答案时间、row_id→reason 表。


## 9. 对接方式、可运行配置与交给 AI 的任务

`routing.load_config() -> RunConfig` 解析配置，`controller.investigate(case, state, *, deadline) -> InvestigationResult` 返回 decision、预存 fallback 和合并证据；routed.solve 把 M1/M5 接起来。M5 返回 invalid 时最多切换一次 fallback，不循环互调。类型、失败路径和 ledger 字段以 rca-v1 为准。

首版实现这些配置入口：

- `RCA_MODE=deterministic`：零模型请求，不初始化 key/client。
- `RCA_MODE=routed`：默认有界路由；`RCA_MODEL` 未设置。
- `RCA_MODE=routed RCA_MODEL=zai-org/GLM-5.2`：所有实际调用钉住该 GLM，不跨模型 fallback，也不强制绕过 deterministic gate。

模型选项中的 candidate 必须有题目要求的合法字段；M2/M3 的 reason=None 候选由 M4 按合法标签扩展弱假设，不让模型自由编造。补查和 candidate扩展不创造 evidence。routes.jsonl 由 M4 写入 --out，供 M5按 manifest关联；官方 Solution 只接 prediction/evidence/usage，不添加 runner无法保存的隐式字段。

从 starter 运行本模块实现的测试：
```bash
python -m unittest discover -s tests/m4 -p 'test_*.py'
```

> 你只负责 M4。先读根 AGENTS.md、TEAM.md、docs/INTERFACES.md、docs/INTEGRATION.md、本文件和 llm.py。按 rca-v1 实现唯一 controller：固定首轮 M2+M3、候选融合、有限补查、deterministic gate、Flash/strong及回退。保留官方 solve，接 M5渲染和M1 RunState，支持 RCA_MODE/RCA_MODEL，显式记录真实调用/usage差量/预算。只改 M4文件/tests/m4；不重复 CSV reader、改共享契约或私自修 renderer。先用 stubs 测全失效/错误响应/超预算，再接真实工具。不得在无数据时伪造因果或无限升级模型；收尾交实际验证、调用轨迹、输入输出和未达成项。
