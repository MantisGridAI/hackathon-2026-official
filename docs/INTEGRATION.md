# Integration gates and acceptance

本文件定义需要实际完成的验收。新模块、`tests/` 和根 `eval/` 入口由各负责人按规范实现；本次文档配置没有创建这些功能。未存在的测试命令应先实现对应测试，不以空目录的“0 tests”冒充通过。

## 1. 唯一代码源和文件所有权

```text
track-1/starter/
  run.py                         M1：仅必要默认入口/计时改动
  llm.py                         M4：有界重试、实际 usage、共享健康状态
  agents/
    routed.py                    M4：solve 适配器
    heuristic.py                 保留官方 baseline，不为提高对照分数改它
    rca/
      __init__.py                M1：不在这里全量 import 所有模块
      contracts.py               M1：INTERFACES.md 的类型/常量
      data_access.py runtime.py  M1
      metrics.py onset.py        M2
      traces.py network.py logs.py M3
      controller.py ranking.py routing.py M4
      prompts/                   M4：小型版本化文本或模块内常量
      evidence.py validation.py  M5
  tests/
    fixtures/                    M1：共用、无答案、微型数据
    m1/ m2/ m3/ m4/ m5/         每人自己的测试/入口
    integration/                 M1：调度端到端，M4/M5审阅
eval/                            M5：离线 harness、manifest、精简结果
REPORT.md                        M5：实际结果与限制，英文
Dockerfile                       M1：最终从 starter 迁至根，仅保留这一份
```

M1 同时拥有 `track-1/Makefile`、`track-1/starter/requirements.txt`、根 `.dockerignore`、共享 fixture。M2/M3 的阈值放模块内版本化常量，不让五人争写一个 config 文件。M4 管预算和模型配置。各自测试目录的 `__init__.py` 归各模块；公共 `tests/__init__.py` 归 M1。

团队协作配置由 M1 维护，但每个人可通过自己的 PR 更新 TEAM.md 对应认领行和模块说明。没有确切 GitHub 负责人前不创建伪 CODEOWNERS；认领表和 PR 评审是当前协调机制。

## 2. G0：先冻结接口，减少互相等待

M1 第一笔实现 PR 只需交：可导入的 contracts、UTC+8 parser、Store 的最小真实窗口读取、状态/定位字段、微型 fixture 和类型测试。先让消费者接线，不等完整 Docker 或高级缓存。

fixture 必须包含两种东西：

- **synthetic fixture**：带 expected 值的微型输入，用于秒/ms、CSV 逗号/换行、跨午夜、缺 parent、多故障等单元测试。明确 synthetic，禁止进入真实 evidence。
- **telemetry-only sample**：真实来源记录及定位信息，不含 scoring_points，用于真实数据接口检查；控制体积，不提交整份 telemetry。

数据类型 import 不依赖 LLM、环境变量、网络或其他四个尚未实现的模块。M2/M3/M4/M5 可先 mock Store/Bundle 完成自己逻辑，但最终必须接 M1 真接口。

验收命令由 M1 实现相应测试后运行：

```bash
cd track-1/starter
python -m unittest discover -s tests/m1 -p 'test_*.py'
```

## 3. G1：每人交一个可调用的最小版本

| 模块 | 必须可观察的输出 | 边界用例 |
|---|---|---|
| M1 | 精确窗口 + 带来源定位的数据块/coverage | ms/s、UTC+8、跨日、无序、CSV多行、缺文件、deadline |
| M2 | 真实 AnalysisBundle + onset区间 + 对照 | MAD=0、counter reset、缺样本、node/pod共变、短尖峰 |
| M3 | 独立 trace bundle + edges + 可定位日志 | 缺父、重复ID、异步span、native单位、低样本、无匹配 |
| M4 | fixture 驱动 Decision + fallback + usage差量 | 假200错误、空choices、非法JSON、模型全失效、总预算 |
| M5 | fixture 的 prediction + 四节 evidence | 字段顺序、错数量、错层级、缺证据ID、partial、不连续row_id |

各人用 `python -m unittest discover -s tests/m2 -p 'test_*.py'` 等相同命令运行所属目录，至少有一个真实接口输入/输出记录。单元测试默认不访问付费 API；模拟失败使用 stub。真实数据检查在模块 smoke/test 入口读取 `RCA_TEST_DATA` 指向 bundle，明确 skip 原因；不能把 skip 当真实数据通过。

## 4. G2：无模型完整路径

M2/M3/M5 可并行合入；M4 的初版 controller 可先用 fixture 开发，随后接真工具。M1 保留 runner 的逐行隔离、原始 row_id、逐题保存行为。默认 Agent 只在端到端可运行后切到 `agents.routed`。

以下命令从仓库根执行；`RCA_MODE` 是团队需实现的开关。使用新建独立输出目录，重复实验更换目录名，不能叠加旧 usage：

```bash
RCA_MODE=deterministic python track-1/starter/run.py \
  --dataset track-1/data/Market-cloudbed-1 \
  --queries track-1/data/Market-cloudbed-1/query.csv \
  --out track-1/out/integration-g2 --agent agents.routed --limit 2

python track-1/starter/score.py \
  --predictions track-1/out/integration-g2/predictions.csv \
  --queries track-1/data/Market-cloudbed-1/dev/query_dev.csv
```

M5 另核对计划的两行都存在、两份 evidence 四节齐全、零模型请求（不是仅 usage=0）、证据引用正确。分数不作为这一步唯一验收。用非连续 row_id 子集再测一次；子集 query 只含公开题目字段，不暴露 dev labels 给 Agent。

原有 shape check（从 `track-1/`）：`make validate AGENT=agents.routed`。**它显式指定 agent，不验证默认入口**；最终必须再运行一次不带 `--agent` 的命令，核对确为新 Agent，而不是 heuristic。

## 5. G3：GLM、失效与对照

M4 先通过无付费的 stub 测试：HTTP 200 error、无 choices、错误/截断 JSON、模型持续失效、usage缺失、deadline用尽、同一模型跨 case 的熔断及实际费用预留。M5 检查 routes 账本可关联 manifest。

真实模型测试使用环境 key 和端点；不要把 key 放命令示例、PR 或文件。下面是配置语义，需用 M5 harness 创建新目录并冻结同一组病例后执行：

```bash
# routed: RCA_MODEL 未设置，RCA_MODE=routed
# single-model: RCA_MODE=routed RCA_MODEL=zai-org/GLM-5.2
# deterministic: RCA_MODE=deterministic，不读 key、不创建客户端
```

单模型与路由配置共享工具、代码、病例顺序和 budget policy；RCA_MODEL 不强制调用。报告 deterministic bypass、实际调用数、fallback和升级；若两组都没有模型调用，不宣称已证明 routing 的取舍，应扩展真实模糊案例再测。20题上限和70题 development run区分：正式预算默认不因病例多而放宽；开发如需分批必须逐批记录并汇总完整70题，不把未跑到的题删除。

## 6. G4：打包、完整评测与提交

M1 最终将唯一 Dockerfile 迁至根，用 `COPY track-1/starter/ /app/` 等方式保持容器 `/app/run.py`，不复制第二套源码；依赖在 build 时安装。删除旧 starter Dockerfile的同一 PR 更新 `track-1/Makefile` 的 build context 与根 `.dockerignore`（排除 data/out/.env/.git）。此迁移是 M1 实现任务，本次仅配置文档。

最终根构建和资源限制验收（真实数据与空输出目录路径由执行者设置）：

```bash
docker build -t rca-team .
docker run --rm --cpus 2 --memory 8g \
  -e FEATHERLESS_API_KEY -e FEATHERLESS_BASE_URL \
  -v "$RCA_DATA":/data:ro -v "$RCA_OUT":/out \
  rca-team python run.py --dataset /data --queries /data/query.csv --out /out
```

检查它不需要 `--agent`，不在运行时安装包/下载数据/交互输入、不写 `/data`，仅访问指定模型端点。外部记录从进程启动到退出的时间和峰值内存；不能仅加总每题 solve 时间。正式20题预算：总20分钟/$25，每题10分钟/$3；内部更保守的配置需在报告中列明。

M5 完成：完整计划分母、官方 strict/partial、费用、总时间、重复次数/差异、routing-versus-single-model、证据记录/数值/推断三层审核、英文 REPORT。不要把公开70题 development score 当隐藏测试分数。源码、精简 eval 产物可提交；原始12 GB数据、key、巨型输出不可提交。

官方以 default branch 最新状态评测，见 [submission.md](../track-1/docs/submission.md)。所有最终功能、默认入口、唯一 Dockerfile、报告和实际 AI usage disclosure 均须合入 main；仅有 PR 或文档不是完成。

## 7. 集成出错谁处理

- 时间/数据缺失/组件映射/缓存/打包 → M1。
- 指标数值/onset/replica/node特征 → M2。
- span配对/边语义/log匹配/trace单位 → M3。
- 候选合并/故障分离/调用顺序/回退/预算/模型账本 → M4。
- 输出格式/证据渲染/分母/评分报告 → M5。

提交最小可复现输入、实际/预期输出和相关 source/evidence IDs，由所属模块修复。消费者不在自己的模块悄悄打补丁掩盖上游错误。接口冲突由 M1 协调并更新唯一契约。
