# P1 最终质量审计

## 范围与安全边界

本轮只读核对当前分支源码、持久化审计结构、离线测试和隔离 LangGraph 回放。没有调用 Qwen/OpenAI，没有写 PostgreSQL/MinIO，没有覆盖历史 Task/Event/Evidence 或冻结评估结果，没有切换/推送 `main`。工作树中的原有未跟踪文件全部保留。

## 真实生产链路

当前 HTTP 入口是 `app/api/routes.py:161` 的 `/api/chat/stream` 和 `:226` 的 conversation stream。Follow-up 在 `:336` 经 `ConversationContextResolver`、`StateSufficiencyResolver` 和 `workflow_query` 解析；需要执行时在 `:488` 调用 `ScientificAgent.stream`。`ScientificAgent.__init__` 在 `app/agents/scientific_agent.py:44-69` 只建立一个 `DecisionRuntime`，生产图由 `app/agents/planning_graph.py:192` 编译。

图的实际节点顺序是 `build_context -> decision -> (execute_tool|update_plan|ask_user|finalize|refuse)`，工具后经 `observation -> decision`。`runtime.py:100` 做 ResourceBinding/QueryScope/GoalContract grounding，`:156` 做 Skill 选择门禁，`:585` 做 AgentDecision 校验，`:763` 调用真实 ToolDispatcher，`:893` 把 Observation/Evidence/Recovery 写回 State，`:1011` 计算 GoalCoverage/QualityStatus 并生成 GroundedResponse。任务持久化在 routes worker 中将所有真实 SSE 审计事件写入 Repository；前端只应按阶段折叠这些事件，不得删除事件。

P1 新模块确实接入生产图：GoalContract 在 `_ground_resources`/`decision`/`finalize` 读写；PlanStep 在 `PlanningPolicy`, `prepare_replacement`, `eligible_plan_steps`, `refresh_steps` 中成为执行边界；RecoveryPolicy 在 `execute_tool`/`observation` 持久化；deterministic executor 在 `decision` 中先于 LLM，但仅在唯一且可证明的动作成立时启用。没有发现第二套生产 Planner；`ScientificAgent` 的旧 stage helpers 和 `deep_runtime.py` 只保留兼容/可选代码，未被当前 `stream` 调用。

## A/B/C 根因与反事实

- A：文件比较的真实 Evidence 与 MAE/结构分组结果满足文件目标。计划不能从发现的 `training_db` 自动增加 database obligation；GoalContract 的 `required_data_sources`/`required_deliverables` 只来自用户请求。UI 失败只能由任务状态派生错误或旧 Plan/Recovery 事件残留解释，不能用“答案看起来正确”强行完成；`scientific_task_status` 仍将 `EXECUTION_FAILED`、`INSUFFICIENT_EVIDENCE` 和不完整 Coverage 判为 failed。
- B：四次调用的功能边界是 schema/search（或真实表结构）、Text2SQL 生成候选、Query Checker 验证、只读 SQL 执行。P1 确定性执行器只在前置事实充分时推进 Checker/Executor，保留四次 ToolCall 和所有审计事件；没有通过合并结果或额外 SQL 校验制造覆盖。
- C：Text2SQL 失败结果保留 `metadata.sql_candidate` 仅作 `diagnostic_only`。`eligible_call_tools` 在 LLM 决策前移除无可信来源的 Query Checker；`UNVERIFIED_SCOPE` 只触发一次定向 SQL repair，`SCOPE_VIOLATION` 使用 `safe_reject`/零预算；QueryScope 不被放宽。复杂 JOIN 无法证明时继续安全拒绝。预测误差与训练覆盖应拆为独立可验证查询；`model_run -> training_dataset_version` 只有真实关联证据存在时才可声称。

## 本轮实际修改

1. `app/agents/runtime.py:523-528`：已安装 Plan 且没有 eligible step 时，deterministic executor 立即返回 `None`，不再从全局工具集发出无 `step_id` 的 CALL_TOOL。这修复了已成功任务被后续伪工具调用污染为失败的真实 Bug。
2. `tests/test_product_expansion.py:55-63`：将固定 23 工具数量断言改为可扩展 Registry 契约，明确核对 `preview_table`、`save_chart`、唯一名称和完整元数据。
3. `tests/test_p1_langgraph_no_model_e2e.py`：新增隔离 Checkpointer、Fake provider、受控 Fake database/storage，但保留真实 LangGraph 节点、ToolRegistry、ToolDispatcher、Scope/Recovery/GoalCoverage。

## UI 审计建议

面向用户的轨迹应按“任务理解、规划、数据分析、证据校验、最终回答”折叠展示；详细 `AGENT_DECISION`、`TOOL_STARTED/FINISHED`、`OBSERVATION_RECORDED`、`RECOVERY_DECISION`、失败原因和输入来源留在开发者展开区。事件仍全部持久化，UI 只改变呈现，不减少审计数据。

## 状态

核心 P1 协议和 P0 SQL 安全边界通过；本轮真实生产 Bug 已修复并由 E2E 锁定。完整真实 PostgreSQL/MinIO/LLM 集成仍需专用受控环境，不能在本地无模型 profile 宣称已验收。
