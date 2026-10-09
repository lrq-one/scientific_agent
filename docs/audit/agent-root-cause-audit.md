# Scientific Agent 根因审计

审计日期：2026-10-09（Asia/Shanghai）

审计基线：`1e5810ad5da18797794090f571e9211833680e27`

修复分支：`codex/agent-protocol-audit-20261009`

## 结论

最新 `model_v1.csv` / `model_v2.csv` 任务的数值没有算错，失败状态也不是前端单独误标。真正根因是 Goal Coverage 把“资源发现同时看见文件和数据库”误解释成“用户要求同时完成文件与数据库分析”，凭空增加了 `executed_database_analysis` 交付条件。后端因此产生 `INSUFFICIENT_EVIDENCE + PARTIAL`，持久化层和前端只是如实把它映射成 `failed`。

本轮修复删除了这条资源共存推导。任务要求现在只来自原始请求形成的 `required_deliverables` 和已安装 Plan 的 `required_capabilities`。真实混合任务仍必须执行数据库分析，反事实测试已证明该门禁没有被放宽。

## 生产实际调用链

```text
POST /api/conversations/{id}/chat/stream
  -> SecurityPolicy / ConversationPolicy / FollowUpResolver
  -> ScientificAgent.stream
  -> DecisionRuntime.stream
  -> LangGraph build_context
       -> ResourceService + ResourceBinding/QueryScope
       -> SkillService
  -> decision
       -> REPLAN -> update_plan -> Plan protocol validation/recovery
       -> CALL_TOOL -> ToolDispatcher -> observation -> Evidence
       -> ASK_USER -> checkpoint interrupt/resume
       -> FINISH/ANSWER -> finalize
  -> GoalCoverage + evidence-quality gate
  -> GroundedResponseService
  -> FINAL_ANSWER
  -> ConversationRepository: task/event/evidence/claim/artifact persistence
  -> scientific_task_status
  -> web executionState.js
```

生产入口证据：

- `app/api/routes.py:45` 创建唯一 `ScientificAgent`；会话执行位于 `app/api/routes.py:488`。
- `app/agents/scientific_agent.py:69` 创建 `DecisionRuntime`；`ScientificAgent.stream()` 在 `:71-76` 只转发到 Runtime。
- `app/agents/planning_graph.py:192-207` 定义当前 LangGraph 节点与边。
- `app/agents/runtime.py:693-836` 统一执行 Goal Coverage、质量状态和最终回答。
- `app/api/routes.py:540-549` 用最终状态持久化任务；`web/src/executionState.js:20-25` 使用同一质量/覆盖语义显示状态。

`scientific_agent.py:82` 之后的大量旧文件/SQL执行方法仍存在，但不再由公开 `stream()` 入口调用。`planning_graph.py:179` 的旧 canonical planning graph 也不是产品 executor。它们是维护债务，不是当前任务同时跑了两套执行器的证据。

## P0 取证

### P0-1：正确文件分析被错误要求数据库交付（已修复）

持久化任务：`1d2df08e-491a-40ea-9404-83f681cf03ef`

- 用户目标：比较 `model_v1.csv` 与 `model_v2.csv`，分析高误差分子的结构类型。
- 6 次工具调用全部成功，6 条 Evidence 已持久化，无 `errors`、无 `blocking_issues`。
- 观察字段包含 `structure_type`；回答给出 V1 MAE `0.425`、V2 MAE `0.725`，并定位 M004/M006 与 `fused_ring`。
- 原始交付契约为 `required_deliverables=["file_analysis"]`。
- 资源绑定同时包含两文件和 `datasource_id=training_db`，因为元数据发现能看到数据库，并不表示用户要求查询数据库。
- 最终错误状态为 `INSUFFICIENT_EVIDENCE / PARTIAL`，唯一缺项是 `executed_database_analysis`。

原缺陷位于 `app/agents/goal_coverage.py:131-137`：代码先读取真实 Plan/交付要求，又额外根据 `ResourceBinding` 中 datasource 与 files 共存强行加入 `database + file`。这混淆了“可用资源”和“要求执行的能力”。

修复：移除资源共存推导；保留 Plan 和原始交付契约作为唯一要求来源。

### P0-2：后端、持久化和前端状态映射本身一致（无需改状态）

`app/services/task_completion.py:16-30` 明确把 `INSUFFICIENT_EVIDENCE` 或 `PARTIAL` 任务持久化为 `failed`；前端 `web/src/executionState.js:20-25` 使用相同规则。因此不能把问题修成“FINAL_ANSWER 一律成功”或只改前端颜色。Goal Coverage 根因修复后，状态会自然变为 `SUPPORTED_CONCLUSION / SATISFIED / completed`。

### P0-3：Schema 前置的历史 PLAN_REJECTED（当前代码已系统修复，测试契约已校正）

数据库共有 12 条 `PLAN_REJECTED`，其中 11 条为“`text_to_sql` 没有已检查 schema 或 schema-producing tool”。历史计划把 `text_to_sql` 写成“获取 schema”，实际却因自身先决条件不可调用。

当前代码已经在 `app/agents/plan_protocol.py:74-109` 由 Runtime 编译受权的 `search_schema/get_table_schema` 前置节点，并在 `app/agents/runtime.py:172-187` 验证真实可调用入口。旧回放测试仍要求直接拒绝计划，与现行恢复设计冲突。本轮将其改成更强的契约：前置节点必须无依赖且真实可调用，`text_to_sql` 在 schema 到达前不可调用，并且所有 SQL 生成步骤依赖该前置节点。

这不是放宽 Plan 校验；循环依赖、越权工具、错误依赖、无 SQL lineage 和 scope 不匹配仍会拒绝。

### P0-4：历史错误与当前执行隔离（现有实现和回归通过）

- `NEW_TASK`、`RERUN` 在 `app/api/routes.py:500-504` 不把历史 summary/provenance 注入当前执行。
- `StateSufficiencyResolver` 在 `app/services/followup.py:477-491` 强制 NEW/RERUN/CONTINUE 重新执行。
- `runtime.finalize()` 在当前轮失败且无 Evidence 时仅使用 `control_observations` 与本轮结果，不让历史 `DataCorrupted` 解释新任务（`app/agents/runtime.py:757-783`）。
- `tests/test_schema_prerequisite_and_clean_rerun.py` 与 `tests/test_exact_rerun_and_error_reply.py` 的隔离回归通过。

### P0-5：另一条最新任务确实没有完成，不应误改为成功

任务 `49395a94-9bc8-48be-9940-63ef37bfb82e` 要求“文件含环误差 + 数据库训练覆盖”。它有文件 Evidence，但数据库 SQL 调用因缺少 SQLCandidate lineage 失败；`structure_type`/数据库覆盖交付没有完成。因此 `PARTIAL / failed` 是正确状态，和前述文件-only 误判不是同一问题。

## P1/P2 风险

1. PlanStep 的 `selected_tools` 同时承担授权范围和默认完成条件。含“主路径 + 备用工具”的一步会默认要求所有工具成功。最新文件任务的最后一步因此仍为 `blocked`，尽管用户目标已由 `compare_structure_groups` 满足。任务成功应由 Goal Coverage 决定；后续应把工具授权范围与完成谓词显式分离，不能用关键词猜测可选工具。
2. `scientific_agent.py` 的遗留执行器体积大且仍被测试直接引用。应先迁移仍被 Runtime 使用的纯辅助函数，再删除死入口，避免一次性大删。
3. 全量测试集合混有旧架构行为、持久 checkpoint 固定 thread id、实时模型依赖和当前契约，不能再把“全量 pytest 一个数字”当作验收结论。
4. 自然语言目标完整性目前主要依赖首次 Decision 产出的 `requested_dimensions/required_deliverables`。CSV 显式导出已有确定性补强，但更一般的自然语言交付项仍需要独立、可持久化的 Goal Contract。

## 本轮修改

- `app/agents/goal_coverage.py`：移除由资源共存推导混合交付的逻辑。
- `tests/test_task_completion_contract.py`：增加最新失败路径回放和真实混合任务反事实。
- `tests/test_final_blockers.py`：把已过时的“拒绝 Plan”断言升级为“Runtime 编译真实可调用 Schema 前置”契约。

未修改数据库、MinIO、历史任务、冻结回放或原始 CSV；未推送远端分支。
