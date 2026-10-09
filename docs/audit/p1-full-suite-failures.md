# P1 全仓失败逐项审计

审计基线：分支 `codex/agent-protocol-audit-20261009`，历史基线 HEAD `fa5586b`；当前本地提交为 `48a52eb`。原报告中的 `408 passed / 18 failed` 是在清除几个环境变量后运行的，但项目配置使用 `os.environ.setdefault` 从 `.env` 恢复了 `CHECKPOINT_DATABASE_URL`，并且 `test_mixed_e2e.py` 自带默认 PostgreSQL URL。因此这不是安全的离线基线。

受控基线通过 Python 入口在导入应用前显式置空 `ADMIN_DATABASE_URL`、`DATABASE_URL`、`CHECKPOINT_DATABASE_URL`、LLM 变量，并把 `TEST_POSTGRES_URL` 指向 `127.0.0.1:1`，避免任何真实 PostgreSQL/MinIO/模型访问。当前全仓结果为 `392 passed / 10 failed / 32 skipped`；工具计数契约已修正，并新增 5 个 LangGraph 无模型 E2E。测试失败没有通过删除断言或 `xfail` 隐藏。

## 原始 18 条逐项记录

| # | 测试与位置 | 原始现象/关键位置 | 受控复现 | 分类与结论 |
|---|---|---|---|---|
| 1 | `tests/test_agent.py::test_simple_file_route_runs:9` | `runtime.py:1222` 报复用线程存在 pending checkpoint；受控隔离后在 `runtime.py:162` 报真实 Skill 路由不可用 | 是（受控 profile） | 测试依赖：旧测试未注入 Skill/决策 provider。不能关闭 pending guard；生产语义是新请求必须换 thread 或 resume。 |
| 2 | `test_agent.py::test_mixed_end_to_end:23` | 同上 | 是（受控 profile） | 测试契约过时：旧测试假定无模型 heuristic workflow 会自动执行；当前生产安全策略明确拒绝 heuristic fallback。 |
| 3 | `test_agent.py::test_hitl_interrupt_and_resume:60` | 同上 | 是（受控 profile） | 测试契约过时；真实 HITL 图已由本轮 `test_p1_langgraph_no_model_e2e.py` 覆盖，使用隔离 Checkpointer 和固定决策。 |
| 4 | `test_agent.py::test_explicit_dataset_version_does_not_trigger_hitl:76` | 同上 | 是（受控 profile） | 测试契约过时；需 Fake Skill/Decision 或真实 provider，不能以空 LLM 伪装成功。 |
| 5 | `test_cross_dataset_skill.py::test_cross_dataset_real_guarded_read_and_evidence:67` | 同上 | 是（受控 profile） | 测试依赖：应使用受控 datasource/Skill 注入；当前 SQL/Scope 核心回归通过。 |
| 6 | `test_cross_dataset_skill.py::test_cross_dataset_missing_version_is_insufficient:91` | 同上 | 是（受控 profile） | 测试依赖；缺版本安全结论由 P0/P1 Scope 测试覆盖。 |
| 7 | `test_cross_dataset_skill.py::test_cross_dataset_permission_and_tool_failure:104` | 直接调用 `ScientificAgent.stream` 得到 `PermissionError`，但旧断言期待 SSE 错误事件 | 是 | 测试 API 层级错误：`app/api/routes.py:174-178` 才将 PermissionError 编码为 SSE；Agent service 层抛出异常是既定边界，不是越权放行。 |
| 8 | `test_cross_dataset_skill.py::test_cross_dataset_missing_schema_stops_before_query:127` | 受控环境先在 Skill 路由处停止 | 是（受控 profile） | 测试依赖；生产 schema 前置门禁仍在 `runtime.py:612`/`decision_node.py:43`。 |
| 9 | `test_cross_dataset_skill.py::test_cross_dataset_unsupported_dimension_is_not_claimed:139` | 同上 | 是（受控 profile） | 测试依赖；Evidence gate 与 unsupported dimension 回归仍通过。 |
| 10 | `test_followup_routing.py::test_multiturn_followup_reuses_persisted_evidence_without_tools:425` | 无 `FINAL_ANSWER`；原始环境曾进入 DB/历史路径 | 受控 profile 跳过（需 PostgreSQL schema） | 外部环境依赖，不是离线失败；必须在专用只读集成 profile 验证。 |
| 11 | `test_followup_routing.py::test_latest_analysis_without_evidence_never_reuses_older_evidence:564` | 无 `FINAL_ANSWER`；依赖持久化历史 | 受控 profile 跳过 | 外部环境依赖；RERUN/新 thread 隔离由本轮无模型回放覆盖。 |
| 12 | `test_hitl_resume.py::test_dataset_version_interrupt_and_command_resume:20` | `runtime.py:162` 明确拒绝 heuristic Skill fallback | 是（受控 profile） | 测试契约过时；真实 HITL 生产图路径本轮已覆盖。 |
| 13 | `test_mixed_e2e.py::test_mixed_trace_uses_deepagents_mcp_text2sql_and_postgres:27` | 原始 profile 连接本地 PostgreSQL；受控 profile 跳过 | 受控 profile 跳过 | 外部集成依赖；不得在普通离线 pytest 隐式连接默认 URL。 |
| 14 | `test_mixed_e2e.py::test_mcp_unavailable_uses_file_subgroup_as_limited_alternative:58` | `ScientificAgent` 已不再提供 `deep_runtime`，抛 `AttributeError` | 原始 profile 可复现 | 过时测试契约。`deep_runtime.py` 是可选旧 bounded runtime，不是当前第二执行器；不恢复废弃属性。 |
| 15 | `test_mixed_e2e.py::test_explicit_version_mixed_task_checks_file_db_molecule_join:76` | 原始 profile 依赖本地 PostgreSQL；受控 profile 跳过 | 受控 profile 跳过 | 外部集成依赖；只读集成环境单独运行。 |
| 16 | `test_product_expansion.py::test_tool_registry_metadata_and_deterministic_filters:55` | 断言 23，当前 Registry 实际 25；新增 `preview_table`、`save_chart` | 是 | 已确认过时测试契约；本轮改为名称唯一性、必需元数据和两项新增工具存在性，不放宽授权。 |
| 17 | `test_product_expansion.py::test_conversation_stream_persists_user_and_assistant_messages:217` | 原始 profile 返回 `ERROR: Real Skill routing unavailable`，无最终回答 | 受控 profile 跳过（需 PostgreSQL schema） | 外部持久化/Skill provider 依赖；不能用空 provider 伪装生产成功。 |
| 18 | `test_product_expansion.py::test_persisted_db_hitl_resume_keeps_conversation_task_and_thread_ids:251` | 无 waiting payload；依赖 PostgreSQL checkpoint/conversation schema | 受控 profile 跳过 | 外部持久化集成依赖；Checkpoint identity 契约由隔离 HITL 回放覆盖。 |

## 结论

已证实的生产 Bug 只有本轮发现的确定性执行器 Plan 边界问题：完成全部 PlanStep 后仍从全局工具集选择没有 `step_id` 的工具，导致 `CALL_TOOL requires a pending/running plan step_id`，并把已完成任务标为 `EXECUTION_FAILED`。修复位于 `app/agents/runtime.py:523-528`，并由无模型 A/D06 E2E 锁定。

已证实的错误恢复/安全设计不是上述 18 条中的缺陷：`Skill fallback disabled` 是安全停止，不是自动猜测；`PermissionError` 在 HTTP 层有明确 SSE 编码；pending checkpoint 是防止新请求污染未完成 HITL 状态的保护。

纯 UI/契约冗余包括旧工具数量断言和 `deep_runtime` 属性测试。需要进一步验证的是 PostgreSQL/MinIO/真实模型集成测试，必须在显式专用 profile 中运行，不能由本地普通 pytest 隐式连接 `.env` 默认服务。
