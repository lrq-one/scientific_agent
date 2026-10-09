# P1.4 Runtime 执行效率

## 安全条件

现有 LangGraph 和唯一 DecisionRuntime 保持不变，没有新增 Planner，也没有把科学
任务写死成固定模板。每次 Observation 仍会经过 runtime，但 DecisionRuntime 先运行
一个受限 `_deterministic_decision` 检查。只有以下条件同时满足才自动推进：

1. 当前计划只有一个可执行 step（无计划时全局候选必须收敛为一个动作）；
2. 工具在授权范围内，依赖、PlanStep scope 和 Population 都已绑定；
3. 参数可由原始目标、授权文件唯一值、检索到的 schema、可信 SQLCandidate 或
   `input_refs` 确定；
4. Query Checker/execute 顺序满足可信候选前置条件；
5. 没有新的科学策略选择、版本选择、文件选择或歧义。

否则保留原有 LLM 决策路径。特别是“只有一个工具”不等于参数已确定：多文件、
缺少分组列、缺少 SQLCandidate、人口/Scope 歧义都会交回 LLM 或 ASK_USER。

## 真实审计

确定性推进产生普通 `AGENT_DECISION`，同时写入
`decision_source=deterministic_executor` 和 `llm_called=false`。`TOOL_STARTED`、
`TOOL_FINISHED`、`OBSERVATION_RECORDED`、`EVIDENCE_ADDED`、`PLAN_STEP_FINISHED` 和
失败恢复事件均保留；减少的是冗余模型决策，不是审计事件。

在 B 类安全 SQL 链路中，schema 成功后唯一推进到 Text2SQL；可信候选后唯一推进到
Query Checker；已检查候选后唯一推进到 readonly execute。任何一步出现多候选时不
自动选择。

## 验证

`tests/test_p1_runtime_efficiency.py` 覆盖 schema→Text2SQL、candidate→Checker→
execute、歧义文件拒绝确定性推进和 decision source。P0/C 相关回归与 P1.1–P1.3
测试合计 151 个通过。没有删除审计事件、没有 Qwen 调用、没有数据写入或历史任务
修改。
