# 五个模块：代码、测试与集成状态

五个模块均已实现并接入 `main`。本目录保存模块规范和入口索引，唯一运行实现位于 `track-1/starter/`。提交历史保留各模块的独立实现、修复及集成记录。

| 模块规范 | 主要实现 | 测试 |
|---|---|---|
| [M1 数据与运行集成](01_contract_data_runtime.md) | [contracts](../../track-1/starter/agents/rca/contracts.py)、[data_access](../../track-1/starter/agents/rca/data_access.py)、[runtime](../../track-1/starter/agents/rca/runtime.py)、[run.py](../../track-1/starter/run.py)、[Dockerfile](../../Dockerfile) | [M1](../../track-1/starter/tests/m1)、[integration](../../track-1/starter/tests/integration) |
| [M2 指标与开始时间](02_metrics_onset.md) | [metrics](../../track-1/starter/agents/rca/metrics.py)、[onset](../../track-1/starter/agents/rca/onset.py) | [M2](../../track-1/starter/tests/m2) |
| [M3 Traces 与日志](03_traces_network_logs.md) | [traces](../../track-1/starter/agents/rca/traces.py)、[network](../../track-1/starter/agents/rca/network.py)、[logs](../../track-1/starter/agents/rca/logs.py) | [M3](../../track-1/starter/tests/m3) |
| [M4 Agent 与路由](04_controller_routing.md) | [routed](../../track-1/starter/agents/routed.py)、[controller](../../track-1/starter/agents/rca/controller.py)、[ranking](../../track-1/starter/agents/rca/ranking.py)、[routing](../../track-1/starter/agents/rca/routing.py)、[prompts](../../track-1/starter/agents/rca/prompts.py)、[llm](../../track-1/starter/llm.py) | [M4](../../track-1/starter/tests/m4) |
| [M5 证据与评测](05_evidence_evaluation.md) | [evidence](../../track-1/starter/agents/rca/evidence.py)、[validation](../../track-1/starter/agents/rca/validation.py)、[eval](../../eval) | [M5](../../track-1/starter/tests/m5) |

默认入口为 `agents.routed`。运行示例、环境变量和安装方法见[根 README](../../README.md)。

最终完整测试 **133 项全部通过，74.747 秒，无跳过项**，包含五项真实遥测检查；[测试记录](../../eval/results/verification/final-full-tests-133.txt)已保存。官方输出格式校验完成两道真实题目，零警告。真实模型限定对照共六次请求，按官方价表估算总费用 **$0.43362408**；[结果与账本](../../eval/results/glm-smoke-20260917)保留完整计划分母和失败调用。

真实模型测试发现的提示过大及请求超时问题已修复，并通过本地 HTTP 与完整集成回归。修复后没有追加付费测试，不能据此声称真实模型准确率或耗时已改善。本机没有 Docker，2 CPU／8 GB 容器验收仍未执行；完整 70 题评测也尚未运行。详见[英文报告及限制](../../REPORT.md)。

密钥、原始数据、虚拟环境及完整大型输出不进入仓库。各模块使用相同的 [rca-v1 接口](../INTERFACES.md)；官方 accuracy evaluator 和 heuristic baseline 保持原实现。
