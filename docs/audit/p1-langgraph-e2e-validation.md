# P1 LangGraph 无模型端到端验收

测试文件：`tests/test_p1_langgraph_no_model_e2e.py`。

测试通过 `CheckpointService(database_url="")` 建立隔离 `InMemorySaver`，使用固定 Fake Skill provider、固定 AgentDecision 序列和 Fake GroundedResponse；真实生产图节点、条件边、PlanStep 校验、GoalContract、QueryScope、RecoveryPolicy、ToolRegistry、ToolDispatcher 和 Observation/Evidence 写入均执行。Fake database/storage 只存在于 pytest 临时目录，不能访问 PostgreSQL/MinIO。

## 已覆盖场景

| 场景 | 真实链路断言 |
|---|---|
| A | REPLAN → deterministic 唯一文件调用 → Observation/Evidence → PlanStep 完成 → FINAL_ANSWER；计划完成后不再发生无 step_id 的工具调用。 |
| B | `search_schema → text_to_sql → query_checker → execute_readonly_sql`；候选状态依次为 `scope_verified`、`checked`、`executed`，ScopeValidation 为 verified。 |
| C | 首次 Text2SQL 为 `diagnostic_only` 的 `UNVERIFIED_SCOPE`；Checker 在前置条件不满足时不执行；只进行一次定向 repair，然后才进入 Checker/Executor。 |
| D06 | 使用 `input_refs=observation:tool-1:data.subgroups` 导出真实记录行到隔离 CSV artifact；错误的 dict rows 不被伪装成表格。 |
| RERUN | 新 thread 使用新的 State/工具调用命名空间，不继承旧失败 Observation。 |
| HITL | 真实 `ask_user` interrupt、隔离 checkpoint、`resume`、HITL_RESUMED 和最终状态。 |

D09/M02/D08 的结构化回放矩阵仍由 `tests/test_p1_acceptance_matrix.py` 加载既有 closure records，并由 P1 GoalContract/Population/Scope 测试验证；它们没有被写回历史记录。真实多 Population PostgreSQL 只读执行和 MinIO artifact 服务尚未在本地 profile 运行，不能冒充已覆盖。

## 计数与证据

本文件新增 5 个 E2E 测试，结果：`5 passed`。当前显式隔离外部服务的聚焦回归（P1、P0、C recovery、plan completion、runtime boundary、new E2E、tool registry contract）结果：`84 passed / 9 skipped`。E2E 中 Fake provider 的 telemetry 明确标记 `fallback=false`；真实付费 LLM 调用次数为 0。

历史只读任务标识：A=`650ffd90-e72a-4ad0-bb7d-69a88fb80c67`，B=`32694656-4c73-4d7f-bc7c-c30874de7f83`，C=`8808d8f1-ee34-443c-a56e-1e2c7eff8bf4`；这些持久化记录未被本轮测试或修改触碰。

反事实已锁定：移除 trusted SQL candidate 时 Query Checker 不在 callable set；用失败 Observation 的 rows 作为 `input_refs` 会被拒绝；Plan 完成后全局工具不会越界；新 RERUN thread 不复用旧 tool/evidence IDs；删除 artifact 的隔离文件会使 artifact 断言失败而不改变历史任务。
