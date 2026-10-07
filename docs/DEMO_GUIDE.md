# Demo Guide

先按 README 启动 Docker、backend 和 frontend。

## Case A：Mixed

上传 `data/demo/model_v1.csv` 与 `model_v2.csv`，然后提交：

> 比较 model_v1.csv 和 model_v2.csv，分析为什么新模型在 fused-ring 分子上误差更高，并检查是不是 training_db 训练数据覆盖不足。

Agent Trace 应显示：mixed/complex Intent、selected skills、Plan、DeepAgents runtime、文件指标、MCP、Evidence、schema/relationship、Text-to-SQL、query checker、PostgreSQL execute 和 Final Answer。没有 LLM API 时 Text-to-SQL metadata 显示 `deterministic_fixture_fallback`。

## Case B：Persistent HITL

提交：

> 分析 fused-ring 在训练数据中的覆盖情况。

系统产生 `WAITING_FOR_USER`。输入 `train_v3` 后调用 resume；trace 首先显示从 checkpoint 恢复，然后继续 PostgreSQL coverage 查询。使用 `CHECKPOINT_DATABASE_URL` 时，即使 backend 重启仍可恢复。

## Synthetic boundary

- CSV 数据：synthetic。
- PostgreSQL molecules/training_molecules：synthetic。
- MCP molecule features：synthetic。
- 没有 TC-TopoRT 权重时不会产生真实预测。

